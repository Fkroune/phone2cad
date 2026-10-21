# Third-party: Orbiter COLMAP helpers

Source: <https://github.com/RawMechatronicsOrg/Orbiter>, folder `colmap/`,
commit `af137c88a1514123a84885e65b3cc7838e0a86d2` (2026-09-02).

Copyright (c) 2026 Orbiter contributors. Licensed under the Apache License,
Version 2.0; the full text is in [`LICENSE`](LICENSE) next to this file.

| File | Status in Phone2CAD |
| --- | --- |
| `sfm_priors_to_colmap.py` | Unmodified. Called by the worker to turn metric camera priors into a COLMAP text model synced to the feature database. |
| `run_colmap_session.sh` | Not vendored (removed in Phone2CAD 1.0.1: Phone2CAD never runs it). It is in the upstream repository at the commit above. |
| `../../phone2cad_worker/colmap_session.py` | **Modified port** of upstream's `colmap/run_colmap_session.sh` to Python (see its header for the list of changes). |

Phone2CAD's changes, in short:

- Camera priors come from the printed ChArUco mat (lens model plus metric
  pose per frame), not from the Orbiter scan rig.
- Image pairs for matching are chosen from those priors instead of running
  the exhaustive matcher.
- A marker-constrained bundle adjustment (`phone2cad_worker/ba.py`) runs between
  two triangulation passes; it is not part of Orbiter.
- GPU steps use a reduced image size for a 2 GB card and fall back to the CPU
  (and to a CPU dense path) when CUDA fails.
- Frames and the COLMAP workspace are deleted after each job.
