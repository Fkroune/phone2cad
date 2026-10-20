"""Carving the part's shape from its outlines (the visual hull), and the crude
known-mat detector that gives SAM its first hint.

The outlines themselves come from SAM 2.1 (segment.py). Each frame's ViewMask
holds its known background: pixels that surely are not the part. A voxel belongs
to the part unless at least two views see known background there (one bad mask
cannot cut the part); everything else is unknown and never carves, so parts
larger than the mat and busy rooms are safe. Carving runs coarse (1 mm), then
fine near the surface.

MaskMaker is the older hand-made detector: it renders the printed mat into the
frame and marks where the photo differs from it (brightness against the known
print, colour against the paper). Not accurate enough for outlines on black or
shiny parts, but good enough to find the part on an overview frame.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import open3d as o3d

from .calib import Calibration
from .mat import DEFAULT_MAT, MatSpec

MASK_SIDE = 2000  # px, longest side the masks are computed at
RASTER_PPM = 6.0  # px per mm of the rendered mat
MIN_BG_VOTES = 2  # views that must see background before a voxel is carved...
BG_SHARE = 0.0  # ...and at least this share of the views that see it (robust to a few wrong outlines)
CHROMA_DIST = 18.0  # Lab a*b* distance from the frame's paper colour that counts as coloured
COARSE_MM = 1.0


@dataclass
class ViewMask:
    background: np.ndarray  # bool, known background: carves (the mat positively seen, not the part)
    K: np.ndarray  # pinhole intrinsics of the undistorted mask image
    R: np.ndarray  # world -> camera
    t: np.ndarray


def _raster_to_world(mat: MatSpec, ppm: float) -> np.ndarray:
    """Homogeneous map: rendered page raster px -> world mm on the mat plane."""
    ox, oy = mat.page_offset
    return np.array([[1 / ppm, 0, 0.5 / ppm - mat.board_w / 2 - ox],
                     [0, -1 / ppm, mat.board_h / 2 + oy - 0.5 / ppm],
                     [0, 0, 1]])


def _pattern_visible(photo: np.ndarray, rendered: np.ndarray, win: int = 21, sigma: float = 1.5,
                     min_ncc: float = 0.3) -> np.ndarray:
    """Where the photo still shows the printed pattern: local normalised
    cross-correlation with the rendered mat (blurred, so 1-2 px of
    misregistration does not matter), only where the mat has a pattern."""
    a, b = cv2.GaussianBlur(photo, (0, 0), sigma), cv2.GaussianBlur(rendered, (0, 0), sigma)
    box = lambda x: cv2.boxFilter(x, -1, (win, win))  # noqa: E731
    ma, mb = box(a), box(b)
    va, vb = box(a * a) - ma * ma, box(b * b) - mb * mb
    ncc = (box(a * b) - ma * mb) / np.sqrt(np.maximum(va * vb, 1e-8))
    # The photo must show contrast there too: on a flat, featureless patch (a black
    # part in front of the black print) the correlation is just noise.
    return (vb > 1e-3) & (va > 0.02 * vb) & (ncc > min_ncc)


def _busy(photo: np.ndarray, win: int = 21, sigma: float = 1.5) -> np.ndarray:
    """Local contrast of the photo (variance in a window): the black-and-white
    print always shows it, even a few pixels off where it was expected (curled
    mat, rough lens fit); a plain part in front of it does not."""
    a = cv2.GaussianBlur(photo, (0, 0), sigma)
    box = lambda x: cv2.boxFilter(x, -1, (win, win))  # noqa: E731
    ma = box(a)
    return box(a * a) - ma * ma


MAINLAND_SHARE = 0.01  # known background regions smaller than this share of the page are islands


def _mainland(known: np.ndarray, page: np.ndarray) -> np.ndarray:
    """The visible mat is one large connected region around the part. A small
    island of "paper-bright" pixels cut off from it is a reflection on the part
    (torch glints, a shiny label: jaw scan 2 lost 8 cm3 to them), not mat: keep
    only regions of at least MAINLAND_SHARE of the page (thin bridges cut first)."""
    k = cv2.morphologyEx(known.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(k, connectivity=4)
    if n <= 1:
        return known
    big = np.zeros(n, bool)
    big[1:] = stats[1:, cv2.CC_STAT_AREA] >= MAINLAND_SHARE * max(int(page.sum()), 1)
    # back to full detail inside the kept regions (the opening only decided which ones)
    keep = cv2.dilate((big[lab]).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return known & keep


def _patterned(rendered: np.ndarray, win: int = 21, sigma: float = 1.5) -> np.ndarray:
    """Where the printed mat has strong, large print (the marker ring, the squares):
    there the print match is reliable. The sparse speckle of the centre zone is
    not included (its small dots often fail to match on slightly blurred frames)."""
    b = cv2.GaussianBlur(rendered, (0, 0), sigma)
    box = lambda x: cv2.boxFilter(x, -1, (win, win))  # noqa: E731
    mb = box(b)
    return box(b * b) - mb * mb > 0.02


class MaskMaker:
    def __init__(self, cal: Calibration, mat: MatSpec = DEFAULT_MAT, side: int = MASK_SIDE):
        self.cal, self.mat, self.side = cal, mat, side
        self.tex = mat.to_raster(RASTER_PPM).astype(np.float32)
        self.A = _raster_to_world(mat, RASTER_PPM)

    def __call__(self, image: np.ndarray, name: str) -> tuple[ViewMask, np.ndarray]:
        """Known background for one frame (and the part mask, for previews)."""
        cal = self.cal
        s = self.side / max(image.shape[:2])
        small = cv2.resize(image, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        K = np.diag([s, s, 1.0]) @ cal.K
        K[0, 2], K[1, 2] = (cal.K[0, 2] + 0.5) * s - 0.5, (cal.K[1, 2] + 0.5) * s - 0.5
        und = cv2.undistort(small, K, np.r_[cal.dist[:4], 0.0])
        R, t = cal.poses[name]
        H = K @ np.c_[R[:, 0], R[:, 1], t] @ self.A
        rendered = cv2.warpPerspective(self.tex, H, und.shape[1::-1], flags=cv2.INTER_AREA, borderValue=-1)
        page = rendered >= 0
        g = cv2.cvtColor(und, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255
        gb = cv2.GaussianBlur(g, (0, 0), 4)
        mb = cv2.GaussianBlur(np.where(page, rendered, 1.0), (0, 0), 4)
        paper = page & (mb > 0.7)  # brightness only means something against white paper
        gain = np.median(gb[paper]) / max(np.median(mb[paper]), 1e-3) if paper.any() else 1.0
        ratio = gb / np.maximum(mb * gain, 1e-3)
        # Colour relative to this frame's own paper (Lab a*b*): a colour cast from
        # white balance or warm light shifts paper and shadows alike, a coloured
        # part stands apart. (A fixed saturation threshold marked whole warm frames.)
        lab = cv2.cvtColor(und, cv2.COLOR_BGR2LAB).astype(np.float32)
        ref = paper & (gb > 0.25)
        a0, b0 = (np.median(lab[..., 1][ref]), np.median(lab[..., 2][ref])) if ref.any() else (128.0, 128.0)
        chroma = np.hypot(lab[..., 1] - a0, lab[..., 2] - b0) > CHROMA_DIST
        # Darker paper is either a dark object or a shadow. In a shadow the
        # printed speckle is still there, just dimmer; an object hides it. So a
        # dark pixel is background when the local pattern still matches the
        # rendered mat (corner benchmark: shadows under the arms were left
        # uncarved as a 5 mm skirt).
        # The same holds for glare (brighter paper): still patterned = paper.
        odd = paper & ((ratio < 0.45) | (ratio > 1.25))
        printed = _pattern_visible(g, np.where(page, rendered, 1.0))
        obj = chroma | (odd & ~printed)
        obj = cv2.morphologyEx(obj.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        obj = cv2.morphologyEx(obj, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
        # Known background only where the mat is positively seen: where the print has
        # a pattern (ring, squares, the speckle of the centre zone), that pattern must
        # be visible; plain white paper keeps the brightness test. Anything else is
        # unknown and never carves: a part in front of the black ring, or a grey,
        # shiny face over the paper, cannot be told from the mat by brightness.
        # On the paper (plain or speckled): the speckle is matched, or the pixel is
        # clearly paper-bright. A grey or shiny face is neither: unknown.
        # On the strong print (ring, squares): the print matches, or the photo shows
        # the print's kind of contrast there (paper/ink squares: variance ~0.1-0.2;
        # a plain part in front of them: ~0).
        patterned = _patterned(np.where(page, rendered, 1.0))
        print_like = printed | (_busy(g) > 0.01)
        known = np.where(patterned, print_like, paper & (printed | ((ratio > 0.8) & (ratio < 1.25))))
        return ViewMask(_mainland(page & ~obj & known, page), K, R, t), obj


def carve(X: np.ndarray, views: list[ViewMask], to_view=None) -> np.ndarray:
    """True where a voxel centre may belong to the part. to_view(i, X) can move
    points into view i's world (a second, flipped pass has its own)."""
    votes = np.zeros(len(X), np.int16)
    seen = np.zeros(len(X), np.int16)
    for i, v in enumerate(views):
        Y = to_view(i, X) if to_view else X
        Xc = Y @ v.R.T + v.t
        z = Xc[:, 2]
        ok = z > 1e-3
        u = np.full(len(X), -1, np.int64)
        w = np.full(len(X), -1, np.int64)
        u[ok] = np.round(v.K[0, 0] * Xc[ok, 0] / z[ok] + v.K[0, 2]).astype(np.int64)
        w[ok] = np.round(v.K[1, 1] * Xc[ok, 1] / z[ok] + v.K[1, 2]).astype(np.int64)
        inside = ok & (u >= 0) & (w >= 0) & (u < v.background.shape[1]) & (w < v.background.shape[0])
        b = np.zeros(len(X), bool)
        b[inside] = v.background[w[inside], u[inside]]
        votes += b
        seen += inside
    return votes < np.maximum(MIN_BG_VOTES, BG_SHARE * seen)


def allow_misalignment(views: list[ViewMask], centre: np.ndarray, tol_mm: float) -> None:
    """Shrink each view's known background by tol_mm (in pixels at that view's
    distance to the part), in place. When two passes are combined, their
    alignment error would otherwise let one pass's outlines shave the other's
    true surface; each side takes half of the measured mismatch."""
    for v in views:
        r = int(round(tol_mm * v.K[0, 0] / np.linalg.norm(v.R @ centre + v.t)))
        if r >= 1:
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
            v.background = cv2.erode(v.background.astype(np.uint8), k) > 0


def visual_hull(box: tuple[np.ndarray, np.ndarray], views: list[ViewMask], voxel: float = 0.25,
                to_view=None) -> tuple[np.ndarray, float]:
    """Occupied voxel centres (n, 3): coarse carve, then fine near the surface."""
    lo, hi = box
    coarse = max(COARSE_MM, 4 * voxel)  # the coarse pass scales with the voxel
    gc = [np.arange(lo[i], hi[i], coarse) + coarse / 2 for i in range(3)]
    Xc = np.stack(np.meshgrid(*gc, indexing="ij"), -1).reshape(-1, 3)
    occ = carve(Xc, views, to_view).reshape([len(a) for a in gc])
    near = occ.copy()
    for a in range(3):
        for sh in (-1, 1):
            near |= np.roll(occ, sh, a)
    sub = int(round(coarse / voxel))
    voxel = coarse / sub
    offs = (np.arange(sub) + 0.5) * voxel - coarse / 2
    O = np.stack(np.meshgrid(offs, offs, offs, indexing="ij"), -1).reshape(-1, 3)
    X = (Xc[near.reshape(-1)][:, None, :] + O[None]).reshape(-1, 3)
    return largest_piece(X[carve(X, views, to_view)], voxel, lo), voxel


def largest_piece(X: np.ndarray, voxel: float, origin: np.ndarray) -> np.ndarray:
    """Only the largest connected piece of the carved volume: a stray blob
    elsewhere on the mat (crease, tape, a mask error) is not the part."""
    if not len(X):
        return X
    from scipy import ndimage
    key = np.floor((X - origin) / voxel).astype(np.int64)
    key -= key.min(0)
    grid = np.zeros(key.max(0) + 1, bool)
    grid[tuple(key.T)] = True
    lab, n = ndimage.label(grid, structure=np.ones((3, 3, 3), bool))
    if n <= 1:
        return X
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return X[lab[tuple(key.T)] == sizes.argmax()]


def hull_surface(X: np.ndarray, voxel: float, origin: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Surface voxels of the hull and outward normals (local plane, oriented
    towards the empty neighbours)."""
    key = np.floor((X - origin) / voxel).astype(np.int64)  # centres sit at half steps: floor, never round
    key -= key.min(0) - 1
    grid = np.zeros(key.max(0) + 2, bool)
    grid[tuple(key.T)] = True
    D6 = np.array([(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)])
    free = np.stack([~grid[tuple((key + d).T)] for d in D6], 1)
    surf = free.any(1)
    P = X[surf]
    out_dir = free[surf].astype(float) @ D6
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P))
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.0, max_nn=40))
    N = np.asarray(pc.normals).copy()
    N[np.einsum("ij,ij->i", N, out_dir) < 0] *= -1
    return P, N
