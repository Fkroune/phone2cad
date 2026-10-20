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
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/scanfit-dev-token)
cd server && SCANFIT_TOKEN_FILE=~/.config/scanfit-dev-token SCANFIT_WEB_ORIGIN=http://localhost:5173 python -m app
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
Measure the before and after with `python -m scanfit_worker.compare` against a
CAD model, and say in the pull request what changed. Do not tune settings to
one scan: a change must help, or at least not hurt, across several parts.

## Rules

- **Privacy first.** No photos of rooms, no local paths, user names, host
  names, keys or tokens in commits, issues or test data.
- **No telemetry.** Before adding a library, check whether it reports usage,
  and switch that off.
- **No paid services** in the default setup.
- Keep code and messages plain: short functions, comments that say why, and
  user-facing text a non-expert understands.
- New code is licensed under Apache-2.0, like the rest of the project.
