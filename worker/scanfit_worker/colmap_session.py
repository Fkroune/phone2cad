"""COLMAP stages for one ScanFit job.

Adapted from Orbiter's ``colmap/run_colmap_session.sh``
(https://github.com/RawMechatronicsOrg/Orbiter, commit af137c8,
Copyright (c) 2026 Orbiter contributors, Apache License 2.0; see
``worker/third_party/orbiter_colmap/``).

Changes made for ScanFit:
* Ported from bash to Python; the step()/[PROFILE] logging is kept.
* Priors come from the ChArUco mat (lens model and metric poses), written in
  Orbiter's ``orbiter.sfm_priors.v1`` schema and converted by the unmodified
  ``sfm_priors_to_colmap.py``.
* ``matches_importer`` with pose-based pairs replaces ``exhaustive_matcher``.
* The sparse model is exported to text so the marker-constrained bundle
  adjustment can refine it; a second ``point_triangulator`` pass then
  re-triangulates with the refined cameras.
* GPU steps run at a reduced image size (2 GB card) and fall back to the
  CPU; when CUDA dense stereo fails, ``DenseUnavailable`` tells the caller to
  use the CPU dense path instead.
* COLMAP runs natively when ``colmap`` is on PATH, otherwise in the official
  ``colmap/colmap`` Docker image with the job folder mounted at the same path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .calib import Calibration, quat_to_rotmat, rotmat_to_quat

PRIORS_SCRIPT = Path(__file__).resolve().parents[1] / "third_party" / "orbiter_colmap" / "sfm_priors_to_colmap.py"
# The same COLMAP build as the server image's base (pinned by digest, see Dockerfile).
COLMAP_IMAGE = os.environ.get(
    "SCANFIT_COLMAP_IMAGE", "colmap/colmap@sha256:c1de51615dff33066cd305071226b6343c1168324ef3bd622f0825e55feb5efe")


class ColmapError(RuntimeError):
    pass


class DenseUnavailable(ColmapError):
    """CUDA dense stereo could not run; use the CPU dense path."""


_GPU_CACHE: dict[str, bool] = {}


CPU_SIFT_THREADS = max(1, int(os.environ.get("SCANFIT_CPU_SIFT_THREADS", "3")))


def _docker_has_gpu() -> bool:
    """True when Docker has the NVIDIA runtime."""
    if "docker" not in _GPU_CACHE:
        ok = False
        try:
            out = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True,
                                 text=True, timeout=120).stdout
            ok = "nvidia" in out
        except (OSError, subprocess.SubprocessError):
            pass
        _GPU_CACHE["docker"] = ok
    return _GPU_CACHE["docker"]


def _run_ticking(cmd: list[str], timeout: float, tick) -> subprocess.CompletedProcess:
    """subprocess.run, calling tick(new_log_text) every few seconds with what
    the command logged since the last call (for progress reports)."""
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True)
        end, pos = time.time() + timeout, 0
        try:
            while True:
                try:
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    if time.time() > end:
                        raise
                    err.seek(pos)
                    new = err.read()
                    pos = err.tell()
                    err.seek(0, 2)
                    tick(new)
        finally:
            if proc.poll() is None:  # timed out, or tick() raised: never leave COLMAP running
                proc.kill()
                proc.wait()
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(cmd, proc.returncode, out.read(), err.read())


_COUNT = re.compile(r"\[(\d+)/(\d+)(?:, (\d+)/(\d+))?\]")
_REGISTERED = re.compile(r"num_reg_frames=(\d+)")


def ticker(report, label: str, total: int | None = None, extra=None):
    """A tick callback that reads COLMAP's progress lines ("Processed file
    [12/100]", "Processing block [2/3, 1/3]", "num_reg_frames=42") and calls
    report(fraction or None, "label 12/100"). extra() runs on every tick."""
    st = {"done": 0, "total": total}

    def tick(text: str) -> None:
        for m in _COUNT.finditer(text):
            if m.group(3):
                i, n_i, j, n_j = map(int, m.groups())
                st.update(done=(i - 1) * n_j + j, total=n_i * n_j)
            else:
                st.update(done=int(m.group(1)), total=int(m.group(2)))
        for m in _REGISTERED.finditer(text):
            st["done"] = int(m.group(1))
        if st["total"]:
            report(min(st["done"] / st["total"], 1.0), f"{label} {st['done']}/{st['total']}")
        else:
            report(None, label)
        if extra:
            extra()
    return tick


EXHAUSTIVE_MAX = 150  # "accurate" matches every pair up to this many frames
SEQ_OVERLAP = 20  # "fast": neighbours in capture order (+ quadratic links)
SFM_ACCURATE_SIZE = 2000  # px; 1600 px located cameras 5x worse on a real 12 MP scan


@dataclass
class Colmap:
    root: Path  # everything COLMAP touches lives under here
    log: callable = print
    use_gpu: bool = True
    max_image_size: int = 1600
    # PatchMatch cost grows with pixels x source images; on an entry-level 2 GB GPU the
    # full 1600 px / 12 sources ran ~2 h per job. 1000 px / 6 sources / 3
    # iterations keeps it in the 10-20 min range.
    dense_image_size: int = int(os.environ.get("SCANFIT_DENSE_IMAGE_SIZE", "1000"))
    dense_timeout: float = float(os.environ.get("SCANFIT_DENSE_TIMEOUT_S", "2400"))

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        override = os.environ.get("SCANFIT_COLMAP")
        if override:
            self.base = override.split()
        elif shutil.which("colmap"):
            self.base = ["colmap"]
        elif shutil.which("docker"):
            self.use_gpu = self.use_gpu and _docker_has_gpu()
            gpu = ["--gpus", "all"] if self.use_gpu else []
            uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0"
            # --init so a COLMAP abort() cannot leave the container hanging; COLMAP needs no network.
            self.base = ["docker", "run", "--rm", "--init", "--network", "none", *gpu, "--user", uid, "-e", "HOME=/tmp",
                         "-v", f"{self.root}:{self.root}", "-w", str(self.root), COLMAP_IMAGE, "colmap"]
        else:
            raise ColmapError("COLMAP not found: install it, or install Docker to use the colmap/colmap image")
        self.log(f"COLMAP via {self.base[0]}, gpu={'yes' if self.use_gpu else 'no'}")

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("SCANFIT_COLMAP") or shutil.which("colmap") or shutil.which("docker"))

    def step(self, title: str, sub: str, *args: str, check: bool = True,
             timeout: float = 3600, tick=None) -> subprocess.CompletedProcess:
        """Run one COLMAP subcommand with Orbiter-style profiling markers."""
        self.log(f"=== {title} ===")
        s = time.time()
        self.log(f"[PROFILE] start name={sub}")
        base = list(self.base)
        name = None
        if base[:2] == ["docker", "run"]:
            # Named so a timeout can stop the container, not just the docker client.
            name = f"scanfit-colmap-{os.getpid()}-{int(s * 1000)}"
            base[2:2] = ["--name", name]
        cmd = [*base, sub, "--log_target", "stderr", *map(str, args), *_seeds(sub)]
        try:
            if tick is None:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            else:
                p = _run_ticking(cmd, timeout, tick)
        except subprocess.TimeoutExpired as e:
            if name:
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=60)
            err = DenseUnavailable if sub == "patch_match_stereo" else ColmapError
            raise err(f"colmap {sub} timed out after {timeout:.0f}s") from e
        dur = time.time() - s
        self.log(f"[PROFILE] end name={sub} rc={p.returncode} dur={dur:.3f}")
        if check and p.returncode != 0:
            tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-15:])
            raise ColmapError(f"colmap {sub} failed (rc={p.returncode}):\n{tail}")
        return p


def _seeds(sub: str) -> list[str]:
    """Fixed random seeds, so the same frames give the same solve. COLMAP's
    two-view verification and mapper default to a fresh seed per run (-1); a
    one-sided scan re-solved from scratch once came out with different faces."""
    out = ["--default_random_seed", "0"]
    if sub in ("mapper", "image_registrator", "point_triangulator"):
        out += ["--Mapper.random_seed", "0"]
    elif sub == "global_mapper":
        out += ["--GlobalMapper.random_seed", "0"]
    elif sub.endswith("_matcher") or sub == "matches_importer":
        out += ["--TwoViewGeometry.random_seed", "0"]
    return out


# ---- priors ------------------------------------------------------------------


def write_priors(path: Path, cal: Calibration, names: list[str]) -> None:
    """Orbiter ``orbiter.sfm_priors.v1``: world->camera poses, millimetres."""
    images = []
    for n in names:
        R, t = cal.poses[n]
        q = rotmat_to_quat(R)
        images.append({"file": n, "qw": q[0], "qx": q[1], "qy": q[2], "qz": q[3],
                       "tx": t[0], "ty": t[1], "tz": t[2]})
    path.write_text(json.dumps({"schema": "orbiter.sfm_priors.v1",
                                "camera_intrinsics": cal.colmap_params(), "images": images}, indent=1))


def priors_to_model(priors: Path, out_dir: Path, database: Path, log=print) -> None:
    p = subprocess.run([sys.executable, str(PRIORS_SCRIPT), str(priors), str(out_dir), "--database", str(database)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ColmapError(f"sfm_priors_to_colmap failed: {p.stderr.strip()}")
    log(p.stdout.strip().splitlines()[-2] if p.stdout.strip() else "priors converted")


def extractor_flags(priors: Path) -> list[str]:
    p = subprocess.run([sys.executable, str(PRIORS_SCRIPT), str(priors), "--emit-extractor-flags"],
                       capture_output=True, text=True, check=True)
    return p.stdout.split()


def set_db_camera(database: Path, cal: Calibration) -> None:
    c = cal.colmap_params()
    params = struct.pack("<8d", c["fx"], c["fy"], c["cx"], c["cy"], c["k1"], c["k2"], c["p1"], c["p2"])
    with sqlite3.connect(database) as con:
        con.execute("UPDATE cameras SET params = ?", (params,))


def view_pairs(cal: Calibration, names: list[str], k: int = 14, max_angle: float = 70.0) -> list[tuple[str, str]]:
    """Pairs of frames that look at the part from nearby directions."""
    dirs = []
    for n in names:
        R, _ = cal.poses[n]
        dirs.append(R[2])  # optical axis in world
    dirs = np.array(dirs)
    cos = dirs @ dirs.T
    pairs = set()
    for i in range(len(names)):
        order = np.argsort(-cos[i])
        for j in order[1:k + 1]:
            if np.degrees(np.arccos(np.clip(cos[i, j], -1, 1))) <= max_angle:
                pairs.add((min(i, j), max(i, j)))
    return [(names[i], names[j]) for i, j in sorted(pairs)]


# ---- text model IO -----------------------------------------------------------


@dataclass
class SparseModel:
    image_ids: dict[str, int]
    poses: dict[str, tuple[np.ndarray, np.ndarray]]  # name -> (R_cw, t_cw)
    point_xyz: dict[int, np.ndarray]
    # observations: list of (image name, point3D id, x, y) in COLMAP pixel coords
    obs_name: list[str]
    obs_pid: np.ndarray
    obs_xy: np.ndarray
    point_rgb: dict = field(default_factory=dict)  # point id -> (r, g, b)


def read_text_model(d: Path) -> SparseModel:
    image_ids, poses, names, pids, xys = {}, {}, [], [], []
    lines = [ln for ln in (d / "images.txt").read_text().splitlines() if not ln.startswith("#")]
    for head, pts in zip(lines[0::2], lines[1::2]):
        f = head.split()
        iid, q, t, name = int(f[0]), [float(v) for v in f[1:5]], np.array([float(v) for v in f[5:8]]), f[9]
        image_ids[name] = iid
        poses[name] = (quat_to_rotmat(q), t)
        v = pts.split()
        for k in range(0, len(v), 3):
            pid = int(v[k + 2])
            if pid >= 0:
                names.append(name)
                pids.append(pid)
                xys.append((float(v[k]), float(v[k + 1])))
    pts, rgb = {}, {}
    for ln in (d / "points3D.txt").read_text().splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        f = ln.split()
        pts[int(f[0])] = np.array([float(f[1]), float(f[2]), float(f[3])])
        rgb[int(f[0])] = (int(f[4]), int(f[5]), int(f[6]))
    return SparseModel(image_ids, poses, pts, names, np.array(pids, np.int64), np.array(xys, np.float64), rgb)


# ---- the pipeline steps ------------------------------------------------------------


def _extract(cm: Colmap, ws: Path, image_dir: Path, names: list[str], priors: Path,
             max_image_size: int | None = None, tick=None) -> None:
    """SIFT features for names, with the lens fixed to the mat solve (one camera)."""
    db = ws / "database.db"
    (ws / "image_list.txt").write_text("\n".join(names) + "\n")
    common = ["--database_path", db, "--image_path", image_dir, "--image_list_path", ws / "image_list.txt",
              "--ImageReader.single_camera", "1", *extractor_flags(priors),
              "--FeatureExtraction.max_image_size", max_image_size or cm.max_image_size,
              "--SiftExtraction.max_num_features", "8192"]
    # CPU SIFT holds one image pyramid per thread: all cores at once can exceed the
    # container's memory limit on an 8-core PC. Same features, a little slower.
    cpu = ["--FeatureExtraction.use_gpu", 0, "--FeatureExtraction.num_threads", CPU_SIFT_THREADS]
    try:
        cm.step("1/5 feature_extractor (gpu)" if cm.use_gpu else "1/5 feature_extractor", "feature_extractor",
                *common, *(["--FeatureExtraction.use_gpu", 1] if cm.use_gpu else cpu), tick=tick)
    except ColmapError as e:
        if not cm.use_gpu:
            raise
        cm.log(f"GPU feature extraction failed, using CPU: {str(e).splitlines()[0]}")
        db.unlink(missing_ok=True)
        cm.step("1/5 feature_extractor (cpu)", "feature_extractor", *common, *cpu)


def sfm_all(cm: Colmap, ws: Path, image_dir: Path, cal: Calibration, names: list[str],
            on_progress=None, mode: str | None = None, on_snapshot=None) -> SparseModel:
    """Locate every frame by SIFT matching (structure from motion, lens fixed).

    The mat only locates frames that show most of its ring; close-ups that cut
    it off are often the most detailed views of the part. Matching them to each
    other recovers them (first real scan: 16 -> 100 of 100 frames). The result
    is in COLMAP's arbitrary frame; align it to the mat afterwards.

    mode "accurate" (default): every pair matched, incremental mapping, 2000 px
    features. "fast": capture-order matching + global mapping, ~5x quicker.
    Measured on a real scan vs CAD: worst dimension error 0.19 vs 0.29 mm.
    """
    mode = mode or os.environ.get("SCANFIT_SFM_MODE", "accurate")
    accurate = mode != "fast" and len(names) <= EXHAUSTIVE_MAX and cm.use_gpu  # all-pairs on CPU: hours
    ws.mkdir(parents=True, exist_ok=True)
    db, priors = ws / "database.db", ws / "sfm_priors.json"
    if db.exists():
        db.unlink()
    write_priors(priors, cal, [n for n in names if n in cal.poses])
    size = SFM_ACCURATE_SIZE if accurate else cm.max_image_size
    report = (lambda lo, hi: lambda f, text: on_progress(lo + (hi - lo) * (f or 0), text)) if on_progress \
        else (lambda lo, hi: lambda f, text: None)
    _extract(cm, ws, image_dir, names, priors, max_image_size=size,
             tick=ticker(report(0.0, 0.2), "Finding features in frame", len(names)))
    if accurate:
        sub, match = "exhaustive_matcher", ["--database_path", db, "--FeatureMatching.guided_matching", 1]
    else:  # neighbours in capture order, plus links 2, 4, 8, 16... apart
        sub, match = "sequential_matcher", ["--database_path", db, "--SequentialMatching.overlap", SEQ_OVERLAP,
                                            "--SequentialMatching.quadratic_overlap", 1,
                                            "--FeatureMatching.guided_matching", 1]
    try:
        cm.step(f"2/5 {sub}", sub, *match, "--FeatureMatching.use_gpu", int(cm.use_gpu),
                tick=ticker(report(0.2, 0.5), "Matching frame pairs, block"))
    except ColmapError as e:
        if not cm.use_gpu:
            raise
        cm.log(f"GPU matching failed, using CPU: {str(e).splitlines()[0]}")
        cm.step(f"2/5 {sub} (cpu)", sub, *match, "--FeatureMatching.use_gpu", 0)
    out = ws / "sfm"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    if accurate:
        snaps = ws / "snapshots"
        snaps.mkdir(exist_ok=True)
        last = [0.0]

        def snapshot():  # live preview of the points found so far, every ~20 s
            if on_snapshot and time.time() - last[0] > 20:
                last[0] = time.time()
                on_snapshot(snaps)
        cm.step("3/5 mapper", "mapper", "--database_path", db, "--image_path", image_dir, "--output_path", out,
                "--Mapper.ba_refine_focal_length", 0, "--Mapper.ba_refine_principal_point", 0,
                "--Mapper.ba_refine_extra_params", 0, "--Mapper.multiple_models", 0,
                "--Mapper.snapshot_path", snaps, "--Mapper.snapshot_frames_freq", 10, timeout=7200,
                tick=ticker(report(0.5, 1.0), "Locating frame", len(names), extra=snapshot))
    else:
        cm.step("3/5 global_mapper", "global_mapper", "--database_path", db, "--image_path", image_dir,
                "--output_path", out, "--GlobalMapper.ba_refine_focal_length", 0,
                "--GlobalMapper.ba_refine_principal_point", 0, "--GlobalMapper.ba_refine_extra_params", 0)
    models = [d for d in out.iterdir() if (d / "images.bin").exists() or (d / "images.txt").exists()]
    if (out / "images.bin").exists():
        models.append(out)
    if not models:
        raise ColmapError("structure from motion found no model")
    best, best_n = None, -1
    for d in models:
        txt = ws / f"sfm_txt_{d.name}"
        txt.mkdir(exist_ok=True)
        cm.step("model_converter -> TXT", "model_converter", "--input_path", d, "--output_path", txt,
                "--output_type", "TXT")
        m = read_text_model(txt)
        if len(m.poses) > best_n:
            best, best_n = m, len(m.poses)
    cm.log(f"structure from motion located {best_n} of {len(names)} frames")
    return best


SEQ_NEIGHBOURS = 8  # capture-order neighbours matched to each frame the mat did not locate
MAT_NEIGHBOURS = 12  # mat-located frames (nearest in capture order) matched to each of them


def sfm_register(cm: Colmap, ws: Path, image_dir: Path, cal: Calibration, names: list[str],
                 on_progress=None, on_model=None) -> SparseModel:
    """Locate every frame fast, in mat coordinates: triangulate points from the
    frames the mat located (their poses are known), then register the others
    (close-ups that cut the ring) into that model, twice so chains of close-ups
    get in too. Only useful pairs are matched: neighbouring viewpoints among the
    mat frames, capture-order neighbours and nearby mat frames for the rest.
    Replaces mapping everything from scratch (scan 2: 33 min of matching and
    mapping for 132 frames, 105 of them already located by the mat)."""
    ws.mkdir(parents=True, exist_ok=True)
    db, priors = ws / "database.db", ws / "sfm_priors.json"
    if db.exists():
        db.unlink()
    on_mat = [n for n in names if n in cal.poses]
    rest = [n for n in names if n not in cal.poses]
    write_priors(priors, cal, on_mat)
    report = (lambda lo, hi: lambda f, text: on_progress(lo + (hi - lo) * (f or 0), text)) if on_progress \
        else (lambda lo, hi: lambda f, text: None)
    _extract(cm, ws, image_dir, names, priors, tick=ticker(report(0.0, 0.4), "Finding features in frame", len(names)))
    order = {n: i for i, n in enumerate(names)}
    pairs = set(view_pairs(cal, on_mat))
    mat_order = np.array([order[n] for n in on_mat])
    for n in rest:
        i = order[n]
        near = [names[j] for j in range(max(0, i - SEQ_NEIGHBOURS), min(len(names), i + SEQ_NEIGHBOURS + 1)) if j != i]
        near += [on_mat[k] for k in np.argsort(np.abs(mat_order - i))[:MAT_NEIGHBOURS]]
        pairs |= {tuple(sorted((n, m))) for m in near}
    (ws / "pairs.txt").write_text("".join(f"{a} {b}\n" for a, b in sorted(pairs)))
    match = ["--database_path", db, "--match_list_path", ws / "pairs.txt", "--match_type", "pairs"]
    try:
        cm.step(f"2/5 matches_importer ({len(pairs)} pairs)", "matches_importer", *match,
                "--FeatureMatching.use_gpu", int(cm.use_gpu), tick=ticker(report(0.4, 0.7), "Matching frame pairs, block"))
    except ColmapError as e:
        if not cm.use_gpu:
            raise
        cm.log(f"GPU matching failed, using CPU: {str(e).splitlines()[0]}")
        cm.step("2/5 matches_importer (cpu)", "matches_importer", *match, "--FeatureMatching.use_gpu", 0)
    cm.log("=== 3/5 sfm_priors_to_colmap (Orbiter) ===")
    priors_to_model(priors, ws / "sparse_priors", db, cm.log)
    report(0.7, 1.0)(0.0, "Placing points from the mat-located frames")
    model = triangulate(cm, ws, image_dir, ws / "sparse_priors", "reg0")
    if on_model:
        on_model(model)  # live preview: the points so far
    current = ws / "reg0"
    for r in (1, 2):
        if len(model.poses) >= len(names):
            break
        report(0.7, 1.0)(r / 3, f"Adding close-ups ({len(model.poses)}/{len(names)} frames located)")
        out = ws / f"reg{r}"
        out.mkdir(exist_ok=True)
        cm.step(f"3/5 image_registrator ({r})", "image_registrator", "--database_path", db, "--input_path", current,
                "--output_path", out, "--Mapper.ba_refine_focal_length", 0, "--Mapper.ba_refine_principal_point", 0,
                "--Mapper.ba_refine_extra_params", 0)
        model = triangulate(cm, ws, image_dir, out, f"reg{r}t")
        if on_model:
            on_model(model)
        current = ws / f"reg{r}t"
    cm.log(f"located {len(model.poses)} of {len(names)} frames ({len(on_mat)} by the mat)")
    return model


def sparse(cm: Colmap, ws: Path, image_dir: Path, cal: Calibration, names: list[str]) -> SparseModel:
    """Features, matching and triangulation from the mat priors."""
    ws.mkdir(parents=True, exist_ok=True)
    db, priors = ws / "database.db", ws / "sfm_priors.json"
    if db.exists():
        db.unlink()
    write_priors(priors, cal, names)
    _extract(cm, ws, image_dir, names, priors)

    pairs = view_pairs(cal, names)
    (ws / "pairs.txt").write_text("".join(f"{a} {b}\n" for a, b in pairs))
    match = ["--database_path", db, "--match_list_path", ws / "pairs.txt", "--match_type", "pairs"]
    try:
        cm.step(f"2/5 matches_importer ({len(pairs)} pairs)", "matches_importer", *match,
                "--FeatureMatching.use_gpu", int(cm.use_gpu))
    except ColmapError as e:
        if not cm.use_gpu:
            raise
        cm.log(f"GPU matching failed, using CPU: {str(e).splitlines()[0]}")
        cm.step("2/5 matches_importer (cpu)", "matches_importer", *match, "--FeatureMatching.use_gpu", 0)

    cm.log("=== 3/5 sfm_priors_to_colmap (Orbiter) ===")
    priors_to_model(priors, ws / "sparse_priors", db, cm.log)
    return triangulate(cm, ws, image_dir, ws / "sparse_priors", "sparse0")


def triangulate(cm: Colmap, ws: Path, image_dir: Path, model_in: Path, tag: str) -> SparseModel:
    out, txt = ws / tag, ws / f"{tag}_txt"
    out.mkdir(exist_ok=True)
    txt.mkdir(exist_ok=True)
    cm.step(f"4/5 point_triangulator ({tag})", "point_triangulator", "--database_path", ws / "database.db",
            "--image_path", image_dir, "--input_path", model_in, "--output_path", out,
            "--refine_intrinsics", "0")
    cm.step("model_converter -> TXT", "model_converter", "--input_path", out, "--output_path", txt,
            "--output_type", "TXT")
    return read_text_model(txt)


def retriangulate(cm: Colmap, ws: Path, image_dir: Path, cal: Calibration, names: list[str]) -> SparseModel:
    """Second pass with the refined lens model and poses from the marker BA."""
    priors = ws / "sfm_priors_ba.json"
    write_priors(priors, cal, names)
    set_db_camera(ws / "database.db", cal)
    priors_to_model(priors, ws / "sparse_priors_ba", ws / "database.db", cm.log)
    return triangulate(cm, ws, image_dir, ws / "sparse_priors_ba", "sparse1")


def dense(cm: Colmap, ws: Path, image_dir: Path, on_progress=None) -> Path:
    """CUDA PatchMatch stereo + fusion. Raises DenseUnavailable on failure.

    on_progress(fraction) is called while PatchMatch runs (photometric then
    geometric depth maps, counted on disk)."""
    dd = ws / "dense"
    if dd.exists():
        shutil.rmtree(dd)
    cm.step("5/5 image_undistorter", "image_undistorter", "--image_path", image_dir, "--input_path", ws / "sparse1",
            "--output_path", dd, "--output_type", "COLMAP", "--max_image_size", cm.dense_image_size,
            "--num_patch_match_src_images", "6")
    p = cm.step("5/5 patch_match_stereo", "patch_match_stereo", "--workspace_path", dd,
                "--workspace_format", "COLMAP", "--PatchMatchStereo.max_image_size", cm.dense_image_size,
                "--PatchMatchStereo.geom_consistency", "true", "--PatchMatchStereo.gpu_index", "0",
                "--PatchMatchStereo.num_iterations", "3", "--PatchMatchStereo.cache_size", "4",
                check=False, timeout=cm.dense_timeout, tick=lambda _t: _report_depth_maps(dd, on_progress))
    if p.returncode != 0:
        raise DenseUnavailable("\n".join((p.stderr or p.stdout).strip().splitlines()[-5:]))
    fused = dd / "fused.ply"
    if on_progress:
        on_progress(1.0, "Merging depth maps into points")
    cm.step("5/5 stereo_fusion", "stereo_fusion", "--workspace_path", dd, "--workspace_format", "COLMAP",
            "--input_type", "geometric", "--output_path", fused, "--StereoFusion.max_image_size", cm.dense_image_size,
            "--StereoFusion.min_num_pixels", "3", "--StereoFusion.cache_size", "4",
            tick=ticker(lambda f, text: on_progress(1.0, text) if on_progress else None, "Merging depth maps, view"))
    return fused


def _report_depth_maps(dd: Path, on_progress) -> None:
    if on_progress is None:
        return
    n = max(len(list((dd / "images").glob("*"))), 1)
    maps = dd / "stereo" / "depth_maps"
    done = len(list(maps.glob("*.photometric.bin"))) + len(list(maps.glob("*.geometric.bin")))
    on_progress(min(done / (2 * n), 1.0), f"Computing depth maps {done}/{2 * n}", dd)

# ---- dense on full-resolution crops of the part -------------------------------------


DENSE_CROP_MAX = int(os.environ.get("SCANFIT_DENSE_CROP_MAX", "1000"))  # px, longest crop side fed to PatchMatch


CROP_BELOW_PX = 400  # crop only when the part would be smaller than this in whole-frame dense images


def part_region_px(cal: Calibration, views: list[str], box: tuple[np.ndarray, np.ndarray]) -> float:
    """Median longest side (full-resolution px) of the part's box across views."""
    lo, hi = box
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    sizes = []
    for n in views:
        R, t = cal.poses[n]
        if ((corners @ R.T + t)[:, 2] <= 10).any():
            continue
        uv = cv2.projectPoints(corners, cv2.Rodrigues(R)[0], t, cal.K, np.r_[cal.dist[:4], 0.0])[0].reshape(-1, 2)
        lo_uv, hi_uv = np.maximum(uv.min(0), 0), np.minimum(uv.max(0), [cal.width, cal.height])
        sizes.append(float(np.max(hi_uv - lo_uv)))
    return float(np.median(sizes)) if sizes else 0.0


def dense_crop(cm: Colmap, ws: Path, image_dir: Path, cal: Calibration, views: list[str],
               box: tuple[np.ndarray, np.ndarray], model: SparseModel, on_progress=None, log=print,
               full_frame: bool = False) -> Path:
    """CUDA PatchMatch on full-resolution crops around the part.

    Shrinking whole 12 MP frames to ~1000 px for the 2 GB GPU left a 40 mm part
    ~75 px wide when filmed from 40 cm. Cropping to the part's box instead keeps
    the camera's full detail at the same GPU cost.

    Each crop is an ordinary image whose camera is the phone lens with a shifted
    (and scaled) principal point; the SfM points and their observations are
    carried over into crop pixels. COLMAP's own undistorter then builds the
    dense workspace: it picks source views and per-view depth ranges from those
    points (a hand-written workspace without them left PatchMatch on noise).
    Raises DenseUnavailable.
    """
    root = ws / "dense_crop"
    if root.exists():
        shutil.rmtree(root)
    crops, st, dd = root / "images", root / "sparse_txt", root / "dense"
    for d in (crops, st, root / "sparse"):
        d.mkdir(parents=True)
    lo, hi = box
    corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    K, dist = cal.K, np.r_[cal.dist[:4], 0.0]
    rows = []  # name, x0, y0, scale, w, h
    for n in views:
        R, t = cal.poses[n]
        if full_frame:
            x0, y0, x1, y1 = 0, 0, cal.width, cal.height
        else:
            if ((corners @ R.T + t)[:, 2] <= 10).any():
                continue
            uv = cv2.projectPoints(corners, cv2.Rodrigues(R)[0], t, K, dist)[0].reshape(-1, 2)
            pad = 0.08 * np.ptp(uv, axis=0).max()
            x0, y0 = np.maximum(np.floor(uv.min(0) - pad).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(uv.max(0) + pad).astype(int), [cal.width, cal.height])
            if x1 - x0 < 64 or y1 - y0 < 64:
                continue
        img = cv2.imread(str(image_dir / n), cv2.IMREAD_COLOR)
        if img is None:
            continue
        crop = img[y0:y1, x0:x1]
        sc = min(1.0, DENSE_CROP_MAX / max(crop.shape[:2]))
        if sc < 1.0:
            crop = cv2.resize(crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(crops / n), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
        rows.append((n, int(x0), int(y0), sc, crop.shape[1], crop.shape[0]))
    if len(rows) < 3:
        raise DenseUnavailable(f"only {len(rows)} views see the part's box")
    full_px = np.median([max(r[4], r[5]) / r[3] for r in rows])
    log(f"dense: {len(rows)} crops around the part, median {int(np.median([r[4] for r in rows]))} px wide; "
        f"part region {int(full_px)} px at full resolution (whole-frame resizing gave "
        f"{int(full_px * cm.dense_image_size / max(cal.width, cal.height))} px)")

    # Observations of the SfM points, moved into crop pixels (COLMAP convention: x+0.5).
    idx = {r[0]: i for i, r in enumerate(rows)}
    obs: list[list[tuple[float, float, int]]] = [[] for _ in rows]
    tracks: dict[int, list[tuple[int, int]]] = {}
    for nm, pid, (x, y) in zip(model.obs_name, model.obs_pid, model.obs_xy):
        i = idx.get(nm)
        if i is None or int(pid) not in model.point_xyz:
            continue
        _, x0, y0, sc, w, h = rows[i]
        u, v = (x - x0) * sc, (y - y0) * sc
        if 0 <= u < w and 0 <= v < h:
            tracks.setdefault(int(pid), []).append((i + 1, len(obs[i])))
            obs[i].append((u, v, int(pid)))
    tracks = {p: t for p, t in tracks.items() if len(t) >= 2}
    fx, fy, cx, cy = cal.K[0, 0], cal.K[1, 1], cal.K[0, 2] + 0.5, cal.K[1, 2] + 0.5
    k1, k2, p1, p2 = cal.dist[:4]
    with open(st / "cameras.txt", "w") as fc, open(st / "images.txt", "w") as fi, \
            open(st / "rigs.txt", "w") as fr, open(st / "frames.txt", "w") as ff:
        for i, (n, x0, y0, sc, w, h) in enumerate(rows, start=1):
            R, t = cal.poses[n]
            q = rotmat_to_quat(R)
            pose = " ".join(f"{v:.12g}" for v in (*q, *t))
            fc.write(f"{i} OPENCV {w} {h} {fx * sc:.10g} {fy * sc:.10g} {(cx - x0) * sc:.10g} {(cy - y0) * sc:.10g} "
                     f"{k1:.10g} {k2:.10g} {p1:.10g} {p2:.10g}\n")
            pts = " ".join(f"{u:.3f} {v:.3f} {pid if pid in tracks else -1}" for u, v, pid in obs[i - 1])
            fi.write(f"{i} {pose} {i} {n}\n{pts}\n")
            fr.write(f"{i} 1 CAMERA {i}\n")
            ff.write(f"{i} {i} {pose} 1 CAMERA {i} {i}\n")
    with open(st / "points3D.txt", "w") as fp:
        for pid, tr in tracks.items():
            X = model.point_xyz[pid]
            fp.write(f"{pid} {X[0]:.6f} {X[1]:.6f} {X[2]:.6f} 128 128 128 1 " + " ".join(f"{a} {b}" for a, b in tr) + "\n")
    log(f"dense: {len(tracks)} SfM points carried into the crops")
    cm.step("5/5 model_converter (crops)", "model_converter", "--input_path", st, "--output_path", root / "sparse",
            "--output_type", "BIN")
    cm.step("5/5 image_undistorter (crops)", "image_undistorter", "--image_path", crops, "--input_path", root / "sparse",
            "--output_path", dd, "--output_type", "COLMAP", "--max_image_size", DENSE_CROP_MAX,
            "--num_patch_match_src_images", "6")
    p = cm.step("5/5 patch_match_stereo (crops)", "patch_match_stereo", "--workspace_path", dd,
                "--workspace_format", "COLMAP", "--PatchMatchStereo.max_image_size", DENSE_CROP_MAX,
                "--PatchMatchStereo.geom_consistency", "true", "--PatchMatchStereo.gpu_index", "0",
                "--PatchMatchStereo.num_iterations", "3", "--PatchMatchStereo.cache_size", "4",
                check=False, timeout=cm.dense_timeout, tick=lambda _t: _report_depth_maps(dd, on_progress))
    if p.returncode != 0:
        raise DenseUnavailable("\n".join((p.stderr or p.stdout).strip().splitlines()[-5:]))
    fused = dd / "fused.ply"
    if on_progress:
        on_progress(1.0, "Merging depth maps into points")
    cm.step("5/5 stereo_fusion (crops)", "stereo_fusion", "--workspace_path", dd, "--workspace_format", "COLMAP",
            "--input_type", "geometric", "--output_path", fused, "--StereoFusion.max_image_size", DENSE_CROP_MAX,
            "--StereoFusion.min_num_pixels", "3", "--StereoFusion.cache_size", "4",
            tick=ticker(lambda f, text: on_progress(1.0, text) if on_progress else None, "Merging depth maps, view"))
    return fused
