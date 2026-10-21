"""Phone2CAD A4 ChArUco mat: one definition shared by the PDF generator, the
synthetic renderer, the worker and the web app (via ``mat.json``).

Coordinate frames
-----------------
* Board frame (OpenCV): origin at the board's top-left corner, x right,
  y down the page, millimetres.
* World frame (everything else): origin at the board centre, X right,
  Y towards the top of the page, Z up out of the paper, millimetres.

Only a ring of squares around the edge is printed; the centre stays clear
for the part, a 100 mm scale bar and an ID-1 card outline.

Run ``python -m phone2cad_worker.mat --pdf out.pdf --json mat.json`` to
generate the printable mat and the layout file for the browser.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path

import cv2
import numpy as np

PAGE_W, PAGE_H = 210.0, 297.0  # A4 portrait, mm
ID1_W, ID1_H = 85.60, 53.98  # ISO/IEC 7810 ID-1 card, mm


@dataclass(frozen=True)
class MatSpec:
    squares_x: int = 12
    squares_y: int = 18
    square_mm: float = 15.0
    marker_mm: float = 11.0
    ring: int = 2  # printed squares around the edge
    dictionary: str = "DICT_5X5_250"
    seed: int = 7  # speckle pattern

    # ---- geometry -------------------------------------------------------

    @property
    def board_w(self) -> float:
        return self.squares_x * self.square_mm

    @property
    def board_h(self) -> float:
        return self.squares_y * self.square_mm

    @property
    def page_offset(self) -> tuple[float, float]:
        """Top-left of the board on the page (page frame: origin top-left, y down)."""
        return (PAGE_W - self.board_w) / 2, (PAGE_H - self.board_h) / 2

    def board_to_world(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64)
        out = np.zeros((len(pts), 3))
        out[:, 0] = pts[:, 0] - self.board_w / 2
        out[:, 1] = self.board_h / 2 - pts[:, 1]
        return out

    def world_to_page(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ox, oy = self.page_offset
        return x + self.board_w / 2 + ox, self.board_h / 2 - y + oy

    def in_ring(self, col: int, row: int) -> bool:
        r = self.ring
        return col < r or row < r or col >= self.squares_x - r or row >= self.squares_y - r

    @property
    def clear_zone_world(self) -> tuple[float, float, float, float]:
        """(xmin, xmax, ymin, ymax) of the unprinted centre in world mm."""
        hx = self.board_w / 2 - self.ring * self.square_mm
        hy = self.board_h / 2 - self.ring * self.square_mm
        return -hx, hx, -hy, hy

    @property
    def part_zone_world(self) -> tuple[float, float, float, float]:
        """Where the part goes: the clear zone minus scale bar and card box."""
        xmin, xmax, ymin, ymax = self.clear_zone_world
        return xmin + 5, xmax - 5, ymin + 70, ymax - 25

    # ---- OpenCV objects -------------------------------------------------

    @cached_property
    def cv_dictionary(self):
        return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))

    @cached_property
    def cv_board(self):
        return cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y), self.square_mm, self.marker_mm, self.cv_dictionary
        )

    @cached_property
    def printed_markers(self) -> list[tuple[int, np.ndarray]]:
        """(id, 4x2 board-frame corners) for every marker inside the ring."""
        out = []
        for mid, pts in zip(self.cv_board.getIds().ravel(), self.cv_board.getObjPoints()):
            c = pts[:, :2].mean(axis=0)
            col, row = int(c[0] // self.square_mm), int(c[1] // self.square_mm)
            if self.in_ring(col, row):
                out.append((int(mid), pts[:, :2].astype(np.float64)))
        return out

    @cached_property
    def black_squares(self) -> list[tuple[int, int]]:
        all_marker_cells = set()
        for pts in self.cv_board.getObjPoints():
            c = pts[:, :2].mean(axis=0)
            all_marker_cells.add((int(c[0] // self.square_mm), int(c[1] // self.square_mm)))
        return [
            (col, row)
            for row in range(self.squares_y)
            for col in range(self.squares_x)
            if self.in_ring(col, row) and (col, row) not in all_marker_cells
        ]

    @cached_property
    def corner_world(self) -> np.ndarray:
        """World XYZ of every ChArUco corner id (inner chessboard corners)."""
        return self.board_to_world(self.cv_board.getChessboardCorners()[:, :2])

    @cached_property
    def valid_corner_ids(self) -> np.ndarray:
        """Corner ids whose four surrounding squares are all printed.

        Corners on the inner edge of the ring touch blank paper, so they are
        not true chessboard saddles and must not be used for measurement.
        """
        nx = self.squares_x - 1
        ids = []
        for j in range(self.squares_y - 1):
            for i in range(nx):
                if all(self.in_ring(i + di, j + dj) for di in (0, 1) for dj in (0, 1)):
                    ids.append(j * nx + i)
        return np.array(ids, dtype=np.int32)

    def marker_bits(self, mid: int) -> np.ndarray:
        n = self.cv_dictionary.markerSize + 2
        return self.cv_dictionary.generateImageMarker(mid, n) < 128  # True = black

    # ---- drawing list (shared by PDF + raster backends) -----------------

    def drawing(self) -> list[tuple]:
        """Primitives in page mm (origin top-left, y down).

        ('rect', x, y, w, h, gray)  filled rectangle, gray 0=black 1=white
        ('circle', cx, cy, r, gray) filled disc
        ('text', x, y, size_mm, gray, string)  left baseline
        """
        ox, oy = self.page_offset
        s, items = self.square_mm, []
        for col, row in self.black_squares:
            items.append(("rect", ox + col * s, oy + row * s, s, s, 0.0))
        for mid, pts in self.printed_markers:
            bits = self.marker_bits(mid)
            n = bits.shape[0]
            cell = self.marker_mm / n
            x0, y0 = ox + pts[:, 0].min(), oy + pts[:, 1].min()
            for r in range(n):
                for c in range(n):
                    if bits[r, c]:
                        items.append(("rect", x0 + c * cell, y0 + r * cell, cell, cell, 0.0))

        def outline(x, y, w, h, t=0.35, g=0.0):
            items.extend([
                ("rect", x, y, w, t, g), ("rect", x, y + h - t, w, t, g),
                ("rect", x, y, t, h, g), ("rect", x + w - t, y, t, h, g),
            ])

        cx0, cx1, cy0, cy1 = self.clear_zone_world
        # 100 mm scale bar near the top of the clear zone.
        bx, by = self.world_to_page(np.array(-50.0), np.array(cy1 - 12))
        bx, by = float(bx), float(by)
        items.append(("rect", bx, by, 100.0, 0.5, 0.0))
        for k in range(11):
            h = 4.0 if k % 5 == 0 else 2.5
            items.append(("rect", bx + 10 * k - 0.2, by - h, 0.4, h, 0.0))
        items.append(("text", bx + 36, by + 5.5, 3.5, 0.0, "100 mm"))
        # ID-1 card box near the bottom of the clear zone.
        kx, ky = self.world_to_page(np.array(-ID1_W / 2), np.array(cy0 + 8 + ID1_H))
        kx, ky = float(kx), float(ky)
        outline(kx, ky, ID1_W, ID1_H, 0.3)
        items.append(("text", kx + 4, ky + 8, 3.2, 0.35, "Card check: lay a bank card here"))
        # Light speckle in the part zone: texture for the 3D solver.
        px0, px1, py0, py1 = self.part_zone_world
        rng = np.random.default_rng(self.seed)
        n = int((px1 - px0) * (py1 - py0) / 14)
        xs, ys = rng.uniform(px0, px1, n), rng.uniform(py0, py1, n)
        rs, gs = rng.uniform(0.3, 0.65, n), rng.uniform(0.45, 0.75, n)
        pxs, pys = self.world_to_page(xs, ys)
        for x, y, r, g in zip(pxs, pys, rs, gs):
            items.append(("circle", float(x), float(y), float(r), float(g)))
        zx, zy = self.world_to_page(np.array(px0), np.array(py1))
        outline(float(zx), float(zy), px1 - px0, py1 - py0, 0.25, 0.6)
        items.append(("text", float(zx) + 2, float(zy) - 1.5, 3.0, 0.45, "Place the part here"))
        # Header in the top margin.
        items.append((
            "text", ox, oy - 4.5, 3.2, 0.0,
            f"Phone2CAD mat A4 - print at 100 % (actual size) - squares {self.square_mm:g} mm",
        ))
        return items

    # ---- backends --------------------------------------------------------

    def to_pdf(self, path: Path) -> None:
        pt = 72 / 25.4
        ops = [f"{pt:.6f} 0 0 {-pt:.6f} 0 {PAGE_H * pt:.4f} cm"]
        for it in self.drawing():
            kind = it[0]
            if kind == "rect":
                _, x, y, w, h, g = it
                ops.append(f"{g:.3f} g {x:.4f} {y:.4f} {w:.4f} {h:.4f} re f")
            elif kind == "circle":
                _, x, y, r, g = it
                k = 0.5523 * r
                ops.append(
                    f"{g:.3f} g {x + r:.4f} {y:.4f} m "
                    f"{x + r:.4f} {y + k:.4f} {x + k:.4f} {y + r:.4f} {x:.4f} {y + r:.4f} c "
                    f"{x - k:.4f} {y + r:.4f} {x - r:.4f} {y + k:.4f} {x - r:.4f} {y:.4f} c "
                    f"{x - r:.4f} {y - k:.4f} {x - k:.4f} {y - r:.4f} {x:.4f} {y - r:.4f} c "
                    f"{x + k:.4f} {y - r:.4f} {x + r:.4f} {y - k:.4f} {x + r:.4f} {y:.4f} c f"
                )
            elif kind == "text":
                _, x, y, size, g, s = it
                s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"{g:.3f} g BT /F1 {size:.3f} Tf 1 0 0 -1 {x:.4f} {y:.4f} Tm ({s}) Tj ET")
        content = "\n".join(ops).encode("latin-1")
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W * pt:.4f} {PAGE_H * pt:.4f}] "
             f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>").encode(),
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        # Metadata is deliberately minimal: no author, host or path.
        out, offsets = bytearray(b"%PDF-1.4\n"), []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        Path(path).write_bytes(bytes(out))

    def to_raster(self, px_per_mm: float = 10.0) -> np.ndarray:
        """Grey float32 image of the page (1 = paper), for synthetic renders."""
        ss = 4  # supersample, then area-average
        sc = px_per_mm * ss
        img = np.full((int(round(PAGE_H * sc)), int(round(PAGE_W * sc))), 255, np.uint8)
        for it in self.drawing():
            if it[0] == "rect":
                _, x, y, w, h, g = it
                x0, y0 = int(round(x * sc)), int(round(y * sc))
                x1, y1 = int(round((x + w) * sc)), int(round((y + h) * sc))
                img[y0:y1, x0:x1] = int(round(g * 255))
            elif it[0] == "circle":
                _, x, y, r, g = it
                cv2.circle(img, (int(round(x * sc)), int(round(y * sc))), max(1, int(round(r * sc))), int(round(g * 255)), -1, cv2.LINE_AA)
            elif it[0] == "text":
                _, x, y, size, g, s = it
                cv2.putText(img, s, (int(x * sc), int(y * sc)), cv2.FONT_HERSHEY_SIMPLEX,
                            size * sc / 22.0, int(round(g * 255)), max(1, int(size * sc / 10)), cv2.LINE_AA)
        small = cv2.resize(img, (img.shape[1] // ss, img.shape[0] // ss), interpolation=cv2.INTER_AREA)
        return small.astype(np.float32) / 255.0

    def to_json(self) -> dict:
        """Layout for the browser: marker corners in world mm."""
        return {
            "schema": "phone2cad.mat.v1",
            "page": "A4",
            "dictionary": self.dictionary,
            "squaresX": self.squares_x,
            "squaresY": self.squares_y,
            "squareMm": self.square_mm,
            "markerMm": self.marker_mm,
            "clearZone": list(self.clear_zone_world),
            "partZone": list(self.part_zone_world),
            "markers": [
                {"id": mid, "corners": np.round(self.board_to_world(pts)[:, :2], 4).tolist()}
                for mid, pts in self.printed_markers
            ],
        }


DEFAULT_MAT = MatSpec()


def printed_mat() -> MatSpec:
    """The mat as it came out of the printer. Printers often scale a page by
    1-3 % ("fit to page"), and every measurement scales with it. Measure the
    length of 10 squares along the printed ring with a ruler and set
    PHONE2CAD_MAT_10_SQUARES_MM (150 when printed at exactly 100 %); unset means
    the nominal size."""
    v = os.environ.get("PHONE2CAD_MAT_10_SQUARES_MM", "").strip()
    if not v:
        return DEFAULT_MAT
    mm = float(v)
    nominal = 10 * DEFAULT_MAT.square_mm
    if not 0.9 * nominal <= mm <= 1.1 * nominal:
        raise ValueError(f"PHONE2CAD_MAT_10_SQUARES_MM={v}: 10 squares should measure about {nominal:g} mm")
    k = mm / nominal
    return replace(DEFAULT_MAT, square_mm=DEFAULT_MAT.square_mm * k, marker_mm=DEFAULT_MAT.marker_mm * k)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", type=Path, help="write the printable A4 PDF here")
    ap.add_argument("--json", type=Path, help="write the browser layout JSON here")
    ap.add_argument("--png", type=Path, help="write a raster preview here")
    a = ap.parse_args()
    m = DEFAULT_MAT
    if a.pdf:
        m.to_pdf(a.pdf)
    if a.json:
        a.json.write_text(json.dumps(m.to_json(), separators=(",", ":")))
    if a.png:
        cv2.imwrite(str(a.png), (m.to_raster(6.0) * 255).astype(np.uint8))


if __name__ == "__main__":
    main()
