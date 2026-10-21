/*
 * Classic worker: loads OpenCV.js (WebAssembly) off the main thread and
 * answers {type:'frame'} messages with marker/pose/sharpness results.
 */
/* global importScripts, cv, Phone2cadCV */
// The page passes ?v=<build> so each deploy loads its own cv-core.js; a cached
// copy from an older build once silently rejected every frame.
importScripts('./opencv.js', './cv-core.js' + self.location.search);

let ready = null;

async function boot(mat) {
	let mod = self.cv;
	if (mod instanceof Promise) mod = await mod;
	else if (!mod.Mat) await new Promise((r) => (mod.onRuntimeInitialized = r));
	self.cvReady = mod;
	Phone2cadCV.init(mod, mat);
}

self.onmessage = async (e) => {
	const msg = e.data;
	try {
		if (msg.type === 'init') {
			ready = boot(msg.mat);
			await ready;
			self.postMessage({ type: 'ready' });
		} else if (msg.type === 'frame') {
			await ready;
			const img = { data: new Uint8ClampedArray(msg.buffer), width: msg.width, height: msg.height };
			const res = Phone2cadCV.analyse(self.cvReady, img, msg.focal);
			self.postMessage({ type: 'result', id: msg.id, ...res });
		}
	} catch (err) {
		self.postMessage({ type: 'error', id: msg && msg.id, message: String(err && err.message ? err.message : err) });
	}
};
