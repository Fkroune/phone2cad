"""Mat detection, lens calibration and metric camera poses.

Every frame contains the mat, so the lens model (focal length, centre,
distortion) is solved per scan from the ChArUco corners, and each camera
pose comes out in millimetres in the mat's world frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .mat import DEFAULT_MAT, MatSpec

MIN_CORNERS = 10
MIN_SPREAD = 0.4  # corners must cover this much of the mat in both directions
# Lens solve error: above the limit the frames are garbage (first real scan: 224 px);
# above the warning the scan still works but its +/- will be wider.
MAX_RMS_PX, MAX_RMS_REL = 2.0, 0.001  # 4 px at 4032 px
# Real phone scans that measured within 0.3 mm had 2.6-3.3 px at 4032 px, so only
# warn when the fit nears the failure limit (3.6 px at 4032 px).
WARN_RMS_REL = 0.0009
BEND_TERMS = 5
BEND_PRIOR = 0.2  # px of penalty per mm of bend coefficient


@dataclass
class FrameObs:
    name: str
    ids: np.ndarray  # (n,) ChArUco corner ids (valid ring corners only)
    px: np.ndarray  # (n, 2) OpenCV pixel coords (pixel centre = integer)


@dataclass
class Calibration:
    width: int
    height: int
    K: np.ndarray
    dist: np.ndarray  # k1, k2, p1, p2
    rms_px: float
    poses: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)  # name -> (R_cw, t_cw)
    obs: dict[str, FrameObs] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    # World XYZ of every ChArUco corner id, with the measured bend of the sheet in Z.
    corner_world: np.ndarray | None = None
    bend_mm: float = 0.0  # largest corner height above/below the flat mat
    same_size: list[str] = field(default_factory=list)  # every frame of the solved size, mat or not

    def colmap_params(self) -> dict:
        """OPENCV model in COLMAP's convention (pixel centre at +0.5)."""
        fx, fy, cx, cy = self.K[0, 0], self.K[1, 1], self.K[0, 2], self.K[1, 2]
        k1, k2, p1, p2 = self.dist[:4]
        return {"model": "OPENCV", "width": self.width, "height": self.height, "fx": fx, "fy": fy,
                "cx": cx + 0.5, "cy": cy + 0.5, "k1": k1, "k2": k2, "p1": p1, "p2": p2}


def detect(gray: np.ndarray, mat: MatSpec = DEFAULT_MAT) -> tuple[np.ndarray, np.ndarray]:
    det = cv2.aruco.CharucoDetector(mat.cv_board)
    cc, ci, _, _ = det.detectBoard(gray)
    if ci is None or len(ci) == 0:
        return np.zeros(0, np.int32), np.zeros((0, 2), np.float32)
    ids, px = ci.ravel().astype(np.int32), cc.reshape(-1, 2).astype(np.float32)
    keep = np.isin(ids, mat.valid_corner_ids)
    ids, px = ids[keep], px[keep]
    if len(px):
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.001)
        px = cv2.cornerSubPix(gray, px.reshape(-1, 1, 2).copy(), (4, 4), (-1, -1), crit).reshape(-1, 2)
    return ids, px


def _spread(ids: np.ndarray, mat: MatSpec) -> float:
    """How much of the mat the corners cover, 0..1 in the narrower direction.
    Corners from one side of the ring are nearly collinear: no use for the lens."""
    W = mat.corner_world[ids]
    return float(min(np.ptp(W[:, 0]) / mat.board_w, np.ptp(W[:, 1]) / mat.board_h))


def bend_height(xy: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Height (mm) of a gently curled sheet: zero and flat at the mat centre."""
    x, y = xy[:, 0] / 100.0, xy[:, 1] / 100.0
    return c[0] * x * x + c[1] * y * y + c[2] * x * y + c[3] * x ** 4 + c[4] * y ** 4


def _per_frame_rms(objp, imgp, rvecs, tvecs, K, dist) -> np.ndarray:
    out = []
    for op, ip, rv, tv in zip(objp, imgp, rvecs, tvecs):
        proj, _ = cv2.projectPoints(op, rv, tv, K, dist)
        out.append(float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - ip) ** 2, axis=1)))))
    return np.array(out)


def _refine_bent(obs, names, mat, K, dist, rvecs, tvecs):
    """Joint lens + poses + sheet bend. Printed paper curls by a few mm at the
    edges, exactly where the marker ring is; a flat model then cannot fit any
    lens (seen on the first real phone scans: 4 mm curl, 3-4 px error)."""
    nf = len(names)
    X0 = [mat.corner_world[obs[n].ids] for n in names]
    uv = [obs[n].px.astype(np.float64) for n in names]
    sizes = np.array([2 * len(u) for u in uv])
    offs = np.r_[0, np.cumsum(sizes)]
    nb = BEND_TERMS

    def unpack(x):
        f, cx, cy = x[:3]
        Kx = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]])
        return Kx, np.r_[x[3:7], 0.0], x[7:7 + 6 * nf].reshape(nf, 6), x[7 + 6 * nf:]

    def resid(x):
        Kx, dx, poses, c = unpack(x)
        out = np.empty(offs[-1] + nb)
        for i in range(nf):
            X = X0[i].copy()
            X[:, 2] = bend_height(X, c)
            p, _ = cv2.projectPoints(X, poses[i, :3], poses[i, 3:], Kx, dx)
            out[offs[i]:offs[i + 1]] = (p.reshape(-1, 2) - uv[i]).ravel()
        out[offs[-1]:] = BEND_PRIOR * c  # weak pull toward a flat sheet
        return out

    n_par = 7 + 6 * nf + nb
    sp = lil_matrix((offs[-1] + nb, n_par), dtype=np.int8)
    for i in range(nf):
        sp[offs[i]:offs[i + 1], :7] = 1
        sp[offs[i]:offs[i + 1], 7 + 6 * i:13 + 6 * i] = 1
        sp[offs[i]:offs[i + 1], n_par - nb:] = 1
    sp[offs[-1]:, n_par - nb:] = np.eye(nb, dtype=np.int8)
    x0 = np.r_[(K[0, 0] + K[1, 1]) / 2, K[0, 2], K[1, 2], dist.ravel()[:4],
               np.hstack([np.r_[r.ravel(), t.ravel()] for r, t in zip(rvecs, tvecs)]), np.zeros(nb)]
    sol = least_squares(resid, x0, jac_sparsity=sp, method="trf", x_scale="jac", max_nfev=200)
    Kx, dx, poses, c = unpack(sol.x)
    r = sol.fun[:offs[-1]]
    per = np.array([np.sqrt(np.mean(r[offs[i]:offs[i + 1]].reshape(-1, 2) ** 2 @ np.ones(2))) for i in range(nf)])
    return Kx, dx, [p[:3] for p in poses], [p[3:] for p in poses], c, float(np.sqrt(np.mean(r ** 2) * 2)), per


def calibrate(frames, mat: MatSpec = DEFAULT_MAT, log=print, on_frame=None) -> Calibration:
    """frames: mapping name -> grayscale image. Only frames of the most common
    size are used (a rotated phone changes it)."""
    names_all = list(frames.names) if hasattr(frames, "names") else list(frames)
    sizes: dict[tuple, list[str]] = {}
    for n in names_all:
        sizes.setdefault(frames[n].shape, []).append(n)
    log("frame sizes: " + ", ".join(f"{w}x{h} x{len(v)}" for (h, w), v in sorted(sizes.items(), key=lambda kv: -len(kv[1]))))
    size, names_ok = max(sizes.items(), key=lambda kv: len(kv[1]))
    rejected = [n for n in names_all if n not in set(names_ok)]
    h, w = size

    obs: dict[str, FrameObs] = {}
    few, one_side, counts = 0, 0, []
    for k, n in enumerate(names_ok):
        if on_frame:
            on_frame(k, len(names_ok))
        ids, px = detect(frames[n], mat)
        counts.append(len(ids))
        if len(ids) < MIN_CORNERS:
            few += 1
        elif _spread(ids, mat) < MIN_SPREAD:
            one_side += 1
        else:
            obs[n] = FrameObs(n, ids, px)
            continue
        rejected.append(n)
    log(f"mat corners per frame: median {int(np.median(counts)) if counts else 0} of {len(mat.valid_corner_ids)}; "
        f"{few} frames under {MIN_CORNERS} corners, {one_side} frames showing only one side of the ring")
    if len(obs) < 8:
        raise RuntimeError(f"the whole mat was in view in only {len(obs)} frames (need 8): hold the phone "
                           f"further back so the printed ring stays in the picture")
    log(f"mat found in {len(obs)} of {len(names_all)} frames")

    f0 = 0.9 * max(w, h)
    K0 = np.array([[f0, 0, (w - 1) / 2], [0, f0, (h - 1) / 2], [0, 0, 1]])
    flags = cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_K3
    names = list(obs)
    for it in range(4):
        objp = [mat.corner_world[obs[n].ids].astype(np.float32) for n in names]
        imgp = [obs[n].px.astype(np.float32) for n in names]
        rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(objp, imgp, (w, h), K0.copy(), np.zeros(5), flags=flags)
        per = _per_frame_rms(objp, imgp, rvecs, tvecs, K, dist)
        log(f"lens solve (flat mat) {it + 1}: f={K[0, 0]:.1f}px rms={rms:.3f}px, per-frame median "
            f"{np.median(per):.2f} max {per.max():.2f}px ({len(names)} frames)")
        bad = per > max(1.0, 3 * np.median(per))
        if not bad.any() or len(names) - bad.sum() < 8:
            break
        rejected += [n for n, b in zip(names, bad) if b]
        names = [n for n, b in zip(names, bad) if not b]

    K, dist, rvecs, tvecs, bend, rms, per = _refine_bent(obs, names, mat, K, dist, rvecs, tvecs)
    corner_world = mat.corner_world.copy()
    corner_world[:, 2] = bend_height(corner_world, bend)
    bend_mm = float(np.abs(corner_world[mat.valid_corner_ids, 2]).max())
    log(f"lens solve (bent mat): f={K[0, 0]:.1f}px rms={rms:.3f}px, per-frame median {np.median(per):.2f} "
        f"max {per.max():.2f}px; sheet bend up to {bend_mm:.1f} mm")
    limit = max(MAX_RMS_PX, MAX_RMS_REL * max(w, h))
    if rms > limit or not (0.3 < K[0, 0] / max(w, h) < 3.0):
        raise RuntimeError(f"could not work out the camera lens from the mat (error {rms:.1f} px, "
                           f"f={K[0, 0]:.0f} px): tape the mat flat and film more slowly")
    if rms > WARN_RMS_REL * max(w, h):
        log(f"warning: rough lens fit ({rms:.1f} px): film more slowly, in good light, with the mat flat")
    if bend_mm > 4.0:
        log(f"warning: the mat is curled by {bend_mm:.1f} mm; tape it flat for best accuracy")

    cal = Calibration(w, h, K, dist.ravel()[:4], float(rms), rejected=rejected,
                      corner_world=corner_world, bend_mm=bend_mm, same_size=list(names_ok))
    for n, rv, tv in zip(names, rvecs, tvecs):
        R, _ = cv2.Rodrigues(np.asarray(rv, np.float64))
        cal.poses[n] = (R, np.asarray(tv, np.float64).ravel())
        cal.obs[n] = obs[n]
    return cal


def rotmat_to_quat(R: np.ndarray) -> np.ndarray:
    """Hamilton (w, x, y, z)."""
    m = R
    tr = np.trace(m)
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        q = [0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [(m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s]
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s]
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s]
    q = np.array(q)
    return q / np.linalg.norm(q) * (1 if q[0] >= 0 else -1)


def quat_to_rotmat(q) -> np.ndarray:
    w, x, y, z = np.asarray(q, float) / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
