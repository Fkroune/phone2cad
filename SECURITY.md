# Security

## Reporting a problem

Please do not open a public issue for a security problem. Use GitHub's
**private vulnerability reporting** for this repository (Security tab →
"Report a vulnerability"). We aim to answer within 7 days.

Please include what is affected, how to reproduce it, and what an attacker
could do.

## How ScanFit is meant to be run

- The server listens **only** on the PC's Tailscale address, with HTTPS (the
  PC's Tailscale certificate). Running it on a LAN address is not supported. Do
  not forward its port to the internet, do not put it behind a public tunnel,
  and never enable Tailscale Funnel for it.
- **Pairing code.** Every request except `/api/health` must carry the PC's
  random pairing code (`Authorization: Bearer …`), or it is refused (401). The
  code is created by `run-server` (`~/.config/scanfit/token`, readable only by
  you), travels in the QR code's link, and is kept by the paired phone's
  browser. Treat the link like a password; `scripts/run-server --new-token`
  replaces the code and unpairs every phone.
- **Pairing never happens silently.** A pairing link opens the app, which shows
  the PC's name and asks before it stores it. The paired PC's name is always
  shown.
- **Who can connect** is decided by your tailnet: every device it lets reach
  the PC can try the API (and needs the pairing code to use it). If other
  people or shared devices are in your tailnet, restrict port 8443 with
  Tailscale access controls: see
  [getting started](docs/getting-started.md#who-can-reach-the-server).
- **Browsers:** the server answers only the phone app's origin (CORS,
  `SCANFIT_WEB_ORIGIN`, checked at start) and Chrome's Private Network Access
  preflight for it; any request that would change something and comes from
  another site is refused (403).
- **Uploads are checked:** a well-formed JPEG only, at most 15 MB and 64
  megapixels per frame, 150 frames per side, 1.5 GB per scan, a limit on open
  scans and queued jobs, and a free-disk reserve. Metadata is stripped before
  a frame is saved.
- **Processing:** each scan runs in a separate worker process group inside the
  container, stopped completely when it ends or after 4 hours. The container
  runs as your own user (never root), with no Linux capabilities, no privilege
  escalation, a read-only system, a memory limit without extra swap and a
  process limit. It needs no internet access while it runs.
- The TLS private key and the pairing code live outside the project folder,
  readable only by you, and are mounted read-only. The worker that reads your
  photos runs as the same user and could read them too; keep the image up to
  date.
- **Public names:** like every HTTPS certificate, the PC's certificate is
  recorded in public Certificate Transparency logs, which makes the PC's and
  the tailnet's names public. Use a neutral PC name.
- The phone app sets strict security headers (no framing, a restrictive content
  security policy that only lets it talk to its own site and `*.ts.net`,
  camera permission for itself only).
- **Supply chain:** the server image is built from a base image pinned by
  digest, Python packages pinned with hashes (`requirements.lock`), and an
  outline model pinned to a revision and checked against its SHA-256. The
  release workflow pins its actions to commit SHAs.

## Supported versions

Only the latest release gets security fixes.
