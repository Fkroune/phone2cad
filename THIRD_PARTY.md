# Third-party software and models

ScanFit itself is licensed under Apache-2.0 (`LICENSE`). It builds on the
following; their licences apply to them. Attribution notices required for
Apache-2.0 code we include are in [`NOTICE`](NOTICE).

| Component | Used for | Licence | How it is included |
|---|---|---|---|
| [COLMAP](https://colmap.github.io/) 4.x | Camera solve (features, matching, triangulation), dense PatchMatch stereo and fusion | BSD 3-Clause | Base Docker image `colmap/colmap`, run as a separate process; not modified |
| [Orbiter](https://github.com/RawMechatronicsOrg/Orbiter) COLMAP helpers | Metric camera priors → COLMAP model; session flow | Apache-2.0 | `sfm_priors_to_colmap.py` is vendored unmodified in `worker/third_party/orbiter_colmap/` (licence, provenance and change list there); `colmap_session.py` is a modified port of Orbiter's session script, which is not vendored |
| [SAM 2.1](https://github.com/facebookresearch/sam2) Hiera-tiny (Meta) | Part outlines in every frame | Apache-2.0 (code and weights) | ONNX export by [vietanhdev/segment-anything-2.1-onnx-models](https://huggingface.co/vietanhdev/segment-anything-2.1-onnx-models) (Apache-2.0, per its model card), downloaded when the Docker image is built from a pinned revision and checked against its SHA-256; weights are not in this repository, but they are in the server image |
| [ONNX Runtime](https://onnxruntime.ai/) | Runs SAM on the CPU | MIT | pip dependency. **Its PyPI build sends usage telemetry to Microsoft by default; ScanFit switches it off** (`ORT_DISABLE_TELEMETRY=1` before import and in the image, plus `disable_telemetry_events()`) |
| [OpenCV](https://opencv.org/) (opencv-contrib, and OpenCV.js in the browser) | ChArUco detection and calibration, image processing, EDCircles/MSER hole candidates, phase correlation | Apache-2.0 | pip / npm dependency |
| [Open3D](https://www.open3d.org/) | Point clouds, registration (FPFH, RANSAC, ICP), Poisson meshing, ray casting | MIT | pip dependency |
| NumPy, SciPy | Numerics, least squares, KD-trees | BSD 3-Clause | pip dependency |
| FastAPI, Uvicorn | Job server | MIT / BSD 3-Clause | pip dependency |
| SvelteKit, Svelte | Phone web app | MIT | npm dependency |
| three.js | 3D viewer on the phone | MIT | npm dependency; licence text served as `/third-party-licenses.txt` with the app |
| wrangler | Publishing the phone app (`scripts/deploy-web`) | MIT / Apache-2.0 | npm development dependency; not in the app |

Methods from the literature used in the code (no code copied): EDCircles
(Akinlar & Topal, 2013), MSER (Matas et al., 2002), RANSAC (Fischler & Bolles,
1981), the "detector box, then SAM" prompting pattern of Grounded-SAM.

## The server image

The prebuilt server image (`ghcr.io/fkroune/scanfit`) redistributes, besides
the components above, the software of its base image `colmap/colmap`: Ubuntu
24.04 packages (various free licences, including GPL and LGPL; sources via
Ubuntu), the NVIDIA CUDA runtime (NVIDIA's CUDA licence), Intel MKL (Intel
Simplified Software License), Qt 6 (LGPL-3.0), and libraries bundled in the
Python wheels (for example FFmpeg in OpenCV, LGPL). Their licence texts are in
the image under `/usr/share/doc` and in each package's folder in
`/opt/venv/lib/python3.12/site-packages`; ScanFit's own `LICENSE`, `NOTICE` and this file
are in `/usr/share/doc/scanfit/`. Each release also carries an SBOM
attestation listing the image's packages.
