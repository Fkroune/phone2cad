# Security review of v1.0.0

**Date:** October 2026, before the first public release.
**Classification:** *conditionally ready* (no known critical or high issues; the
remaining low-severity risks and verification gaps are listed below).

This was a review of the whole project by an AI assistant (Claude) working for
the maintainer, with the maintainer approving every change in behaviour. It is
an engineering assessment, not a certification, and it cannot prove that no
problem is left. Please report anything you find: see [SECURITY.md](../../SECURITY.md).

## Scope

The phone web app and its hosting headers, the API server, the processing
worker, the Docker image and Compose file, the scripts (`run-server`,
`setup-check`, `deploy-web`), the release workflow, the dependencies, the
release downloads, the full Git history, and every privacy and security claim in
the documentation. There is no native mobile code.

Not covered: tests on a real phone (camera, Chrome's local-network prompt),
your tailnet's access rules, and the hosting accounts' settings.

## How it was checked

- Code review of every component, and a threat model.
- Tests run: the server test suite (now 46 tests), the synthetic end-to-end
  scan in a hardened container on the GPU and the CPU path, malicious and
  malformed requests against a running server, malformed results in a headless
  browser, and a capture of all network traffic of the server during start, a
  full scan, a failed scan and shutdown (result: no traffic to the internet).
- Tools: gitleaks (secrets in all history: none), trivy, syft and grype
  (image and SBOM), osv-scanner and npm audit, pip-audit, semgrep, bandit,
  hadolint, actionlint and zizmor (release workflow).

## Findings

| Severity | Found | Fixed | Open or partly fixed |
|---|---|---|---|
| Critical | 0 | 0 | 0 |
| High | 0 | 0 | 0 |
| Medium | 4 | 4 | 0 |
| Low | 36 | 29 | 7 |
| Informational | 32 | 20 | 10 (one more concern was shown to be false, one needed no action) |

Correction (v1.0.1): the first version of this table counted 28 low findings as
fixed and 8 as open. One of the 8, SUP-07 (personal data in an earlier version
of the repository history), is closed: this repository was created from a clean
tree, and only its clean first commit is published. So 29 are fixed and 7 remain.

## What was fixed

- **Pairing:** the server now requires a random pairing code on every request
  except the health check. A pairing link can no longer switch the phone to
  another server without asking: the app names the PC and waits for the user.
- **Abuse limits:** limits on open scans, queue length, scan size, frame
  resolution and free disk; requests from other websites are refused.
- **Robustness:** the worker loop and the retention sweeps survive errors; no
  COLMAP process outlives a job; malformed frames are rejected cleanly; the
  phone app checks and bounds what the server sends.
- **Privacy:** complete metadata stripping; no access log, rotated logs; the
  live thumbnail is deleted with the job; result links keep the job id out of
  requests to the app's host; documentation corrected (LAN mode unsupported,
  certificates and Certificate Transparency, services involved).
- **Container:** non-root, no Linux capabilities, no privilege escalation,
  read-only system, init, process and memory limits.
- **Supply chain:** pinned base image, hash-pinned Python packages, pinned and
  checksummed model download, CI actions pinned to commit SHAs, pinned
  deployment tool, a stricter privacy check of the web bundle.

## Known remaining risks (low)

- The worker that reads your photos runs as the same user as the server, so a
  flaw in an image decoder could expose the PC's TLS key and pairing code.
- The container can reach the network, although ScanFit never uses it at
  run time.
- Some operating-system packages and libraries in the base image have known
  vulnerabilities in parts ScanFit does not use (GnuPG, DTLS, the video codecs
  bundled with OpenCV). They will be updated with the base image.
- Who can reach the server depends on your tailnet's rules; see
  [getting started](../getting-started.md#who-can-reach-the-server).
- Licence notices for everything inside the prebuilt image are being reviewed.

The planned fixes are listed under [security hardening](roadmap.md#security-hardening)
in the roadmap.
