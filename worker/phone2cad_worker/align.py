"""Bring a structure-from-motion model into the mat's world frame.

SfM locates frames in an arbitrary frame and scale. Frames that also see the
mat have metric poses from the ChArUco solve, so a similarity transform
(rotation, translation, scale) fitted on their camera centres puts every
frame, mat or not, in millimetres on the mat. The marker-constrained bundle
adjustment then refines everything together.
"""

from __future__ import annotations

import numpy as np

from .calib import Calibration
from .colmap_session import SparseModel

MIN_COMMON = 5  # mat-located frames that SfM must also locate
MAX_CENTRE_ERR_MM = 8.0  # median camera-centre disagreement above this = no trust


def _umeyama(A: np.ndarray, B: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    """R, s, t minimising |s R A + t - B|."""
    ma, mb = A.mean(0), B.mean(0)
    U, S, Vt = np.linalg.svd((A - ma).T @ (B - mb))
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    s = float(np.trace(np.diag(S) @ D) / ((A - ma) ** 2).sum())
    return R, s, mb - s * R @ ma


def to_mat(model: SparseModel, cal: Calibration, log=print) -> SparseModel:
    """Transform model into mat world (in place) and give cal a starting pose
    for every frame SfM located but the mat did not. Raises if the two
    disagree, so the caller can fall back to mat-only poses."""
    centre = lambda R, t: -R.T @ t  # noqa: E731
    common = sorted(set(model.poses) & set(cal.poses))
    if len(common) < MIN_COMMON:
        raise RuntimeError(f"only {len(common)} frames located by both the mat and SfM")
    A = np.array([centre(*model.poses[n]) for n in common])
    B = np.array([centre(*cal.poses[n]) for n in common])
    keep = np.ones(len(common), bool)
    for _ in range(3):  # drop frames whose two poses disagree (bad mat solve or bad SfM)
        R, s, t = _umeyama(A[keep], B[keep])
        err = np.linalg.norm(s * A @ R.T + t - B, axis=1)
        new = err < max(3 * np.median(err[keep]), 1.0)
        if new.sum() < MIN_COMMON or (new == keep).all():
            break
        keep = new
    med = float(np.median(err[keep]))
    log(f"SfM aligned to the mat on {keep.sum()} of {len(common)} shared frames: "
        f"camera centres agree to {med:.2f} mm (median), scale {s:.4f}")
    if med > MAX_CENTRE_ERR_MM:
        raise RuntimeError(f"SfM and the mat disagree by {med:.1f} mm")

    added = 0
    for n, (Rc, tc) in model.poses.items():
        Rn = Rc @ R.T
        tn = s * tc - Rn @ t
        model.poses[n] = (Rn, tn)
        if n not in cal.poses:
            cal.poses[n] = (Rn, tn)
            added += 1
    model.point_xyz = {k: s * R @ X + t for k, X in model.point_xyz.items()}
    log(f"{added} frames added that the mat alone could not locate")
    return model


def pick_views(cal: Calibration, names: list[str], centre: np.ndarray, k: int) -> list[str]:
    """Up to k frames with the most distinct viewpoints on the part, for the
    dense step (its time grows with every frame; near-duplicates add little).
    Greedy farthest-point sampling on viewing directions, starting from the
    closest frame (the most detailed one)."""
    if len(names) <= k:
        return list(names)
    C = np.array([-cal.poses[n][0].T @ cal.poses[n][1] for n in names]) - centre
    dist = np.linalg.norm(C, axis=1)
    D = C / dist[:, None]
    chosen = [int(np.argmin(dist))]
    best = D @ D[chosen[0]]
    while len(chosen) < k:
        i = int(np.argmin(best))
        chosen.append(i)
        best = np.maximum(best, D @ D[i])
    return [names[i] for i in sorted(chosen)]
