# Changelog

## v1.1.0

ScanFit is now **Phone2CAD**. Same tool, a name that says what it does and is
not shared with other apps.

- Repository: `github.com/Fkroune/phone2cad` (old links redirect).
- Server image: `ghcr.io/fkroune/phone2cad`. Older versions stay under
  `ghcr.io/fkroune/scanfit`.
- Phone app: `https://phone2cad-app.pages.dev`. Phones pair once again with the
  new QR code.
- Settings: `SCANFIT_*` became `PHONE2CAD_*`. `scripts/run-server` still reads
  the old names and moves `~/.config/scanfit` to `~/.config/phone2cad`, so an
  existing setup keeps its certificate. Your scans folder is unchanged.
- The mat PDF is now `phone2cad-mat-a4.pdf`. Mats already printed still work:
  the pattern is identical.

## v1.0.3

Fewer scans that lose a piece of the part, a warning when a two-sided scan may
still be missing one, capture coaching to keep the whole mat in view, and an
optional per-scan switch to keep the photos.

- **Outlines that leave out a piece are redone.** When some photos' outlines
  cut away a piece that most outlines keep (an arm, a tab), the 3D copy used to
  lose it. ScanFit now finds that piece from the texture points and outlines
  those photos again with clicks inside it (about 3 minutes more, only when it
  happens). The result page counts the outlines redone.
- **Two-sided scans warn when the two sides' shapes match poorly.** If a piece
  is still missing, the result now says so and points to the outline photo. See
  the limits in the README.
- A finished scan could be reported as failed ("processing stopped (code 134)")
  when a library crashed while the worker was exiting. The worker now exits as
  soon as the result is written.
- **Accuracy tables are now from scratch.** The README and the accuracy page
  show the six test scans processed from scratch, as a new scan would be; the
  earlier numbers, which reused each scan's stored camera solve, are kept on
  the accuracy page.
- **Capture: "Step back: show the whole mat again".** After 10 kept frames
  without the whole mat, the app asks for it (the PC places the close-ups from
  those frames). The counter, the side-1 screen and the upload screen show how
  many frames showed the whole mat.
- The live 3D preview no longer stops the texture step when it reads a depth
  map that COLMAP is still writing.
- **Keep this scan's photos (optional, per scan).** A checkbox next to the scan
  button (home page and result page) asks your PC to keep that one scan's photos
  and COLMAP's working files until its result expires (24 hours), to diagnose a
  bad result. Off by default, cleared when a scan starts, and removed with the
  result (also by "Remove it now"). The privacy default is unchanged: without
  the checkbox, photos are deleted when the job ends.
- `scripts/setup-check` warns when `qrencode` is missing; the getting-started guide says
  where the pairing QR code appears. `scripts/privacy-scan` prints a summary line when clean.
- The server and the app now report version 1.0.3.

## v1.0.2 — maintenance

NOTICE included in the container image; ScanFit icon replaces the framework's default.

- The image carries `NOTICE` next to `LICENSE` and `THIRD_PARTY.md` in
  `/usr/share/doc/scanfit/`.
- The phone app has its own icon (a phone showing the test block) instead of
  the SvelteKit default: browser tab icons at 16 and 32 px and an iOS home-screen
  icon.
- The server and the app now report version 1.0.2.

## v1.0.1 — maintenance

Maintenance: removed unused code and dependencies; no behaviour change.

- Removed unused code in the worker (`_fused_preview`, `_box_of`) and the
  vendored Orbiter shell script `run_colmap_session.sh`, which ScanFit never
  runs (licence, provenance and the script ScanFit uses stay).
- The server now declares `starlette`, which it imports directly (the installed
  set is unchanged).
- `CONTRIBUTING.md` documents the developer-only switches `SCANFIT_RESUME` and
  `SCANFIT_HOLES_DEBUG`.
- The server and the app now report version 1.0.1.
- Roadmap: slimming Open3D's unused optional packages from the image (not attempted).
- Security review corrected: low-severity findings are 29 fixed and 7 open, not
  28 and 8 (one finding is closed by the clean repository).

## v1.0.0 — first public release

### Capture (phone)
- Web app for Chrome on Android: live marker detection, automatic choice of
  sharp, well-lit frames from new directions, motion sensors between marker
  sightings, coverage dome and step-by-step hints.
- Torch button for dark parts; colour lock after calibration.
- Two sides by default: after side 1 the app asks to turn the part over and
  film side 2, joined on the PC. **Quick scan** films one side only; its
  overall size is marked "may be incomplete" and gets no grade.
- Live progress during processing, 3D viewer coloured by certainty, tap a
  measurement to see its faces.
- "Does this match your part?": the 3D copy's outline drawn over one of your
  photos on the result page.

### Processing (PC)
- COLMAP camera solve anchored on the ChArUco mat (metric scale, paper-bend
  model), with fixed random seeds.
- Part outlines by SAM 2.1 (CPU, ONNX Runtime, telemetry off).
- Shape from outlines (visual hull), refined with dense stereo; two-sided merge.
- Faces, widths, heights with ± values; round holes from photos (experimental).
- Mat-scale setting `SCANFIT_MAT_10_SQUARES_MM` to correct printer scaling.

### Setup
- `scripts/setup-check`: plain pass/fail checks of the PC.
- `scripts/run-server`: Tailscale-only HTTPS server and a QR code that pairs
  the phone.
- Prebuilt server image on the GitHub Container Registry (on version tags).
- Public phone app at https://scanfit-app.pages.dev (`run-server` uses it by
  default).

### Security and privacy hardening (before release)
- **Pairing code:** `run-server` creates a random code (readable only by you)
  and puts it in the QR link; the server refuses every request without it
  (except the health check). `scripts/run-server --new-token` replaces it.
- **No silent pairing:** a pairing link shows the PC's name and asks before
  the phone uses it; the paired PC is always shown, with "Forget this PC".
- Requests from other websites that would change something are refused.
- Upload limits: frame size and resolution, scan size, open scans, queue
  length, a free-disk reserve; malformed frames are rejected cleanly.
- Metadata stripping now covers the whole JPEG (data after the image too).
- The worker loop survives errors, retention sweeps run on their own timer,
  and no COLMAP process outlives a job; the live thumbnail is deleted when a
  job ends; result links keep the job id out of requests to the app's host.
- No access log; Docker logs are rotated.
- Hardened container: your own user (never root), no Linux capabilities, no
  privilege escalation, read-only system, init, process limit, no extra swap.
  The CPU path uses fewer SIFT threads so it fits in that memory limit.
- Supply chain: base image pinned by digest, Python packages pinned with
  hashes (`requirements.lock`), the SAM 2.1 model pinned to a revision and
  checked against its SHA-256, CI actions pinned to commit SHAs, wrangler
  pinned for `deploy-web`, a stricter privacy scan of the web bundle.
- Docs match the code: LAN mode is not supported, Certificate Transparency
  makes the PC's and tailnet's names public, how to restrict who can reach
  the server; licence notices in the image and the web app.
- Security review: [docs/dev/security-review.md](docs/dev/security-review.md).

### Disclaimer
- ScanFit is experimental hobby software: see the
  [Disclaimer](README.md#disclaimer-experimental-software-use-at-your-own-risk).
  The result page now says that its numbers are estimates, not certified
  measurements.

### Downloads
- `scanfit-mat-a4.pdf`: the mat, print on A4 at 100 %.
- `scanfit-test-block.stl`: the synthetic test block (40 × 30 × 20 mm, 10 mm
  through-hole), from `artefacts/test_block.scad`.
- `scanfit-corner-connector.stl`: CAD of the corner connector used in the
  accuracy tests (39.5 × 39.5 × 36.8 mm). Print it, scan it and compare.
  The test parts are ScanFit's own designs, under Apache-2.0.

### Known limits
- Holes are experimental (often ~0.5 mm small; false holes possible; threaded
  holes may be missed).
- Sizes may read up to ~1.5 % large (0.4–1.65 % on the test scans): the shape
  from outlines can only err outwards.
- Quick scans may miss parts of the object; re-processing the same frames can
  differ slightly.
- Openings are filled in the 3D copy; inside corners are rounded.
