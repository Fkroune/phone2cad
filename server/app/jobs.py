"""Job records (SQLite), on-disk job folders, the worker loop and expiry.

The server keeps no personal data: a job is a random id, a status and
timings. Frames live on disk only until the job ends (unless the user switched on
"keep photos" for that scan: then until the result expires); results expire after
RESULT_TTL; job records after RECORD_TTL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("scanfit")

DATA = Path(os.environ.get("SCANFIT_DATA", Path(__file__).resolve().parents[2] / "data")).resolve()
JOBS = DATA / "jobs"
MAX_FRAMES = int(os.environ.get("SCANFIT_MAX_FRAMES", "150"))
MIN_FRAMES = 8
MAX_FRAME_BYTES = int(float(os.environ.get("SCANFIT_MAX_FRAME_MB", "15")) * 1024 * 1024)
MAX_FRAME_PIXELS = int(float(os.environ.get("SCANFIT_MAX_FRAME_MP", "64")) * 1_000_000)
MAX_FRAME_SIDE = 12_000
MAX_JOB_BYTES = int(float(os.environ.get("SCANFIT_MAX_JOB_MB", "1500")) * 1024 * 1024)
MAX_QUEUE = int(os.environ.get("SCANFIT_MAX_QUEUE", "5"))  # queued + running
MAX_OPEN = int(os.environ.get("SCANFIT_MAX_OPEN", "8"))  # uploading + queued + running
MIN_FREE_BYTES = int(float(os.environ.get("SCANFIT_MIN_FREE_GB", "2")) * 1024 ** 3)
UPLOAD_TTL = 3600  # an upload that stops for 1 h is dropped
RESULT_TTL = int(os.environ.get("SCANFIT_RESULT_TTL_H", "24")) * 3600
RECORD_TTL = 7 * 24 * 3600
JOB_TIMEOUT = 4 * 3600
SWEEP_EVERY = 600
WORKER_DIR = Path(os.environ.get("SCANFIT_WORKER_DIR", Path(__file__).resolve().parents[2] / "worker"))
WORKER_CMD = shlex.split(os.environ.get("SCANFIT_WORKER_CMD", f"{shlex.quote(sys.executable)} -m scanfit_worker.pipeline"))

# Debugging only, off by default (see .env.example): keep a job's frames on this PC
# in data/server/debug/ instead of deleting them. "failed" or "all".
KEEP_FRAMES = os.environ.get("SCANFIT_KEEP_FRAMES") or ("failed" if os.environ.get("SCANFIT_KEEP_FAILED") == "1" else "")

JOB_ID = re.compile(r"[a-f0-9]{32}")


def new_id() -> str:
    return secrets.token_hex(16)


def job_dir(job_id: str) -> Path:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("bad job id")
    return JOBS / job_id


def last_activity(jid: str) -> float:
    """When a frame was last added (the frames folders' mtime), else 0."""
    frames = job_dir(jid) / "frames"
    times = [p.stat().st_mtime for p in (frames, frames / "side2") if p.is_dir()]
    return max(times, default=0.0)


def job_bytes(jid: str) -> int:
    frames = job_dir(jid) / "frames"
    if not frames.is_dir():
        return 0
    return sum(p.stat().st_size for p in frames.rglob("*") if p.is_file())


def free_bytes() -> int:
    return shutil.disk_usage(JOBS).free


class Store:
    def __init__(self, path: Path | None = None):
        DATA.mkdir(parents=True, exist_ok=True)
        JOBS.mkdir(parents=True, exist_ok=True)
        self.path = path or DATA / "scanfit.db"
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, frames INTEGER DEFAULT 0,
                created REAL NOT NULL, started REAL, finished REAL, error TEXT)""")
            cols = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            if "keep" not in cols:  # databases from before the per-scan switch
                db.execute("ALTER TABLE jobs ADD COLUMN keep INTEGER DEFAULT 0")

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def create(self, keep: bool = False) -> str:
        """keep: the user asked (per scan) to keep its photos until the result expires."""
        jid = new_id()
        (job_dir(jid) / "frames").mkdir(parents=True)
        with self.lock, self._db() as db:
            db.execute("INSERT INTO jobs (id, status, created, keep) VALUES (?, 'uploading', ?, ?)",
                       (jid, time.time(), int(keep)))
        return jid

    def get(self, jid: str) -> sqlite3.Row | None:
        with self._db() as db:
            return db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()

    def update(self, jid: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock, self._db() as db:
            db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), jid))

    def transition(self, jid: str, frm: str, **fields) -> bool:
        """update() only if the job is still in status `frm`; True if it was."""
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock, self._db() as db:
            cur = db.execute(f"UPDATE jobs SET {cols} WHERE id = ? AND status = ?", (*fields.values(), jid, frm))
            return cur.rowcount == 1

    def queue_position(self, jid: str) -> int:
        with self._db() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY started").fetchall()
        ids = [r["id"] for r in rows]
        return ids.index(jid) + 1 if jid in ids else 0

    def queued_count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]

    def open_count(self) -> int:
        with self._db() as db:
            return db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('uploading', 'queued', 'running')").fetchone()[0]

    def claim_next(self) -> str | None:
        """Atomically move the oldest queued job to running."""
        with self.lock, self._db() as db:
            r = db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY started LIMIT 1").fetchone()
            if r is None:
                return None
            db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (r["id"],))
            return r["id"]

    def delete(self, jid: str) -> bool:
        """Mark a job deleted and remove its files; refused (False) while it runs."""
        with self.lock, self._db() as db:
            cur = db.execute("UPDATE jobs SET status = 'deleted' WHERE id = ? AND status != 'running'", (jid,))
            if cur.rowcount != 1:
                return False
        self._remove(jid)
        return True

    @staticmethod
    def _remove(jid: str) -> None:
        for d in (job_dir(jid), DATA / "debug" / jid):
            shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                log.warning("could not remove all of %s; will retry", d.relative_to(DATA))

    # ---- housekeeping ----------------------------------------------------

    def sweep(self) -> None:
        now = time.time()
        with self._db() as db:
            rows = db.execute("SELECT id, status, created, started, finished FROM jobs").fetchall()
        for r in rows:
            jid, st = r["id"], r["status"]
            if st == "uploading" and now - max(r["created"], last_activity(jid)) > UPLOAD_TTL:
                self.delete(jid)
            elif st in ("done", "failed") and now - (r["finished"] or r["created"]) > RESULT_TTL:
                self.delete(jid)
            elif st == "queued" and now - (r["started"] or r["created"]) > RECORD_TTL:
                self.delete(jid)
            elif st == "deleted" and job_dir(jid).exists():
                self._remove(jid)  # an earlier removal failed
        with self.lock, self._db() as db:
            db.execute("DELETE FROM jobs WHERE created < ? AND status != 'running'", (now - RECORD_TTL,))
        # Folders with no record (e.g. after a crash) are removed too.
        for d in list(JOBS.iterdir()):
            try:
                if d.is_dir() and now - d.stat().st_mtime > UPLOAD_TTL and self.get(d.name) is None:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass  # removed meanwhile

    def recover(self) -> None:
        """After a restart, jobs that were running cannot resume: fail them."""
        with self._db() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
        for r in rows:
            clean_job(r["id"])
            self.update(r["id"], status="failed", finished=time.time(), error="The PC restarted during processing.")


def clean_job(jid: str) -> None:
    """What never outlives a job: frames, COLMAP work, the live thumbnail."""
    d = job_dir(jid)
    shutil.rmtree(d / "frames", ignore_errors=True)
    shutil.rmtree(d / "work", ignore_errors=True)
    (d / "result" / "frame.jpg").unlink(missing_ok=True)


def read_progress(jid: str) -> dict:
    try:
        return json.loads((job_dir(jid) / "progress.json").read_text())
    except (OSError, ValueError):
        return {}


def _kill_group(p: subprocess.Popen) -> None:
    """Stop the worker and everything it started (COLMAP, helpers)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    p.wait()


class Worker(threading.Thread):
    """Runs one job at a time in a child process (memory is freed after each).
    A second thread sweeps expired jobs on its own timer, so retention holds
    while a long job runs. Neither thread dies on an error."""

    def __init__(self, store: Store):
        super().__init__(daemon=True, name="scanfit-worker")
        self.store, self.wake = store, threading.Event()
        self.sweeper = threading.Thread(target=self._sweep_loop, daemon=True, name="scanfit-sweeper")

    def healthy(self) -> bool:
        return self.is_alive() and self.sweeper.is_alive()

    def _sweep_loop(self) -> None:
        while True:
            try:
                self.store.sweep()
            except Exception:  # noqa: BLE001 - keep sweeping
                log.exception("sweep failed")
            time.sleep(SWEEP_EVERY)

    def run(self) -> None:
        try:
            self.store.recover()
        except Exception:  # noqa: BLE001
            log.exception("recovery failed")
        self.sweeper.start()
        while True:
            try:
                jid = self.store.claim_next()
            except Exception:  # noqa: BLE001 - e.g. database locked: try again
                log.exception("could not read the queue")
                jid = None
            if jid is None:
                self.wake.wait(30)
                self.wake.clear()
                continue
            try:
                ok, err = self.process(jid)
            except Exception:  # noqa: BLE001
                log.exception("job %s failed in the server", jid[:8])
                ok, err = False, "processing stopped (server error)"
                try:
                    clean_job(jid)
                except Exception:  # noqa: BLE001 - the sweep retries with the folder
                    log.exception("could not clean job %s", jid[:8])
            try:
                self.store.transition(jid, "running", status="done" if ok else "failed",
                                      finished=time.time(), error=err)
            except Exception:  # noqa: BLE001
                log.exception("could not record the end of job %s", jid[:8])

    def process(self, jid: str) -> tuple[bool, str | None]:
        d = job_dir(jid)
        ok, err = False, "processing stopped"
        row = self.store.get(jid)
        keep = bool(row and row["keep"])
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if keep:  # the worker must not delete the frames itself; they are moved below
            env["SCANFIT_KEEP_FRAMES"] = "all"
        try:
            with open(d / "worker.log", "w") as wlog:
                p = subprocess.Popen([*WORKER_CMD, str(d)], cwd=WORKER_DIR, stdout=wlog, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
                try:
                    p.wait(timeout=JOB_TIMEOUT)
                except subprocess.TimeoutExpired:
                    err = "processing took too long"
                finally:
                    _kill_group(p)  # also when it ended: no COLMAP left behind
            if err != "processing took too long":
                prog = read_progress(jid)
                ok = p.returncode == 0 and prog.get("status") == "done"
                err = None if ok else (prog.get("error") or f"processing stopped (code {p.returncode})")
        finally:
            try:
                if (keep or KEEP_FRAMES == "all" or (KEEP_FRAMES == "failed" and not ok)) and (d / "frames").is_dir():
                    _keep_for_debug(jid)
            except Exception:  # noqa: BLE001 - debugging aid only; the frames are deleted below
                log.exception("could not keep job %s for debugging", jid[:8])
            # Otherwise frames never outlive the job, whatever happened.
            clean_job(jid)
        return ok, err


def _keep_for_debug(jid: str) -> None:
    # Opt-in only: keep this scan on this PC to diagnose it: frames,
    # COLMAP work, log, progress and results, in data/server/debug/<job>/.
    d, keep = job_dir(jid), DATA / "debug" / jid
    keep.mkdir(parents=True, exist_ok=True)
    shutil.move(d / "frames", keep / "frames")
    if (d / "work").is_dir():
        shutil.move(d / "work", keep / "work")
    for name in ("worker.log", "progress.json"):
        if (d / name).is_file():
            shutil.copy2(d / name, keep / name)
    if (d / "result").is_dir():
        shutil.copytree(d / "result", keep / "result", dirs_exist_ok=True)
