"""Acceptance: a synthetic scan of artefacts/test_block.scad (40×30×20 mm block,
Ø10 through-hole) must measure within ±0.5 mm.

Slow (COLMAP + BA + dense: several minutes). Uses data/synth64 when present,
otherwise renders a fresh 64-frame set first. PHONE2CAD_TEST_DENSE picks the
dense path (default: cpu, so the result does not depend on the GPU).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from phone2cad_worker import pipeline, synth
from phone2cad_worker.colmap_session import Colmap

REPO = Path(__file__).resolve().parents[2]
TOL = 0.5


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    if not Colmap.available():
        pytest.skip("COLMAP (or Docker) not available")
    src = REPO / "data" / "synth64"
    if not (src / "frames").is_dir() or len(list((src / "frames").glob("*.jpg"))) < 32:
        src = tmp_path_factory.mktemp("synth")
        synth.make_job(src, 64, synth.DEFAULT_SCAD, 3)
    job = tmp_path_factory.mktemp("job")
    shutil.copytree(src / "frames", job / "frames")  # the pipeline deletes its frames
    rep = pipeline.run(job, dense=os.environ.get("PHONE2CAD_TEST_DENSE", "cpu"))
    assert not (job / "frames").exists(), "frames must be deleted after the job"
    assert (job / "result" / "mesh.ply").stat().st_size > 10_000
    check = job / "result" / "check.jpg"
    assert rep.get("check") == "check.jpg" and check.read_bytes()[:2] == b"\xff\xd8"
    assert b"Exif" not in check.read_bytes()[:64], "the check picture must carry no metadata"
    return rep


def _values(rep: dict, kind: str) -> list[float]:
    return [f["value"] for f in rep["features"] if f["kind"] == kind]


def test_hole_diameter(report):
    holes = _values(report, "hole")
    assert holes, report["features"]
    assert abs(holes[0] - 10.0) <= TOL, holes


def test_block_height(report):
    heights = _values(report, "height")
    assert any(abs(h - 20.0) <= TOL for h in heights), heights


@pytest.mark.parametrize("size", [40.0, 30.0])
def test_block_length_and_width(report, size):
    across = _values(report, "distance")
    assert any(abs(d - size) <= TOL for d in across), across


def test_every_measurement_has_plus_minus(report):
    for f in report["features"]:
        if f["kind"] != "overall":
            assert f["pm"] is not None and f["pm"] > 0 and "±" in f["text"], f


def test_one_side_is_a_quick_scan(report):
    assert report["mode"] == "quick"
    overall = next(f for f in report["features"] if f["kind"] == "overall")
    assert overall["text"].endswith("(may be incomplete)") and not overall["grade"] and overall["pm"] is None
