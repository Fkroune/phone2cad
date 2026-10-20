// Copies OpenCV.js from node_modules into static/cv/ (served from our own
// origin: no third-party CDN at runtime), with the licence texts of the
// third-party code in the bundle (OpenCV.js, three.js). Runs before dev and build.
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const mod = (p) => resolve(root, 'node_modules', p);
mkdirSync(resolve(root, 'static/cv'), { recursive: true });
copyFileSync(mod('@techstark/opencv-js/dist/opencv.js'), resolve(root, 'static/cv/opencv.js'));
copyFileSync(mod('@techstark/opencv-js/LICENSE'), resolve(root, 'static/cv/LICENSE.txt'));
const notices = [
	['OpenCV.js (@techstark/opencv-js), served as /cv/opencv.js', '@techstark/opencv-js/LICENSE'],
	['three.js, part of the app scripts in /_app/', 'three/LICENSE']
].map(([what, file]) => `${what}\n${'-'.repeat(what.length)}\n\n${readFileSync(mod(file), 'utf8').trim()}\n`);
writeFileSync(
	resolve(root, 'static/third-party-licenses.txt'),
	`Third-party software in the ScanFit phone app\n\n${notices.join('\n\n')}`
);
