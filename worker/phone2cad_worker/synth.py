"""Synthetic frame sets for the acceptance test.

Ray-traces the printed mat (exact same drawing as the PDF) with the test
block from ``artefacts/test_block.scad`` standing on it, from a ring of
phone-like viewpoints. Output looks like an uploaded job: ``frames/*.jpg``
plus ``truth.json`` (camera model and poses, used only for debugging).

    python -m phone2cad_worker.synth OUT_DIR [--frames 64] [--scad PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .mat import DEFAULT_MAT, PAGE_H, PAGE_W, MatSpec

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SCAD = REPO / "artefacts" / "test_block.scad"


@dataclass
class Block:
    x: float
    y: float
    z: float
    hole_d: float
    hole_x: float
    hole_y: float
    centre: tuple[float, float] = (0.0, 0.0)  # world XY of the block centre
    yaw_deg: float = 0.0

    @classmethod
    def from_scad(cls, path: Path = DEFAULT_SCAD, **kw) -> "Block":
        text = Path(path).read_text()
        vals = {k: float(v) for k, v in re.findall(r"^\s*(\w+)\s*=\s*([-\d.]+)\s*;", text, re.M)}
        return cls(vals["block_x"], vals["block_y"], vals["block_z"], vals["hole_d"],
                   vals["hole_x"], vals["hole_y"], **kw)

    def world_to_local(self, p: np.ndarray, is_dir: bool = False) -> np.ndarray:
        c, s = math.cos(math.radians(self.yaw_deg)), math.sin(math.radians(self.yaw_deg))
        q = p if is_dir else p - np.array([self.centre[0], self.centre[1], 0.0])
        lx = c * q[..., 0] + s * q[..., 1]
        ly = -s * q[..., 0] + c * q[..., 1]
        out = np.stack([lx, ly, q[..., 2]], axis=-1)
        if not is_dir:
            out[..., 0] += self.x / 2
            out[..., 1] += self.y / 2
        return out

    def local_to_world_dir(self, d: np.ndarray) -> np.ndarray:
        c, s = math.cos(math.radians(self.yaw_deg)), math.sin(math.radians(self.yaw_deg))
        return np.stack([c * d[..., 0] - s * d[..., 1], s * d[..., 0] + c * d[..., 1], d[..., 2]], axis=-1)


@dataclass
class Camera:
    width: int = 1600
    height: int = 1200
    f: float = 1400.0
    k1: float = -0.06
    k2: float = 0.03
    p1: float = 0.0005
    p2: float = -0.0003

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.f, 0, (self.width - 1) / 2 + 3.0], [0, self.f, (self.height - 1) / 2 - 2.0], [0, 0, 1]])

    @property
    def dist(self) -> np.ndarray:
        return np.array([self.k1, self.k2, self.p1, self.p2, 0.0])


# ---- procedural texture ----------------------------------------------------

def _hash3(i: np.ndarray, j: np.ndarray, k: np.ndarray, seed: int) -> np.ndarray:
    h = (i * 73856093) ^ (j * 19349663) ^ (k * 83492791) ^ (seed * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return (h & 0xFFFF).astype(np.float32) / 65535.0


def value_noise(p: np.ndarray, cell: float, seed: int) -> np.ndarray:
    q = p / cell
    i0 = np.floor(q).astype(np.int64)
    f = q - i0
    f = f * f * (3 - 2 * f)
    out = np.zeros(p.shape[:-1], np.float32)
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = (f[..., 0] if dx else 1 - f[..., 0]) * (f[..., 1] if dy else 1 - f[..., 1]) * (f[..., 2] if dz else 1 - f[..., 2])
                out += w * _hash3(i0[..., 0] + dx, i0[..., 1] + dy, i0[..., 2] + dz, seed)
    return out


def block_albedo(p_local: np.ndarray) -> np.ndarray:
    n = 0.6 * value_noise(p_local, 1.1, 1) + 0.4 * value_noise(p_local, 0.45, 2)
    speck = np.clip((n - 0.52) / 0.12, 0, 1)
    return 0.82 - 0.62 * speck


# ---- renderer ----------------------------------------------------------------

class Scene:
    def __init__(self, block: Block, mat: MatSpec = DEFAULT_MAT, px_per_mm: float = 12.0):
        self.block, self.mat, self.ppm = block, mat, px_per_mm
        self.tex = mat.to_raster(px_per_mm)
        self.light = np.array([0.35, -0.45, 0.82]) / np.linalg.norm([0.35, -0.45, 0.82])

    def _mat_albedo(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        px, py = self.mat.world_to_page(x, y)
        u, v = px * self.ppm - 0.5, py * self.ppm - 0.5
        on_page = (px >= 0) & (px < PAGE_W) & (py >= 0) & (py < PAGE_H)
        h, w = self.tex.shape
        u, v = np.clip(u, 0, w - 1.001), np.clip(v, 0, h - 1.001)
        iu, iv = u.astype(np.int64), v.astype(np.int64)
        fu, fv = u - iu, v - iv
        t = self.tex
        page = ((1 - fv) * ((1 - fu) * t[iv, iu] + fu * t[iv, iu + 1])
                + fv * ((1 - fu) * t[iv + 1, iu] + fu * t[iv + 1, iu + 1]))
        table = 0.30 + 0.12 * value_noise(np.stack([x, y, np.zeros_like(x)], -1), 4.0, 9)
        return np.where(on_page, 0.92 * page + 0.03, table)

    def shade(self, o: np.ndarray, d: np.ndarray) -> np.ndarray:
        """o: (3,) camera centre, d: (N,3) unit ray directions, world frame."""
        b = self.block
        ol, dl = b.world_to_local(o[None, :])[0], b.world_to_local(d, is_dir=True)
        eps = 1e-12
        dsafe = np.where(np.abs(dl) < eps, eps, dl)
        lo, hi = np.zeros(3), np.array([b.x, b.y, b.z])
        ta, tb = (lo - ol) / dsafe, (hi - ol) / dsafe
        tmin, tmax = np.minimum(ta, tb), np.maximum(ta, tb)
        t0, axis = tmin.max(axis=1), tmin.argmax(axis=1)
        t1 = tmax.min(axis=1)
        hit_box = (t1 >= t0) & (t0 > 0)

        r = b.hole_d / 2
        ox, oy = ol[0] - b.hole_x, ol[1] - b.hole_y
        a = dl[:, 0] ** 2 + dl[:, 1] ** 2
        bb = 2 * (ox * dl[:, 0] + oy * dl[:, 1])
        c = ox * ox + oy * oy - r * r
        disc = bb * bb - 4 * a * c
        sq = np.sqrt(np.maximum(disc, 0))
        asafe = np.where(a < eps, eps, a)
        tc0 = np.where(disc > 0, (-bb - sq) / (2 * asafe), np.inf)
        tc1 = np.where(disc > 0, (-bb + sq) / (2 * asafe), -np.inf)
        inside = (t0 > tc0) & (t0 < tc1)
        face = hit_box & ~inside
        wall = hit_box & inside & (tc1 < t1)

        n = len(d)
        t = np.full(n, np.inf)
        normal_l = np.zeros((n, 3))
        t[face] = t0[face]
        ax = axis[face]
        normal_l[face, ax] = -np.sign(dl[face, ax])
        t[wall] = tc1[wall]
        pw = ol[None, :] + tc1[wall, None] * dl[wall]
        normal_l[wall, 0] = -(pw[:, 0] - b.hole_x) / r
        normal_l[wall, 1] = -(pw[:, 1] - b.hole_y) / r

        out = np.empty(n, np.float32)
        solid = face | wall
        p_local = ol[None, :] + t[solid, None] * dl[solid]
        nw = b.local_to_world_dir(normal_l[solid])
        lam = np.clip(nw @ self.light, 0, 1)
        occl = np.ones(solid.sum())
        wsel = wall[solid]
        occl[wsel] = 0.45 + 0.55 * np.clip(p_local[wsel, 2] / b.z, 0, 1)
        out[solid] = block_albedo(p_local) * (0.40 + 0.60 * lam) * occl

        rest = ~solid
        tp = -o[2] / np.where(d[rest, 2] < -eps, d[rest, 2], -eps)
        pp = o[None, :] + tp[:, None] * d[rest]
        hole_floor = hit_box[rest]  # passed through the hole: darker floor
        alb = self._mat_albedo(pp[:, 0], pp[:, 1])
        out[rest] = alb * (0.40 + 0.60 * self.light[2]) * np.where(hole_floor, 0.55, 1.0)
        return out

    def _rays(self, cam: Camera, ss: int) -> np.ndarray:
        """Camera-frame ray directions for a supersampled pixel grid (cached)."""
        key = (tuple(cam.K.ravel()), tuple(cam.dist), cam.width, cam.height, ss)
        if getattr(self, "_ray_key", None) != key:
            offs = (np.arange(ss) + 0.5) / ss - 0.5
            us = (np.arange(cam.width)[:, None] + offs[None, :]).ravel()
            vs = (np.arange(cam.height)[:, None] + offs[None, :]).ravel()
            uu, vv = np.meshgrid(us, vs)
            und = undistort(np.stack([uu.ravel(), vv.ravel()], -1), cam.K, cam.dist)
            d = np.concatenate([und, np.ones((len(und), 1))], 1).astype(np.float32)
            self._rays_cam = d / np.linalg.norm(d, axis=1, keepdims=True)
            self._ray_key = key
        return self._rays_cam

    def render(self, cam: Camera, R_cw: np.ndarray, C: np.ndarray, ss: int = 2, chunk_rows: int = 150,
               rng: np.random.Generator | None = None) -> np.ndarray:
        rng = rng or np.random.default_rng(0)
        rays = self._rays(cam, ss)
        img = np.zeros((cam.height, cam.width), np.float32)
        row = cam.width * ss * ss
        for y0 in range(0, cam.height, chunk_rows):
            y1 = min(cam.height, y0 + chunk_rows)
            dw = rays[y0 * row:y1 * row].astype(np.float64) @ R_cw
            img[y0:y1] = self.shade(C, dw).reshape(y1 - y0, ss, cam.width, ss).mean(axis=(1, 3))
        img = cv2.GaussianBlur(img, (0, 0), 0.55)
        img += rng.normal(0, 0.012, img.shape).astype(np.float32)
        return np.clip(img, 0, 1)


def undistort(px: np.ndarray, K: np.ndarray, dist: np.ndarray, iters: int = 30) -> np.ndarray:
    """Pixel -> normalized pinhole coords for OpenCV's (k1, k2, p1, p2) model."""
    k1, k2, p1, p2 = dist[:4]
    xd = (px[:, 0] - K[0, 2]) / K[0, 0]
    yd = (px[:, 1] - K[1, 2]) / K[1, 1]
    x, y = xd.copy(), yd.copy()
    for _ in range(iters):
        r2 = x * x + y * y
        rad = 1 + k1 * r2 + k2 * r2 * r2
        dx = 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
        dy = p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
        x, y = (xd - dx) / rad, (yd - dy) / rad
    return np.stack([x, y], -1)


def look_at(C: np.ndarray, target: np.ndarray, roll_deg: float = 0.0) -> np.ndarray:
    fwd = target - C
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    R = np.stack([right, down, fwd])
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    roll = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return roll @ R


def viewpoints(n: int, target: np.ndarray, rng: np.random.Generator) -> list[tuple[np.ndarray, np.ndarray]]:
    rings = [(32, 0.30), (47, 0.30), (62, 0.25), (75, 0.15)]
    out = []
    for elev, share in rings:
        k = max(4, round(n * share))
        az0 = rng.uniform(0, 360)
        for i in range(k):
            if len(out) >= n:
                break
            az = math.radians(az0 + 360 * i / k + rng.normal(0, 3))
            el = math.radians(elev + rng.normal(0, 2.5))
            dist = rng.uniform(190, 240)
            C = target + dist * np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
            tgt = target + rng.normal(0, 6, 3) * np.array([1, 1, 0.3])
            out.append((look_at(C, tgt, rng.normal(0, 8)), C))
    return out


_W: dict = {}


def _render_one(args):
    i, R, C, out = args
    scene, cam = _W["scene"], _W["cam"]
    img = scene.render(cam, R, C, rng=np.random.default_rng(1000 + i))
    cv2.imwrite(str(out / f"{i:04d}.jpg"), (img * 255 + 0.5).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 92])


def _init_worker(block, cam):
    _W["scene"], _W["cam"] = Scene(block), cam


def make_job(out_dir: Path, n_frames: int = 64, scad: Path = DEFAULT_SCAD, seed: int = 3,
             cam: Camera | None = None, processes: int = 4) -> dict:
    out_dir = Path(out_dir)
    frames = out_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    px0, px1, py0, py1 = DEFAULT_MAT.part_zone_world
    block = Block.from_scad(scad, centre=((px0 + px1) / 2 + 4, (py0 + py1) / 2 - 3), yaw_deg=23.0)
    cam = cam or Camera()
    target = np.array([block.centre[0], block.centre[1], block.z * 0.4])
    views = viewpoints(n_frames, target, rng)
    jobs = [(i, R, C, frames) for i, (R, C) in enumerate(views)]
    if processes > 1:
        import multiprocessing as mp
        with mp.get_context("fork").Pool(processes, _init_worker, (block, cam)) as pool:
            pool.map(_render_one, jobs, chunksize=1)
    else:
        _init_worker(block, cam)
        for j in jobs:
            _render_one(j)
    poses = [{"file": f"{i:04d}.jpg", "R_cw": R.tolist(), "C": C.tolist()} for i, (R, C) in enumerate(views)]
    truth = {"block": asdict(block), "camera": asdict(cam), "K": cam.K.tolist(), "poses": poses}
    (out_dir / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--frames", type=int, default=64)
    ap.add_argument("--scad", type=Path, default=DEFAULT_SCAD)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    make_job(a.out, a.frames, a.scad, a.seed)


if __name__ == "__main__":
    main()
