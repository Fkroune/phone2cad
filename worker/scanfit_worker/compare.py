"""Score a scan against the part's CAD model.

    python -m scanfit_worker.compare SCAN.ply MODEL.stl [--save aligned.ply]

SCAN is a job's result/mesh.ply (or any point cloud of the part alone, in mm).
The scan is aligned to the model by shape alone (feature matching, then ICP;
rotation and translation only, so size errors stay visible) and three numbers
come out: scale error, distance of the scan to the true surface, and how much
of the true surface the scan covers.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import open3d as o3d

reg = o3d.pipelines.registration


@dataclass
class Score:
    points: int
    fitness: float
    scale_error_pct: float
    dist_median: float
    dist_p90: float
    within: dict[float, float]  # tolerance mm -> share of scan points
    coverage: dict[float, float]  # tolerance mm -> share of model surface
    transform: np.ndarray  # 4x4, scan -> model


def _load_points(path: Path, n: int = 60000) -> o3d.geometry.PointCloud:
    mesh = o3d.io.read_triangle_mesh(str(path))
    if len(mesh.triangles):
        return mesh.sample_points_uniformly(n)
    return o3d.io.read_point_cloud(str(path))


def _features(pc: o3d.geometry.PointCloud, voxel: float):
    d = pc.voxel_down_sample(voxel)
    d.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.5 * voxel, max_nn=30))
    f = reg.compute_fpfh_feature(d, o3d.geometry.KDTreeSearchParamHybrid(radius=5 * voxel, max_nn=100))
    return d, f


def score(scan_path: Path, model_path: Path, voxel: float = 1.0, tries: int = 8) -> tuple[Score, o3d.geometry.PointCloud]:
    model = o3d.io.read_triangle_mesh(str(model_path))
    model.compute_vertex_normals()
    ref = model.sample_points_poisson_disk(20000)
    scan = _load_points(scan_path)
    sd, sf = _features(scan, voxel)
    rd, rf = _features(ref, voxel)
    best = None
    for seed in range(tries):  # global alignment is randomised; keep the best of a few
        o3d.utility.random.seed(seed)
        g = reg.registration_ransac_based_on_feature_matching(
            sd, rd, sf, rf, True, 1.5 * voxel, reg.TransformationEstimationPointToPoint(False), 3,
            [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9), reg.CorrespondenceCheckerBasedOnDistance(1.5 * voxel)],
            reg.RANSACConvergenceCriteria(200000, 0.999))
        icp = reg.registration_icp(scan, ref, voxel, g.transformation, reg.TransformationEstimationPointToPlane())
        if best is None or icp.fitness > best.fitness:
            best = icp
    scaled = reg.registration_icp(scan, ref, 0.8 * voxel, best.transformation,
                                  reg.TransformationEstimationPointToPoint(True),
                                  reg.ICPConvergenceCriteria(max_iteration=200))
    s = float(np.cbrt(np.linalg.det(scaled.transformation[:3, :3])))
    aligned = copy.deepcopy(scan).transform(best.transformation)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(model))
    A = np.asarray(aligned.points)
    d = scene.compute_distance(o3d.core.Tensor(A.astype(np.float32))).numpy()
    back = np.asarray(ref.compute_point_cloud_distance(aligned))
    tol = (0.25, 0.5, 1.0)
    return Score(len(A), float(best.fitness), 100 * (1 / s - 1), float(np.median(d)), float(np.percentile(d, 90)),
                 {t: float((d < t).mean()) for t in tol}, {t: float((back < t).mean()) for t in tol},
                 best.transformation.copy()), aligned


def feature_errors(features: list[dict], model_path: Path, transform: np.ndarray) -> list[dict]:
    """Pair each measured feature with the same measurement taken on the CAD.

    The model is placed where the scan put the part (inverse of transform),
    sampled, and run through the same plane/cylinder fitting. Pairs of the same
    kind are matched by smallest difference first. The base (z < 0.5 mm) is
    left out: no camera sees it.
    """
    from .features import describe, fit_all

    model = o3d.io.read_triangle_mesh(str(model_path))
    model.transform(np.linalg.inv(transform))
    model.compute_vertex_normals()
    pc = model.sample_points_poisson_disk(40000)
    P, N = np.asarray(pc.points), np.asarray(pc.normals)
    up = P[:, 2] > 0.5
    truth = describe(fit_all(P[up], N[up], log=lambda *a: None), P[up], 0.0)
    scan = [f for f in features if f["kind"] != "overall"]
    pairs = sorted((abs(f["value"] - t["value"]), i, j) for i, f in enumerate(scan)
                   for j, t in enumerate(truth) if t["kind"] == f["kind"])
    used_s, used_t, out = set(), set(), {}
    for d, i, j in pairs:
        if i in used_s or j in used_t:
            continue
        used_s.add(i)
        used_t.add(j)
        out[i] = truth[j]["value"]
    rows = []
    for i, f in enumerate(scan):
        cad = out.get(i)
        rows.append({"label": f["label"], "kind": f["kind"], "scan": f["value"], "pm": f["pm"], "cad": cad,
                     "error": None if cad is None else round(f["value"] - cad, 3),
                     "within_pm": None if cad is None or f["pm"] is None else bool(abs(f["value"] - cad) <= f["pm"])})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scan", type=Path)
    ap.add_argument("model", type=Path)
    ap.add_argument("--save", type=Path, help="write the aligned scan here (e.g. to view over the model)")
    a = ap.parse_args()
    sc, aligned = score(a.scan, a.model)
    pct = lambda m: " / ".join(f"{100 * v:.0f}%" for v in m.values())  # noqa: E731
    print(f"alignment fitness   {sc.fitness:.2f}  ({sc.points} scan points)")
    print(f"scale error         {sc.scale_error_pct:+.2f}%")
    print(f"scan to surface     median {sc.dist_median:.2f} mm, 90% within {sc.dist_p90:.2f} mm")
    print(f"scan within         {pct(sc.within)}  of 0.25 / 0.5 / 1 mm")
    print(f"surface covered     {pct(sc.coverage)}  within 0.25 / 0.5 / 1 mm")
    if a.save:
        o3d.io.write_point_cloud(str(a.save), aligned)


if __name__ == "__main__":
    main()
