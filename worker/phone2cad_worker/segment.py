"""The part's outline in each frame, by a pretrained segmentation model.

SAM 2.1 (Meta's Segment Anything 2.1, Hiera-tiny; Apache-2.0 weights, ONNX
export by vietanhdev/segment-anything-2.1-onnx-models) run with ONNX Runtime on
the CPU. This is what current photogrammetry tools use for object masks
(Meshroom's SAM segmentation nodes, PIX4Dmatic, SlicerMorph PhotoMasking): it
cuts out the object whatever its colour, gloss or texture, where hand-made
brightness rules against the mat failed (black parts, torch glints, a part in
front of the printed border).

The prompts come from the 3D scene: the part's box projected into the frame,
some of the part's own 3D points (positive clicks) and some mat points beside
it (negative clicks). SAM runs on a crop around the part, so a small part in an
overview frame still gets the model's full resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from .calib import Calibration
from .silhouette import ViewMask

MODEL_DIRS = [os.environ.get("PHONE2CAD_SAM_DIR", ""), "/models/sam21_tiny",
              str(Path.home() / ".cache" / "phone2cad" / "models" / "sam21_tiny")]
MASK_SIDE = 2000  # px, longest side of the undistorted frames the masks are made on
CROP_MARGIN = 0.35  # crop around the projected box, as a share of its size
BG_LOGIT = float(os.environ.get("PHONE2CAD_SAM_BG_LOGIT", "-2.0"))  # surely not the part below this (0 = 50 %; sweep 2026-10-13)
EDGE_PX = int(os.environ.get("PHONE2CAD_SAM_EDGE_PX", "1"))  # pixels next to the outline stay unknown
CORE_INSIDE = 0.8  # share of the part's projected 3D points an outline must hold
MEAN, STD = np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])


def model_dir() -> Path | None:
    for d in MODEL_DIRS:
        if d and (Path(d) / "sam2.1_hiera_tiny.encoder.onnx").exists():
            return Path(d)
    return None


class Segmenter:
    """SAM 2.1 tiny: encode an image once, then decode prompts (cheap)."""

    def __init__(self, folder: Path | None = None):
        # ONNX Runtime's PyPI build reports usage to Microsoft (1DS telemetry: OS,
        # CPU, the Python path with the user's name, a device id), starting when the
        # library loads. Never allowed here: off before the import, and once more after.
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import onnxruntime as ort

        ort.disable_telemetry_events()

        folder = folder or model_dir()
        if folder is None:
            raise RuntimeError("the SAM 2.1 model files are missing (see segment.MODEL_DIRS)")
        so = ort.SessionOptions()
        so.log_severity_level = 3
        cpu = ["CPUExecutionProvider"]
        self.enc = ort.InferenceSession(str(folder / "sam2.1_hiera_tiny.encoder.onnx"), so, providers=cpu)
        self.dec = ort.InferenceSession(str(folder / "sam2.1_hiera_tiny.decoder.onnx"), so, providers=cpu)

    def logits(self, img_rgb: np.ndarray, box, pos=(), neg=(), prefer_large: bool = False) -> np.ndarray:
        """SAM logits (h, w) for img_rgb (any size) with a box (x0, y0, x1, y1)
        and positive / negative points, all in img_rgb pixels."""
        h, w = img_rgb.shape[:2]
        x = cv2.resize(img_rgb, (1024, 1024), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
        x = ((x - MEAN) / STD).astype(np.float32).transpose(2, 0, 1)[None]
        f0, f1, emb = self.enc.run(None, {"image": x})
        sx, sy = 1024 / w, 1024 / h
        if box is not None:
            pts = [(box[0], box[1]), (box[2], box[3])] + list(pos) + list(neg)
            lab = [2, 3] + [1] * len(pos) + [0] * len(neg)
        else:  # clicks only (SAM's convention: a padding point labelled -1)
            pts = list(pos) + list(neg) + [(0.0, 0.0)]
            lab = [1] * len(pos) + [0] * len(neg) + [-1]
        coords = (np.array(pts, np.float32) * [sx, sy]).astype(np.float32)[None]
        masks, iou = self.dec.run(None, {
            "image_embed": emb, "high_res_feats_0": f0, "high_res_feats_1": f1,
            "point_coords": coords, "point_labels": np.array([lab], np.float32),
            "mask_input": np.zeros((1, 1, 256, 256), np.float32), "has_mask_input": np.zeros(1, np.float32)})
        # Of SAM's three proposals: holds the part's clicks, leaves out the paper's (then its own score).
        def share(m, pts):
            return np.mean([m[int(np.clip(v, 0, h - 1)), int(np.clip(u, 0, w - 1))] > 0 for u, v in pts]) if len(pts) else 0.0
        best, best_score = 0, -9.0
        for k in range(masks.shape[1]):
            m = cv2.resize(masks[0, k], (w, h), interpolation=cv2.INTER_LINEAR)
            score = (share(m, pos) if len(pos) else 1.0) - 2.0 * share(m, neg) + 0.1 * float(iou[0, k])
            if prefer_large:  # the whole object rather than one of its faces
                score += 0.3 * float((m > 0).mean())
            if score > best_score:
                best, best_score = k, score
        return cv2.resize(masks[0, best], (w, h), interpolation=cv2.INTER_LINEAR)


def _filled(part: np.ndarray) -> np.ndarray:
    """The part with everything inside its outer outline: SAM's unsure speckles
    inside a body are never background (a through-hole is measured from the
    photos instead, see holes.py)."""
    cs, _ = cv2.findContours(part.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = np.zeros(part.shape, np.uint8)
    cv2.drawContours(out, cs, -1, 1, -1)
    return out.astype(bool)


def _project(P: np.ndarray, K, R, t) -> tuple[np.ndarray, np.ndarray]:
    Xc = P @ R.T + t
    z = Xc[:, 2]
    uv = np.c_[K[0, 0] * Xc[:, 0] / np.maximum(z, 1e-6) + K[0, 2], K[1, 1] * Xc[:, 1] / np.maximum(z, 1e-6) + K[1, 2]]
    return uv, z > 1e-3


def _box_corners(box) -> np.ndarray:
    lo, hi = box
    return np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])


class Outliner:
    """Per frame: the undistorted frame at MASK_SIDE, the camera for it, and
    SAM's outline of the part given the part's 3D box and points."""

    def __init__(self, cal: Calibration, seg: Segmenter):
        self.cal, self.seg = cal, seg
        self.record: list | None = None  # testing: (K, R, t, logits) of every accepted outline

    def frame(self, image: np.ndarray, name: str):
        cal = self.cal
        s = MASK_SIDE / max(image.shape[:2])
        small = cv2.resize(image, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        K = np.diag([s, s, 1.0]) @ cal.K
        K[0, 2], K[1, 2] = (cal.K[0, 2] + 0.5) * s - 0.5, (cal.K[1, 2] + 0.5) * s - 0.5
        und = cv2.undistort(small, K, np.r_[cal.dist[:4], 0.0])
        R, t = cal.poses[name]
        return und, K, R, t

    def outline2d(self, und, K, R, t, box2d, pos=(), neg=(), clicks_only: bool = False,
                  check=None) -> tuple[ViewMask, np.ndarray] | None:
        """SAM on a crop around box2d (x0, y0, x1, y1, undistorted-frame px) with
        positive / negative clicks (same px). None when the mask is not believable:
        it takes in the paper clicks, or spills far beyond the box."""
        h, w = und.shape[:2]
        x0, y0, x1, y1 = (int(v) for v in box2d)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w - 1), min(y1, h - 1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        margin = 1.0 if clicks_only else CROP_MARGIN  # clicks only: the part may reach well beyond the box
        mx, my = int(margin * (x1 - x0)) + 16, int(margin * (y1 - y0)) + 16
        cx0, cy0, cx1, cy1 = max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my)
        crop = cv2.cvtColor(und[cy0:cy1, cx0:cx1], cv2.COLOR_BGR2RGB)
        inside = lambda pts: [(u - cx0, v - cy0) for u, v in pts if cx0 <= u < cx1 and cy0 <= v < cy1]  # noqa: E731
        pos_c, neg_c = inside(pos), inside(neg)
        box_c = None if clicks_only else (x0 - cx0, y0 - cy0, x1 - cx0, y1 - cy0)
        logit = self.seg.logits(crop, box_c, pos_c, neg_c, prefer_large=True)
        part_c = logit > 0
        if clicks_only and part_c.any():  # tighten: once more, boxed by what SAM found
            ys, xs = np.nonzero(part_c)
            pad = int(0.05 * max(np.ptp(xs), np.ptp(ys))) + 4
            tb = (max(xs.min() - pad, 0), max(ys.min() - pad, 0), min(xs.max() + pad, part_c.shape[1] - 1),
                  min(ys.max() + pad, part_c.shape[0] - 1))
            logit = self.seg.logits(crop, tb, pos_c, neg_c)
            part_c = logit > 0
        if len(neg_c) >= 4 and np.mean([part_c[int(v), int(u)] for u, v in neg_c]) > 0.25:
            return None  # took in the paper around the part
        if part_c.sum() > (6.0 if clicks_only else 2.5) * (x1 - x0 + 1) * (y1 - y0 + 1):
            return None  # spilled far beyond the box
        full = np.full((h, w), -20.0, np.float32)  # outside the crop: far from the part, surely not it
        full[cy0:cy1, cx0:cx1] = logit
        if self.record is not None:
            self.record.append((K, R, t, full.astype(np.float16)))
        part = _filled(full > 0)
        if check is not None and len(check):
            # The part's own 3D points must fall inside the outline: SAM sometimes
            # outlines only a piece (the toothed cap of the jaw); such a frame
            # would carve the rest away. Left out instead.
            u, v = np.round(check[:, 0]).astype(int), np.round(check[:, 1]).astype(int)
            ok = (u >= 0) & (v >= 0) & (u < w) & (v < h)
            if ok.sum() >= 10 and part[v[ok], u[ok]].mean() < CORE_INSIDE:
                return None
        sure_bg = (full < BG_LOGIT) & ~part
        if EDGE_PX:
            sure_bg &= ~cv2.dilate(part.astype(np.uint8), np.ones((2 * EDGE_PX + 1,) * 2, np.uint8)).astype(bool)
        return ViewMask(sure_bg, K, R, t), part

    def __call__(self, image: np.ndarray, name: str, box, part_pts=None, mat_pts=None,
                 n_pos: int = 16, n_neg: int = 8, extra_pts=None, n_extra: int = 8) -> tuple[ViewMask, np.ndarray] | None:
        """Outline prompted by the part's 3D box and points (projected). None when
        the box is not in this frame or the mask is not believable. extra_pts: more
        of the part's points, n_extra of them clicked wherever they fall in the frame
        (a piece other outlines left out)."""
        und, K, R, t = self.frame(image, name)
        h, w = und.shape[:2]
        uv, front = _project(_box_corners(box), K, R, t)
        if not front.all():
            return None
        x0, y0 = np.floor(uv.min(0)).astype(int)
        x1, y1 = np.ceil(uv.max(0)).astype(int)
        if x1 < 0 or y1 < 0 or x0 >= w or y0 >= h:
            return None
        rng = np.random.default_rng(0)

        def pick(P, n, inside_box: bool):
            if P is None or not len(P):
                return []
            q, ok = _project(P, K, R, t)
            ok &= (q[:, 0] >= 0) & (q[:, 0] < w) & (q[:, 1] >= 0) & (q[:, 1] < h)
            inb = (q[:, 0] >= x0) & (q[:, 0] <= x1) & (q[:, 1] >= y0) & (q[:, 1] <= y1)
            q = q[ok & (inb if inside_box else ~inb)]
            return [tuple(map(float, v)) for v in q[rng.choice(len(q), min(n, len(q)), replace=False)]] if len(q) else []

        check = None
        if part_pts is not None and len(part_pts):
            q, okq = _project(part_pts, K, R, t)
            check = q[okq]
        pos = pick(part_pts, n_pos, True)
        if extra_pts is not None and len(extra_pts):
            pos += pick(extra_pts, n_extra, True) + pick(extra_pts, n_extra, False)
        return self.outline2d(und, K, R, t, (x0, y0, x1, y1), pos, pick(mat_pts, n_neg, False), check=check)

    def seed(self, image: np.ndarray, name: str, rough: np.ndarray, page: np.ndarray, mat_pts,
             n_pos: int = 5, n_neg: int = 10) -> tuple[ViewMask, np.ndarray] | None:
        """First find, on an overview frame: rough is a crude part mask (where the
        photo differs from the known print, see silhouette.MaskMaker), page the
        printed sheet. The biggest rough blob well inside the page gives SAM its box
        and clicks (the "detector box, then SAM" pattern of Grounded-SAM)."""
        und, K, R, t = self.frame(image, name)
        inner = cv2.erode(page.astype(np.uint8), np.ones((25, 25), np.uint8)) > 0
        blob = cv2.morphologyEx((rough & inner).astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(blob, connectivity=8)
        if n <= 1:
            return None
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[i, cv2.CC_STAT_AREA] < 200:
            return None
        x, y, bw, bh = stats[i, :4]
        pad = int(0.08 * max(bw, bh)) + 4
        box2d = (x - pad, y - pad, x + bw + pad, y + bh + pad)
        dt = cv2.distanceTransform((lab == i).astype(np.uint8), cv2.DIST_L2, 5)
        ys, xs = np.nonzero(dt > 0.5 * dt.max())
        rng = np.random.default_rng(0)
        k = rng.choice(len(xs), min(n_pos, len(xs)), replace=False)
        pos = [(float(xs[j]), float(ys[j])) for j in k]
        q, ok = _project(mat_pts, K, R, t)
        ok &= (q[:, 0] >= 0) & (q[:, 0] < page.shape[1]) & (q[:, 1] >= 0) & (q[:, 1] < page.shape[0])
        q = q[ok]
        q = q[(q[:, 0] < box2d[0]) | (q[:, 0] > box2d[2]) | (q[:, 1] < box2d[1]) | (q[:, 1] > box2d[3])]
        neg = [tuple(map(float, v)) for v in q[rng.choice(len(q), min(n_neg, len(q)), replace=False)]] if len(q) else []
        return self.outline2d(und, K, R, t, box2d, pos, neg, clicks_only=True)
