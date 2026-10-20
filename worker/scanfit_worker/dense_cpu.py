"""CPU dense reconstruction, used when CUDA PatchMatch is unavailable.

For neighbouring views with a mostly sideways baseline, rectify the pair,
run semi-global block matching restricted to the part zone and its depth
range, and lift the disparities to world points. Points are kept only where
at least two independent pairs agree.
"""

from __future__ import annotations

import cv2
import numpy as np

from .calib import Calibration


def _centre(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return -R.T @ t


def choose_pairs(cal: Calibration, names: list[str], per_image: int = 2,
                 min_deg: float = 7.0, max_deg: float = 28.0, best_deg: float = 14.0) -> list[tuple[str, str]]:
    axes = {n: cal.poses[n][0][2] for n in names}
    pairs = set()
    for a in names:
        Ra, ta = cal.poses[a]
        Ca = _centre(Ra, ta)
        cands = []
        for b in names:
            if b == a:
                continue
            ang = np.degrees(np.arccos(np.clip(axes[a] @ axes[b], -1, 1)))
            if not (min_deg <= ang <= max_deg):
                continue
            base = Ra @ (_centre(*cal.poses[b]) - Ca)
            if abs(base[0]) < 1.5 * abs(base[1]):
                continue  # mostly vertical baseline: skip, SGBM is horizontal only
            cands.append((abs(ang - best_deg), b))
        for _, b in sorted(cands)[:per_image]:
            pairs.add(tuple(sorted((a, b))))
    return sorted(pairs)


def _box_corners(zone: tuple[float, float, float, float], zmax: float) -> np.ndarray:
    x0, x1, y0, y1 = zone
    return np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (0.0, zmax)])


def stereo_pair(img_a: np.ndarray, img_b: np.ndarray, cal: Calibration, a: str, b: str,
                zone: tuple[float, float, float, float], zmax: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """World points (n,3), grey values (n,) and the observing camera centre (3,)."""
    K, D = cal.K, np.r_[cal.dist[:4], 0.0]
    size = (cal.width, cal.height)
    for swap in (False, True):
        (la, lb) = (b, a) if swap else (a, b)
        Ra, ta = cal.poses[la]
        Rb, tb = cal.poses[lb]
        R_rel = Rb @ Ra.T
        t_rel = tb - R_rel @ ta
        R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(K, D, K, D, size, R_rel, t_rel.reshape(3, 1),
                                                    flags=cv2.CALIB_ZERO_DISPARITY, alpha=-1)
        if abs(P2[1, 3]) > abs(P2[0, 3]):
            return np.zeros((0, 3)), np.zeros(0), np.zeros(3)
        if P2[0, 3] < 0:
            break
    else:
        return np.zeros((0, 3)), np.zeros(0), np.zeros(3)
    left, right = (img_b, img_a) if swap else (img_a, img_b)
    f, cx, cy = P1[0, 0], P1[0, 2], P1[1, 2]
    B = -P2[0, 3] / f

    corners = _box_corners(zone, zmax)
    Xr = (R1 @ (Ra @ corners.T + ta[:, None])).T
    if (Xr[:, 2] <= 1).any():
        return np.zeros((0, 3)), np.zeros(0), np.zeros(3)
    u, v = f * Xr[:, 0] / Xr[:, 2] + cx, f * Xr[:, 1] / Xr[:, 2] + cy
    d = f * B / Xr[:, 2]
    dmin = int(np.floor(d.min())) - 8
    ndisp = int(np.ceil((d.max() + 8 - dmin) / 16.0)) * 16
    y0, y1 = max(0, int(v.min()) - 8), min(cal.height, int(v.max()) + 8)
    x0, x1 = max(0, int(u.min()) - 8), min(cal.width, int(u.max()) + 8)
    if y1 - y0 < 32 or x1 - x0 < 32 or ndisp > 1024:
        return np.zeros((0, 3)), np.zeros(0), np.zeros(3)

    m1 = cv2.initUndistortRectifyMap(K, D, R1, P1, size, cv2.CV_32FC1)
    m2 = cv2.initUndistortRectifyMap(K, D, R2, P2, size, cv2.CV_32FC1)
    L = cv2.remap(left, *m1, cv2.INTER_LINEAR)[y0:y1]
    Rimg = cv2.remap(right, *m2, cv2.INTER_LINEAR)[y0:y1]
    # Shift the right image so the search starts at dmin (keeps the cost volume small).
    pad = max(0, dmin)
    Rs = np.zeros_like(Rimg)
    if pad < Rimg.shape[1]:
        Rs[:, pad:] = Rimg[:, :Rimg.shape[1] - pad]
    bs = 5
    sgbm = cv2.StereoSGBM_create(minDisparity=dmin - pad, numDisparities=ndisp, blockSize=bs,
                                 P1=8 * bs * bs, P2=32 * bs * bs, disp12MaxDiff=1, uniquenessRatio=10,
                                 speckleWindowSize=200, speckleRange=2, mode=cv2.STEREO_SGBM_MODE_SGBM)
    disp = sgbm.compute(L, Rs).astype(np.float32) / 16.0
    valid = disp > (dmin - pad)
    valid[:, :x0] = False
    valid[:, x1:] = False
    vv, uu = np.nonzero(valid)
    dd = disp[vv, uu] + pad
    Z = f * B / dd
    Xr = np.stack([(uu - cx) * Z / f, (vv + y0 - cy) * Z / f, Z], 1)
    Xa = (R1.T @ Xr.T)  # rectified -> left camera
    Xw = (Ra.T @ (Xa - ta[:, None])).T
    gray = L[vv, uu].astype(np.float32) / 255.0
    x0w, x1w, y0w, y1w = zone
    inside = ((Xw[:, 0] > x0w) & (Xw[:, 0] < x1w) & (Xw[:, 1] > y0w) & (Xw[:, 1] < y1w)
              & (Xw[:, 2] > -2) & (Xw[:, 2] < zmax))
    return Xw[inside], gray[inside], _centre(Ra, ta)


def _voxel_keys(P: np.ndarray, voxel: float) -> np.ndarray:
    """Exact int64 key per voxel (21 bits per axis, ample for a mat-sized zone)."""
    k = np.floor(P / voxel).astype(np.int64) + (1 << 20)
    return (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]


class _VoxelGrid:
    """Running per-voxel sums over all pairs. Memory scales with the surface
    area in the zone, not with pairs x pixels (raw SGBM output is ~1M points
    per pair; keeping it all once took the 8 GB WSL VM down)."""

    def __init__(self, voxel: float):
        self.voxel = voxel
        self.keys = np.zeros(0, np.int64)
        self.sums = np.zeros((0, 7))  # x, y, z, grey, view x, view y, view z
        self.count = np.zeros(0)  # points
        self.pairs = np.zeros(0, np.int32)  # pairs that hit this voxel

    def add(self, P: np.ndarray, g: np.ndarray, C: np.ndarray) -> None:
        V = C - P
        V /= np.linalg.norm(V, axis=1, keepdims=True)
        X = np.column_stack([P, g, V])
        uk, inv = np.unique(_voxel_keys(P, self.voxel), return_inverse=True)
        sums = np.stack([np.bincount(inv, X[:, i], len(uk)) for i in range(7)], 1)
        cnt = np.bincount(inv, minlength=len(uk)).astype(float)
        keys, inv2 = np.unique(np.r_[self.keys, uk], return_inverse=True)
        n = len(keys)
        old, new = inv2[:len(self.keys)], inv2[len(self.keys):]
        S = np.zeros((n, 7))
        S[old] += self.sums
        S[new] += sums
        c = np.zeros(n)
        c[old] += self.count
        c[new] += cnt
        q = np.zeros(n, np.int32)
        q[old] += self.pairs
        q[new] += 1
        self.keys, self.sums, self.count, self.pairs = keys, S, c, q

    def points(self, min_pairs: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ok = self.pairs >= min_pairs
        M = self.sums[ok] / self.count[ok, None]
        V = M[:, 4:7]
        V /= np.linalg.norm(V, axis=1, keepdims=True)
        return M[:, :3], M[:, 3], V


def reconstruct(images: dict[str, np.ndarray], cal: Calibration, names: list[str],
                zone: tuple[float, float, float, float], zmax: float = 80.0, voxel: float = 0.4,
                min_pairs: int = 2, log=print) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fused world points (one per voxel), grey values and unit directions
    toward the observing cameras. A voxel is kept only when at least
    min_pairs independent stereo pairs put points in it."""
    pairs = choose_pairs(cal, names)
    log(f"CPU dense: {len(pairs)} stereo pairs")
    grid = _VoxelGrid(voxel)
    raw = 0
    for a, b in pairs:
        P, g, C = stereo_pair(images[a], images[b], cal, a, b, zone, zmax)
        if len(P):
            raw += len(P)
            grid.add(P, g, C)
    P, G, V = grid.points(min_pairs)
    log(f"CPU dense: {len(P)} of {len(grid.keys)} voxels agree across pairs ({raw} raw points)")
    return P, G, V
