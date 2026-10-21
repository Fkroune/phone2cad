"""Run one scan job end to end.

    python -m phone2cad_worker.pipeline JOB_DIR [--dense auto|cuda|cpu] [--keep]

JOB_DIR/frames/*.jpg are the uploaded frames. Results go to JOB_DIR/result/
(mesh.ply, features.json); progress to JOB_DIR/progress.json. Frames and
the working folder are deleted when the job ends, whether it succeeds or
fails (pass --keep only for local debugging).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import pickle
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
from scipy.spatial import cKDTree

from . import align, ba, colmap_session, dense_cpu, overlay, silhouette
from .calib import WARN_RMS_REL, calibrate
from .features import (FitResult, describe, estimate_normals, face_extent, fit_all, fit_cylinders, fit_planes,
                       grade, hull_face, isolate_part, level_to_mat, refine_with_points)
from .holes import Photo, find_holes, hole_features
from .mat import printed_mat
from .preview import ACTIVE, MAT, MATCHED, PROBLEM, Heartbeat, Preview, depth_points, in_view_region, snapshot_points

STAGES = {
    "mat": "Checking the mat",
    "shape": "Finding the shape",
    "dense": "Building the 3D copy",
    "measure": "Measuring",
    "done": "Done",
}


class LazyFrames:
    """Grey frames loaded on demand with a small cache (phones send big images)."""

    def __init__(self, folder: Path, names: list[str], cache: int = 12):
        self.folder, self.names, self.cache, self._lru = folder, names, cache, OrderedDict()

    def __getitem__(self, name: str) -> np.ndarray:
        if name in self._lru:
            self._lru.move_to_end(name)
            return self._lru[name]
        img = cv2.imread(str(self.folder / name), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"cannot read frame {name}")
        self._lru[name] = img
        if len(self._lru) > self.cache:
            self._lru.popitem(last=False)
        return img

    def items(self):
        for n in self.names:
            yield n, self[n]


def gpu_available() -> bool:
    if os.environ.get("PHONE2CAD_GPU", "auto") == "0":
        return False
    if shutil.which("nvidia-smi"):
        try:
            return subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return shutil.which("docker") is not None and colmap_session._docker_has_gpu()

# 1-sigma surface offset per dense method. CPU: synthetic block (max error
# 0.4 mm). CUDA: real benchmark corner-v1 (2026-10-08): 9.6 mm arm widths up to
# 0.3-0.46 mm thin, other dimensions within 0.1 mm; 0.08 claimed too much.
SYSTEMATIC_MM = {"cuda": 0.15, "cpu": 0.15}
DENSE_MAX_FRAMES = int(os.environ.get("PHONE2CAD_DENSE_MAX_FRAMES", "32"))


class Progress:
    """progress.json for the phone: stage, overall fraction, the current step
    in words, running stats (frames located, points found...) and the preview
    version. Rewritten at most twice a second; a stage change or the end is
    always written."""

    def __init__(self, job: Path, log):
        self.path, self.log, self.t0, self._last = job / "progress.json", log, time.time(), 0.0
        self._lock = threading.RLock()  # the preview heartbeat writes from its own thread
        self.state: dict = {"status": "running", "stage": "mat", "label": STAGES["mat"], "progress": 0.0,
                            "step": "", "stats": {}, "preview": 0, "frame": 0, "warnings": []}

    def __call__(self, stage: str, frac: float, status: str = "running", quiet: bool = False,
                 step: str | None = None, **extra):
        with self._lock:
            changed = stage != self.state["stage"] or status != "running"
            frac = frac if status != "running" else max(frac, self.state["progress"])
            self.state.update(status=status, stage=stage, label=STAGES.get(stage, stage), progress=round(frac, 3),
                              **extra)
            if step is not None:
                self.state["step"] = step
            self._write(force=changed or not quiet)
        if not quiet:
            self.log(f"[progress] {self.state['label']} {int(100 * frac)}%" + (f" - {step}" if step else ""))

    def stat(self, key: str, value) -> None:
        with self._lock:
            self.state["stats"][key] = value
            self._write(force=True)

    def warn(self, text: str) -> None:
        """Something the user should know, in plain words (shown on the phone)."""
        with self._lock:
            if text in self.state["warnings"]:
                return
            self.state["warnings"].append(text)
            self._write(force=True)
        self.log(f"warning for the user: {text}")

    def update(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)
            self._write(force=True)

    def _write(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self._last < 0.5:
                return
            self._last = now
            self.state["elapsed_s"] = round(now - self.t0, 1)
            tmp = self.path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.state))
            tmp.replace(self.path)


def build_mesh(P: np.ndarray, N: np.ndarray, grey: np.ndarray, max_tris: int = 150_000) -> o3d.geometry.TriangleMesh:
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pc.normals = o3d.utility.Vector3dVector(N)
    pc.colors = o3d.utility.Vector3dVector(np.repeat(np.clip(grey, 0, 1)[:, None], 3, 1))
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pc, depth=9, scale=1.1)
    dens = np.asarray(dens)
    # Low density = no data there: drop instead of inventing a surface.
    mesh.remove_vertices_by_mask(dens < np.quantile(dens, 0.08))
    lo, hi = P.min(0) - 0.5, P.max(0) + 0.5
    lo[2] = max(lo[2], 0.0)
    mesh = mesh.crop(o3d.geometry.AxisAlignedBoundingBox(lo, hi))
    if len(mesh.triangles) > max_tris:
        mesh = mesh.simplify_quadric_decimation(max_tris)
    mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    return mesh


def _voxel_for(box: tuple[np.ndarray, np.ndarray]) -> float:
    """Hull voxel: ~1/240 of the object's size (0.25 mm for a 60 mm box, 2 mm
    for half a metre), so time and memory stay bounded for big objects."""
    return float(np.clip(np.ptp(np.array(box), axis=0).max() / 240.0, 0.25, 2.0))


def _centres(cal, names: list[str]) -> np.ndarray:
    return np.array([-cal.poses[n][0].T @ cal.poses[n][1] for n in names if n in cal.poses])


def _snapshot_preview(preview, snaps: Path, cal, log) -> None:
    """Points the mapper has found so far, placed on the mat (best effort)."""
    try:
        zone = tuple(np.array(printed_mat().clear_zone_world) + [3, -3, 3, -3])
        got = snapshot_points(snaps, cal.poses, zone)
        if got is not None:
            preview.write("Points found so far", *got)
    except Exception as e:  # noqa: BLE001 - a preview must never stop the job
        log(f"preview skipped: {e}")


def _grey_rgb(G: np.ndarray) -> np.ndarray:
    return (np.repeat(np.clip(G, 0, 1)[:, None], 3, 1) * 255).astype(np.uint8)


def _hull_grey(H: np.ndarray, D: np.ndarray, DG: np.ndarray) -> np.ndarray:
    """Hull colours: the nearest dense point's grey where one is close, else neutral."""
    g = np.full(len(H), 0.75)
    if len(D):
        d, i = cKDTree(D).query(H, distance_upper_bound=1.0)
        ok = np.isfinite(d)
        g[ok] = DG[i[ok]]
    return g


def _measure_dense_only(P, N, G, V, path, zone, stats, log, progress):
    """The pre-silhouette measurement: level on the paper, isolate the part on
    the clear zone, fit faces and holes on the dense points."""
    R0, t0, noise = level_to_mat(P, zone, log=log)
    P = P @ R0.T + t0
    if N is not None:
        N = N @ R0.T
    if V is not None:
        V = V @ R0.T
    idx = isolate_part(P, zone, z_min=max(0.8, 4 * noise))
    if len(idx) < 500:
        raise RuntimeError("could not find the part on the mat: is it inside the marked zone?")
    Pp, Gp = P[idx], G[idx]
    Np = N[idx] if N is not None and path == "cuda" else estimate_normals(Pp, V[idx])
    fit = fit_all(Pp, Np, log=log)
    for p in fit.planes:  # each face's own points, for drawing it and searching it for holes
        p.pts = Pp[p.idx]
    feats = describe(fit, Pp, stats.scale_sigma_rel, SYSTEMATIC_MM[path])
    L = np.eye(4)  # mat world -> the levelled frame everything here is in
    L[:3, :3], L[:3, 3] = R0, t0
    return fit, feats, build_mesh(Pp, Np, Gp), int(len(idx)), L


def write_ply(path: Path, mesh: o3d.geometry.TriangleMesh) -> None:
    """Binary PLY with float32 vertices: Open3D writes float64, which WebGL
    (the phone's viewer) cannot draw, and it doubles the download."""
    V = np.asarray(mesh.vertices, np.float32)
    N = np.asarray(mesh.vertex_normals, np.float32) if mesh.has_vertex_normals() else None
    C = (np.clip(np.asarray(mesh.vertex_colors), 0, 1) * 255).round().astype(np.uint8) if mesh.has_vertex_colors() else None
    F = np.asarray(mesh.triangles, np.uint32)
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    fields += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")] if N is not None else []
    fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")] if C is not None else []
    vert = np.empty(len(V), fields)
    vert["x"], vert["y"], vert["z"] = V.T
    if N is not None:
        vert["nx"], vert["ny"], vert["nz"] = N.T
    if C is not None:
        vert["red"], vert["green"], vert["blue"] = C.T
    face = np.empty(len(F), [("n", "u1"), ("v", "<u4", 3)])
    face["n"], face["v"] = 3, F
    kinds = {"<f4": "float", "u1": "uchar"}
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {len(V)}"]
    head += [f"property {kinds[t]} {n}" for n, t in fields]
    head += [f"element face {len(F)}", "property list uchar uint vertex_indices", "end_header"]
    with open(path, "wb") as fh:
        fh.write(("\n".join(head) + "\n").encode())
        fh.write(vert.tobytes())
        fh.write(face.tobytes())


def _read_fused(path: Path, voxel: float = 0.3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Voxel-reduce: fused clouds run to millions of points, and clustering
    # them at full density does not fit in the WSL VM's memory.
    pc = o3d.io.read_point_cloud(str(path)).voxel_down_sample(voxel)
    P = np.asarray(pc.points)
    N = np.asarray(pc.normals) if pc.has_normals() else None
    G = np.asarray(pc.colors).mean(1) if pc.has_colors() else np.full(len(P), 0.7)
    return P, N, G


@dataclass
class Side:
    """One capture pass: the part as it stood on the mat (a second, optional
    pass shows it flipped)."""
    tag: str
    frames_dir: Path
    names: list[str]
    cal: object = None
    model: object = None
    used: list = field(default_factory=list)
    stats: object = None
    on_mat: list = field(default_factory=list)
    masks: list = field(default_factory=list)
    core: np.ndarray | None = None  # sparse points the outlines call the object's
    mask_names: list = field(default_factory=list)  # the frames side.masks belong to
    clicks: np.ndarray | None = None  # the part's points the outlines were prompted with
    box: tuple = None
    views: list = field(default_factory=list)
    D: np.ndarray = None
    DN: np.ndarray = None
    DG: np.ndarray = None
    V: np.ndarray = None
    path: str = "cpu"


def _locate(side: Side, cm, ws: Path, progress, preview, log, f, prefix: str) -> None:
    """Lens and every camera of one side, in that side's mat coordinates."""
    frames = LazyFrames(side.frames_dir, side.names)

    def on_frame(k, n):
        progress("mat", f(0.08 * k / n), quiet=True, step=f"{prefix}Looking for the mat in frame {k + 1}/{n}")
        if preview.frame_due() and k < len(side.names):  # 1/8-size decode: a few ms
            img = cv2.imread(str(side.frames_dir / side.names[k]), cv2.IMREAD_REDUCED_COLOR_8)
            if img is not None:
                preview.frame(img, None, f"{prefix}Frame {k + 1}/{n}: looking for the mat")

    mat = printed_mat()
    if mat.square_mm != 15.0:
        log(f"mat scale: squares measured at {mat.square_mm:.3f} mm (nominal 15)")
    cal = calibrate(frames, mat, log=log, on_frame=on_frame)
    on_mat = [n for n in side.names if n in cal.poses]
    progress.stat(f"{prefix}Frames showing the whole mat", f"{len(on_mat)}/{len(side.names)}")
    progress.stat(f"{prefix}Lens fit", f"{cal.rms_px:.1f} px")
    if cal.bend_mm > 0.5:
        progress.stat(f"{prefix}Mat bend", f"{cal.bend_mm:.1f} mm")
    if cal.bend_mm > 1.0:
        progress.warn(f"{prefix}The mat was not lying flat (bent by about {cal.bend_mm:.1f} mm). "
                      "Tape its corners down next time: heights can be off by that much.")
    if cal.rms_px > WARN_RMS_REL * max(cal.width, cal.height):
        progress.warn(f"{prefix}The camera check was rough ({cal.rms_px:.1f} px). Film more slowly, "
                      "in good light: the measurements get wider ± values.")
    preview.write(f"{prefix}Cameras placed from the mat", cameras=_centres(cal, on_mat), kinds=[MAT] * len(on_mat))
    progress("shape", f(0.10), step=f"{prefix}Finding features in the frames")
    shown = {"i": 0}

    def on_progress(x, text):
        progress("shape", f(0.10 + 0.35 * x), quiet=True, step=prefix + text)
        if preview.frame_due() and side.names:  # the frame being read, or cycle through them while matching
            m = re.search(r"frame (\d+)/(\d+)", text or "")
            i = int(m.group(1)) - 1 if m else shown["i"] % len(side.names)
            shown["i"] = i + 1
            img = cv2.imread(str(side.frames_dir / side.names[min(i, len(side.names) - 1)]), cv2.IMREAD_REDUCED_COLOR_8)
            if img is not None:
                preview.frame(img, None, prefix + (text or ""))

    def on_model(m):
        P = np.array(list(m.point_xyz.values())) if m.point_xyz else np.zeros((0, 3))
        C = np.array([m.point_rgb.get(k, (128, 128, 128)) for k in m.point_xyz]) if m.point_xyz else None
        located = [n for n in side.names if n in m.poses]
        keep = in_view_region(P, _zone()) if len(P) else np.zeros(0, bool)
        preview.write(f"{prefix}{len(located)} of {len(side.names)} frames placed", P[keep],
                      C[keep] if C is not None else None,
                      np.array([-m.poses[n][0].T @ m.poses[n][1] for n in located]), _kinds(located, on_mat))
    model = None
    if os.environ.get("PHONE2CAD_SFM_MODE", "register") == "register":
        try:  # triangulate from the mat-located frames, register the close-ups
            model = colmap_session.sfm_register(cm, ws, side.frames_dir, cal, cal.same_size, on_progress=on_progress,
                                                on_model=on_model)
            if len(model.poses) < 0.6 * len(cal.same_size):
                log(f"registration located only {len(model.poses)} frames; mapping from scratch")
                model = None
            else:
                for n, pose in model.poses.items():  # already in mat coordinates
                    cal.poses.setdefault(n, pose)
        except (colmap_session.ColmapError, RuntimeError) as e:
            log(f"registration failed ({str(e).splitlines()[0][:200]}); mapping from scratch")
            model = None
    if model is None:
        try:  # every frame by SIFT matching, then placed on the mat
            model = colmap_session.sfm_all(cm, ws, side.frames_dir, cal, cal.same_size, on_progress=on_progress,
                                           on_snapshot=lambda d: _snapshot_preview(preview, d, cal, log))
            model = align.to_mat(model, cal, log=log)
        except (colmap_session.ColmapError, RuntimeError) as e:
            log(f"locating all frames failed ({str(e).splitlines()[0][:200]}); using mat-located frames only")
            model = colmap_session.sparse(cm, ws, side.frames_dir, cal, on_mat)
    used = [n for n in side.names if n in cal.poses and n in model.poses]
    progress.stat(f"{prefix}Frames located", f"{len(used)}/{len(side.names)}")
    if len(used) < len(side.names):
        progress.warn(f"{prefix}{len(side.names) - len(used)} of {len(side.names)} frames could not be placed "
                      "(blurred, or nothing recognisable in them); they are left out.")
    progress.stat(f"{prefix}3D points found", len(model.point_xyz))
    zone = _zone()
    sp = np.array(list(model.point_xyz.values()))
    sc = np.array([model.point_rgb.get(k, (128, 128, 128)) for k in model.point_xyz])
    keep = in_view_region(sp, zone)
    preview.write(f"{prefix}Points found by matching the frames", sp[keep], sc[keep], _centres(cal, used),
                  kinds=_kinds(used, on_mat))
    progress("shape", f(0.46), step=f"{prefix}Fitting every camera to the mat")
    def on_round(k, P, C):
        keep = in_view_region(P, _zone())
        preview.write(f"{prefix}Every camera fitted to the mat (round {k} of 2)", P[keep], None, C, _kinds(used, on_mat))

    cal, stats = ba.adjust(cal, model, used, printed_mat(), log=log, on_round=on_round)
    side.cal, side.model, side.used, side.stats, side.on_mat = cal, model, used, stats, on_mat


def _solve_side(side: Side, cm, work: Path, dense: str, use_gpu: bool, progress, preview, log,
                lo: float, hi: float, prefix: str) -> Side:
    f = lambda x: lo + (hi - lo) * x  # noqa: E731  (this side's share of the bar)
    ws = work / side.tag
    ws.mkdir(parents=True, exist_ok=True)
    state = ws / "state.pkl"
    # Developer-only (CONTRIBUTING.md): unpickles a file this worker wrote itself; never set in the server.
    if os.environ.get("PHONE2CAD_RESUME") == "1" and state.exists():  # skip the camera solve
        side.cal, side.model, side.used, side.stats, side.on_mat = pickle.loads(state.read_bytes())[:5]
        log(f"resumed {side.tag} from {state}")
    else:
        _locate(side, cm, ws, progress, preview, log, f, prefix)
        state.write_bytes(pickle.dumps((side.cal, side.model, side.used, side.stats, side.on_mat)))
    cal, used = side.cal, side.used

    # The part's outline in the frames (SAM, see segment.py); the hull is carved
    # once all sides are in.
    side.mask_names = _outline(side, cal, used, progress, preview, log, f, prefix)
    log(f"{side.tag}: object box {np.round(side.box[0], 1).tolist()} .. {np.round(side.box[1], 1).tolist()} mm, "
        f"{len(side.core)} points on the part, {len(side.masks)} outlines")
    centres_m, base_m = _centres(cal, side.mask_names), _kinds(side.mask_names, side.on_mat)
    _check_outlines(side, centres_m, base_m, progress, preview, prefix)
    _check_coverage(side, progress, prefix)

    # Dense points from texture, to refine the faces. Whole frames: on plain
    # plastic crops lost coverage; crops only when the part is tiny in frame.
    side.views = align.pick_views(cal, used, (side.box[0] + side.box[1]) / 2, DENSE_MAX_FRAMES)
    shown = {"t": time.time()}
    centres = _centres(cal, used)
    view_kinds = [ACTIVE if n in side.views else k for n, k in zip(used, _kinds(used, side.on_mat))]

    def dense_progress(x, text=None, depth_dir=None):
        progress("dense", f(0.60 + 0.40 * x), quiet=True, step=prefix + (text or "Matching texture for fine detail"))
        final = (text or "").startswith("Merging depth maps into points")
        if depth_dir is not None and (final or time.time() - shown["t"] > 10.0):  # the dense cloud filling in
            shown["t"] = time.time()
            P, done = depth_points(depth_dir, side.box)
            if len(P):
                kinds = [k if n not in done else MAT if n in side.on_mat else MATCHED
                         for n, k in zip(used, view_kinds)]
                preview.write(f"{prefix}Texture points from {len(done)} of {len(side.views)} views",
                              P, None, centres, kinds)
    if dense in ("auto", "cuda") and use_gpu:
        part_px = colmap_session.part_region_px(cal, side.views, side.box) * cm.dense_image_size / max(cal.width, cal.height)
        try:
            reuse = [p for p in (ws / "dense" / "fused.ply", ws / "dense_crop" / "dense" / "fused.ply") if p.exists()]
            if os.environ.get("PHONE2CAD_RESUME") == "1" and reuse:
                fused = reuse[0]
            elif part_px < colmap_session.CROP_BELOW_PX:
                fused = colmap_session.dense_crop(cm, ws, side.frames_dir, cal, side.views, side.box, side.model,
                                                  on_progress=dense_progress, log=log)
            else:
                colmap_session.retriangulate(cm, ws, side.frames_dir, cal, side.views)
                fused = colmap_session.dense(cm, ws, side.frames_dir, on_progress=dense_progress)
            side.D, side.DN, side.DG = _read_fused(fused)
            side.path = "cuda"
        except colmap_session.ColmapError as e:
            if dense == "cuda":
                raise
            log(f"CUDA dense failed, using the CPU path: {str(e).splitlines()[0][:200]}")
    if side.D is None:
        side.D, side.DG, side.V = dense_cpu.reconstruct(LazyFrames(side.frames_dir, side.names), cal, side.views,
                                                        _zone(), log=log)
    progress.stat(f"{prefix}Dense points", int(len(side.D)))
    _fix_cut_outlines(side, progress, log, f, prefix)
    return side


_FACE_RGB = np.array([[230, 80, 70], [70, 160, 230], [90, 190, 90], [240, 180, 50], [170, 100, 210],
                      [60, 200, 190], [240, 120, 170], [150, 150, 60], [120, 120, 230], [220, 140, 90]])


# Certainty colours on the 3D copy (same as the phone's measurement list).
GRADE_RGB = {"precise": (0.18, 0.62, 0.36), "good": (0.55, 0.76, 0.29), "approx": (0.94, 0.71, 0.16),
             "rough": (0.9, 0.33, 0.24)}
_GRADE_ORDER = ["precise", "good", "approx", "rough"]


def tint_by_certainty(mesh, planes, extents, feats) -> None:
    """Colour each measured face by how sure its best measurement is; the rest
    stays grey. Shading is kept (the colour is scaled by the grey)."""
    best: dict[int, str] = {}
    for f in feats:
        g = f.get("geometry", {})
        ids = g.get("planes") or ([g["plane"]] if "plane" in g else [])
        for i in ids:
            if f["grade"] in _GRADE_ORDER and (i not in best or _GRADE_ORDER.index(f["grade"]) < _GRADE_ORDER.index(best[i])):
                best[i] = f["grade"]
    if not best or not mesh.has_vertex_colors():
        return
    V, N = np.asarray(mesh.vertices), np.asarray(mesh.vertex_normals)
    C = np.asarray(mesh.vertex_colors).copy()
    grey = C.mean(1, keepdims=True)
    for i, g in sorted(best.items(), key=lambda kv: -_GRADE_ORDER.index(kv[1])):  # surest drawn last
        p, e = planes[i], extents[i]
        if e is None:
            continue
        c, u, v, (hu, hv) = np.array(e["centre"]), np.array(e["u"]), np.array(e["v"]), e["half"]
        d = V - c
        on = (np.abs(V @ p.normal + p.offset) < max(0.6, 3 * p.rms)) & (N @ p.normal > 0.6) \
            & (np.abs(d @ u) <= hu + 0.5) & (np.abs(d @ v) <= hv + 0.5)
        C[on] = np.array(GRADE_RGB[g]) * (0.45 + 0.55 * grey[on])
    mesh.vertex_colors = o3d.utility.Vector3dVector(np.clip(C, 0, 1))


def _photos(side: Side, world_to_side=None) -> list[Photo]:
    """The side's located frames as cameras in side 1's coordinates (world_to_side:
    4x4 from side 1's world into this side's, for a flipped second side)."""
    out = []
    for n in side.used:
        R, t = side.cal.poses[n]
        if world_to_side is not None:
            R, t = R @ world_to_side[:3, :3], R @ world_to_side[:3, 3] + t
        out.append(Photo(side.frames_dir / n, side.cal.K, side.cal.dist, R, t, (side.cal.width, side.cal.height)))
    return out


def _mark_experimental(feats: list[dict]) -> None:
    """Holes and hole spacings are flagged experimental in V1: on the test parts real
    holes read ~0.5 mm small, one false hole appeared and a threaded bore was missed."""
    for f in feats:
        if f["kind"] == "hole" or f.get("label") == "Hole to hole":
            f["experimental"] = True
            if not f["detail"].startswith("Experimental"):
                f["detail"] = "Experimental: check holes before relying on them. " + f["detail"]


def _merge_holes(feats: list[dict], face_holes: list[dict]) -> list[dict]:
    """Add the holes found in the photos; where the texture points found the same
    hole as a cylinder, keep the surer of the two."""
    out = list(feats)
    for h in face_holes:
        if h["kind"] != "hole":
            out.append(h)
            continue
        a, p = np.array(h["geometry"]["axis"]), np.array(h["geometry"]["point"])
        same = [f for f in out if f["kind"] == "hole" and abs(np.dot(f["geometry"]["axis"], a)) > 0.95
                and np.linalg.norm(np.cross(np.array(f["geometry"]["point"]) - p, a)) < h["geometry"]["radius"]
                and abs(f["value"] - h["value"]) < max(1.0, 0.2 * h["value"])]
        if same and min(f["pm"] for f in same) <= h["pm"]:
            continue
        out = [f for f in out if f not in same] + [h]
    order = {"hole": 0, "boss": 1, "distance": 2, "height": 3, "overall": 4}
    out.sort(key=lambda f: (order[f["kind"]], -f["value"]))
    return out


def _mark_quick(feats: list[dict]) -> None:
    """A quick scan films one side only: whatever it never saw is missing from the
    3D copy, so its overall size may fall short and gets no grade."""
    for f in feats:
        if f["kind"] == "overall":
            f["text"] += " (may be incomplete)"
            f["grade"] = ""
            f["detail"] = "Quick scan: only one side was filmed, so parts of the object may be missing"


def _hull_holds_points(X: np.ndarray, vox: float, side: Side) -> float:
    """Share of the points the outlines voted the object's (side.core) that lie in
    or on the carved shape. Hardly any voted points means the outlines did not
    find the part at all: 0 (real scans had 500-2000)."""
    if side.core is None or len(side.core) < 30 or not len(X):
        return 0.0
    return float(np.isfinite(cKDTree(X).query(side.core, distance_upper_bound=3 * vox)[0]).mean())


def _kinds(names: list[str], on_mat) -> list[int]:
    m = set(on_mat)
    return [MAT if n in m else MATCHED for n in names]


def _height_rgb(P: np.ndarray) -> np.ndarray:
    """Colour by height (low: blue-grey, high: warm), so the shape reads at a glance."""
    t = np.clip((P[:, 2] - P[:, 2].min()) / max(np.ptp(P[:, 2]), 1e-6), 0, 1)[:, None]
    return (np.array([70, 110, 190]) * (1 - t) + np.array([235, 170, 70]) * t).astype(int)


def _coarse_shape(box, masks) -> np.ndarray | None:
    """Rough hull surface (~1/80 of the object) for previews and checks: about a second."""
    box = (np.r_[box[0][:2], 0.0], box[1])
    X, v = silhouette.visual_hull(box, masks, voxel=max(np.ptp(np.array(box), axis=0).max() / 80.0, 0.5))
    if not len(X):
        return None
    return silhouette.hull_surface(X, v, box[0])[0]


OUTLINE_DISAGREE = 0.15  # share of the agreed shape a frame's outline calls background


def _check_outlines(side: Side, centres, base, progress, preview, prefix) -> None:
    """Frames whose outline cuts into the shape all the others agree on (a
    shadow taken for paper, glare, the part leaving the frame): red cameras."""
    H = _coarse_shape(side.box, side.masks)
    if H is None:
        progress.warn(f"{prefix}The part's outline could not be found against the mat. A part the colour "
                      "of the paper, or very shiny, is hard to separate: try a contrasting colour mat.")
        return
    bad = []
    for i, v in enumerate(side.masks):
        Xc = H @ v.R.T + v.t
        z = Xc[:, 2]
        u = np.round(v.K[0, 0] * Xc[:, 0] / z + v.K[0, 2]).astype(int)
        w = np.round(v.K[1, 1] * Xc[:, 1] / z + v.K[1, 2]).astype(int)
        ins = (z > 0) & (u >= 0) & (w >= 0) & (u < v.background.shape[1]) & (w < v.background.shape[0])
        if ins.sum() > 50 and v.background[w[ins], u[ins]].mean() > OUTLINE_DISAGREE:
            bad.append(i)
    kinds = [PROBLEM if i in bad else k for i, k in enumerate(base)]
    preview.write(f"{prefix}Shape from all {len(side.masks)} outlines", H, _height_rgb(H), centres, kinds)
    progress.stat(f"{prefix}Unclear outlines", f"{len(bad)}/{len(side.masks)}")
    if len(bad) > max(2, 0.1 * len(side.masks)):
        progress.warn(f"{prefix}In {len(bad)} frames the part's outline was unclear (red cameras): "
                      "shadows, glare or the part cut off at the frame edge.")


COVERAGE_RINGS = ((12, 35), (35, 55), (55, 82))  # same dome as the phone's capture guide
COVERAGE_SECTORS = 12


def _check_coverage(side: Side, progress, prefix) -> None:
    """Which directions around the part were filmed; say plainly what is missing."""
    c = (side.box[0] + side.box[1]) / 2
    d = _centres(side.cal, side.used) - c
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    elev = np.degrees(np.arcsin(np.clip(d[:, 2], -1, 1)))
    sector = (np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 360 // (360 / COVERAGE_SECTORS)).astype(int)
    covered = [len(set(sector[(elev >= lo) & (elev < hi)])) for lo, hi in COVERAGE_RINGS]
    progress.stat(f"{prefix}Directions filmed (low / mid / high)", " / ".join(f"{n}/{COVERAGE_SECTORS}" for n in covered))
    if covered[0] < 8:
        progress.warn(f"{prefix}Low angles were filmed from only {covered[0]} of {COVERAGE_SECTORS} directions: "
                      "next time circle the part once with the phone held low.")
    if covered[1] < 6:
        progress.warn(f"{prefix}Side views are thin ({covered[1]} of {COVERAGE_SECTORS} directions): "
                      "the sides of the part may come out rounded.")


SEG_SEED_VIEWS = 10  # overview frames that find the part
SEG_VIEWS = int(os.environ.get("PHONE2CAD_SEG_VIEWS", "64"))  # frames outlined for carving (SAM: ~5 s each on CPU)


def _mat_points(box, step: float = 8.0) -> np.ndarray:
    """Paper points (z = 0) in a band around the part's footprint: negative
    clicks that tell SAM "not the paper" (SAM otherwise likes to outline the
    whole sheet), plus a sparse grid over the rest of the page."""
    from .mat import PAGE_H, PAGE_W
    xs, ys = np.arange(-PAGE_W / 2 + 4, PAGE_W / 2 - 3, step), np.arange(-PAGE_H / 2 + 4, PAGE_H / 2 - 3, step)
    G = np.array([[x, y, 0.0] for x in xs for y in ys])
    lo, hi = box
    pad = max(4.0, 0.15 * float(np.max(hi[:2] - lo[:2])))
    outside = np.any((G[:, :2] < lo[:2] - 1.0) | (G[:, :2] > hi[:2] + 1.0), axis=1)
    near = outside & np.all((G[:, :2] > lo[:2] - pad) & (G[:, :2] < hi[:2] + pad), axis=1)
    rng = np.random.default_rng(0)
    far = G[outside & ~near]
    far = far[rng.choice(len(far), min(len(far), 20), replace=False)] if len(far) else far
    return np.vstack([G[near], far])


def _vote(sparse: np.ndarray, masks: list, parts: list) -> np.ndarray:
    """The sparse points most outlines put inside the part (and few outside)."""
    P = sparse[sparse[:, 2] > 1.0]
    pos, neg = np.zeros(len(P), int), np.zeros(len(P), int)
    for vm, part in zip(masks, parts):
        Xc = P @ vm.R.T + vm.t
        z = Xc[:, 2]
        u = np.round(vm.K[0, 0] * Xc[:, 0] / np.maximum(z, 1e-6) + vm.K[0, 2]).astype(int)
        w = np.round(vm.K[1, 1] * Xc[:, 1] / np.maximum(z, 1e-6) + vm.K[1, 2]).astype(int)
        ins = (z > 0) & (u >= 0) & (w >= 0) & (u < part.shape[1]) & (w < part.shape[0])
        pos[ins] += part[w[ins], u[ins]]
        neg[ins] += vm.background[w[ins], u[ins]]
    return P[(pos >= 3) & (pos >= 4 * neg)]


def _page_grid(step: float = 10.0) -> np.ndarray:
    from .mat import PAGE_H, PAGE_W
    xs, ys = np.arange(-PAGE_W / 2 + 4, PAGE_W / 2 - 3, step), np.arange(-PAGE_H / 2 + 4, PAGE_H / 2 - 3, step)
    return np.array([[x, y, 0.0] for x in xs for y in ys])


def _page_mask(vm, shape) -> np.ndarray:
    """The printed sheet in an (undistorted, mask-size) frame."""
    from .mat import PAGE_H, PAGE_W
    C = np.array([[-PAGE_W / 2, -PAGE_H / 2, 0], [PAGE_W / 2, -PAGE_H / 2, 0], [PAGE_W / 2, PAGE_H / 2, 0],
                  [-PAGE_W / 2, PAGE_H / 2, 0]])
    Xc = C @ vm.R.T + vm.t
    uv = np.c_[vm.K[0, 0] * Xc[:, 0] / Xc[:, 2] + vm.K[0, 2], vm.K[1, 1] * Xc[:, 1] / Xc[:, 2] + vm.K[1, 2]]
    m = np.zeros(shape, np.uint8)
    cv2.fillPoly(m, [np.round(uv).astype(np.int32)], 1)
    return m.astype(bool)


def _main_cluster(P: np.ndarray) -> np.ndarray:
    """The biggest connected group of points (a stray point far away must not
    stretch the part's box). Link distance relative to the group's own size."""
    if len(P) < 30:
        return P
    lo, hi = np.percentile(P, 5, axis=0), np.percentile(P, 95, axis=0)
    eps = max(2.0, 0.08 * float((hi - lo).max()))
    lab = np.asarray(o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P)).cluster_dbscan(eps=eps, min_points=4))
    if not (lab >= 0).any():
        return P
    return P[lab == np.bincount(lab[lab >= 0]).argmax()]


def _hull_box(region, masks, parts, voxel: float, margin: float, points: bool = False):
    """Box (with a margin share) around where the part is: grid points inside the
    part's outline in at least 80 % of the views that see them (and in 3 or more).
    Positive evidence, unlike carving: space no view sees is not kept. Biggest
    connected piece only. None when nothing qualifies."""
    lo, hi = np.asarray(region[0], float), np.asarray(region[1], float)
    g = [np.arange(lo[i], hi[i], voxel) + voxel / 2 for i in range(3)]
    if np.prod([len(a) for a in g]) > 3_000_000:  # keep memory bounded
        return _hull_box(region, masks, parts, voxel * 1.5, margin, points=points)
    X = np.stack(np.meshgrid(*g, indexing="ij"), -1).reshape(-1, 3)
    seen, inside = np.zeros(len(X), np.int16), np.zeros(len(X), np.int16)
    for vm, part in zip(masks, parts):
        Xc = X @ vm.R.T + vm.t
        z = Xc[:, 2]
        u = np.round(vm.K[0, 0] * Xc[:, 0] / np.maximum(z, 1e-6) + vm.K[0, 2]).astype(np.int32)
        w = np.round(vm.K[1, 1] * Xc[:, 1] / np.maximum(z, 1e-6) + vm.K[1, 2]).astype(np.int32)
        ins = (z > 1e-3) & (u >= 0) & (w >= 0) & (u < part.shape[1]) & (w < part.shape[0])
        seen += ins
        inside[ins] += part[w[ins], u[ins]]
    keep = (inside >= 3) & (inside >= 0.8 * seen)
    if keep.sum() < 10:
        return (None, None) if points else None
    X = silhouette.largest_piece(X[keep], voxel, lo)
    lo2, hi2 = X.min(0) - voxel, X.max(0) + voxel
    m = 2.0 + margin * float((hi2 - lo2).max())
    box = (np.r_[lo2[:2] - m, 0.0], hi2 + m)
    return (box, X) if points else box


def _outline(side: Side, cal, used, progress, preview, log, f, prefix) -> list[str]:
    """SAM outlines (segment.py). 1. Find the part on overview frames: a crude
    detector (where the photo differs from the known print) gives SAM a box and
    clicks. 2. The 3D points those outlines agree on give the part's core and box.
    3. Outline SEG_VIEWS frames spread around it, prompted with the projected box,
    the part's own points and paper points beside it. Sets side.masks, side.core,
    side.box; returns the outlined frames' names."""
    from . import segment

    seg = segment.Outliner(cal, segment.Segmenter())
    sparse = np.array(list(side.model.point_xyz.values()))
    grid = _page_grid()
    crude = silhouette.MaskMaker(cal, printed_mat())

    # 1. Find the part on overview frames.
    overview = [n for n in used if n in set(side.on_mat)] or list(used)
    x0, x1, y0, y1 = _zone()
    seed_views = align.pick_views(cal, overview, np.array([(x0 + x1) / 2, (y0 + y1) / 2, 0.0]), SEG_SEED_VIEWS)
    masks, parts = [], []
    for k, n in enumerate(seed_views):
        progress("dense", f(0.48 + 0.04 * k / len(seed_views)), quiet=True,
                 step=f"{prefix}Finding the part in frame {k + 1}/{len(seed_views)}")
        img = cv2.imread(str(side.frames_dir / n), cv2.IMREAD_COLOR)
        vm0, rough = crude(img, n)
        out = seg.seed(img, n, rough, _page_mask(vm0, rough.shape), grid)
        if out is not None:
            masks.append(out[0])
            parts.append(out[1])
            preview.frame(img, out[1], f"{prefix}Finding the part in frame {k + 1}/{len(seed_views)}")
    core = _main_cluster(_vote(sparse, masks, parts)) if masks else np.zeros((0, 3))
    # The part's box from a coarse shape carved by these outlines alone: needs no
    # 3D points (a black or plain part has few), only the outlines.
    from .mat import PAGE_H, PAGE_W
    region = (np.array([-0.75 * PAGE_W, -0.75 * PAGE_H, 0.0]), np.array([0.75 * PAGE_W, 0.75 * PAGE_H, 0.5 * PAGE_H]))
    box, inside = _hull_box(region, masks, parts, 3.0, 0.15, points=True) if len(masks) >= 3 else (None, None)
    if inside is not None and len(inside) > 50:  # interior only: clicks on the rim could land on the paper
        surf = silhouette.hull_surface(inside, 3.0, inside.min(0) - 1.5)[0]
        inner = inside[~np.isfinite(cKDTree(surf).query(inside, distance_upper_bound=0.1)[0])]
        inside = inner if len(inner) >= 20 else inside
    if box is None:  # e.g. a part printed like the mat: measured from its texture alone (see run())
        log(f"the part could not be outlined on the overview frames ({len(masks)} usable)")
        side.masks, side.core = [], np.zeros((0, 3))
        side.box = (np.array([x0, y0, 0.0]), np.array([x1, y1, 80.0]))
        return []
    log(f"{side.tag}: part found on {len(masks)} of {len(seed_views)} overview frames, {len(core)} points, "
        f"box {np.round(box[1] - box[0], 0).tolist()} mm")

    # 2. Outline it on frames spread around it.
    views = align.pick_views(cal, used, (box[0] + box[1]) / 2, SEG_VIEWS)
    masks, parts, names = [], [], []
    for k, n in enumerate(views):
        progress("dense", f(0.52 + 0.08 * k / len(views)), quiet=True,
                 step=f"{prefix}Outlining the part in frame {k + 1}/{len(views)}")
        img = cv2.imread(str(side.frames_dir / n), cv2.IMREAD_COLOR)
        # Clicks spread over the whole part (the overview outlines' shape, cap and
        # body alike): clicks on one region make SAM outline only that region.
        out = seg(img, n, box, inside, _mat_points(box))
        if out is None:
            continue
        masks.append(out[0])
        parts.append(out[1])
        names.append(n)
        preview.frame(img, out[1], f"{prefix}Outlining the part in frame {k + 1}/{len(views)}")
    side.masks, side.clicks = masks, inside
    final = _main_cluster(_vote(sparse, masks, parts)) if masks else np.zeros((0, 3))
    side.core = final if len(final) >= 30 else core
    # The box only has to hold the part (the carving makes the shape): the one
    # from the overview stage, never tightened (a tightened box cut off an arm
    # some outlines missed).
    side.box = (np.r_[box[0][:2], 0.0], box[1])
    return names


# A piece of the part some outlines left out. Carving keeps only what every
# outline keeps, so a third of the outlines each shaving 1-2 mm off an arm lying
# on the mat lost the whole arm (a test scan). Votes alone cannot tell such a
# piece from empty space only a few views see into (the inner corner between
# two arms); the dense points can: they come from texture, not from the
# outlines, and there are none in empty space.
LOST_OUTSIDE_MM = 1.5  # beyond the carved shape's surface noise
LOST_KEEP = 1 / 3  # carved by fewer than this share of the views that see the point
LOST_MIN_POINTS = 40  # a lump of at least this many dense points (and 2 % of the side's)
LOST_CUT = 0.2  # an outline calling this share of a lump background cut it away


def _bg_votes(P: np.ndarray, masks: list) -> tuple[np.ndarray, np.ndarray]:
    """Per outline (rows): which points it calls background, and which it sees."""
    B, S = np.zeros((len(masks), len(P)), bool), np.zeros((len(masks), len(P)), bool)
    for i, v in enumerate(masks):
        Xc = P @ v.R.T + v.t
        z = Xc[:, 2]
        u = np.round(v.K[0, 0] * Xc[:, 0] / np.maximum(z, 1e-6) + v.K[0, 2]).astype(np.int64)
        w = np.round(v.K[1, 1] * Xc[:, 1] / np.maximum(z, 1e-6) + v.K[1, 2]).astype(np.int64)
        S[i] = (z > 1e-3) & (u >= 0) & (w >= 0) & (u < v.background.shape[1]) & (w < v.background.shape[0])
        B[i, S[i]] = v.background[w[S[i]], u[S[i]]]
    return B, S


def _lost_lumps(side: Side) -> list[np.ndarray]:
    """Lumps of dense points well outside the carved shape that most outlines keep."""
    if side.D is None or len(side.masks) < 6:
        return []
    lo, hi = side.box
    P = side.D[(side.D[:, 2] > 1.0) & np.all((side.D >= lo) & (side.D <= hi), axis=1)]
    H, vox = silhouette.visual_hull(side.box, side.masks, voxel=max(1.0, 2 * _voxel_for(side.box)))
    if len(P) < LOST_MIN_POINTS or not len(H):
        return []
    big = max(LOST_MIN_POINTS, 0.02 * len(P))
    P = P[~np.isfinite(cKDTree(H).query(P, distance_upper_bound=LOST_OUTSIDE_MM + vox / 2)[0])]
    B, S = _bg_votes(P, side.masks)
    votes, seen = B.sum(0), S.sum(0)
    Q = P[(seen >= 3) & (votes < LOST_KEEP * seen)]
    if len(Q) < LOST_MIN_POINTS:
        return []
    lab = np.asarray(o3d.geometry.PointCloud(o3d.utility.Vector3dVector(Q)).cluster_dbscan(eps=2.0, min_points=5))
    return [Q[lab == k] for k in range(lab.max() + 1) if (lab == k).sum() >= big]


def _cut_share(lumps: list[np.ndarray], masks: list) -> np.ndarray:
    """Per outline, the largest share of one lump it calls background."""
    return np.max([_bg_votes(L, masks)[0].mean(1) for L in lumps], axis=0)


def _fix_cut_outlines(side: Side, progress, log, f, prefix) -> None:
    """Outlines that cut away a piece most others keep: outlined again with clicks
    on that piece, kept if the new outline cuts less of it. (Leaving such outlines
    out instead also kept the arm on the test scan, but with half the outlines
    gone the shape came out fatter: median error 0.27 mm against 0.21.)"""
    lumps = _lost_lumps(side)
    if not lumps:
        return
    cut = _cut_share(lumps, side.masks)
    bad = [int(i) for i in np.flatnonzero(cut >= LOST_CUT)]
    log(f"{side.tag}: {len(lumps)} piece(s) most outlines keep ({sum(map(len, lumps))} dense points, "
        f"{[np.round(L.mean(0), 0).tolist() for L in lumps]}) cut away by {len(bad)} of {len(side.masks)} outlines")
    if not bad:
        return
    from . import segment
    seg = segment.Outliner(side.cal, segment.Segmenter())
    piece = np.vstack(lumps)
    fixed = 0
    for k, i in enumerate(bad):
        progress("dense", f(1.0), quiet=True, step=f"{prefix}Outlining frame {k + 1}/{len(bad)} again: it left out part of the object")
        n = side.mask_names[i]
        img = cv2.imread(str(side.frames_dir / n), cv2.IMREAD_COLOR)
        out = seg(img, n, side.box, side.clicks, _mat_points(side.box), extra_pts=piece) if img is not None else None
        if out is not None and _cut_share(lumps, [out[0]])[0] < cut[i]:
            side.masks[i] = out[0]
            fixed += 1
    log(f"{side.tag}: outlined {len(bad)} frames again, {fixed} now cut less of the piece")
    progress.stat(f"{prefix}Outlines redone (they left out part of the object)", f"{fixed}/{len(bad)}")


# Rough match of the two sides' outline shapes. Below this fitness the two shapes
# disagree: on the test scans that meant one side's outlines had missed an arm (the
# sides themselves lined up within a degree and a millimetre), so the copy lost it.
# Calibrated on replays of the test scans: an arm was lost at 0.38-0.66, kept at 0.74-0.83.
# A weak match is retried with more seeds first, in case RANSAC missed the right one.
MIN_SIDE_FITNESS = 0.70
SIDE_SEEDS, SIDE_RETRY_SEEDS = 8, 32
UNSURE_SIDES = ("The two sides' shapes do not match well, so part of the 3D copy may be missing: "
                "check the outline photo and scan again.")


def _align_sides(a: Side, b: Side, log, warn=lambda msg: None) -> tuple[np.ndarray, float]:
    """4x4 taking side b's mat coordinates into side a's: the part was flipped
    and moved. Rough match of the two hulls' shapes (each side's own carving),
    then point-to-plane ICP on the dense points near them (the true surfaces).
    A weak rough match is retried with more RANSAC seeds; warns if it stays weak.
    Also returns the fit's rms (mm), the mismatch the joint carve must allow."""
    reg = o3d.pipelines.registration
    clouds = []
    for s in (a, b):
        X, vox = silhouette.visual_hull(s.box, s.masks, voxel=2 * _voxel_for(s.box))
        H, HN = silhouette.hull_surface(X, vox, s.box[0])
        up = H[:, 2] > 1.0  # the floor is where it touched the paper, not seen
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(H[up]))
        pc.normals = o3d.utility.Vector3dVector(HN[up])
        near = np.isfinite(cKDTree(H[up]).query(s.D, distance_upper_bound=1.5)[0]) & (s.D[:, 2] > 0.8) \
            if s.D is not None and len(s.D) else np.zeros(0, bool)
        dp = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(s.D[near] if len(near) else np.zeros((0, 3))))
        clouds.append((pc, dp))
    (ha, da), (hb, db) = clouds
    feats = []
    for pc in (hb, ha):
        d = pc.voxel_down_sample(1.0)
        d.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.5, max_nn=30))
        feats.append((d, reg.compute_fpfh_feature(d, o3d.geometry.KDTreeSearchParamHybrid(radius=5.0, max_nn=100))))
    (sb, fb), (sa, fa) = feats
    A, B = np.asarray(ha.points), np.asarray(hb.points)
    cB = B.mean(0)

    def ransac(seeds) -> list:
        out = []
        for seed in seeds:
            o3d.utility.random.seed(seed)
            g = reg.registration_ransac_based_on_feature_matching(
                sb, sa, fb, fa, True, 1.5, reg.TransformationEstimationPointToPoint(False), 3,
                [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9), reg.CorrespondenceCheckerBasedOnDistance(1.5)],
                reg.RANSACConvergenceCriteria(200000, 0.999))
            out.append(reg.registration_icp(hb, ha, 1.5, g.transformation, reg.TransformationEstimationPointToPlane()))
        # A long or symmetric part also matches turned end-for-end: try the 180 degree
        # turns about side b's main axes too.
        top = max(out, key=lambda c: c.fitness)
        for axis in np.linalg.eigh(np.cov((B - cB).T))[1].T:
            F = np.eye(4)
            F[:3, :3] = 2 * np.outer(axis, axis) - np.eye(3)
            F[:3, 3] = cB - F[:3, :3] @ cB
            out.append(reg.registration_icp(hb, ha, 1.5, np.asarray(top.transformation) @ F,
                                            reg.TransformationEstimationPointToPlane()))
        return out

    # The right alignment is the one where each side's views carve least of the
    # other side's shape (a wrong turn cuts the part to pieces).
    def kept(c) -> float:
        T = np.asarray(c.transformation)
        Ti = np.linalg.inv(T)
        return 0.5 * (silhouette.carve(B @ T[:3, :3].T + T[:3, 3], a.masks).mean()
                      + silhouette.carve(A @ Ti[:3, :3].T + Ti[:3, 3], b.masks).mean())

    def pick(cands):
        # Only candidates whose shapes actually meet (at least half the best match).
        fmax = max(c.fitness for c in cands)
        scores = [kept(c) if c.fitness >= 0.5 * fmax else -1.0 for c in cands]
        return cands[int(np.argmax(scores))], scores

    cands = ransac(range(SIDE_SEEDS))
    best, scores = pick(cands)
    if best.fitness < MIN_SIDE_FITNESS:
        log(f"sides matched weakly (fitness {best.fitness:.2f}): retrying with {SIDE_RETRY_SEEDS} more seeds")
        cands += ransac(range(SIDE_SEEDS, SIDE_SEEDS + SIDE_RETRY_SEEDS))
        best, scores = pick(cands)
    T, rms = best.transformation, best.inlier_rmse
    log(f"sides lined up on the outlines: fitness {best.fitness:.2f}, rms {best.inlier_rmse:.2f} mm, "
        f"{max(scores):.0%} of each side's shape agrees with the other's views "
        f"(other candidates {max(sorted(scores)[-2], 0):.0%} and below)")
    if best.fitness < MIN_SIDE_FITNESS:
        warn(UNSURE_SIDES)
    if len(da.points) > 500 and len(db.points) > 500:
        da.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=1.5, max_nn=30))
        fine = reg.registration_icp(db, da, 1.0, T, reg.TransformationEstimationPointToPlane())
        if fine.fitness > 0.3:
            T, rms = fine.transformation, fine.inlier_rmse
            log(f"refined on the dense points: fitness {fine.fitness:.2f}, rms {fine.inlier_rmse:.2f} mm")
        else:
            log(f"dense points did not agree well enough to refine (fitness {fine.fitness:.2f})")
    else:
        log(f"too few dense points near the outlines to refine ({len(da.points)}, {len(db.points)})")
    return np.asarray(T), float(rms)


def run(job: Path, dense: str = "auto", max_image_size: int = 1600, keep: bool = False, log=print) -> dict:
    """One scan: one side, or two when the part was also filmed flipped
    (frames/side2/). Results in JOB/result/."""
    job = Path(job).resolve()
    frames_dir, work, result = job / "frames", job / "work", job / "result"
    result.mkdir(parents=True, exist_ok=True)
    progress = Progress(job, log)
    report: dict = {"schema": "phone2cad.result.v1"}
    failed = False
    heartbeat = None
    try:
        preview = Preview(result, progress)
        heartbeat = Heartbeat(preview, lambda: progress.state.get("step", ""))
        heartbeat.start()
        progress("mat", 0.01, step="Reading the frames")
        sides = [Side("side1", frames_dir, sorted(p.name for p in frames_dir.glob("*.jpg")))]
        if len(sides[0].names) < 8:
            raise RuntimeError(f"only {len(sides[0].names)} frames uploaded; need at least 8")
        names2 = sorted(p.name for p in (frames_dir / "side2").glob("*.jpg"))
        if len(names2) >= 8:
            sides.append(Side("side2", frames_dir / "side2", names2))
        two = len(sides) == 2
        report["mode"] = "full" if two else "quick"
        # The GPU serves feature matching whatever dense path is asked for:
        # CPU matching of every frame pair took 40+ min and 3 GB on 64 frames.
        use_gpu = gpu_available()
        cm = colmap_session.Colmap(job, log=log, use_gpu=use_gpu, max_image_size=max_image_size)
        spans = [(0.0, 0.84)] if not two else [(0.0, 0.42), (0.42, 0.84)]
        for side, (lo, hi), prefix in zip(sides, spans, ("", "Other side: ")):
            heartbeat.show(side.frames_dir, side.names)
            _solve_side(side, cm, work, dense, use_gpu, progress, preview, log, lo, hi, prefix)
        s1 = sides[0]
        heartbeat.show(s1.frames_dir, s1.used)
        report["frames"] = {"uploaded": sum(len(s.names) for s in sides), "used": sum(len(s.used) for s in sides),
                            "located_by_mat": sum(len(s.on_mat) for s in sides), "sides": len(sides)}
        report["solve"] = {"marker_rms_px": round(s1.stats.marker_rms_px, 3),
                           "feature_rms_px": round(s1.stats.feature_rms_px, 3),
                           "points": s1.stats.n_points, "scale_sigma_rel": s1.stats.scale_sigma_rel,
                           "focal_px": round(float(s1.cal.K[0, 0]), 2)}

        # One hull carved by every view; side 2's views see side 1's coordinates through T.
        box, views, to_view = s1.box, list(s1.masks), None
        D, DN, DG = s1.D, s1.DN, s1.DG
        if two:
            s2 = sides[1]
            progress("measure", 0.85, step="Lining up the two sides")
            T, rms = _align_sides(s1, s2, log, progress.warn)
            Ti = np.linalg.inv(T)
            A, B = _coarse_shape(s1.box, s1.masks), _coarse_shape(s2.box, s2.masks)
            if A is not None and B is not None:  # side 1 blue, side 2 orange, where the match put it
                B = B @ T[:3, :3].T + T[:3, 3]
                preview.write(f"Both sides lined up (they differ by {rms:.2f} mm)", np.vstack([A, B]),
                              np.r_[np.tile([70, 130, 220], (len(A), 1)), np.tile([235, 150, 60], (len(B), 1))],
                              np.vstack([_centres(s1.cal, s1.used)]), _kinds(s1.used, s1.on_mat))
            for s in (s1, s2):  # the masks' last use: shrink them in place
                silhouette.allow_misalignment(s.masks, (s.box[0] + s.box[1]) / 2, rms / 2)
            c2 = np.array([[x, y, z] for x in (s2.box[0][0], s2.box[1][0]) for y in (s2.box[0][1], s2.box[1][1])
                           for z in (s2.box[0][2], s2.box[1][2])]) @ T[:3, :3].T + T[:3, 3]
            # Each side's box holds the whole part, so the part is inside both: the
            # overlap (a union would grow with the rotation and coarsen the voxels).
            box = (np.maximum(s1.box[0], c2.min(0)), np.minimum(s1.box[1], c2.max(0)))
            n1 = len(s1.masks)
            views = s1.masks + s2.masks
            to_view = lambda i, X: X if i < n1 else X @ Ti[:3, :3].T + Ti[:3, 3]  # noqa: E731
            D = np.vstack([s1.D, s2.D @ T[:3, :3].T + T[:3, 3]])
            DG = np.concatenate([s1.DG, s2.DG])
            DN = np.vstack([s1.DN, s2.DN @ T[:3, :3].T]) if s1.DN is not None and s2.DN is not None else None
            report["sides_transform"] = np.round(T, 5).tolist()
        progress("measure", 0.86, step="Carving the shape from the outlines")
        X, vox = silhouette.visual_hull(box, views, voxel=_voxel_for(box), to_view=to_view)
        H, HN = (silhouette.hull_surface(X, vox, box[0]) if len(X) else (np.zeros((0, 3)), np.zeros((0, 3))))
        face_pts = np.ones(len(H), bool) if two else H[:, 2] > 1.0  # one side: the floor is the paper
        progress.stat("Shape from outlines", f"{len(X):,} voxels of {vox} mm")
        preview.write("Shape from the outlines", H, _height_rgb(H) if len(H) else None, _centres(s1.cal, s1.used),
                      _kinds(s1.used, s1.on_mat))
        report["hull"] = {"voxels": int(len(X)), "voxel_mm": vox, "surface_points": int(len(H))}
        path = s1.path
        report["dense"] = {"path": path, "points": int(len(D)), "systematic_mm": SYSTEMATIC_MM[path]}

        progress("measure", 0.88, step="Fitting faces on the shape")
        # The outline shape must hold the part's own 3D points (from the camera solve):
        # when outlines fail (a part the colour of the paper) a thin sliver can survive.
        held = _hull_holds_points(X, vox, s1)
        log(f"the outline shape holds {held:.0%} of the points voted the part's")
        if held < 0.6:
            log(f"the outline shape holds only {held:.0%} of the part's 3D points; not using it")
            progress.warn("The part's outline was unclear against the mat, so it was measured from its "
                          "surface texture only: a contrasting mat or more light helps.")
        if face_pts.sum() >= 500 and held >= 0.6:
            on = np.zeros(len(D), bool)
            if len(D):
                inside = np.isfinite(cKDTree(X).query(D, distance_upper_bound=vox)[0])
                near = np.isfinite(cKDTree(H).query(D, distance_upper_bound=1.5)[0])
                on = (inside | near) & ((D[:, 2] > 0.8) | two)
            Dp, DGp = D[on], DG[on]
            if DN is not None:
                DNp = DN[on]
            else:
                DNp = estimate_normals(Dp, s1.V[on]) if (len(Dp) and s1.V is not None and not two) else np.zeros((len(Dp), 3))
            if len(Dp):
                preview.write("Texture points on the part", Dp, _grey_rgb(DGp), _centres(s1.cal, s1.views),
                              _kinds(s1.views, s1.on_mat))
            hull_faces, _ = fit_planes(H[face_pts], HN[face_pts], log=log)
            if hull_faces:  # each face its own colour
                Hf = H[face_pts]
                FP = np.vstack([Hf[p.idx] for p in hull_faces])
                FC = np.vstack([np.tile(_FACE_RGB[i % len(_FACE_RGB)], (len(p.idx), 1)) for i, p in enumerate(hull_faces)])
                preview.write(f"{len(hull_faces)} flat faces found", FP, FC, _centres(s1.cal, s1.used),
                              _kinds(s1.used, s1.on_mat))
            if two:
                # Carved from both takes, the hull is tight on every face; on plain
                # parts the textured points are the noisier reference (corner
                # benchmark, two sides: hull 0.36 mm mean error, dense-refined 0.61).
                faces = [hull_face(p, H[face_pts][p.idx]) for p in hull_faces]
                log(f"faces: {len(faces)} from the hull of both sides")
            else:
                faces = refine_with_points(hull_faces, H[face_pts], Dp, DNp, log=log)
            progress("measure", 0.92, step="Looking for holes and round features")
            cyl = fit_cylinders(Dp, DNp, np.arange(len(Dp)), faces, log=log) if len(Dp) >= 150 else []
            fit = FitResult(faces, cyl)
            feats = describe(fit, H[face_pts], s1.stats.scale_sigma_rel, SYSTEMATIC_MM[path])
            photos = _photos(s1) + (_photos(sides[1], np.linalg.inv(T)) if two else [])
            progress("measure", 0.95, step="Building the mesh")
            preview.write("Building the final surface", H, _grey_rgb(np.full(len(H), 0.75)), _centres(s1.cal, s1.used),
                          _kinds(s1.used, s1.on_mat))
            mesh = build_mesh(H, HN, _hull_grey(H, Dp, DGp))
            part_points = int(len(H))
        else:
            # Silhouettes failed (e.g. a white part on white paper): measure the dense points alone.
            log("the outlines gave no usable shape; measuring from the dense points only")
            fit, feats, mesh, part_points, L = _measure_dense_only(s1.D, s1.DN, s1.DG, s1.V, path, _zone(), s1.stats,
                                                                   log, progress)
            photos = _photos(s1, np.linalg.inv(L))
        # Round holes seen in the photos of each face (texture-free), wherever the faces came from.
        progress("measure", 0.97, step="Looking for holes in the photos of each face")
        extents = [face_extent(p, p.pts) if p.pts is not None else None for p in fit.planes]
        feats = _merge_holes(feats, hole_features(find_holes(fit.planes, extents, photos, log=log), grade))
        _mark_experimental(feats)
        if not two:
            _mark_quick(feats)
        progress.stat("Faces / holes found", f"{len(fit.planes)} / {sum(f['kind'] == 'hole' for f in feats)}")
        tint_by_certainty(mesh, fit.planes, extents, feats)
        write_ply(result / "mesh.ply", mesh)
        progress("measure", 0.99, step="Drawing the 3D copy over a photo")
        n1 = len(s1.used)  # side 1's photos: the part as it first sat on the mat
        try:
            if overlay.write_check(result / "check.jpg", photos[:n1], np.asarray(mesh.vertices),
                                   np.asarray(mesh.triangles), log):
                report["check"] = "check.jpg"
        except Exception as e:  # noqa: BLE001 - a missing check picture must not fail the scan
            log(f"check picture skipped: {e}")
        report.update({
            "units": "mm",
            "features": feats,
            "planes": [{"normal": p.normal.round(5).tolist(), "offset": round(p.offset, 4),
                        "centroid": p.centroid.round(3).tolist(), "points": int(len(p.idx)),
                        "rms": round(p.rms, 4), "source": p.source, "extent": e}
                       for p, e in zip(fit.planes, extents)],
            "part_points": part_points,
            "mesh": {"file": "mesh.ply", "triangles": len(mesh.triangles)},
        })
        (result / "features.json").write_text(json.dumps(report, indent=1))
        progress("done", 1.0, status="done")
        return report
    except Exception as e:  # noqa: BLE001 - reported to the phone as a plain message
        failed = True
        log(traceback.format_exc())
        progress("done", 1.0, status="failed", error=_user_message(e))
        raise
    finally:
        if heartbeat:
            heartbeat.stop()
        # PHONE2CAD_KEEP_FRAMES=failed|all (opt-in, local debugging): the server
        # moves the frames to data/server/debug/ instead of deleting them.
        if not keep:
            kf = os.environ.get("PHONE2CAD_KEEP_FRAMES") or ("failed" if os.environ.get("PHONE2CAD_KEEP_FAILED") == "1" else "")
            if not (kf == "all" or (kf == "failed" and failed)):
                shutil.rmtree(frames_dir, ignore_errors=True)
            if kf != "all":  # "all" also keeps COLMAP's work for later analysis
                shutil.rmtree(work, ignore_errors=True)


def _user_message(e: Exception) -> str:
    """What the phone shows for a failed scan: our own messages (RuntimeError,
    ValueError) as written, without file paths; anything else stays in worker.log."""
    if isinstance(e, (RuntimeError, ValueError)) and not isinstance(e, subprocess.CalledProcessError) and str(e):
        msg = re.sub(r"(?:/[^\s/'\":]+)+/([^\s/'\":]+)", r"\1", str(e).splitlines()[0])
        return msg[:300]
    return "processing failed on the PC (details in its worker.log)"


def _zone() -> tuple[float, float, float, float]:
    return tuple(np.array(printed_mat().clear_zone_world) + [3, -3, 3, -3])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", type=Path)
    ap.add_argument("--dense", choices=["auto", "cuda", "cpu"], default=os.environ.get("PHONE2CAD_DENSE", "auto"))
    ap.add_argument("--max-image-size", type=int, default=int(os.environ.get("PHONE2CAD_MAX_IMAGE_SIZE", "1600")))
    ap.add_argument("--keep", action="store_true", help="keep frames and work dir (debug only)")
    a = ap.parse_args()
    run(a.job, a.dense, a.max_image_size, a.keep)
    # The result is written. Skip the interpreter's teardown: a native library's
    # thread can abort it ("terminate called without an active exception", code
    # 134), and the server would then report a finished scan as failed.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
