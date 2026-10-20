# One image for the server and the worker it spawns. The official COLMAP image
# already carries CUDA COLMAP; we add Python and the measurement stack on top.
# Pinned by digest (COLMAP 4.2.1, Ubuntu 24.04, CUDA 12.9); update it deliberately.
FROM colmap/colmap@sha256:c1de51615dff33066cd305071226b6343c1168324ef3bd622f0825e55feb5efe

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libgl1 libgomp1 libusb-1.0-0 libegl1 \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp

# Exact versions with their hashes, wheels only (requirements.lock, see scripts/lock).
# pip is removed afterwards: nothing installs packages at run time.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
 && pip uninstall -y pip && rm /tmp/requirements.lock

# Part outlines: SAM 2.1 Hiera-tiny (Meta, Apache-2.0 weights), ONNX export by
# vietanhdev/segment-anything-2.1-onnx-models (Apache-2.0), run on the CPU (see
# worker segment.py). Pinned repository revision; the download must match its SHA-256.
ARG SAM_REVISION=6a3ac868340a3196a349050a6efae22a5acc0330
ARG SAM_SHA256=c602cb3f6fd297312a415f885f0df3f0eef9fbb334b069c9d30e828f2ae7c69a
RUN python -c "import hashlib, sys, urllib.request, zipfile; \
url = 'https://huggingface.co/vietanhdev/segment-anything-2.1-onnx-models/resolve/$SAM_REVISION/sam2.1_hiera_tiny_20260221.zip'; \
urllib.request.urlretrieve(url, '/tmp/sam.zip'); \
h = hashlib.sha256(open('/tmp/sam.zip', 'rb').read()).hexdigest(); \
sys.exit('SAM download: SHA-256 mismatch ' + h) if h != '$SAM_SHA256' else None; \
zipfile.ZipFile('/tmp/sam.zip').extractall('/models/sam21_tiny')" \
 && rm /tmp/sam.zip && ls /models/sam21_tiny/sam2.1_hiera_tiny.encoder.onnx
# OpenCV refuses to decode pictures above ~67 MP (the server already refuses frames above 64 MP).
ENV SCANFIT_SAM_DIR=/models/sam21_tiny ORT_DISABLE_TELEMETRY=1 OPENCV_IO_MAX_IMAGE_PIXELS=67108864

COPY LICENSE NOTICE THIRD_PARTY.md /usr/share/doc/scanfit/
COPY worker/scanfit_worker /app/worker/scanfit_worker
COPY worker/third_party /app/worker/third_party
COPY server/app /app/server/app

ENV SCANFIT_DATA=/data SCANFIT_WORKER_DIR=/app/worker PYTHONPATH=/app/worker SCANFIT_HOST=0.0.0.0 SCANFIT_PORT=8443
WORKDIR /app/server
EXPOSE 8443
# Never root; docker compose sets your own user id instead (scripts/run-server).
USER 1000:1000
CMD ["python", "-m", "app"]
