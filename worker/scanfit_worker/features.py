"""Feature fitting: planes and cylinders with uncertainties.

Works on the part's point cloud in world millimetres (mat surface at z=0,
z up). RANSAC finds candidates, least squares refines them, and every
reported value carries a +/- (about two standard deviations) built from fit
residuals, point count and the scale uncertainty of the marker solve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
import open3d as o3d
from scipy.optimize import least_squares
from scipy.spatial import cKDTree

PLANE_TOL = 0.30  # mm
CYL_TOL = 0.30  # mm
MIN_COVERAGE = 0.45  # fraction of the circle that must be seen; less fits box edges too
STAND_MM = 5.0  # a cluster whose top 5 % reaches this height is an object, not paper
MERGE_MM = 6.0  # standing fragments this close to the main cluster belong to it
EDGE_MM = 1.2  # face points closer than this to another surface are edge, not face
FOOTPRINT_MM = 2.0  # low points farther than this (in XY) from the part's upper points are paper
LEVEL_MAX_NOISE = 0.35  # mm; real scans 0.28-0.30, synthetic CPU path with 48 views 0.40
RADIAL_NORMALS = 0.96  # median |normal . radial|: real holes 0.99-1.00; square bars with rounded edges 0.91-0.94
CORRELATION = 10.0  # dense points are not independent; effective n = n / CORRELATION
# How sure a measurement is, by its +/- (mm): words for the phone, no instructions.
GRADES = ((0.3, "precise"), (0.6, "good"), (1.0, "approx"))  # above the last: "rough"


@dataclass
class Plane:
    normal: np.ndarray  # unit, pointing out of the material
    offset: float  # normal . x + offset = 0
    centroid: np.ndarray
    idx: np.ndarray
    rms: float
    pts: np.ndarray | None = None  # the face's extent when it comes from another cloud (e.g. the hull)
    extra: float = 0.0  # added 1-sigma (mm), e.g. a face known only from the visual hull
    source: str = "dense"

    @property
    def sigma(self) -> float:
        return float(np.hypot(self.rms / np.sqrt(max(len(self.idx) / CORRELATION, 1.0)), self.extra))


@dataclass
class Cylinder:
    axis: np.ndarray
    point: np.ndarray  # on the axis, at the middle of the observed extent
    radius: float
    idx: np.ndarray
    rms: float
    sigma_r: float
    kind: str  # "hole" or "boss"
    coverage: float  # fraction of the circumference seen
    length: float


@dataclass
class FitResult:
    planes: list[Plane] = field(default_factory=list)
    cylinders: list[Cylinder] = field(default_factory=list)


def _pcd(P: np.ndarray, N: np.ndarray | None = None) -> o3d.geometry.PointCloud:
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    if N is not None:
        pc.normals = o3d.utility.Vector3dVector(N)
    return pc


def level_to_mat(P: np.ndarray, zone: tuple[float, float, float, float], log=print
                 ) -> tuple[np.ndarray, np.ndarray, float]:
    """Fit the paper's surface in the part zone and return (R, t, noise) that put
    it at z=0. The marker solve fixes the mat to within ~1 mm, but a real sheet
    is not perfectly flat, and heights must be measured from the paper the part
    stands on. noise is the robust spread of the paper's points (mm)."""
    x0, x1, y0, y1 = zone
    m = (P[:, 0] > x0) & (P[:, 0] < x1) & (P[:, 1] > y0) & (P[:, 1] < y1) & (np.abs(P[:, 2]) < 4.0)
    if m.sum() < 500:
        return np.eye(3), np.zeros(3), 0.3
    Q = P[m]
    _, inl = _ransac_plane(Q, 0.4, np.random.default_rng(0))
    n, _, c, _ = _fit_plane_lsq(Q[inl])
    if n[2] < 0:
        n = -n
    tilt = float(np.degrees(np.arccos(np.clip(n[2], -1, 1))))
    if tilt > 5.0:
        log(f"mat surface tilted {tilt:.1f} deg from the marker solve; not levelling")
        return np.eye(3), np.zeros(3), 0.3
    # Rotation taking n to +z (Rodrigues), then shift the plane to z=0.
    v = np.cross(n, [0, 0, 1.0])
    sv = np.linalg.norm(v)
    R = np.eye(3) if sv < 1e-9 else cv2.Rodrigues(v / sv * np.arcsin(sv))[0]
    t = -np.array([0, 0, (R @ c)[2]])
    h = (Q[inl] - c) @ n
    noise = float(1.4826 * np.median(np.abs(h - np.median(h))))
    if noise > LEVEL_MAX_NOISE:
        # Blank paper reconstructs badly (CPU path, few views): its fitted
        # height is then less reliable than the marker solve's z=0.
        log(f"mat surface too noisy to level on ({noise:.2f} mm); keeping the marker frame")
        return np.eye(3), np.zeros(3), noise
    log(f"mat surface: tilt {tilt:.2f} deg, offset {-t[2]:+.2f} mm, noise {noise:.2f} mm")
    return R, t, noise


def isolate_part(P: np.ndarray, zone: tuple[float, float, float, float], z_min: float = 0.8) -> np.ndarray:
    """Indices of the part: above the mat, inside the zone; the largest cluster
    that clearly stands up, plus standing fragments touching it. (Largest
    alone picked a flat patch of curled paper; tallest alone picked a 300-point
    arm tip over the 15 000-point body.)"""
    x0, x1, y0, y1 = zone
    sel = np.nonzero((P[:, 2] > z_min) & (P[:, 0] > x0) & (P[:, 0] < x1) & (P[:, 1] > y0) & (P[:, 1] < y1))[0]
    if len(sel) < 100:
        return sel
    pc = _pcd(P[sel])
    _, keep = pc.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.5)
    sel = sel[np.asarray(keep)]
    labels = np.asarray(_pcd(P[sel]).cluster_dbscan(eps=1.5, min_points=8))
    if labels.max() < 0:
        return sel
    clusters = [sel[labels == k] for k in range(labels.max() + 1)]
    # Standing clusters: noisy or curled paper stays within a few mm of the mat.
    standing = [c for c in clusters if len(c) >= 200 and np.percentile(P[c, 2], 95) >= STAND_MM]
    if not standing:
        return max(clusters, key=len)
    main = max(standing, key=len)
    # Thin parts can break into pieces (an arm top separated from the body):
    # take in standing clusters that touch the main one's box.
    lo, hi = P[main].min(0) - MERGE_MM, P[main].max(0) + MERGE_MM
    parts = [main] + [c for c in standing if c is not main
                      and (np.all(P[c].min(0) <= hi) and np.all(P[c].max(0) >= lo))]
    idx = np.concatenate(parts)
    # Paper around the foot can join the cluster: keep low points only under
    # the part's own footprint (seen from above).
    low = P[idx, 2] < STAND_MM
    if low.any() and (~low).any():
        d, _ = cKDTree(P[idx[~low], :2]).query(P[idx[low], :2])
        keep = np.ones(len(idx), bool)
        keep[np.nonzero(low)[0][d > FOOTPRINT_MM]] = False
        idx = idx[keep]
    return idx


def estimate_normals(P: np.ndarray, view_dirs: np.ndarray | None = None) -> np.ndarray:
    """Normals from local PCA, flipped toward the observing camera when known."""
    pc = _pcd(P)
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=1.2, max_nn=40))
    N = np.asarray(pc.normals).copy()
    if view_dirs is not None:
        flip = np.einsum("ij,ij->i", N, view_dirs) < 0
        N[flip] *= -1
    return N


def _fit_plane_lsq(X: np.ndarray) -> tuple[np.ndarray, float, np.ndarray, float]:
    c = X.mean(axis=0)
    _, s, vt = np.linalg.svd(X - c, full_matrices=False)
    n = vt[2]
    res = (X - c) @ n
    return n, -n @ c, c, float(np.sqrt(np.mean(res ** 2)))


def _ransac_plane(X: np.ndarray, tol: float, rng, iters: int = 2048, sample: int = 20000) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic RANSAC plane (Open3D's segment_plane is multi-threaded and
    gave 5 or 7 faces on the same cloud). Hypotheses are scored on a fixed
    subsample; returns the unit normal and the inlier indices of X."""
    S = X[rng.choice(len(X), min(sample, len(X)), replace=False)]
    best, best_n = None, -1
    for _ in range(iters // 256):
        tri = X[rng.integers(0, len(X), (256, 3))]
        nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(nrm, axis=1)
        ok = ln > 1e-9
        nrm, tri = nrm[ok] / ln[ok, None], tri[ok]
        off = -np.einsum("ij,ij->i", nrm, tri[:, 0])
        counts = (np.abs(S @ nrm.T + off) < tol).sum(0)
        k = int(np.argmax(counts))
        if counts[k] > best_n:
            best_n, best = counts[k], (nrm[k], off[k])
    n, off = best
    return n, np.nonzero(np.abs(X @ n + off) < tol)[0]


def _core(P: np.ndarray, face: np.ndarray, n: np.ndarray, off: float, edge_mm: float = EDGE_MM) -> np.ndarray:
    """The face's points away from its edges. Reconstructed edges are rounded,
    and on a narrow face the rounded strips pull a least-squares plane inward
    (9.6 mm arms measured 0.2-0.45 mm thin on the corner benchmark). An edge
    point has another surface (a point > 0.8 mm off this plane) within edge_mm."""
    off_plane = np.abs(P @ n + off) > 0.8
    if not off_plane.any():
        return P[face]
    d, _ = cKDTree(P[off_plane]).query(P[face], distance_upper_bound=edge_mm)
    core = P[face][np.isinf(d)]
    return core if len(core) >= max(100, len(face) // 5) else P[face]


PLANE_MIN_POINTS = 500  # a face needs this many points (absolute: a share of the whole part missed small faces)
PLANE_MAX_MISSES = 15


def fit_planes(P: np.ndarray, N: np.ndarray, max_planes: int = 20, log=print,
               tol: float = PLANE_TOL, min_points: int | None = None,
               max_misses: int | None = None) -> tuple[list[Plane], np.ndarray]:
    """Faces of the part, largest first. A RANSAC hypothesis becomes a face when
    its inliers that also face the same way form one connected patch of at
    least min_points. Patches that fail are left out of later hypotheses (but
    stay available to grow neighbouring faces), so the search moves on instead
    of retrying the same spot; on the corner benchmark this found 7 of 28 faces
    with a size share and 6 retries."""
    rng = np.random.default_rng(0)  # same cloud, same planes
    min_points = min_points or PLANE_MIN_POINTS
    max_misses = max_misses or PLANE_MAX_MISSES
    remaining = np.arange(len(P))  # not yet in a face
    pool = remaining.copy()  # where hypotheses are drawn from
    planes, misses = [], 0
    cos_n = np.cos(np.radians(25))
    while len(planes) < max_planes and len(pool) > min_points and misses < max_misses:
        n, inl = _ransac_plane(P[pool], tol, rng)
        cand = pool[inl]
        cand = cand[np.abs(N[cand] @ n) > cos_n]
        comp = cand
        if len(cand) >= min_points:
            labels = np.asarray(_pcd(P[cand]).cluster_dbscan(eps=1.2, min_points=6))
            comp = cand[labels == np.bincount(labels[labels >= 0]).argmax()] if labels.max() >= 0 else cand[:0]
        if len(comp) < min_points:
            misses += 1
            pool = np.setdiff1d(pool, cand if len(cand) else pool[inl])
            continue
        n, off, c, rms = _fit_plane_lsq(P[comp])
        grow_tol = max(2.5 * rms, 0.15)
        d = np.abs(P[remaining] @ n + off)
        grow = remaining[(d < grow_tol) & (np.abs(N[remaining] @ n) > cos_n)]
        grow = grow[np.linalg.norm(P[grow] - c, axis=1) < np.linalg.norm(P[comp] - c, axis=1).max() + 2.0]
        n, off, c, rms = _fit_plane_lsq(P[grow])
        n, off, c, rms = _fit_plane_lsq(_core(P, grow, n, off))
        if np.mean(N[grow] @ n) < 0:
            n, off = -n, -off
        planes.append(Plane(n, off, c, grow, rms))
        remaining = np.setdiff1d(remaining, grow)
        pool = np.setdiff1d(pool, grow)
        misses = 0
    log(f"planes: {len(planes)} found, {len(remaining)} points left")
    return planes, remaining


def _circle_from_3(p: np.ndarray) -> tuple[np.ndarray, float] | None:
    a, b, c = p
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-9:
        return None
    ux = ((a @ a) * (b[1] - c[1]) + (b @ b) * (c[1] - a[1]) + (c @ c) * (a[1] - b[1])) / d
    uy = ((a @ a) * (c[0] - b[0]) + (b @ b) * (a[0] - c[0]) + (c @ c) * (b[0] - a[0])) / d
    centre = np.array([ux, uy])
    return centre, float(np.linalg.norm(a - centre))


def _basis(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(a, t)
    u /= np.linalg.norm(u)
    return u, np.cross(a, u)


def _radial(P: np.ndarray, axis: np.ndarray, point: np.ndarray) -> np.ndarray:
    v = P - point
    return v - np.outer(v @ axis, axis)


def _ransac_circle(P: np.ndarray, N: np.ndarray, axis: np.ndarray, rng, iters: int = 1500, tol: float = CYL_TOL):
    perp = np.abs(N @ axis) < 0.35
    idx = np.nonzero(perp)[0]
    if len(idx) < 60:
        return None
    u, v = _basis(axis)
    Q = np.stack([P[idx] @ u, P[idx] @ v], 1)
    best, best_n = None, 0
    for _ in range(iters):
        s = rng.choice(len(idx), 3, replace=False)
        circ = _circle_from_3(Q[s])
        if circ is None or not (1.0 < circ[1] < 60.0):
            continue
        d = np.abs(np.linalg.norm(Q - circ[0], axis=1) - circ[1])
        n_in = int((d < tol).sum())
        if n_in > best_n:
            best_n, best = n_in, circ
    if best is None:
        return None
    centre = best[0][0] * u + best[0][1] * v
    return centre, best[1]


def _refine_cylinder(P: np.ndarray, axis: np.ndarray, point: np.ndarray, r: float, tol: float = CYL_TOL):
    u, v = _basis(axis)

    def unpack(x):
        a = axis + x[0] * u + x[1] * v
        a /= np.linalg.norm(a)
        return a, point + x[2] * u + x[3] * v, x[4]

    def res(x):
        a, c, rr = unpack(x)
        return np.linalg.norm(_radial(P, a, c), axis=1) - rr

    sol = least_squares(res, np.array([0, 0, 0, 0, r]), loss="soft_l1", f_scale=tol)
    a, c, rr = unpack(sol.x)
    J = sol.jac
    resid = res(sol.x)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    try:
        cov = np.linalg.inv(J.T @ J) * rms ** 2 * CORRELATION
        sigma_r = float(np.sqrt(max(cov[4, 4], 0)))
    except np.linalg.LinAlgError:
        sigma_r = rms
    return a, c, float(rr), rms, sigma_r


def _near_planes(X: np.ndarray, planes: list[Plane], tol: float) -> np.ndarray:
    d = np.min(np.abs(np.stack([X @ p.normal + p.offset for p in planes], 1)), axis=1)
    return d < tol


def fit_cylinders(P: np.ndarray, N: np.ndarray, candidates: np.ndarray, planes: list[Plane],
                  max_cyl: int = 6, log=print, seed: int = 0, tol: float = CYL_TOL) -> list[Cylinder]:
    """Plane normals are the preferred axis directions."""
    rng = np.random.default_rng(seed)
    remaining = candidates.copy()
    out: list[Cylinder] = []
    tried_axes = [p.normal for p in planes] + [np.array([0, 0, 1.0])]
    rejected = 0
    while len(out) < max_cyl and len(remaining) >= 150 and rejected < 6:
        best = None
        for ax in tried_axes:
            r = _ransac_circle(P[remaining], N[remaining], ax, rng, tol=tol)
            if r is None:
                continue
            centre, rad = r
            d = np.abs(np.linalg.norm(_radial(P[remaining], ax, centre), axis=1) - rad)
            inl = remaining[(d < tol) & (np.abs(N[remaining] @ ax) < 0.35)]
            if best is None or len(inl) > len(best[3]):
                best = (ax, centre, rad, inl)
        if best is None or len(best[3]) < 150:
            break
        ax, centre, rad, inl = best
        a, c, r, rms, sigma_r = _refine_cylinder(P[inl], ax, centre, rad, tol)
        d = np.abs(np.linalg.norm(_radial(P[remaining], a, c), axis=1) - r)
        inl = remaining[(d < max(2.5 * rms, 0.15)) & (np.abs(N[remaining] @ a) < 0.4)]
        a, c, r, rms, sigma_r = _refine_cylinder(P[inl], a, c, r, tol)
        radial = _radial(P[inl], a, c)
        rhat = radial / np.linalg.norm(radial, axis=1, keepdims=True)
        nr = np.einsum("ij,ij->i", N[inl], rhat)
        kind = "hole" if np.mean(nr) < 0 else "boss"
        # On a real cylinder the normals point along the radius; points on two
        # flat faces meeting at an edge can fit an arc but keep face normals.
        radial_normals = float(np.median(np.abs(nr)))
        u, v = _basis(a)
        ang = np.arctan2(radial @ v, radial @ u)
        coverage = len(np.unique(np.floor((ang + np.pi) / (2 * np.pi) * 36))) / 36
        along = (P[inl] - c) @ a
        lo, hi = np.percentile(along, [1, 99])
        remaining = np.setdiff1d(remaining, inl)
        # Rounded block edges fit an arc too, but their points hug the faces.
        on_planes = float(np.mean(_near_planes(P[inl], planes, 1.0))) if planes else 0.0
        coaxial = any(abs(o.axis @ a) > 0.99 and np.linalg.norm(_radial(c[None], o.axis, o.point)) < o.radius
                      for o in out)
        if (coverage < MIN_COVERAGE or rms > 0.5 or len(inl) < 150 or radial_normals < RADIAL_NORMALS
                or on_planes > 0.5 or coaxial):
            log(f"cylinder rejected: r={r:.2f} coverage={coverage:.2f} rms={rms:.2f} radial normals="
                f"{radial_normals:.2f} on planes={on_planes:.2f} coaxial={coaxial} n={len(inl)}")
            rejected += 1
            continue
        rejected = 0
        log(f"cylinder accepted: r={r:.2f} coverage={coverage:.2f} rms={rms:.2f} radial normals="
            f"{radial_normals:.2f} on planes={on_planes:.2f} n={len(inl)}")
        out.append(Cylinder(a, c + a * (lo + hi) / 2, r, inl, rms, sigma_r, kind, coverage, float(hi - lo)))
    log(f"cylinders: {len(out)} found")
    return out


def grade(pm: float, partial: bool = False) -> str:
    """precise / good / approx / rough. A partly seen circle is never "precise"."""
    g = next((name for lim, name in GRADES if pm <= lim), "rough")
    return "good" if partial and g == "precise" else g


def face_extent(p: Plane, P: np.ndarray) -> dict:
    """Where a face is, for drawing it: centre, two in-plane axes and half sizes
    (2nd-98th percentile of its points, so stray edge points do not stretch it)."""
    F = p.pts if p.pts is not None else P[p.idx]
    Q = F - F.mean(0)
    Q -= np.outer(Q @ p.normal, p.normal)
    _, V = np.linalg.eigh(Q.T @ Q)  # main axis of the face points inside the plane
    u = V[:, 2] - (V[:, 2] @ p.normal) * p.normal
    u /= np.linalg.norm(u)
    v = np.cross(p.normal, u)
    a, b = Q @ u, Q @ v
    lo, hi = np.percentile(np.c_[a, b], [2, 98], axis=0)
    c = F.mean(0) + u * (lo[0] + hi[0]) / 2 + v * (lo[1] + hi[1]) / 2
    c -= (c @ p.normal + p.offset) * p.normal  # onto the fitted plane
    return {"centre": c.round(3).tolist(), "u": u.round(5).tolist(), "v": v.round(5).tolist(),
            "half": (np.maximum((hi - lo) / 2, 0.5)).round(3).tolist()}


def _pm(sigma: float, value: float, scale_sigma: float, systematic: float = 0.0) -> float:
    return float(max(2 * np.sqrt(sigma ** 2 + (value * scale_sigma) ** 2 + systematic ** 2), 0.05))


def overall_size(P: np.ndarray) -> tuple[float, float, float, np.ndarray]:
    import cv2
    xy = P[:, :2].astype(np.float32)
    (cx, cy), (w, h), ang = cv2.minAreaRect(xy)
    th = np.radians(ang)
    R = np.array([[np.cos(th), np.sin(th)], [-np.sin(th), np.cos(th)]])
    loc = (P[:, :2] - [cx, cy]) @ R.T
    lo, hi = np.percentile(loc, [0.5, 99.5], axis=0)
    L, W = sorted(hi - lo, reverse=True)
    H = float(np.percentile(P[:, 2], 99.5))
    return float(L), float(W), H, np.array([cx, cy])


def _face_pts(p: Plane, P: np.ndarray) -> np.ndarray:
    return p.pts if p.pts is not None else P[p.idx]


def _facing(a: Plane, b: Plane, P: np.ndarray) -> float:
    """How much two opposite faces face each other: overlap of their outlines
    projected on a's plane, as a share of the smaller one (0..1). Faces of
    different arms are parallel too, but do not face each other."""
    u = np.cross(a.normal, [0, 0, 1.0] if abs(a.normal[2]) < 0.9 else [1.0, 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(a.normal, u)
    A, B = _face_pts(a, P), _face_pts(b, P)
    if len(A) < 3 or len(B) < 3:
        return 0.0
    pa, pb = np.c_[A @ u, A @ v], np.c_[B @ u, B @ v]
    lo, hi = np.maximum(np.percentile(pa, 2, 0), np.percentile(pb, 2, 0)), np.minimum(np.percentile(pa, 98, 0), np.percentile(pb, 98, 0))
    inter = np.prod(np.clip(hi - lo, 0, None))
    area = lambda q: np.prod(np.percentile(q, 98, 0) - np.percentile(q, 2, 0))  # noqa: E731
    return float(inter / max(min(area(pa), area(pb)), 1e-6))


def describe(fit: FitResult, P: np.ndarray, scale_sigma: float, systematic: float = 0.0) -> list[dict]:
    """systematic: 1-sigma surface offset of the dense method (mm), measured on
    the synthetic test block; fit residuals alone cannot see it."""
    feats: list[dict] = []
    for k, c in enumerate(sorted(fit.cylinders, key=lambda c: -len(c.idx))):
        d = 2 * c.radius
        pm = _pm(2 * c.sigma_r, d, scale_sigma, 2 * systematic)
        name = "Hole" if c.kind == "hole" else "Boss"
        partial = c.coverage < 0.6
        feats.append({
            "id": f"{c.kind}-{k + 1}", "kind": c.kind, "label": name, "value": round(d, 3), "pm": round(pm, 3),
            "text": f"{name} Ø{d:.1f} ±{pm:.1f} mm", "grade": grade(pm, partial),
            "detail": f"{c.length:.1f} mm long, {round(100 * c.coverage)} % of the circle seen",
            "geometry": {"axis": c.axis.round(5).tolist(), "point": c.point.round(3).tolist(),
                         "radius": round(c.radius, 4), "length": round(c.length, 3)},
        })
    # Faces parallel to the mat: height above it.
    for i, p in enumerate(fit.planes):
        if p.normal[2] > np.cos(np.radians(5)):
            h = float(p.centroid[2])  # the fitted plane, whatever cloud its points came from
            pm = _pm(p.sigma, h, scale_sigma, systematic)
            feats.append({
                "id": f"height-{i}", "kind": "height", "label": "Height", "value": round(h, 3), "pm": round(pm, 3),
                "text": f"Height {h:.1f} ±{pm:.1f} mm", "grade": grade(pm),
                "detail": "flat face above the mat", "geometry": {"plane": i},
            })
    # Opposite faces: distance across.
    used = set()
    for i, a in enumerate(fit.planes):
        best = None
        for j, b in enumerate(fit.planes):
            if j == i or a.normal @ b.normal > -np.cos(np.radians(6)) or _facing(a, b, P) < 0.3:
                continue
            gap = (b.centroid - a.centroid) @ a.normal
            dist = 0.5 * (abs((b.centroid - a.centroid) @ a.normal) + abs((a.centroid - b.centroid) @ b.normal))
            if best is None or dist < best[1]:
                best = (j, dist, gap)
        if best is None:
            continue
        j, dist, gap = best
        key = tuple(sorted((i, j)))
        if key in used:
            continue
        used.add(key)
        b = fit.planes[j]
        pm = _pm(np.hypot(a.sigma, b.sigma), dist, scale_sigma, np.sqrt(2) * systematic)
        label = "Across" if gap < 0 else "Gap"
        feats.append({
            "id": f"across-{key[0]}-{key[1]}", "kind": "distance", "label": label, "value": round(dist, 3),
            "pm": round(pm, 3), "text": f"{label} {dist:.1f} ±{pm:.1f} mm", "grade": grade(pm),
            "detail": "between two parallel faces", "geometry": {"planes": list(key)},
        })
    L, W, H, _ = overall_size(P)
    feats.append({
        "id": "overall", "kind": "overall", "label": "Overall", "value": round(L, 3), "pm": None,
        "text": f"Overall {L:.1f} × {W:.1f} × {H:.1f} mm", "grade": "info",
        "detail": "bounding box of the 3D copy (rough)", "geometry": {"size": [round(L, 2), round(W, 2), round(H, 2)]},
    })
    order = {"hole": 0, "boss": 1, "distance": 2, "height": 3, "overall": 4}
    feats.sort(key=lambda f: (order[f["kind"]], -f["value"]))
    return feats


HULL_ONLY_SIGMA = 0.3  # mm: a face seen only in silhouettes is an over-estimate of up to ~0.7 mm
MIN_SUPPORT = 40  # dense points needed to refine a hull face


def refine_with_points(faces: list[Plane], H: np.ndarray, D: np.ndarray, DN: np.ndarray,
                       log=print) -> list[Plane]:
    """Faces found on the visual hull (H: its surface points), refined with the
    dense points D. The hull gives each face whole, texture or not, but tilted
    or pushed out where views were missing; a few dozen dense points on the face
    correct that (corner benchmark: tilts up to 10 deg, offsets up to 0.7 mm).
    Faces without enough dense support keep the hull's plane and a wider +/-."""
    out = []
    n_dense = 0
    for p in faces:
        F = H[p.idx]
        u = np.cross(p.normal, [0, 0, 1.0] if abs(p.normal[2]) < 0.9 else [1.0, 0, 0])
        u /= np.linalg.norm(u)
        v = np.cross(p.normal, u)
        fu, fv = (F - p.centroid) @ u, (F - p.centroid) @ v
        du, dv = (D - p.centroid) @ u, (D - p.centroid) @ v
        sel = ((du > fu.min() - 1) & (du < fu.max() + 1) & (dv > fv.min() - 1) & (dv < fv.max() + 1)
               & (np.abs(D @ p.normal + p.offset) < 1.5) & (DN @ p.normal > np.cos(np.radians(30))))
        if sel.sum() >= MIN_SUPPORT:
            X = D[sel]
            for _ in range(3):  # trim stray points
                n, off, c, rms = _fit_plane_lsq(X)
                X = X[np.abs(X @ n + off) < max(2.5 * rms, 0.1)]
            n, off, c, rms = _fit_plane_lsq(X)
            if n @ p.normal < 0:
                n, off = -n, -off
            out.append(Plane(n, off, c, np.nonzero(sel)[0], rms, pts=F, source="dense"))
            n_dense += 1
        else:
            out.append(hull_face(p, F))
    log(f"faces: {len(out)} from the hull, {n_dense} refined with dense points")
    return out


def hull_face(p: Plane, F: np.ndarray) -> Plane:
    """A face measured on the hull alone, with the hull's wider +/-."""
    return Plane(p.normal, p.offset, p.centroid, p.idx, p.rms, pts=F, extra=HULL_ONLY_SIGMA, source="hull")


def fit_all(P: np.ndarray, N: np.ndarray, log=print) -> FitResult:
    planes, rest = fit_planes(P, N, log=log)
    cyl = fit_cylinders(P, N, rest, planes, log=log)
    return FitResult(planes, cyl)
