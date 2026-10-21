"""Outlines that leave out a piece of the part (fast, synthetic views, no scan needed)."""

from types import SimpleNamespace

import cv2
import numpy as np

from phone2cad_worker import pipeline
from phone2cad_worker.silhouette import ViewMask

SIZE, F = 400, 500.0  # px, focal length (px)


def _corner(step=0.5, arm=True):
    """A corner part on the mat: a 12 mm cube with a 30 mm arm along x and one along y."""
    g = np.arange(0, 42, step)
    X = np.stack(np.meshgrid(g, g, np.arange(0, 12, step), indexing="ij"), -1).reshape(-1, 3)
    body = np.all(X < 12, axis=1)
    keep = body | (X[:, 1] < 12) & (X[:, 0] < 42) & arm | (X[:, 0] < 12) & (X[:, 1] < 42)
    return X[keep] - [12, 12, 0]


def _camera(az, el, r=160.0):
    c = r * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    z = -c / r
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    R = np.stack([x, np.cross(z, x), z])
    return R, -R @ c


def _view(shape, R, t):
    K = np.array([[F, 0, SIZE / 2], [0, F, SIZE / 2], [0, 0, 1]])
    Xc = shape @ R.T + t
    uv = np.round(Xc[:, :2] / Xc[:, 2:] * F + SIZE / 2).astype(int)
    sil = np.zeros((SIZE, SIZE), np.uint8)
    sil[uv[:, 1], uv[:, 0]] = 1
    sil = cv2.dilate(cv2.morphologyEx(sil, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)), np.ones((3, 3), np.uint8))
    return ViewMask(sil == 0, K, R, t)


def _side(every_bad=0):
    whole, cut = _corner(), _corner(arm=False)
    masks = []
    for k, (az, el) in enumerate((a, e) for e in np.radians([25, 45, 65]) for a in np.radians(np.arange(0, 360, 24))):
        R, t = _camera(az + 0.1 * el, el)
        masks.append(_view(cut if every_bad and k % every_bad == 0 else whole, R, t))
    return SimpleNamespace(D=_surface(_corner(step=1.0)), masks=masks,
                           box=(np.array([-25.0, -25.0, 0.0]), np.array([45.0, 45.0, 25.0])))


def _surface(X):
    """Dense points: the solid's points with an empty neighbour (not on the mat)."""
    key = np.round(X - X.min(0)).astype(int) + 1
    grid = np.zeros(key.max(0) + 2, bool)
    grid[tuple(key.T)] = True
    inner = np.ones(len(X), bool)
    for a in range(3):
        for s in (-1, 1):
            inner &= grid[tuple((key + np.eye(3, dtype=int)[a] * s).T)]
    return X[~inner & (X[:, 2] > 1.5)]


def test_a_piece_a_quarter_of_the_outlines_leave_out_is_found():
    side = _side(every_bad=4)
    lumps = pipeline._lost_lumps(side)
    assert len(lumps) == 1
    assert lumps[0][:, 0].mean() > 12  # on the x arm, beyond the cube
    cut = pipeline._cut_share(lumps, side.masks)
    bad = np.flatnonzero(cut >= pipeline.LOST_CUT)
    assert list(bad) == list(range(0, len(side.masks), 4))


def test_the_empty_inner_corner_is_not_a_lost_piece():
    # Only a few views see the mat between the two arms: carved by few votes, like
    # a lost piece, but with no dense points in it.
    side = _side()
    B, S = pipeline._bg_votes(np.array([[3.0, 3.0, 3.0]]), side.masks)
    assert 2 <= B.sum() < pipeline.LOST_KEEP * S.sum()
    assert pipeline._lost_lumps(side) == []
