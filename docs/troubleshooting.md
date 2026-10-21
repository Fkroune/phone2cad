# Troubleshooting and FAQ

Start with `scripts/setup-check`: it finds most setup problems.

## Setup

**`setup-check` says Docker is not running.**
Start Docker. On Windows, open Docker Desktop and turn on its WSL2 integration
for your Ubuntu. On Linux, add yourself to the `docker` group, then log out and
back in.

**My NVIDIA card is found, but Docker cannot use it.**
Install the NVIDIA Container Toolkit and restart Docker. Until then, Phone2CAD
uses the slower CPU path.

**`run-server` stops with "set PHONE2CAD_APP_URL".**
The script has lost its built-in app address (it was edited). Get a fresh copy with
`git checkout scripts/run-server`, or put the app's address in `.env`:
`PHONE2CAD_APP_URL=https://phone2cad-app.pages.dev` (or your own copy's address).

**`run-server` cannot get a certificate.**
In the Tailscale admin page, turn on MagicDNS and HTTPS certificates. Then run
once: `sudo tailscale set --operator=$USER`.

**No QR code appears.**
Install `qrencode` (`sudo apt install qrencode`), or paste the printed link into
the app on the phone.

**`run-server` says it must not run as root, or that a folder is on a Windows drive.**
Run it as your normal user. Keep the certificates and the pairing code under
your Linux home (the defaults); set `PHONE2CAD_DATA_DIR` in `.env` to a folder
under your Linux home if the warning is about the scans folder.

**WSL2 freezes or the terminal closes during a scan.**
WSL2 ran out of memory. Give it at least 8 GB in `%UserProfile%\.wslconfig`
(`[wsl2]` then `memory=8GB`), and close other heavy programs.

## Phone

**The phone cannot reach the server.**
- Is the phone signed in to Tailscale, with the same account as the PC?
- Is the server running (`docker ps` shows `phone2cad`)?
- Open `https://<your-pc>.<your-tailnet>.ts.net:8443/api/health` in the phone's
  browser: it should show `"ok": true`. (`"ok": false` means the server's
  worker stopped: restart it with `scripts/run-server`.)

**The app says "This phone is not paired with your PC".**
The PC has a new pairing code (`scripts/run-server --new-token`), or the phone
was paired from an older link. Scan the QR code that `run-server` prints again.

**The app asks "Pair this phone with the PC …?" for a PC I do not know.**
Tap **Cancel**. Someone sent you a link to their own server; pairing would
send your scans there. Only pair with the name of your own PC.

**The camera does not start.**
Allow the camera for the app's site in Chrome's settings. The page must be
opened over HTTPS.

**The calibration counter does not move.**
The whole mat must be in view, flat and well lit, from 20–50 cm. Move to a new
direction before each view, and tilt the phone more if you film from straight
above.

## Results

**A measurement is clearly wrong.**
- Check that the mat scale is set (`PHONE2CAD_MAT_10_SQUARES_MM`).
- Use a normal two-sided scan, not a quick scan.
- Sizes may read up to ~1.5 % large (see [accuracy](accuracy.md)): check
  critical sizes with a caliper.
- Check the notes at the bottom of the result page.

**The 3D copy is missing part of the object.**
This happens mostly on quick scans (one side only). Film again as a normal
two-sided scan, with more low angles and slower. The "Does this match your
part?" photo on the result page shows what the copy covers.

**A hole is reported that is not there, or its size is off.**
Holes are experimental in this version. Check holes on the part.

**Black or very dark parts come out poorly.**
Use the Light button for close-ups, and add room light.

**Shiny parts come out poorly.**
Reflections confuse the outlines and the matching. A thin matt coating (matt
spray, dry shampoo, or scanning spray) helps.

## FAQ

**Does it work on iPhone?**
Not tested. The app needs Chrome features (motion sensors, torch control) that
Safari may not offer.

**Can I use it without a GPU?**
Yes. The CPU path is slower.

**How big can the part be?**
It must fit in the clear centre of the mat, about 11 × 11 cm.

**Where are my photos stored?**
On your PC only, during processing. They are deleted when the job ends (unless
you tick "Keep this scan's photos" before scanning, to diagnose a bad result:
then they stay for 24 hours, like the result); the
result keeps one small cropped photo of the part ("Does this match your
part?") until the result expires. See [PRIVACY.md](../PRIVACY.md).

**Can I use it on my local network without Tailscale?**
No. The phone app only connects to Tailscale addresses. Do not expose the
server to your LAN or the internet instead.

**Can I measure a part bigger than the mat?**
Not yet. See the [roadmap](dev/roadmap.md).
