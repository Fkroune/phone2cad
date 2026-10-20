// Node check of static/cv/cv-core.js on rendered frames:
//   node scripts/test-cv-core.cjs FRAME.png [truth.json]
// FRAME must be a raw RGBA dump made by the worker test helper (see below).
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const [, , rawPath, metaPath] = process.argv;

(async () => {
	let cv = require(path.join(root, 'node_modules/@techstark/opencv-js/dist/opencv.js'));
	if (cv instanceof Promise) cv = await cv;
	else if (!cv.Mat) await new Promise((r) => (cv.onRuntimeInitialized = r));
	globalThis.self = globalThis;
	require(path.join(root, 'static/cv/cv-core.js'));
	const mat = JSON.parse(fs.readFileSync(path.join(root, 'static/mat.json'), 'utf8'));
	self.ScanfitCV.init(cv, mat);
	const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
	const data = new Uint8ClampedArray(fs.readFileSync(rawPath));
	const t0 = Date.now();
	const res = self.ScanfitCV.analyse(cv, { data, width: meta.width, height: meta.height }, meta.focal);
	console.log(JSON.stringify({ ...res, ms: Date.now() - t0 }));
})();
