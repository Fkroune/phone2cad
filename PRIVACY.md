# Privacy

ScanFit is built so that your photos go only to **your own PC**. Nobody else,
including the ScanFit project, receives your photos, your results or your PC's
address.

## Where your data goes

- **Photos** go from the phone straight to **your own PC**, over your Tailscale
  network, encrypted with HTTPS. Only a phone that opened your PC's pairing
  link can send them.
- **Photo metadata** (EXIF, XMP and similar: location, phone model, time, and
  any extra data after the image) is removed from every frame before it is
  saved on the PC.
- The PC processes the photos and **deletes them when the job ends**, whether
  it succeeded or failed. The same goes for COLMAP's working copies and the
  small live thumbnail shown while it works.
- **Results** stay on your PC: the 3D copy (`mesh.ply`), the measurements
  (`features.json`), and one small **cropped photo of the part** with the 3D
  copy's outline drawn on it (`check.jpg`, the "Does this match your part?"
  picture). They are deleted after 24 hours (`SCANFIT_RESULT_TTL_H`), or at
  once when you tap **Remove it now** on the result page.
- A **job record** (a random id, the status, the number of frames, times, and
  the error message of a failed scan) is kept for 7 days, then deleted.
- The server writes **no access log** (no client addresses, no job ids). Its
  own log (start, errors) is kept by Docker, at most 3 files of 10 MB.
- The **phone** keeps your PC's address, its pairing code and the id of your
  last result in the browser's storage for the app's site, until you tap
  **Forget this PC** or clear the site's data. They are never sent to the
  site's host: they travel in the part of the link after `#`, which browsers do
  not send.

## Services involved, and what they see

| Service | When | What it receives |
|---|---|---|
| **Cloudflare Pages** (hosts the public phone app, scanfit-app.pages.dev) | When the phone loads the app | Your IP address, browser type, the pages and files requested, and when. Not your PC's address, pairing code, photos or results. ScanFit adds no analytics or cookies. See [Cloudflare's privacy policy](https://www.cloudflare.com/privacypolicy/). |
| **Tailscale** (the private network) | While Tailscale runs on your phone and PC | Your Tailscale account and device information (names, addresses, operating system). Your traffic is end-to-end encrypted: Tailscale cannot read your photos, even when it relays them. See [Tailscale's privacy policy](https://tailscale.com/privacy-policy). |
| **Public Certificate Transparency logs** | When `run-server` gets the PC's HTTPS certificate | The PC's name and your tailnet's name (such as `my-pc.tail1234.ts.net`) are published permanently, as for every HTTPS certificate. Choose a neutral PC name: see [getting started](docs/getting-started.md). |
| **GitHub, Docker Hub, PyPI, Hugging Face** | Only when you build or download the server image | Your IP address and what is downloaded (the code, the COLMAP base image, Python packages, the SAM 2.1 model). Never anything about your scans. |

When the server runs, it contacts nothing on the internet: in tests of start,
a full synthetic scan, a failed scan and shutdown, it sent no network traffic
at all.

Running ScanFit's server is your own processing on your own PC: you are
responsible for it and for securing that PC (see the
[disclaimer](README.md#disclaimer-experimental-software-use-at-your-own-risk)).
For the public phone app, the ScanFit maintainer is responsible for its
hosting; questions about it go through the
[GitHub repository](https://github.com/Fkroune/scanfit) (for anything private,
use its private vulnerability reporting). The maintainer has no access to your
scans, results, PC address or pairing code, so there is nothing about them to
show, correct or delete. Data held by Cloudflare or Tailscale is governed by
their policies above.

## No tracking

- No ScanFit account, no analytics, no ads, no tracking cookies. (Tailscale
  needs its own free account.)
- **ONNX Runtime**, used to run the outline model, sends usage telemetry to
  Microsoft by default. ScanFit **switches this off**
  (`ORT_DISABLE_TELEMETRY=1`, plus `disable_telemetry_events()`).
- The outline model is downloaded once, when the server image is built, from a
  fixed version checked against its checksum. Nothing is downloaded while the
  server runs.

## Debugging options

`SCANFIT_KEEP_FRAMES=failed` or `all` keeps photos on your PC (in
`data/server/debug/`) to help find problems. It is off by default, and
`run-server` warns while it is on. If you turn it on, the photos may show your
room: delete that folder when you are done, and never attach those photos to a
public issue. Removing a scan from the result page also removes its kept copy.

## Sharing results in issues

If you report a problem, share numbers and screenshots of the 3D copy, not raw
photos or logs, unless you are sure they show nothing private.
