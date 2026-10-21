"""python -m app  — start the API (and its worker thread).

Listens on PHONE2CAD_HOST:PHONE2CAD_PORT (default 127.0.0.1:8000; the container
sets 0.0.0.0:8443 and docker compose publishes it on the Tailscale IP only).
If PHONE2CAD_TLS_CERT and PHONE2CAD_TLS_KEY point to a `tailscale cert` pair, it
serves HTTPS. No access log: client addresses and job ids are not recorded.
"""

import logging
import os

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cert, key = os.environ.get("PHONE2CAD_TLS_CERT"), os.environ.get("PHONE2CAD_TLS_KEY")
    tls = {"ssl_certfile": cert, "ssl_keyfile": key} if cert and key else {}
    uvicorn.run("app.main:app", host=os.environ.get("PHONE2CAD_HOST", "127.0.0.1"),
                port=int(os.environ.get("PHONE2CAD_PORT", "8000")), proxy_headers=False,
                server_header=False, log_level="info", access_log=False,
                limit_concurrency=32, timeout_keep_alive=5, **tls)
