# Roadmap

Known problems and planned work after v1.0.0, most important first. Each item
can become a GitHub issue; help is welcome.

## 1. Holes: false detections, sizes read small, threaded holes missed
On the black test jaw, the photo hole finder finds both real side holes but
reads them 0.4–0.7 mm small (Ø3.1 and Ø2.8 for Ø3.5). It also reports a false
hole next to one of them, and misses a threaded Ø6.5 hole inside a shallow
recess. A false hole also appeared once on a two-sided corner scan.
Ideas: check whether the rim is taken on the inside of the lit edge; ask SAM to
outline the hole itself; ask for square-on close-ups of faces with holes.
Goal: no false holes on the test scans; side holes within ±0.3 mm.

## 2. Systematic oversize +0.4–1.65 %: needs caliper-checked prints from contributors; test outline erosion
On all six test scans, sizes read 0.4–1.65 % large. The shape is carved from
the part's outlines (a visual hull), which can only be too big where an
outline is a little wide or a view is missing. Wanted from contributors:
prints of simple parts measured with a caliper, scanned, with the numbers
posted in a "Scan accuracy report" issue. To test: erode each outline by a
fraction of a pixel (or of its measured blur) before carving, and see if the
error falls on every scan without opening holes in thin walls.
Goal: no systematic size error above 0.3 %.

## 3. Repeatability of the camera solve
Processing the same frames twice gives slightly different camera positions
(median 0.14 mm, worst 1.9 mm), even with fixed random seeds. On one one-sided
scan, a fresh solve kept only one arm of the part. Goal: the same frames give
the same faces, or the app says the result is unsure.

## 4. Quick scans (one side): coverage
Quick scans cover less of the part than two-sided ones. Compare outlines
frame by frame on the test scans; look at the far side and near the mat.

## 5. More image cues for hard parts
Dark, shiny or low-contrast parts need more than one cue. A shared toolbox:
colour (HSV/Lab), brightness and local contrast, match against the known print,
edges and closed contours, frequency content, disagreement between views, and
focus. Cheap cues always; expensive ones only where needed. Holes first, then
outlines.

## 6. Live rough 3D on the phone
Carve a coarse shape on the phone while filming, from the frames that see the
mat. Show it live, and warn when the part is cut off at the frame edge.

## 7. Sharper close-ups
When the phone is held still, take a full-resolution photo, or average a few
frames, instead of a video frame. Aim: sharper edges and less noise on small
holes and dark parts.

## 8. Mesh refinement
The shape from outlines rounds inside corners and has a lumpy surface. Try
OpenMVS `RefineMesh` (AGPL licence: only as a separate, optional tool) on the
mesh with the solved cameras, and score it against the CAD.

## Security hardening
Low-severity risks accepted for v1.0.0 (see the
[security review](security-review.md)). None is reachable in normal use, but
each should be closed.

- **Block the container's internet access.** ScanFit needs no network at run
  time (measured: no outgoing traffic during a scan), but the container could
  still reach the internet. Goal: the server answers only on the tailnet port,
  with all outgoing traffic blocked, for example an internal Docker network
  behind a small port-forwarding proxy, or firewall rules set up by `run-server`.
- **Keep the TLS key and the pairing code away from the worker.** The worker
  decodes photos and runs as the same user as the server, so the key and code
  are not shielded from it. Goal: run the worker under its own user
  ID without access to `/run/scanfit`, or end TLS outside the container (for
  example in a reverse proxy) so the key never enters it.
- **Base image and OpenCV updates.** The pinned base image carries known CVEs
  in OS packages ScanFit does not use (GnuPG, OpenSSL DTLS), and the OpenCV
  wheel bundles FFmpeg libraries with known CVEs in video code ScanFit never
  calls. Goal: move to a patched base digest and a newer OpenCV wheel as soon as
  they pass the synthetic acceptance; check with trivy and grype on every
  release.
- **Slim the image.** Open3D pulls in an unused web and notebook stack (dash,
  flask, werkzeug, ipywidgets, nbformat and more). Goal: install Open3D without
  its optional packages plus only the subset ScanFit imports, re-generate the
  hashed `requirements.lock` with `scripts/lock`, and keep the change only if
  the synthetic acceptance passes in the hardened container (GPU and CPU).
  Fewer packages also means fewer CVEs to track. Not yet tried.
- **Signed images.** Releases on GHCR carry build provenance and an SBOM, but
  no signature. Goal: sign each release image with cosign (keyless, from the
  release workflow) and document how to verify it before `docker pull`.

## Also wanted
- Parts larger than the mat.
- Testing on iPhone and other Android phones.
- A LAN setup without Tailscale that is easy for non-experts.
