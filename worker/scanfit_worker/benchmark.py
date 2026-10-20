"""Run the full pipeline on a benchmark capture and score it against the CAD.

    python -m scanfit_worker.benchmark [--dataset ../data/benchmark/corner-v1]
                                       [--dense auto|cuda|cpu] [--keep-work] [--note TEXT]

A benchmark folder holds frames/ (a real phone capture) and reference.stl.
Each run copies the frames into data/benchmark/runs/<time>/ (the pipeline
deletes its input), runs the same code the server runs, scores the mesh with
scanfit_worker.compare, writes score.json there and appends a row to
data/benchmark/results.csv so changes can be compared over time.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import time
from pathlib import Path

import numpy as np

from . import pipeline
from .compare import feature_errors, score

REPO = Path(__file__).resolve().parents[2]
DEFAULT = REPO / "data" / "benchmark" / "corner-v1"
COLUMNS = ["time", "dataset", "note", "sfm_mode", "dense_path", "minutes", "frames_used", "frames_on_mat",
           "coverage_1mm", "coverage_05mm", "median_dist_mm", "p90_dist_mm", "scale_error_pct",
           "features", "features_matched", "worst_error_mm", "mean_abs_error_mm", "within_pm"]


def run(dataset: Path, dense: str = "auto", keep_work: bool = False, note: str = "") -> dict:
    dataset = dataset.resolve()
    runs = dataset.parent / "runs"
    job = runs / f"{dataset.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(dataset / "frames", job / "frames")
    t0 = time.time()
    report = pipeline.run(job, dense=dense, keep=keep_work)
    minutes = (time.time() - t0) / 60
    if keep_work:
        shutil.rmtree(job / "frames", ignore_errors=True)  # the dataset keeps the originals
    return score_run(job, dataset, report, minutes, note)


def score_run(job: Path, dataset: Path, report: dict, minutes: float, note: str = "") -> dict:
    """Score a finished run against the dataset's CAD and append it to results.csv."""
    runs = dataset.parent / "runs"
    sc, _ = score(job / "result" / "mesh.ply", dataset / "reference.stl")
    feats = feature_errors(report["features"], dataset / "reference.stl", sc.transform)
    errs = np.array([abs(f["error"]) for f in feats if f["error"] is not None])
    within = [f["within_pm"] for f in feats if f["within_pm"] is not None]
    row = {
        "time": time.strftime("%Y-%m-%d %H:%M"), "dataset": dataset.name, "note": note,  # noqa: E501
        "sfm_mode": os.environ.get("SCANFIT_SFM_MODE", "accurate"), "dense_path": report["dense"]["path"],
        "minutes": round(minutes, 1), "frames_used": report["frames"]["used"],
        "frames_on_mat": report["frames"].get("located_by_mat", 0),
        "coverage_1mm": round(sc.coverage[1.0], 3), "coverage_05mm": round(sc.coverage[0.5], 3),
        "median_dist_mm": round(sc.dist_median, 3), "p90_dist_mm": round(sc.dist_p90, 3),
        "scale_error_pct": round(sc.scale_error_pct, 2), "features": len(feats), "features_matched": len(errs),
        "worst_error_mm": round(float(errs.max()), 3) if len(errs) else None,
        "mean_abs_error_mm": round(float(errs.mean()), 3) if len(errs) else None,
        "within_pm": f"{sum(within)}/{len(within)}",
    }
    (job / "score.json").write_text(json.dumps({"summary": row, "features": feats}, indent=1))
    table = runs.parent / "results.csv"
    new = not table.exists()
    with table.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)
    return {"summary": row, "features": feats, "job": str(job)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=DEFAULT)
    ap.add_argument("--dense", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--keep-work", action="store_true", help="keep COLMAP work files of the run (debugging)")
    ap.add_argument("--note", default="", help="what changed, for the results table")
    ap.add_argument("--rescore", type=Path, help="score an existing run folder instead of running the pipeline")
    a = ap.parse_args()
    if a.rescore:
        rep = json.loads((a.rescore / "result" / "features.json").read_text())
        prog = json.loads((a.rescore / "progress.json").read_text())
        out = score_run(a.rescore, a.dataset.resolve(), rep, prog.get("elapsed_s", 0) / 60, a.note)
    else:
        out = run(a.dataset, a.dense, a.keep_work, a.note)
    s = out["summary"]
    print(f"\n== {s['dataset']}  ({s['minutes']} min, {s['frames_used']} frames, {s['frames_on_mat']} on the mat)")
    print(f"surface covered      {100 * s['coverage_1mm']:.0f}% within 1 mm, {100 * s['coverage_05mm']:.0f}% within 0.5 mm")
    print(f"scan to surface      median {s['median_dist_mm']} mm, 90% within {s['p90_dist_mm']} mm")
    print(f"scale error          {s['scale_error_pct']:+.2f}%")
    print(f"{'feature':10s} {'scan':>8s} {'CAD':>8s} {'error':>7s} {'±':>6s}")
    for f in out["features"]:
        cad = "-" if f["cad"] is None else f"{f['cad']:.2f}"
        err = "" if f["error"] is None else f"{f['error']:+.2f}"
        print(f"{f['label']:10s} {f['scan']:8.2f} {cad:>8s} {err:>7s} {f['pm'] if f['pm'] is not None else '':>6}")
    print(f"worst error {s['worst_error_mm']} mm, within ± {s['within_pm']}; run folder {out['job']}")


if __name__ == "__main__":
    main()
