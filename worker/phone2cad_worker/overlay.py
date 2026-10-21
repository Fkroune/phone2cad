"""The check picture for the phone: the 3D copy's outline drawn over one real
photo, so the user sees at a glance whether the copy matches the part.

Writes JOB/result/check.jpg (cropped around the part, no metadata: OpenCV
writes none). Frames are deleted when the job ends, so this runs before.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .holes import Photo

MAX_SIDE = 1200  # px, longest side of the written picture
WORK_SIDE = 1600  # px, the photo is reduced to this before drawing
ELEVATION = np.radians(40)  # an oblique view shows the top and two sides
MARGIN = 0.35  # crop margin around the outline, share of its size


def _centre(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return -R.T @ t


def pick(photos: list[Photo], centre: np.ndarray) -> Photo:
    """The photo looking at the part from closest to ELEVATION above the mat."""
    def miss(ph: Photo) -> float:
        d = _centre(ph.R, ph.t) - centre
        return abs(np.arcsin(np.clip(d[2] / max(np.linalg.norm(d), 1e-9), -1, 1)) - ELEVATION)
    return min(photos, key=miss)


def silhouette_mask(V: np.ndarray, T: np.ndarray, ph: Photo, scale: float, shape: tuple[int, int]) -> np.ndarray:
    """Pixels the mesh (vertices V, triangles T) covers in the photo, at `scale`."""
    cam = V @ ph.R.T + ph.t
    uv, _ = cv2.projectPoints(V, cv2.Rodrigues(ph.R)[0], ph.t, ph.K, ph.dist)
    uv = (uv.reshape(-1, 2) * scale).round().astype(np.int32)
    mask = np.zeros(shape, np.uint8)
    front = (cam[:, 2] > 1.0)[T].all(1)
    for tri in uv[T[front]]:
        cv2.fillConvexPoly(mask, tri, 255)
    return mask


def write_check(path: Path, photos: list[Photo], V: np.ndarray, T: np.ndarray, log=print) -> bool:
    """Draw the outline of the mesh (V, T, in the photos' world) over one photo."""
    if not photos or not len(V) or not len(T):
        return False
    ph = pick(photos, V.mean(0))
    img = cv2.imread(str(ph.path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return False
    s = min(1.0, WORK_SIDE / max(img.shape[:2]))
    img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
    mask = silhouette_mask(V, T, ph, s, img.shape[:2])
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        log("check picture: the 3D copy falls outside the chosen photo")
        return False
    w = max(2, round(max(img.shape[:2]) / 400))
    cv2.drawContours(img, contours, -1, (20, 20, 20), w + 2, cv2.LINE_AA)  # dark rim: visible on light parts
    cv2.drawContours(img, contours, -1, (60, 230, 60), w, cv2.LINE_AA)
    x, y, bw, bh = cv2.boundingRect(np.vstack(contours))
    mx, my = round(MARGIN * bw), round(MARGIN * bh)
    x0, y0 = max(0, x - mx), max(0, y - my)
    crop = img[y0:min(img.shape[0], y + bh + my), x0:min(img.shape[1], x + bw + mx)]
    k = min(1.0, MAX_SIDE / max(crop.shape[:2]))
    if k < 1:
        crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return False
    Path(path).write_bytes(buf.tobytes())
    log(f"check picture from {ph.path.name}: outline of the 3D copy over the photo")
    return True
