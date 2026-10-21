"""Marker-constrained bundle adjustment.

Refines the shared lens model, every camera pose and the COLMAP feature
points together. The mat's ChArUco corners enter as 3D points with fixed,
known world coordinates, which pins the metric scale and the world frame;
COLMAP's feature tracks tie the cameras together around the part.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix

from .calib import Calibration
from .colmap_session import SparseModel
from .mat import DEFAULT_MAT, MatSpec

SIGMA_MARKER_PX = 0.3
SIGMA_FEATURE_PX = 1.0
MAX_POINTS = 4000


@dataclass
class BAStats:
    marker_rms_px: float
    feature_rms_px: float
    n_marker_obs: int
    n_feature_obs: int
    n_points: int
    scale_sigma_rel: float
    points: np.ndarray | None = None  # refined feature points, world mm


def _rodrigues_batch(r: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(r, axis=1)
    k = r / np.where(th < 1e-12, 1, th)[:, None]
    K = np.zeros((len(r), 3, 3))
    K[:, 0, 1], K[:, 0, 2], K[:, 1, 2] = -k[:, 2], k[:, 1], -k[:, 0]
    K[:, 1, 0], K[:, 2, 0], K[:, 2, 1] = k[:, 2], -k[:, 1], k[:, 0]
    s, c = np.sin(th)[:, None, None], np.cos(th)[:, None, None]
    return np.eye(3)[None] + s * K + (1 - c) * (K @ K)


def project(intr: np.ndarray, R: np.ndarray, t: np.ndarray, X: np.ndarray) -> np.ndarray:
    """OpenCV (k1,k2,p1,p2) projection. R: (n,3,3), t: (n,3), X: (n,3)."""
    fx, fy, cx, cy, k1, k2, p1, p2 = intr
    Xc = np.einsum("nij,nj->ni", R, X) + t
    z = np.where(np.abs(Xc[:, 2]) < 1e-9, 1e-9, Xc[:, 2])
    x, y = Xc[:, 0] / z, Xc[:, 1] / z
    r2 = x * x + y * y
    rad = 1 + k1 * r2 + k2 * r2 * r2
    xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return np.stack([fx * xd + cx, fy * yd + cy], 1)


def adjust(cal: Calibration, model: SparseModel, names: list[str], mat: MatSpec = DEFAULT_MAT,
           log=print, seed: int = 0, on_round=None) -> tuple[Calibration, BAStats]:
    """on_round(round, points, camera_centres) after each solver round (live preview)."""
    rng = np.random.default_rng(seed)
    name_idx = {n: i for i, n in enumerate(names)}
    n_img = len(names)

    # Marker observations (fixed world points). Frames located by feature
    # matching alone (close-ups that cut the ring) have none; the features
    # tie them to the mat-located frames.
    corners = cal.corner_world if cal.corner_world is not None else mat.corner_world
    m_img, m_X, m_uv = [], [], []
    for n in names:
        o = cal.obs.get(n)
        if o is None:
            continue
        m_img.append(np.full(len(o.ids), name_idx[n]))
        m_X.append(corners[o.ids])
        m_uv.append(o.px)
    m_img, m_X, m_uv = np.concatenate(m_img), np.concatenate(m_X), np.concatenate(m_uv).astype(np.float64)

    # Feature observations (free points), COLMAP pixel coords -> OpenCV.
    if len(model.obs_name) == 0:
        raise RuntimeError("no matching points were found between frames: film slowly, with the part well lit")
    keep = np.array([nm in name_idx for nm in model.obs_name], bool)
    f_img = np.array([name_idx[nm] for nm, k in zip(model.obs_name, keep) if k], np.int64)
    f_pid, f_uv = model.obs_pid[keep], model.obs_xy[keep] - 0.5
    uniq, counts = np.unique(f_pid, return_counts=True)
    good = uniq[counts >= 3]
    if len(good) > MAX_POINTS:
        good = rng.choice(good, MAX_POINTS, replace=False)
    sel = np.isin(f_pid, good)
    f_img, f_pid, f_uv = f_img[sel], f_pid[sel], f_uv[sel]
    pid_list = np.unique(f_pid)
    pid_idx = {p: i for i, p in enumerate(pid_list)}
    f_pt = np.array([pid_idx[p] for p in f_pid], np.int64)
    n_pts = len(pid_list)

    intr0 = np.array([cal.K[0, 0], cal.K[1, 1], cal.K[0, 2], cal.K[1, 2], *cal.dist[:4]])
    pose0 = []
    for n in names:
        R, t = cal.poses[n]
        pose0.append(np.concatenate([cv2.Rodrigues(R)[0].ravel(), t]))
    pts0 = np.array([model.point_xyz[p] for p in pid_list]).reshape(-1, 3)
    x0 = np.concatenate([intr0, np.ravel(pose0), pts0.ravel()])
    o_pose, o_pts = 8, 8 + 6 * n_img

    def unpack(x):
        poses = x[o_pose:o_pts].reshape(n_img, 6)
        return x[:8], _rodrigues_batch(poses[:, :3]), poses[:, 3:], x[o_pts:].reshape(n_pts, 3)

    f_mask = np.ones(len(f_img), bool)

    def residuals(x):
        intr, R, t, P = unpack(x)
        rm = (project(intr, R[m_img], t[m_img], m_X) - m_uv) / SIGMA_MARKER_PX
        fi, fp = f_img[f_mask], f_pt[f_mask]
        rf = (project(intr, R[fi], t[fi], P[fp]) - f_uv[f_mask]) / SIGMA_FEATURE_PX
        return np.concatenate([rm.ravel(), rf.ravel()])

    def sparsity():
        fi, fp = f_img[f_mask], f_pt[f_mask]
        n_m, n_f = len(m_img), len(fi)
        rows, cols = [], []
        r_m = np.arange(2 * n_m)
        img_r = np.repeat(m_img, 2)
        for c in range(8):
            rows.append(r_m), cols.append(np.full_like(r_m, c))
        for c in range(6):
            rows.append(r_m), cols.append(o_pose + 6 * img_r + c)
        r_f = 2 * n_m + np.arange(2 * n_f)
        img_f, pt_f = np.repeat(fi, 2), np.repeat(fp, 2)
        for c in range(8):
            rows.append(r_f), cols.append(np.full_like(r_f, c))
        for c in range(6):
            rows.append(r_f), cols.append(o_pose + 6 * img_f + c)
        for c in range(3):
            rows.append(r_f), cols.append(o_pts + 3 * pt_f + c)
        rows, cols = np.concatenate(rows), np.concatenate(cols)
        return coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(2 * (n_m + n_f), len(x0))).tocsr()

    x = x0
    for rnd in range(2):
        res = least_squares(residuals, x, jac_sparsity=sparsity(), loss="huber", f_scale=2.0, x_scale="jac",
                            method="trf", tr_solver="lsmr", max_nfev=100, xtol=1e-10, ftol=1e-9)
        x = res.x
        intr, R, t, P = unpack(x)
        rf = project(intr, R[f_img], t[f_img], P[f_pt]) - f_uv
        err = np.linalg.norm(rf, axis=1)
        new_mask = err < 3.0
        log(f"marker BA round {rnd + 1}: cost={res.cost:.1f} kept {new_mask.sum()}/{len(new_mask)} feature obs")
        if on_round:
            on_round(rnd + 1, P, -np.einsum("nji,nj->ni", R, t))
        if (new_mask == f_mask).all():
            break
        f_mask = new_mask

    intr, R, t, P = unpack(x)
    rm = project(intr, R[m_img], t[m_img], m_X) - m_uv
    rf = project(intr, R[f_img[f_mask]], t[f_img[f_mask]], P[f_pt[f_mask]]) - f_uv[f_mask]
    m_rms = float(np.sqrt(np.mean(np.sum(rm ** 2, 1))))
    f_rms = float(np.sqrt(np.mean(np.sum(rf ** 2, 1)))) if len(rf) else float("nan")

    # Scale uncertainty: per-frame scale noise ~ (rms/f)*(depth/marker spread),
    # averaged over frames, floored at 0.1 % for print scale and paper flatness.
    marker_imgs = np.unique(m_img)
    depth = np.median([np.linalg.norm(t[i]) for i in marker_imgs])
    per_frame = m_rms / intr[0] * depth / 150.0
    scale_sigma = float(max(per_frame / np.sqrt(len(marker_imgs)), 0.001))

    out = Calibration(cal.width, cal.height, np.array([[intr[0], 0, intr[2]], [0, intr[1], intr[3]], [0, 0, 1]]),
                      intr[4:8].copy(), m_rms, obs=cal.obs, rejected=cal.rejected,
                      corner_world=cal.corner_world, bend_mm=cal.bend_mm, same_size=cal.same_size)
    for i, n in enumerate(names):
        out.poses[n] = (R[i], t[i])
    stats = BAStats(m_rms, f_rms, len(m_img), int(f_mask.sum()), n_pts, scale_sigma, P.copy())
    log(f"marker BA: marker rms {m_rms:.3f}px, feature rms {f_rms:.3f}px, {n_pts} points, "
        f"f={intr[0]:.1f}px")
    return out, stats
