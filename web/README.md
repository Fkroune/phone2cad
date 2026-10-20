# ScanFit phone app

The part of ScanFit that runs in the phone's browser: capture, upload, live
progress and the result viewer. It is a static SvelteKit site (TypeScript); it
needs no server of its own.

## What it does

- **Capture** (`src/routes/scan`): camera, marker detection in a web worker
  (OpenCV.js, `static/cv/`), choice of useful frames (`src/lib/keyframes.ts`),
  motion sensors (`src/lib/motion.ts`), coverage dome (`src/lib/Dome.svelte`).
- **Upload** to the ScanFit server on your PC (`src/lib/api.ts`). The server's
  address and its pairing code come from the link `#server=https://...&token=...`
  that `run-server` prints (the fragment is never sent to the site's host). The
  app shows the PC's name and stores both in the browser's storage only when the
  user taps Pair (`src/lib/pc.ts`); every API call sends the code as
  `Authorization: Bearer`. Nothing about the PC is built into the site.
- **Result** (`src/routes/result`): live progress, 3D viewer (three.js,
  `src/lib/Viewer.svelte`), measurements with their grades.

## Develop

```sh
npm ci
npm run dev        # local dev server; open it on the phone over HTTPS to use the camera
npm run check      # type check
npm run build      # static build in build/
```

`npm run dev` and `npm run build` first copy the OpenCV.js build from
`node_modules` into `static/cv/opencv.js` (`scripts/copy-opencv.mjs`).

## Deploy

Any static HTTPS host works. `../scripts/deploy-web` builds the site and
publishes it to Cloudflare Pages with the wrangler version pinned in
`package-lock.json`. It refuses to upload uncommitted code, or a build that
contains source maps, local paths, user or machine names, tailnet addresses,
e-mail addresses or keys (`../scripts/privacy-scan`). Then set
`SCANFIT_APP_URL` in the PC's `.env` to the site's address.

Security headers for the host are in `static/_headers`.
