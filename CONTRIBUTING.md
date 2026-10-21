# Contributing

Thank you for helping. Scan results from new parts are as welcome as code.

## Ways to help

- **Share a scan result**: part type, material and colour, one or two sides,
  and how the numbers compare with your caliper or CAD. Use the
  "Scan accuracy report" issue template.
- **Report a bug**: use the "Bug report" template.
- **Pick a roadmap item**: see [docs/dev/roadmap.md](docs/dev/roadmap.md).

Docs for contributors (how it works, benchmark, roadmap): [docs/dev/](docs/dev/README.md).

## Development setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cd web && npm ci
```

COLMAP runs in Docker (`colmap/colmap`); the worker starts it for each step.

To run the API by itself (it refuses to start without a pairing token):

```sh
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/phone2cad-dev-token)
cd server && PHONE2CAD_TOKEN_FILE=~/.config/phone2cad-dev-token PHONE2CAD_WEB_ORIGIN=http://localhost:5173 python -m app
```

The server image installs the exact, hash-checked versions in
`requirements.lock`. After changing `server/requirements.txt` or
`worker/requirements.txt`, regenerate it with `scripts/lock` (needs
[uv](https://docs.astral.sh/uv/)) and run all the tests.

## Tests

```sh
cd server && python -m pytest     # API, fast
cd worker && python -m pytest     # synthetic end-to-end scan, ~6 minutes
cd web && npm run check           # type check of the phone app
scripts/privacy-scan web/build    # after npm run build: nothing personal in the bundle
```

Every change to the measuring pipeline should also be scored on real scans.
Measure the before and after with `python -m phone2cad_worker.compare` against a
CAD model, and say in the pull request what changed. Do not tune settings to
one scan: a change must help, or at least not hurt, across several parts.

## Developer-only switches

These environment variables exist for working on the pipeline. The Docker
setup (`docker-compose.yml`) does not pass them on, so a normal server never
sees them; set them only when you run the worker yourself, for example
`python -m phone2cad_worker.pipeline JOB_DIR`, on frames you made.

- `PHONE2CAD_RESUME=1`: reuse the camera solve (`work/<side>/state.pkl`) and the
  dense points of an earlier run of the same job folder, to iterate on the later
  stages without redoing the slow ones. The state file is a Python pickle that
  the worker wrote itself; **never** point this at a job folder or file from
  someone else, because unpickling runs code.
- `PHONE2CAD_HOLES_DEBUG=/path/to/folder`: write each face's flattened photos
  (`face01_<frame>.png`) there while holes are searched. These are scan photos:
  the privacy rules apply, and nothing deletes the folder for you.

Other `PHONE2CAD_*` variables that are not in `.env.example` (`PHONE2CAD_WORKER_CMD`,
`PHONE2CAD_COLMAP`, `PHONE2CAD_COLMAP_IMAGE`, `PHONE2CAD_SEG_VIEWS`, `PHONE2CAD_SAM_*`,
`PHONE2CAD_DENSE_CROP_MAX`, `PHONE2CAD_DENSE_TIMEOUT_S`, `PHONE2CAD_CPU_SIFT_THREADS`,
`PHONE2CAD_GPU`, `PHONE2CAD_MAX_FRAMES`, `PHONE2CAD_MAX_FRAME_MB`, `PHONE2CAD_MAX_FRAME_MP`)
are tuning and test settings; their defaults are what the release was tested with.

## Rules

- **Privacy first.** No photos of rooms, no local paths, user names, host
  names, keys or tokens in commits, issues or test data.
- **No telemetry.** Before adding a library, check whether it reports usage,
  and switch that off.
- **No paid services** in the default setup.
- Keep code and messages plain: short functions, comments that say why, and
  user-facing text a non-expert understands.
- New code is licensed under Apache-2.0, like the rest of the project.
  Unless you state otherwise, any contribution you submit for inclusion is
  accepted under the Apache License 2.0 (section 5 of the licence), with no
  additional terms: what you contribute is licensed the same way the project is.
