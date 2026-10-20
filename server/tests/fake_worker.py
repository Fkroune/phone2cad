"""Stands in for scanfit_worker.pipeline in API tests.

A file `mode` in the job folder picks a behaviour: "fail" (a failed scan),
"orphan" (leaves a child process running, writes its pid to orphan.pid).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

job = Path(sys.argv[1])
mode = (job / "mode").read_text().strip() if (job / "mode").is_file() else ""
n = len(list((job / "frames").glob("*.jpg")))
(job / "result").mkdir(exist_ok=True)
(job / "result" / "frame.jpg").write_bytes(b"\xff\xd8\xff\xd9")
(job / "work").mkdir(exist_ok=True)
if mode == "orphan":
    child = subprocess.Popen(["sleep", "300"])
    (job / "orphan.pid").write_text(str(child.pid))
if mode == "fail":
    (job / "progress.json").write_text(json.dumps({"status": "failed", "error": "the mat was not found"}))
    sys.exit(1)
(job / "result" / "features.json").write_text(json.dumps({"features": [], "frames_seen": n}))
(job / "result" / "mesh.ply").write_bytes(b"ply\n")
(job / "result" / "check.jpg").write_bytes(b"\xff\xd8\xff\xd9")
(job / "progress.json").write_text(json.dumps({"status": "done", "stage": "done", "progress": 1.0}))
shutil.rmtree(job / "frames")
