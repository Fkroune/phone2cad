# Phone2CAD

Phone films a part on a printed ChArUco mat → PC reconstructs it at metric scale →
phone shows the 3D copy and the measured faces and holes. How it works:
`docs/dev/how-it-works.md`; contributor docs: `docs/dev/`.

## Privacy rule (non-negotiable)

**Never publish personal data, paths, host or tailnet names, keys or room photos.**
This covers commits, docs, images, issues, release assets and the web bundle. Use
example values (`my-pc.tail1234.ts.net`, `/path/to/phone2cad`) instead. Images in the
docs are renders, app screenshots on synthetic frames, or scan frames warped onto
the printed mat so that nothing beyond the paper shows; strip all metadata.

- The server is reachable over Tailscale only. No public tunnel, no port forwarding.
- Only the static web build goes to Cloudflare Pages: no source maps, no secrets, no
  server address baked in (the phone gets it and the pairing token via
  `#server=…&token=…` at runtime, and stores them only after the user confirms).
  `scripts/deploy-web` refuses to deploy a bundle that fails its privacy scan.
- `data/` and `.env` are gitignored. Uploaded frames are deleted when a job ends (unless the user ticks "keep photos" for that scan: then when its result expires);
  results expire (default 24 h). EXIF is stripped on upload.

## Layout

- `web/` SvelteKit + TypeScript, static adapter. Capture (`/scan`, two sides by
  default, `?quick=1` for one side), results (`/result`).
- `server/` FastAPI: job API, CORS for the app origin only, spawns the worker per job.
- `worker/` Python pipeline (`python -m phone2cad_worker.pipeline JOB_DIR`): mat
  calibration → COLMAP → outlines (SAM 2.1) → visual hull → faces, holes → result
  (`features.json`, `mesh.ply`, `check.jpg`).
- `artefacts/test_block.scad`: synthetic test part.

## Tests

- `cd server && python -m pytest`
- `cd worker && python -m pytest` (synthetic scan of the test block, slow)
