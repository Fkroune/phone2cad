"""Live 3D preview for the phone while a job runs.

Writes JOB/result/preview.json (at most MAX_POINTS points, 0.1 mm rounding,
~60 kB) at each milestone: the cameras located from the mat, the points
structure from motion discovers (read from COLMAP's mapper snapshots), the dense
points. progress.json carries a version number, so the phone downloads a new
preview only when there is one.
"""

from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path

import numpy as np

MAX_POINTS = 5000


def read_images_bin(path: Path) -> dict[str, tuple[np.ndarray, np.ndarray, int]]:
    """COLMAP images.bin -> name: (quaternion wxyz, t, camera id), world -> camera."""
    out = {}
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            f.read(4)  # image id
            q = np.array(struct.unpack("<4d", f.read(32)))
            t = np.array(struct.unpack("<3d", f.read(24)))
            (cam,) = struct.unpack("<I", f.read(4))
            name = bytearray()
            while (c := f.read(1)) != b"\0":
                if not c or len(name) > 4096:  # truncated (COLMAP still writing it)
                    raise EOFError(f"{path.name}: truncated")
                name += c
            (npts,) = struct.unpack("<Q", f.read(8))
            f.seek(npts * 24, 1)
            out[name.decode()] = (q, t, cam)
    return out


def read_pinhole_cameras_bin(path: Path) -> dict[int, tuple[int, int, np.ndarray]]:
    """COLMAP cameras.bin of an undistorted workspace -> id: (width, height, K)."""
    nparams = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}
    out = {}
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            cid, model = struct.unpack("<iI", f.read(8))
            w, h = struct.unpack("<QQ", f.read(16))
            prm = struct.unpack(f"<{nparams[model]}d", f.read(8 * nparams[model]))
            fx, fy, cx, cy = (prm[0], prm[0], prm[1], prm[2]) if model in (0, 2, 3) else prm[:4]
            out[cid] = (int(w), int(h), np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]]))
    return out


def read_depth_map(path: Path) -> np.ndarray:
    """COLMAP depth map (.bin): text header "w&h&c&", then float32 rows."""
    with open(path, "rb") as f:
        head = b""
        while head.count(b"&") < 3:
            c = f.read(1)
            if not c or len(head) > 64:  # truncated or not a depth map
                raise EOFError(f"{path.name}: truncated")
            head += c
        w, h, c = (int(x) for x in head.split(b"&")[:3])
        return np.fromfile(f, np.float32, w * h * c).reshape(h, w, c)[..., 0]


def depth_points(dense_dir: Path, box, stride: int = 6) -> tuple[np.ndarray, list[str]]:
    """World points (mm) from the depth maps PatchMatch has finished so far
    (geometric when ready, else photometric), inside the part box: the dense
    cloud filling in view by view. Also the names of the finished views."""
    from .calib import quat_to_rotmat

    sp, maps = Path(dense_dir) / "sparse", Path(dense_dir) / "stereo" / "depth_maps"
    try:
        images, cams = read_images_bin(sp / "images.bin"), read_pinhole_cameras_bin(sp / "cameras.bin")
    except (OSError, struct.error, KeyError):
        return np.zeros((0, 3)), []
    lo, hi = np.asarray(box[0]), np.asarray(box[1])
    pts, done = [], []
    for name, (q, t, cid) in images.items():
        f = maps / f"{name}.geometric.bin"
        if not f.exists():
            f = maps / f"{name}.photometric.bin"
        if not f.exists() or cid not in cams:
            continue
        try:
            d = read_depth_map(f)
        except (OSError, ValueError):
            continue  # still being written
        w, _, K = cams[cid]
        s = d.shape[1] / w  # depth maps may be smaller than the undistorted image
        v, u = np.mgrid[0:d.shape[0]:stride, 0:d.shape[1]:stride]
        z = d[v, u]
        ok = z > 0
        x = ((u[ok] + 0.5) / s - K[0, 2]) / K[0, 0] * z[ok]  # COLMAP: pixel centres at +0.5
        y = ((v[ok] + 0.5) / s - K[1, 2]) / K[1, 1] * z[ok]
        R = quat_to_rotmat(q)
        P = (np.c_[x, y, z[ok]] - t) @ R
        pts.append(P[np.all((P >= lo) & (P <= hi), axis=1)])
        done.append(name)
    return (np.vstack(pts) if pts else np.zeros((0, 3))), done


def read_points_bin(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """COLMAP points3D.bin -> xyz (n,3), rgb (n,3) uint8."""
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        P, C = np.empty((n, 3)), np.empty((n, 3), np.uint8)
        for i in range(n):
            f.read(8)  # point id
            P[i] = struct.unpack("<3d", f.read(24))
            C[i] = np.frombuffer(f.read(3), np.uint8)
            f.read(8)  # error
            (tl,) = struct.unpack("<Q", f.read(8))
            f.seek(tl * 8, 1)
    return P, C


# Camera colours on the phone: where each camera's position came from, and what the PC does with it now.
MAT, MATCHED, PROBLEM, ACTIVE = 0, 1, 2, 3
FRAME_SIDE = 320  # px, longest side of the "frame being worked on" thumbnail
FRAME_EVERY_S = 1.5


class Preview:
    def __init__(self, result_dir: Path, progress):
        self.path, self.progress, self.version = Path(result_dir) / "preview.json", progress, 0
        self.frame_path, self.frame_version, self._frame_t = Path(result_dir) / "frame.jpg", 0, 0.0

    def write(self, label: str, points: np.ndarray | None = None, colors: np.ndarray | None = None,
              cameras: np.ndarray | None = None, kinds=None) -> None:
        """kinds: per camera, MAT / MATCHED / PROBLEM / ACTIVE (default MATCHED)."""
        data: dict = {"label": label, "points": [], "colors": [], "cameras": [], "kinds": []}
        if points is not None and len(points):
            keep = np.random.default_rng(0).permutation(len(points))[:MAX_POINTS]
            data["points"] = np.round(points[keep], 1).ravel().tolist()
            if colors is not None:
                data["colors"] = np.asarray(colors)[keep].astype(int).ravel().tolist()
        if cameras is not None and len(cameras):
            data["cameras"] = np.round(cameras, 1).ravel().tolist()
            data["kinds"] = [int(k) for k in kinds] if kinds is not None else [MATCHED] * len(cameras)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        tmp.replace(self.path)
        self.version += 1
        self.progress.update(preview=self.version)

    def frame_due(self, quiet_s: float = FRAME_EVERY_S) -> bool:
        return time.time() - self._frame_t >= quiet_s

    def frame(self, image, part=None, label: str = "", force: bool = False) -> None:
        """Thumbnail of the frame being worked on, the part tinted when its
        outline is known. Throttled: the phone polls every 2 s."""
        import cv2

        now = time.time()
        if not force and now - self._frame_t < FRAME_EVERY_S:
            return
        self._frame_t = now
        s = FRAME_SIDE / max(image.shape[:2])
        small = cv2.resize(image, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if part is not None:
            m = cv2.resize(part.astype(np.uint8), small.shape[1::-1], interpolation=cv2.INTER_NEAREST) > 0
            tint = small.copy()
            tint[m] = (0.45 * tint[m] + 0.55 * np.array([80, 200, 60])).astype(np.uint8)  # BGR green
            edge = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
            tint[edge] = (40, 230, 40)
            small = tint
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return
        tmp = self.frame_path.with_suffix(f".{threading.get_ident()}.tmp")  # the heartbeat thread writes too
        tmp.write_bytes(buf.tobytes())
        tmp.replace(self.frame_path)
        self.frame_version += 1
        self.progress.update(frame=self.frame_version, frame_label=label)


def in_view_region(P: np.ndarray, zone: tuple[float, float, float, float]) -> np.ndarray:
    """Points worth showing: over the mat's clear zone (plus a margin), near the mat."""
    x0, x1, y0, y1 = zone
    m = 25.0
    return (P[:, 0] > x0 - m) & (P[:, 0] < x1 + m) & (P[:, 1] > y0 - m) & (P[:, 1] < y1 + m) \
        & (P[:, 2] > -5) & (P[:, 2] < 200)


def snapshot_points(snapshot_root: Path, mat_poses: dict, zone) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Latest mapper snapshot, moved onto the mat with the frames the mat
    located (similarity fit of camera centres). None until it can be placed."""
    from .align import _umeyama
    from .calib import quat_to_rotmat

    snaps = sorted((d for d in Path(snapshot_root).glob("*") if (d / "points3D.bin").exists()),
                   key=lambda d: d.stat().st_mtime)
    if not snaps:
        return None
    try:
        images = read_images_bin(snaps[-1] / "images.bin")
        P, C = read_points_bin(snaps[-1] / "points3D.bin")
    except (OSError, struct.error):
        return None  # snapshot still being written
    common = [n for n in images if n in mat_poses]
    if len(common) < 3:
        return None
    A = np.array([-quat_to_rotmat(images[n][0]).T @ images[n][1] for n in common])
    B = np.array([-mat_poses[n][0].T @ mat_poses[n][1] for n in common])
    R, s, t = _umeyama(A, B)
    Pw = s * P @ R.T + t
    cams = np.array([s * R @ (-quat_to_rotmat(q).T @ tt) + t for q, tt, _ in images.values()])
    m = in_view_region(Pw, zone)
    return Pw[m], C[m], cams


class Heartbeat(threading.Thread):
    """Fills silences: when no thumbnail has been shown for QUIET_S (a long
    COLMAP step or solver round that reports nothing), show the next frame of
    the scan, labelled with what the PC is doing at that moment."""

    QUIET_S = 4.0

    def __init__(self, preview: "Preview", step_text):
        super().__init__(daemon=True)
        self.preview, self.step_text = preview, step_text
        self.frames: list[Path] = []
        self.i = 0
        self._stop = threading.Event()

    def show(self, folder: Path, names: list[str]) -> None:
        """Which frames to cycle through (the side being worked on)."""
        self.frames, self.i = [Path(folder) / n for n in names], 0

    def run(self) -> None:
        import cv2

        while not self._stop.wait(1.0):
            if not self.frames or not self.preview.frame_due(self.QUIET_S):
                continue
            path = self.frames[self.i % len(self.frames)]
            self.i += 1
            img = cv2.imread(str(path), cv2.IMREAD_REDUCED_COLOR_8)
            if img is not None:
                self.preview.frame(img, None, self.step_text(), force=True)

    def stop(self) -> None:
        self._stop.set()
