"""Round holes on flat faces, from the photos alone (no texture needed).

The outline shape cannot see into holes, and on plain plastic the texture
points rarely land on their walls. But a hole is easy to see in a photo: a
round opening that differs from the face around it (a dark inside, a lit rim,
or the paper showing through). Each measured face is a known plane in
millimetres, so the photos that look at it most squarely are unwarped into
to-scale pictures of that face (orthophotos). A hole's rim lies in the face
plane, so its size in the orthophoto is its size in mm.

Per face: the orthophotos are lined up on each other (phase correlation),
candidate openings are collected from several standard detectors, and each
candidate is measured in every photo on its own. A hole is kept when several
photos agree on its size; the spread between them, and how far the opening is
from a circle, give its +/-. Windows, slots, round marks and things seen
through a bigger opening are rejected (see _circles).
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

MAX_ANGLE_DEG = 40.0  # photos looking at the face more obliquely than this are not used
MAX_VIEWS = 6  # the most square-on photos per face
ORTHO_MAX_PX = 600  # longest side of a face orthophoto
MIN_HALF_MM = 2.5  # faces smaller than 5 mm across are not searched
CONTRAST = 3.0  # a hole differs from the face by this many robust standard deviations
RIM_CONTRAST = 3.0  # the step at a hole's rim, in robust standard deviations of the face around it
ROUNDNESS = 0.85  # 4*pi*area/perimeter^2 of the blob's outline (circle 1, square 0.785)
AXIS_RATIO = 0.7  # candidate ellipse minor/major axis (a deep bore seen off-axis: its dark part 0.75)
POLYGON_SWING = 0.08  # outline radius swinging 3..8 times per turn, share of the radius
#                      (round holes 0.02-0.05, the corner part's rounded-square windows 0.11-0.16)
RUNS_ON = 0.1  # share of an opening's outline allowed past 1.45 r
MIN_PX = 8  # a hole must be at least this many orthophoto pixels across...
MIN_D_MM = 1.5  # ...and at least this size (smaller "holes" were specks on the corner part)
DEPTH = 2.5  # photos disagree this much (robust sd) inside or on the rim: the opening has depth
MIN_VIEWS = 3  # photos a hole must be seen in (2 let a round stain through on the synthetic block)
RAYS = 72  # rim samples per photo
AGREE_SIZE = 0.12  # photos agree on a hole when their radii are this close (share)...
AGREE_PLACE = 0.35  # ...and their centres (share of the radius)
MIN_ARC = 0.5  # share of the rim that must lie on the circle
ROUND_TOL = 0.1  # rim points may stray this share of the radius from the circle (a printed thread's opening: ~0.1)
RIM_SMOOTH_MM = 0.2  # photos smoothed this much before the rim is found (printed lines ~0.2 mm apart)
REG_MIN_PEAK = 0.1  # phase-correlation peak below this: a photo's shift is not trusted
REG_MAX_MM = 3.0  # nor a shift larger than this
SYSTEMATIC_MM = 0.15  # rim found slightly inside (synthetic Ø10: 9.78), chamfers: 1-sigma
DEBUG_DIR = os.environ.get("PHONE2CAD_HOLES_DEBUG", "")  # developer-only (CONTRIBUTING.md): writes scan photos there


@dataclass
class Photo:
    """One frame and its camera, in the coordinates the faces are in."""
    path: Path
    K: np.ndarray
    dist: np.ndarray
    R: np.ndarray  # world -> camera
    t: np.ndarray
    size: tuple[int, int]  # width, height


@dataclass
class Hole:
    centre: np.ndarray  # world mm, on the face plane
    normal: np.ndarray  # the face's outward normal (the hole's axis)
    diameter: float
    pm: float
    photos: int
    face: int


class _Images:
    """Grey frames, a few kept in memory (12 MP each)."""

    def __init__(self, keep: int = 8):
        self.keep, self.cache = keep, OrderedDict()

    def __call__(self, path: Path) -> np.ndarray | None:
        if path in self.cache:
            self.cache.move_to_end(path)
            return self.cache[path]
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        self.cache[path] = img
        if len(self.cache) > self.keep:
            self.cache.popitem(last=False)
        return img


def _views_for(normal, centre, photos: list[Photo]) -> list[tuple[float, Photo]]:
    """Photos that see the face's front, most square-on and closest first."""
    out = []
    for ph in photos:
        C = -ph.R.T @ ph.t
        d = C - centre
        dist = np.linalg.norm(d)
        cos = d @ normal / dist
        if cos < np.cos(np.radians(MAX_ANGLE_DEG)):
            continue
        x = ph.R @ centre + ph.t
        if x[2] <= 0:
            continue
        u, v = ph.K[0, 0] * x[0] / x[2] + ph.K[0, 2], ph.K[1, 1] * x[1] / x[2] + ph.K[1, 2]
        if not (0 <= u < ph.size[0] and 0 <= v < ph.size[1]):
            continue
        out.append((cos * ph.K[0, 0] / dist, ph))  # foreshortening x pixels per mm
    out.sort(key=lambda s: -s[0])
    return out[:MAX_VIEWS]


def _ortho(img: np.ndarray, ph: Photo, grid: np.ndarray) -> np.ndarray:
    """Sample the photo at world points grid (h, w, 3): the face seen square-on."""
    h, w = grid.shape[:2]
    rvec = cv2.Rodrigues(ph.R)[0]
    px, _ = cv2.projectPoints(grid.reshape(-1, 3), rvec, ph.t, ph.K, ph.dist)
    m = px.reshape(h, w, 2).astype(np.float32)
    return cv2.remap(img, m[..., 0], m[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)


def _robust(x: np.ndarray) -> tuple[float, float]:
    med = float(np.median(x))
    return med, float(np.median(np.abs(x - med))) * 1.4826 + 1e-3


def _not_round(M: np.ndarray, valid: np.ndarray, cx: float, cy: float, r: float) -> bool:
    """Is this opening something other than one round hole? Judged on the plain
    combined picture (the smoothed maps round corners off): the outline at
    mid-brightness between the opening and the face around it, within 1.5 r.
    - a polygon with rounded corners: its radius swings 3..8 times per turn by
      more than POLYGON_SWING of r (round holes 0.02-0.05, the corner part's
      rounded-square windows 0.11-0.16);
    - a longer opening: the outline runs on past 1.45 r on more than RUNS_ON of
      the turn (a circle fitted to each rounded end of the corner part's window:
      0.2; real holes 0-0.04).
    Only when the opening stands out by brightness; on a busy textured face the
    outline is not clean."""
    Mb = cv2.GaussianBlur(M.astype(np.float32), (0, 0), 1.0)
    yy, xx = np.mgrid[0:M.shape[0], 0:M.shape[1]]
    rr = np.hypot(xx - cx, yy - cy)
    inside, ring = Mb[(rr < 0.6 * r) & valid], Mb[(rr > 1.3 * r) & (rr < 1.8 * r) & valid]
    if inside.size < 10 or ring.size < 10:
        return False
    face, spread = _robust(ring)
    hole = float(np.median(inside))
    if abs(hole - face) < CONTRAST * spread:
        return False
    level = 0.5 * (hole + face)
    blob = (((Mb < level) if hole < face else (Mb > level)) & (rr < 1.5 * r)).astype(np.uint8)
    cs, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return False
    d = max(cs, key=cv2.contourArea).reshape(-1, 2).astype(float) - (cx, cy)
    th, rad = np.arctan2(d[:, 1], d[:, 0]), np.hypot(d[:, 0], d[:, 1])
    order = np.argsort(th)
    g = np.linspace(-np.pi, np.pi, 360, endpoint=False)
    rg = np.interp(g, th[order], rad[order], period=2 * np.pi)
    swing = max(2 * abs(np.mean((rg - rg.mean()) * np.exp(-1j * k * g))) for k in range(3, 9)) / rg.mean()
    return swing > POLYGON_SWING or float((rg > 1.45 * r).mean()) > RUNS_ON


def _has_depth(S, s_med, s_mad, cx, cy, r) -> bool:
    """A hole goes below the face: seen from different sides, its inside and the
    wall just below its rim look different from photo to photo. A round mark
    printed or stained on the face looks the same in all of them (the synthetic
    block's texture spots: 1.7 at most; real holes 3 to 10)."""
    yy, xx = np.mgrid[0:S.shape[0], 0:S.shape[1]]
    rr = np.hypot(xx - cx, yy - cy)
    inside, rim = S[rr < 0.7 * r], S[(rr >= 0.7 * r) & (rr < 1.1 * r)]
    return inside.size > 0 and max(np.median(inside), np.median(rim)) > s_med + DEPTH * s_mad


def _ring_seen(valid: np.ndarray, cx: float, cy: float, r: float) -> bool:
    """Every photo sees the ring around the hole (not cut by the edge of the picture).
    A test that the ring is plain face was tried and dropped: under the torch the
    face's brightness and glints vary too much from place to place and photo to photo."""
    yy, xx = np.mgrid[0:valid.shape[0], 0:valid.shape[1]]
    rr = np.hypot(xx - cx, yy - cy)
    ring = (rr > 1.25 * r) & (rr < 1.7 * r)
    return ring.sum() >= 20 and valid[ring].mean() >= 0.9


def _detail(img: np.ndarray, ok: np.ndarray, sig: float) -> np.ndarray:
    """The picture minus its local mean, zero where the photo has no data: what the
    photos are lined up on (edges and texture, not shading)."""
    w = ok.astype(np.float32)
    F = np.where(ok, img, 0).astype(np.float32)
    mean = cv2.GaussianBlur(F, (0, 0), sig) / np.maximum(cv2.GaussianBlur(w, (0, 0), sig), 1e-3)
    return (np.clip(np.where(ok, F - mean, 0), -4, 4) * cv2.GaussianBlur(w, (0, 0), 2)).astype(np.float32)


def _register(stack: np.ndarray, ok: np.ndarray, res: float) -> None:
    """Line the photos up on the first (most square-on) one, in place. Small pose
    and plane errors shift a face's orthophotos by up to ~1 mm against each other
    (the jaw's side holes showed twice in the median); phase correlation measures
    that shift. A photo it cannot match confidently stays as projected."""
    ref = _detail(stack[0], ok[0], 3.0 / res)
    win = cv2.createHanningWindow(ref.shape[::-1], cv2.CV_32F)
    h, w = ref.shape
    for i in range(1, len(stack)):
        (dx, dy), peak = cv2.phaseCorrelate(ref, _detail(stack[i], ok[i], 3.0 / res), win)
        if peak < REG_MIN_PEAK or np.hypot(dx, dy) * res > REG_MAX_MM:
            continue
        A = np.float32([[1, 0, -dx], [0, 1, -dy]])
        stack[i] = cv2.warpAffine(stack[i], A, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
        ok[i] = cv2.warpAffine(ok[i].astype(np.uint8), A, (w, h), flags=cv2.INTER_NEAREST, borderValue=0) > 0


def _u8(img: np.ndarray, inner: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(img[inner], [1, 99])
    return np.clip((img - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)


def _round(c: np.ndarray, rmin: float, rmax: float, area: float | None = None) -> tuple[float, float, float] | None:
    """(x, y, r) of an outline (or of a region's pixels, area = their count) that
    is a filled, round shape of a hole's size."""
    hull = cv2.convexHull(c)
    a, p = cv2.contourArea(hull), cv2.arcLength(hull, True)
    filled = cv2.contourArea(c) if area is None else area
    if len(hull) < 5 or p <= 0 or 4 * np.pi * a / p ** 2 < ROUNDNESS or filled < 0.6 * a:
        return None
    (cx, cy), (ma, mi), _ = cv2.fitEllipse(hull)
    r = 0.25 * (ma + mi)
    return (cx, cy, r) if min(ma, mi) >= AXIS_RATIO * max(ma, mi) and rmin <= r <= rmax else None


def _candidates(stack, ok, valid, M, cue, inner, rmin, rmax) -> list[tuple[float, float, float]]:
    """Places that might be holes, from three standard detectors; each catches
    holes the others miss, and every one is measured and checked afterwards.
    (1) Round blobs of the hole-likeness map, and round gaps inside its blobs
    (the jaw's bore inside its lighter recess). (2) EDCircles (OpenCV EdgeDrawing,
    Akinlar & Topal 2013): circular edges, also broken ones. (3) MSER (Matas 2002):
    regions that stay apart over a wide range of thresholds - a dark hole whose
    rim is too ragged for an edge detector."""
    out = []
    odd = (cue > CONTRAST).astype(np.uint8)
    cs, _ = cv2.findContours(odd, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    out += [r for r in (_round(c, rmin, rmax) for c in cs) if r is not None]
    ed = cv2.ximgproc.createEdgeDrawing()
    p = cv2.ximgproc.EdgeDrawing.Params()
    p.PFmode, p.NFAValidation, p.MinPathLength, p.GradientThresholdValue, p.AnchorThresholdValue = True, False, 8, 8, 2
    ed.setParams(p)
    ms = cv2.MSER_create(delta=5, min_area=int(np.pi * rmin ** 2), max_area=int(np.pi * rmax ** 2), max_variation=0.5)
    for img, mask in [(M, valid)] + list(zip(stack, ok)):
        g = cv2.GaussianBlur(_u8(img, inner & mask), (0, 0), 1.0)
        ed.detectEdges(g)
        el = ed.detectEllipses()
        for cx, cy, r, a, b, _ in ([] if el is None else el.reshape(-1, 6)):
            if r == 0 and min(a, b) >= AXIS_RATIO * max(a, b):
                r = 0.5 * (a + b)
            if rmin <= r <= rmax:
                out.append((float(cx), float(cy), float(r)))
        regions, _ = ms.detectRegions(g)
        out += [r for r in (_round(P.reshape(-1, 1, 2), rmin, rmax, len(P)) for P in regions) if r is not None]
    return out


def _circle_lsq(P: np.ndarray) -> tuple[np.ndarray, float]:
    A = np.c_[2 * P, np.ones(len(P))]
    sol = np.linalg.lstsq(A, (P ** 2).sum(1), rcond=None)[0]
    return sol[:2], float(np.sqrt(max(sol[2] + sol[:2] @ sol[:2], 1e-6)))


def _ransac_circle(P: np.ndarray, tol: float, tries: int = 300):
    """The circle most rim points agree on (RANSAC, Fischler & Bolles 1981), then
    least squares on those. A deep bore seen off-axis shows its lit wall on one
    side, which pulls that part of the outline inwards: those points are left out.
    Needs MIN_ARC of the rays. Returns (centre, r, deviations, inliers) or None."""
    if len(P) < MIN_ARC * RAYS:
        return None
    rng = np.random.default_rng(0)
    idx = np.argsort(rng.random((tries, len(P))), axis=1)[:, :3]  # 3 different points per try
    a, b, c = P[idx[:, 0]], P[idx[:, 1]], P[idx[:, 2]]
    M = 2 * np.stack([b - a, c - a], axis=1)
    rhs = np.stack([(b * b).sum(1) - (a * a).sum(1), (c * c).sum(1) - (a * a).sum(1)], axis=1)
    fine = np.abs(np.linalg.det(M)) > 1e-6
    if not fine.any():
        return None
    O = np.linalg.solve(M[fine], rhs[fine][..., None])[..., 0]
    R = np.linalg.norm(a[fine] - O, axis=1)
    D = np.abs(np.linalg.norm(P[None] - O[:, None], axis=2) - R[:, None])
    best = D[int(np.argmax((D < tol).sum(1)))] < tol
    if best.sum() < MIN_ARC * RAYS:
        return None
    for _ in range(2):
        o, r = _circle_lsq(P[best])
        dev = np.abs(np.linalg.norm(P - o, axis=1) - r)
        best = dev < tol
        if best.sum() < MIN_ARC * RAYS:
            return None
    return o, r, dev, best


def _rim(img: np.ndarray, ok: np.ndarray, cx: float, cy: float, r0: float, res: float):
    """A hole's rim in one photo, around a rough place and size, on the photo
    smoothed just enough to wash out printed layer lines. Along 72 rays: the most
    face-like point just outside the hole (brightest, or darkest for a see-through
    hole), then walking in from it, the mid-level between it and the inside of the
    hole (so a threaded or see-through inside does not matter). A least-squares
    circle through those points, far points dropped. Both polarities are tried; a
    black part's hole is often marked by a lit rim more than by a dark inside.
    Returns (cx, cy, r, rms, contrast) in pixels, contrast: the step at the rim in
    robust standard deviations of the face around the hole; None where it cannot
    be measured."""
    h, w = img.shape
    rho = np.arange(0.0, 2.2 * r0, 0.5)
    th = np.linspace(0, 2 * np.pi, RAYS, endpoint=False)
    X = (cx + np.cos(th)[:, None] * rho).astype(np.float32)
    Y = (cy + np.sin(th)[:, None] * rho).astype(np.float32)
    if X.min() < 0 or Y.min() < 0 or X.max() > w - 1 or Y.max() > h - 1:
        return None
    if (cv2.remap(ok.astype(np.float32), X, Y, cv2.INTER_NEAREST) < 0.5).mean() > 0.05:
        return None
    sm = cv2.GaussianBlur(img.astype(np.float32), (0, 0), max(1.0, min(RIM_SMOOTH_MM / res, 0.15 * r0)))
    I0 = cv2.remap(sm, X, Y, cv2.INTER_LINEAR)
    _, spread = _robust(I0[:, rho >= 1.6 * r0].ravel())  # the face, past a lit rim (which reaches ~1.5 r)
    i_in, i_lo, i_hi = (int(np.searchsorted(rho, f * r0)) for f in (0.35, 0.5, 1.6))
    best = None
    n = len(rho)
    at = np.arange(n)[None, :]
    rows = np.arange(RAYS)
    for I in (I0, -I0):
        hole = float(np.median(I[:, :i_in]))
        pk = i_lo + np.argmax(I[:, i_lo:i_hi], axis=1)  # most face-like point per ray
        top = I[rows, pk]
        level = 0.5 * (hole + top)
        # walking in from that point: the last sample below mid-level
        below = (I < level[:, None]) & (at > i_in) & (at <= pk[:, None])
        i = n - 1 - np.argmax(below[:, ::-1], axis=1)
        use = below.any(1) & (i != pk) & (i < n - 1)
        if use.sum() < 0.6 * RAYS:
            continue
        i, k = i[use], rows[use]
        f = (level[use] - I[k, i]) / np.maximum(I[k, i + 1] - I[k, i], 1e-6)
        d = rho[i] + f * (rho[i + 1] - rho[i])
        pts = np.c_[cx + d * np.cos(th[k]), cy + d * np.sin(th[k])]
        circle = _ransac_circle(pts, max(1.5, ROUND_TOL * r0))
        if circle is None:
            continue
        c, r, dev, keep = circle
        fit = (float(c[0]), float(c[1]), r, float(np.sqrt(np.mean(dev[keep] ** 2))),
               float(np.median((top - hole)[use][keep]) / spread))
        if best is None or fit[4] > best[4]:
            best = fit
    return best


def _measure(stack, ok, cx, cy, r0, res):
    """The hole at (cx, cy) of rough radius r0, measured in every photo on its own;
    kept when MIN_VIEWS photos agree on its size and roughly on its place (the largest
    group that agrees: on a lit rim some photos lock onto its outer edge instead;
    where the photos could not be lined up well, the same hole sits up to ~1 mm apart). Twice: the
    second time around the first answer. Returns (cx, cy, r, sd_r, rms, photos) or None."""
    for _ in range(2):
        fits = [f for f in (_rim(s, o, cx, cy, r0, res) for s, o in zip(stack, ok)) if f is not None]
        F = np.array([f for f in fits if f[4] >= RIM_CONTRAST and f[3] <= max(1.5, ROUND_TOL * f[2])]).reshape(-1, 5)
        if len(F) < MIN_VIEWS:
            return None
        best = None
        for x, y, r, _, _ in F:
            group = F[(np.hypot(F[:, 0] - x, F[:, 1] - y) < max(1.5, AGREE_PLACE * r)) & (np.abs(F[:, 2] - r) < max(1.5, AGREE_SIZE * r))]
            if best is None or (len(group), -group[:, 3].sum()) > (len(best), -best[:, 3].sum()):
                best = group
        if len(best) < MIN_VIEWS:
            return None
        cx, cy, r0 = (float(v) for v in np.median(best[:, :3], axis=0))
    return cx, cy, r0, float(np.std(best[:, 2])), float(np.median(best[:, 3])), len(best)


def _circles(stack: np.ndarray, ok: np.ndarray, res: float, inner: np.ndarray,
             half_px: float) -> list[tuple[float, float, float, float, float, int]]:
    """Holes in a face from its lined-up orthophotos (n, h, w), each normalised;
    ok: where each photo has data; inner: the face's measured extent.

    Candidates come from two cues (disagreement between photos - the inside of a
    hole lies below the face and shifts with the viewpoint; brightness - a dark
    or see-through opening) and two standard detectors (see _candidates). Each
    is then measured photo by photo (_measure) and must be: centred in the face,
    round (not a rounded polygon, not the end of a longer opening), seen whole,
    with depth. Of two overlapping openings: concentric, the inner one is the
    bore (the outer, a recess or a chamfer); otherwise the larger one is kept
    (the smaller is part of it, or something seen through it).
    Returns (x, y, diameter, sd of the diameter between photos, rim scatter,
    photos), in pixels."""
    valid = ok.all(0)
    M = np.median(stack, axis=0)
    spread = np.median(np.abs(stack - M), axis=0) if len(stack) > 2 else np.abs(stack[0] - stack[1]) / 2
    sig = max(1.5, 0.3 / res)
    S = cv2.GaussianBlur(spread.astype(np.float32), (0, 0), sig)
    B = cv2.GaussianBlur(M.astype(np.float32), (0, 0), sig)
    s_med, s_mad = _robust(S[valid])
    b_med, b_mad = _robust(B[valid])
    # How hole-like each pixel is, in robust standard deviations: the stronger cue.
    # Disagreement needs 3+ photos: between two it is mostly noise.
    cue = np.abs(B - b_med) / b_mad
    if len(stack) >= 3:
        cue = np.maximum(cue, (S - s_med) / s_mad)
    cue[~valid] = 0
    rmin, rmax = max(MIN_PX / 2, 0.5 * MIN_D_MM / res), half_px
    found = []
    for cx, cy, r in _candidates(stack, ok, valid, M, cue, inner & valid, rmin, rmax):
        if any(np.hypot(cx - f[0], cy - f[1]) < 0.3 * f[2] and abs(r - f[6]) < 0.3 * f[6] for f in found):
            continue  # this candidate was measured already
        m = _measure(stack, ok, cx, cy, r, res)
        if m is None:
            found.append((cx, cy, r, 0.0, 0.0, 0, r, False))
            continue
        x, y, rr, sd, rms, n = m
        good = (2 * rr * res >= MIN_D_MM and inner[int(round(y)), int(round(x))]
                and not _not_round(M, valid, x, y, rr) and _ring_seen(valid, x, y, rr)
                and _has_depth(S, s_med, s_mad, x, y, rr))
        found.append((x, y, rr, sd, rms, n, r, good))
    holes = []
    for h in sorted([f[:6] for f in found if f[7]], key=lambda f: (-f[5], f[3])):  # most photos first
        if not any(np.hypot(h[0] - k[0], h[1] - k[1]) < 0.5 * max(h[2], k[2]) and abs(h[2] - k[2]) < 0.25 * max(h[2], k[2])
                   for k in holes):
            holes.append(h)  # not the same hole again
    drop = set()
    for i, a in enumerate(holes):  # overlapping openings
        for j, b in enumerate(holes):
            d = np.hypot(a[0] - b[0], a[1] - b[1])
            if b[2] < a[2] and d < a[2] + b[2]:
                # concentric: a recess or chamfer around a bore (keep the bore);
                # otherwise one opening, or something seen through it (keep the larger)
                drop.add(i if d < 0.3 * a[2] else j)
    return [(x, y, 2 * r, 2 * sd, rms, n) for i, (x, y, r, sd, rms, n) in enumerate(holes) if i not in drop]


def find_holes(planes, extents, photos: list[Photo], log=print) -> list[Hole]:
    """Round holes on every face that has an extent (see features.face_extent)."""
    images = _Images()
    holes: list[Hole] = []
    for fi, (p, e) in enumerate(zip(planes, extents)):
        if e is None or min(e["half"]) < MIN_HALF_MM:
            continue
        c, u, v, (hu, hv) = np.array(e["centre"]), np.array(e["u"]), np.array(e["v"]), e["half"]
        views = _views_for(p.normal, c, photos)
        if DEBUG_DIR:
            log(f"face {fi}: normal {np.round(p.normal, 2).tolist()} half {np.round(e['half'], 1).tolist()} mm, {len(views)} photos")
        if len(views) < MIN_VIEWS:
            continue
        ppm = float(np.median([s for s, _ in views]))  # photo pixels per mm on this face
        res = max(1.0 / ppm, 2.3 * max(hu, hv) / ORTHO_MAX_PX)  # mm per orthophoto pixel
        # A margin beyond the face's measured extent, so a hole near the face's end
        # is seen whole (the jaw's side holes); the ring test below keeps edges out.
        mu, mv = hu + max(2.0, 0.15 * hu), hv + max(2.0, 0.15 * hv)
        a = np.arange(-mu, mu, res) + res / 2
        b = np.arange(-mv, mv, res) + res / 2
        grid = c + a[None, :, None] * u + b[:, None, None] * v
        stack = []
        for _, ph in views:
            img = images(ph.path)
            if img is None:
                continue
            O = _ortho(img, ph, grid)
            if DEBUG_DIR:  # PHONE2CAD_HOLES_DEBUG=folder: save each face orthophoto
                Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(Path(DEBUG_DIR) / f"face{fi:02d}_{ph.path.stem}.png"), np.clip(O, 0, 255).astype(np.uint8))
            stack.append(O)
        if len(stack) < MIN_VIEWS:
            continue
        stack = np.array(stack, np.float32)
        ok = stack >= 0
        if ok.all(0).sum() < 100:
            continue
        for i in range(len(stack)):  # same brightness scale in every photo
            m, d = _robust(stack[i][ok[i]])
            stack[i] = np.where(ok[i], (stack[i] - m) / d, 0)
        _register(stack, ok, res)
        X, Y = np.meshgrid(a, b)
        inner = (np.abs(X) <= hu) & (np.abs(Y) <= hv)  # the face's measured extent, without the margin
        for x, y, d, sd, rms, n in _circles(stack, ok, res, inner, min(hu, hv) / res):
            # spread between photos, pixel size, the systematic rim offset, and how far the
            # opening is from a circle (a printed thread: its depth makes "the" diameter vague)
            pm = 2 * float(np.sqrt((sd * res) ** 2 / n + (0.5 * res) ** 2 + SYSTEMATIC_MM ** 2 + (rms * res) ** 2))
            centre = c + u * (a[0] + x * res) + v * (b[0] + y * res)
            holes.append(Hole(centre, p.normal.copy(), d * res, pm, n, fi))
    # A through-hole shows on both of its faces: keep the surer one.
    holes.sort(key=lambda h: h.pm)
    kept: list[Hole] = []
    for h in holes:
        dup = any(abs(h.normal @ k.normal) > 0.95 and abs(h.diameter - k.diameter) < max(0.5, 0.2 * h.diameter)
                  and np.linalg.norm(np.cross(h.centre - k.centre, k.normal)) < max(0.5, 0.25 * h.diameter)
                  for k in kept)
        if not dup:
            kept.append(h)
    log(f"holes on faces: {len(kept)} found ({len(holes)} before merging through-holes)")
    return kept


def hole_features(holes: list[Hole], grade) -> list[dict]:
    """Measurement entries for the phone: each hole, and the spacing of holes on one face."""
    feats = []
    for k, h in enumerate(holes):
        feats.append({
            "id": f"face-hole-{k + 1}", "kind": "hole", "label": "Hole", "value": round(h.diameter, 3),
            "pm": round(h.pm, 3), "text": f"Hole Ø{h.diameter:.1f} ±{h.pm:.1f} mm", "grade": grade(h.pm),
            "detail": f"round opening in a flat face, seen in {h.photos} photos",
            "geometry": {"axis": h.normal.round(5).tolist(), "point": (h.centre - h.normal).round(3).tolist(),
                         "radius": round(h.diameter / 2, 4), "length": 2.0},
        })
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            a, b = holes[i], holes[j]
            if a.face != b.face:
                continue
            dist = float(np.linalg.norm(a.centre - b.centre))
            pm = float(np.hypot(a.pm, b.pm)) / 2
            feats.append({
                "id": f"hole-spacing-{i + 1}-{j + 1}", "kind": "distance", "label": "Hole to hole",
                "value": round(dist, 3), "pm": round(pm, 3), "text": f"Hole to hole {dist:.1f} ±{pm:.1f} mm",
                "grade": grade(pm), "detail": "centre to centre",
                "geometry": {"points": [a.centre.round(3).tolist(), b.centre.round(3).tolist()]},
            })
    return feats
