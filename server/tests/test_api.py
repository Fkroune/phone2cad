import os
import struct
import time

import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient

from app import __version__, jobs, main
from app.jpeg import BadJpeg, strip_metadata
from app.main import app

ORIGIN = "https://scanfit.pages.dev"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def make_jpeg(w=64, h=48, app1=b"", after_eoi=b"") -> bytes:
    """JPEG-shaped payload: SOI, JFIF, optional APP1, a comment, tables, frame
    header, scan + data, EOI, then optional trailing bytes."""
    return (b"\xff\xd8" + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + (seg(0xE1, app1) if app1 else b"") + seg(0xFE, b"hello")
            + seg(0xDB, b"\x00" + bytes(64))
            + seg(0xC0, b"\x08" + struct.pack(">HH", h, w) + b"\x01\x01\x11\x00")
            + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\x34\xff\x00\x56" + b"\xff\xd9" + after_eoi)


EXIF = b"Exif\x00\x00GPS 48.8584 N 2.2945 E"
JPEG = make_jpeg(app1=EXIF)


@pytest.fixture(scope="module")
def client():
    with TestClient(app, headers=AUTH) as c:
        yield c


@pytest.fixture(autouse=True)
def tidy():
    """Each test starts with no open jobs (the open-job limit is global)."""
    yield
    for _ in range(100):
        with main.store._db() as db:
            rows = db.execute("SELECT id, status FROM jobs WHERE status IN ('uploading','queued','running')").fetchall()
        for r in rows:
            if r["status"] != "running":
                main.store.delete(r["id"])
        if not any(r["status"] == "running" for r in rows):
            return
        time.sleep(0.1)


def wait_end(client, jid):
    for _ in range(100):
        st = client.get(f"/api/jobs/{jid}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.1)
    raise AssertionError(st)


def upload(client, jid, n=8, side=1):
    q = "?side=2" if side == 2 else ""
    for i in range(n):
        assert client.put(f"/api/jobs/{jid}/frames/{i}{q}", content=JPEG).status_code == 204


# ---- metadata stripping ------------------------------------------------------

def test_strip_metadata_removes_exif_and_comments():
    out, w, h = strip_metadata(JPEG)
    assert b"GPS" not in out and b"hello" not in out
    assert b"JFIF" in out and out.endswith(b"\x12\x34\xff\x00\x56\xff\xd9")
    assert (w, h) == (64, 48)


def test_strip_metadata_drops_trailing_data_and_late_segments():
    # Data after EOI (MPF secondary images, phone trailers) and APPn between progressive scans.
    progressive = JPEG[:-2] + seg(0xE1, EXIF) + seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x77\xff\xd9"
    for data in (make_jpeg(after_eoi=b"\xff\xe1" + EXIF), progressive):
        out, _, _ = strip_metadata(data)
        assert b"GPS" not in out and out.endswith(b"\xff\xd9")


@pytest.mark.parametrize("bad", [b"\xff\xd8\xff", b"\xff\xd8\xff\xe1\x00", b"\xff\xd8" + b"\xff" * 1000,
                                 b"\xff\xd8\xff" + b"0" * 100, make_jpeg()[:-2],
                                 make_jpeg().replace(b"\xff\xc0", b"\xff\xfe")])
def test_strip_metadata_rejects_malformed(bad):
    with pytest.raises(BadJpeg):
        strip_metadata(bad)


def test_strip_metadata_keeps_pixels_of_a_real_jpeg():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    img = (np.indices((120, 160)).sum(0) % 256).astype(np.uint8)
    for params in ([], [cv2.IMWRITE_JPEG_PROGRESSIVE, 1]):
        data = cv2.imencode(".jpg", img, params)[1].tobytes()
        out, w, h = strip_metadata(data)
        assert (w, h) == (160, 120)
        assert (cv2.imdecode(np.frombuffer(out, np.uint8), 0) == cv2.imdecode(np.frombuffer(data, np.uint8), 0)).all()


# ---- health, headers, CORS -----------------------------------------------------

def test_health_has_no_host_details_and_needs_no_token(client):
    r = client.get("/api/health", headers={"Origin": ORIGIN, "Authorization": ""})
    assert r.json() == {"ok": True, "name": "scanfit", "version": __version__}
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "server" not in {k.lower() for k in r.headers}


def test_security_headers_on_every_response(client):
    for r in (client.get("/api/health"), client.get("/api/jobs/" + "0" * 32),
              client.post("/api/jobs", headers={"Authorization": ""})):
        assert r.headers["content-security-policy"] == "default-src 'none'"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"


def test_cors_rejects_other_origins(client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = client.options("/api/jobs", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 400


def test_foreign_origin_cannot_change_anything(client):
    for origin in ("https://evil.example", "null"):
        r = client.post("/api/jobs", headers={"Origin": origin, "Content-Type": "text/plain"}, content=b"x")
        assert r.status_code == 403
    assert client.post("/api/jobs", headers={"Origin": ORIGIN}).status_code == 201


def test_private_network_preflight(client):
    r = client.options("/api/jobs", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type",
        "Access-Control-Request-Private-Network": "true", "Authorization": ""})
    assert r.status_code == 200
    assert r.headers["access-control-allow-private-network"] == "true"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


def test_no_api_docs(client):
    assert client.get("/docs").status_code in (401, 404)
    assert client.get("/openapi.json").status_code in (401, 404)


# ---- pairing token ---------------------------------------------------------------

@pytest.mark.parametrize("auth", ["", "Bearer", "Bearer wrong", f"Bearer {TOKEN}x", f"Basic {TOKEN}", TOKEN])
def test_routes_need_the_pairing_token(client, auth):
    h = {"Authorization": auth, "Origin": ORIGIN}
    jid = client.post("/api/jobs").json()["id"]
    for method, path in [("POST", "/api/jobs"), ("GET", f"/api/jobs/{jid}"), ("PUT", f"/api/jobs/{jid}/frames/0"),
                         ("POST", f"/api/jobs/{jid}/start"), ("GET", f"/api/jobs/{jid}/result/mesh.ply"),
                         ("GET", f"/api/jobs/{jid}/preview"), ("GET", f"/api/jobs/{jid}/frame"),
                         ("DELETE", f"/api/jobs/{jid}"), ("GET", "/api/session")]:
        r = client.request(method, path, headers=h, content=JPEG if method == "PUT" else None)
        assert r.status_code == 401, (method, path)
        assert r.headers["access-control-allow-origin"] == ORIGIN  # the app can tell "not paired"
    assert client.get(f"/api/jobs/{jid}").status_code == 200
    assert client.get("/api/session").status_code == 204


def test_server_refuses_to_start_without_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("SCANFIT_TOKEN_FILE")
    with pytest.raises(RuntimeError):
        main._token()
    (tmp_path / "t").write_text("short")
    monkeypatch.setenv("SCANFIT_TOKEN_FILE", str(tmp_path / "t"))
    with pytest.raises(RuntimeError):
        main._token()


@pytest.mark.parametrize("bad", ["*", "http://evil.example", "https://a.example/path", "ftp://x"])
def test_web_origin_is_validated(monkeypatch, bad):
    monkeypatch.setenv("SCANFIT_WEB_ORIGIN", bad)
    with pytest.raises(RuntimeError):
        main._origins()


# ---- ids and paths ----------------------------------------------------------------

def test_bad_ids_and_traversal(client):
    assert client.get("/api/jobs/../../etc/passwd").status_code == 404
    assert client.get("/api/jobs/zzzz").status_code == 404
    assert client.get("/api/jobs/" + "a" * 32 + "%0A").status_code == 404
    with pytest.raises(ValueError):
        jobs.job_dir("a" * 32 + "\n")
    r = client.post("/api/jobs").json()
    assert client.get(f"/api/jobs/{r['id']}/result/..%2Fscanfit.db").status_code in (404, 409)


# ---- uploads ------------------------------------------------------------------------

def test_job_lifecycle(client):
    jid = client.post("/api/jobs").json()["id"]
    assert client.put(f"/api/jobs/{jid}/frames/0", content=b"not a jpeg").status_code == 415
    assert client.post(f"/api/jobs/{jid}/start").status_code == 400  # too few frames
    upload(client, jid)
    stored = (jobs.job_dir(jid) / "frames" / "0000.jpg").read_bytes()
    assert b"GPS" not in stored and b"hello" not in stored
    assert not list((jobs.job_dir(jid) / "frames").glob("*.part"))
    assert client.post(f"/api/jobs/{jid}/start").json() == {"status": "queued", "frames": 8, "sides": 1}
    assert client.put(f"/api/jobs/{jid}/frames/9", content=JPEG).status_code == 409
    st = wait_end(client, jid)
    assert st["status"] == "done", st
    d = jobs.job_dir(jid)
    assert not (d / "frames").exists(), "frames must be deleted after the job"
    assert not (d / "work").exists() and not (d / "result" / "frame.jpg").exists()
    assert client.get(f"/api/jobs/{jid}/frame").status_code == 404
    assert client.get(f"/api/jobs/{jid}/result/features.json").json()["frames_seen"] == 8
    assert client.get(f"/api/jobs/{jid}/result/mesh.ply").content == b"ply\n"
    r = client.get(f"/api/jobs/{jid}/result/check.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/jobs/{jid}/result/worker.log").status_code == 404
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert client.get(f"/api/jobs/{jid}").status_code == 404
    assert not jobs.job_dir(jid).exists()


def test_failed_job_reports_and_cleans_up(client):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)
    (jobs.job_dir(jid) / "mode").write_text("fail")
    client.post(f"/api/jobs/{jid}/start")
    st = wait_end(client, jid)
    assert st["status"] == "failed" and st["error"] == "the mat was not found"
    assert not (jobs.job_dir(jid) / "frames").exists()
    assert not (jobs.job_dir(jid) / "result" / "frame.jpg").exists()


def test_worker_leaves_no_process_behind(client):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)
    (jobs.job_dir(jid) / "mode").write_text("orphan")
    client.post(f"/api/jobs/{jid}/start")
    wait_end(client, jid)
    pid = int((jobs.job_dir(jid) / "orphan.pid").read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    # A zombie (killed, not yet reaped by the test runner) also counts as stopped.
    assert open(f"/proc/{pid}/stat").read().split(")")[1].split()[0] == "Z"


def test_second_side(client):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)
    assert client.put(f"/api/jobs/{jid}/frames/0?side=3", content=JPEG).status_code == 400
    upload(client, jid, 3, side=2)
    assert (jobs.job_dir(jid) / "frames" / "side2" / "0002.jpg").is_file()
    assert client.post(f"/api/jobs/{jid}/start").status_code == 400  # other side: too few frames
    upload(client, jid, 8, side=2)
    assert client.post(f"/api/jobs/{jid}/start").json() == {"status": "queued", "frames": 16, "sides": 2}
    wait_end(client, jid)


def test_frame_size_limits(client):
    jid = client.post("/api/jobs").json()["id"]
    big = b"\xff\xd8\xff" + b"0" * (jobs.MAX_FRAME_BYTES + 10)
    assert client.put(f"/api/jobs/{jid}/frames/0", content=big).status_code == 413
    assert client.put(f"/api/jobs/{jid}/frames/{jobs.MAX_FRAMES}", content=JPEG).status_code == 400
    # Decompression bombs: a tiny file that declares a huge picture.
    for w, h in ((65000, 65000), (13000, 100), (9000, 8000)):
        r = client.put(f"/api/jobs/{jid}/frames/1", content=make_jpeg(w, h))
        assert r.status_code == 413, (w, h)
    assert client.put(f"/api/jobs/{jid}/frames/1", content=make_jpeg(4000, 3000)).status_code == 204


def test_declared_length_checked_before_reading(client):
    jid = client.post("/api/jobs").json()["id"]
    r = client.put(f"/api/jobs/{jid}/frames/0", content=JPEG,
                   headers={"Content-Length": str(jobs.MAX_FRAME_BYTES + 1)})
    assert r.status_code == 413


@pytest.mark.parametrize("bad", [b"\xff\xd8\xff\xe1\x00", b"\xff\xd8\xff" + b"0" * 100, make_jpeg()[:-2]])
def test_malformed_jpeg_is_415_not_500(client, bad):
    jid = client.post("/api/jobs").json()["id"]
    r = client.put(f"/api/jobs/{jid}/frames/0", content=bad, headers={"Origin": ORIGIN})
    assert r.status_code == 415
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_unexpected_error_is_a_plain_500_with_cors(client, monkeypatch):
    def boom(*_a):
        raise OSError("/secret/path/scanfit.db is broken")
    monkeypatch.setattr(main.store, "get", boom)
    r = client.get("/api/jobs/" + "0" * 32, headers={"Origin": ORIGIN})
    assert r.status_code == 500 and "secret" not in r.text
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_open_job_and_queue_limits(client, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_OPEN", 3)
    ids = [client.post("/api/jobs").json()["id"] for _ in range(3)]
    assert client.post("/api/jobs").status_code == 429
    monkeypatch.setattr(jobs, "MAX_OPEN", 50)
    monkeypatch.setattr(jobs, "MAX_QUEUE", 2)
    monkeypatch.setattr(main.store, "queued_count", lambda: 2)
    upload(client, ids[0])
    assert client.post(f"/api/jobs/{ids[0]}/start").status_code == 429
    assert client.post("/api/jobs").status_code == 429


def test_disk_space_and_job_size_limits(client, monkeypatch):
    jid = client.post("/api/jobs").json()["id"]
    monkeypatch.setattr(jobs, "MAX_JOB_BYTES", 2 * len(JPEG))
    upload(client, jid, 1)
    assert client.put(f"/api/jobs/{jid}/frames/1", content=JPEG).status_code == 204
    assert client.put(f"/api/jobs/{jid}/frames/2", content=JPEG).status_code == 413
    monkeypatch.setattr(jobs, "free_bytes", lambda: jobs.MIN_FREE_BYTES - 1)
    assert client.post("/api/jobs").status_code == 507
    monkeypatch.setattr(jobs, "MAX_JOB_BYTES", 10 ** 9)
    assert client.put(f"/api/jobs/{jid}/frames/3", content=JPEG).status_code == 507


def test_frames_folder_is_never_recreated(client):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)
    client.post(f"/api/jobs/{jid}/start")
    wait_end(client, jid)
    main.store.update(jid, status="uploading")  # as if a slow PUT passed the status check just before
    assert client.put(f"/api/jobs/{jid}/frames/9", content=JPEG).status_code == 409
    assert client.put(f"/api/jobs/{jid}/frames/9?side=2", content=JPEG).status_code == 409
    assert not (jobs.job_dir(jid) / "frames").exists()


def test_frame_written_while_job_starts_is_removed(client, monkeypatch):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)
    real = main.store.get

    def started_meanwhile(j):
        row = dict(real(j))
        row["status"] = "queued"
        return row
    calls = {"n": 0}

    def get(j):  # first call (route check) sees uploading, the re-check after writing sees queued
        calls["n"] += 1
        return real(j) if calls["n"] == 1 else started_meanwhile(j)
    monkeypatch.setattr(main.store, "get", get)
    assert client.put(f"/api/jobs/{jid}/frames/20", content=JPEG).status_code == 409
    assert not (jobs.job_dir(jid) / "frames" / "0020.jpg").exists()


def test_live_frame_thumbnail_only_while_running(client):
    jid = client.post("/api/jobs").json()["id"]
    assert client.get(f"/api/jobs/{jid}/frame").status_code == 404
    res = jobs.job_dir(jid) / "result"
    res.mkdir(parents=True, exist_ok=True)
    (res / "frame.jpg").write_bytes(JPEG)
    assert client.get(f"/api/jobs/{jid}/frame").status_code == 404  # uploading: not running
    main.store.update(jid, status="running")
    r = client.get(f"/api/jobs/{jid}/frame")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    main.store.update(jid, status="failed")
    assert client.get(f"/api/jobs/{jid}/frame").status_code == 404
    assert client.get("/api/jobs/zz/frame").status_code in (400, 404)


# ---- worker robustness and retention -------------------------------------------------

def test_worker_survives_errors(client, monkeypatch):
    jid = client.post("/api/jobs").json()["id"]
    upload(client, jid)

    def broken(_jid):
        raise OSError("disk full")
    monkeypatch.setattr(main.worker, "process", broken)
    client.post(f"/api/jobs/{jid}/start")
    st = wait_end(client, jid)
    assert st["status"] == "failed" and "server error" in st["error"]
    assert not (jobs.job_dir(jid) / "frames").exists()
    monkeypatch.undo()
    assert main.worker.is_alive() and client.get("/api/health").json()["ok"] is True
    jid2 = client.post("/api/jobs").json()["id"]
    upload(client, jid2)
    client.post(f"/api/jobs/{jid2}/start")
    assert wait_end(client, jid2)["status"] == "done"


def test_delete_of_queued_job_and_running_job(client):
    jid = client.post("/api/jobs").json()["id"]
    main.store.update(jid, status="queued", started=time.time() + 10 ** 6)  # last in the queue
    assert client.delete(f"/api/jobs/{jid}").status_code == 204
    assert main.store.claim_next() != jid
    jid = client.post("/api/jobs").json()["id"]
    main.store.update(jid, status="running")
    assert client.delete(f"/api/jobs/{jid}").status_code == 409
    assert jobs.job_dir(jid).exists()
    main.store.update(jid, status="failed")


def test_claim_is_atomic():
    jid = main.store.create()
    main.store.update(jid, status="queued", started=0)
    assert main.store.claim_next() == jid
    assert main.store.get(jid)["status"] == "running"
    assert main.store.claim_next() != jid
    main.store.update(jid, status="failed")


def test_sweep_retention(monkeypatch):
    s = main.store
    old = time.time() - jobs.UPLOAD_TTL - 60
    # An upload that is still receiving frames survives; an idle one is dropped.
    active, idle = s.create(), s.create()
    s.update(active, created=old)
    s.update(idle, created=old)
    os.utime(jobs.job_dir(idle) / "frames", (old, old))
    # A deleted job whose folder removal failed earlier is retried.
    gone = s.create()
    with s.lock, s._db() as db:
        db.execute("UPDATE jobs SET status = 'deleted' WHERE id = ?", (gone,))
    # An expired result, and a debug copy of it, go too.
    done = s.create()
    s.update(done, status="done", finished=time.time() - jobs.RESULT_TTL - 60)
    (jobs.DATA / "debug" / done).mkdir(parents=True)
    s.sweep()
    assert s.get(active)["status"] == "uploading"
    assert s.get(idle)["status"] == "deleted" and not jobs.job_dir(idle).exists()
    assert not jobs.job_dir(gone).exists()
    assert s.get(done)["status"] == "deleted" and not (jobs.DATA / "debug" / done).exists()
