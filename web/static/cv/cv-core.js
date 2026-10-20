/*
 * ScanFit live vision core (runs inside cv-worker.js, also loadable in Node
 * for tests). Detects the mat's ArUco markers in a small preview frame,
 * estimates the camera position in mat millimetres, and scores sharpness.
 * No DOM, no network.
 */
(function (root) {
	'use strict';
	let detector = null;
	let markerCorners = null; // id -> [[x,y],[x,y],[x,y],[x,y]] world mm
	let outline = null; // the mat's four outer corners, world mm (x, y, 0)

	function init(cv, mat) {
		const dictionary = cv.getPredefinedDictionary(cv[mat.dictionary]);
		const params = new cv.aruco_DetectorParameters();
		const refine = new cv.aruco_RefineParameters(10, 3, true);
		detector = new cv.aruco_ArucoDetector(dictionary, params, refine);
		markerCorners = new Map(mat.markers.map((m) => [m.id, m.corners]));
		const hx = (mat.squaresX * mat.squareMm) / 2, hy = (mat.squaresY * mat.squareMm) / 2;
		outline = [-hx, -hy, 0, hx, -hy, 0, hx, hy, 0, -hx, hy, 0];
	}

	/**
	 * @param img {data: Uint8ClampedArray (RGBA), width, height}
	 * @param focal approximate focal length in preview pixels
	 * @returns {{markers:number, inView:boolean, sharp:number, light:number, clipped:number,
	 *   pose:null|{cam:number[], R:number[], rms:number}}}
	 *   R: world -> camera rotation, row-major. light: mean grey of the centre (0..1); clipped: share of
	 *   blown-out (glare) pixels there.
	 *   inView: at least three of the mat's four outer corners project inside the frame. The PC needs corners from
	 *   several sides of the ring; frames cut at the ring give few, nearly collinear ones.
	 */
	function analyse(cv, img, focal) {
		const keep = [];
		const track = (m) => (keep.push(m), m);
		try {
			const rgba = track(cv.matFromImageData(img));
			const gray = track(new cv.Mat());
			cv.cvtColor(rgba, gray, cv.COLOR_RGBA2GRAY);

			// Sharpness: variance of the Laplacian on the central half.
			const w = gray.cols, h = gray.rows;
			const roi = track(gray.roi(new cv.Rect(w >> 2, h >> 2, w >> 1, h >> 1)));
			const lap = track(new cv.Mat());
			cv.Laplacian(roi, lap, cv.CV_32F);
			const mean = track(new cv.Mat()), std = track(new cv.Mat());
			cv.meanStdDev(lap, mean, std);
			const sharp = std.doubleAt(0, 0) ** 2;
			const light = cv.mean(roi)[0] / 255;
			const hot = track(new cv.Mat());
			cv.threshold(roi, hot, 249, 255, cv.THRESH_BINARY);
			const clipped = cv.countNonZero(hot) / (roi.rows * roi.cols);

			const corners = track(new cv.MatVector());
			const ids = track(new cv.Mat());
			const rejected = track(new cv.MatVector());
			detector.detectMarkers(gray, corners, ids, rejected);

			const obj = [], pix = [];
			let n = 0;
			const nIds = ids.rows * ids.cols; // OpenCV.js returns a 1xN row
			for (let i = 0; i < nIds; i++) {
				const wc = markerCorners.get(ids.data32S[i]);
				if (!wc) continue;
				const c = corners.get(i);
				const d = c.data32F;
				for (let k = 0; k < 4; k++) {
					obj.push(wc[k][0], wc[k][1], 0);
					pix.push(d[2 * k], d[2 * k + 1]);
				}
				c.delete();
				n++;
			}
			let pose = null;
			let inView = false;
			if (n >= 3) {
				const objM = track(cv.matFromArray(obj.length / 3, 1, cv.CV_64FC3, obj));
				const pixM = track(cv.matFromArray(pix.length / 2, 1, cv.CV_64FC2, pix));
				const K = track(cv.matFromArray(3, 3, cv.CV_64F, [focal, 0, (w - 1) / 2, 0, focal, (h - 1) / 2, 0, 0, 1]));
				const dist = track(cv.Mat.zeros(4, 1, cv.CV_64F));
				const rvec = track(new cv.Mat()), tvec = track(new cv.Mat());
				if (cv.solvePnP(objM, pixM, K, dist, rvec, tvec, false, cv.SOLVEPNP_IPPE)) {
					const R = track(new cv.Mat());
					cv.Rodrigues(rvec, R);
					const r = R.data64F, t = tvec.data64F;
					// camera centre C = -R^T t
					const cam = [
						-(r[0] * t[0] + r[3] * t[1] + r[6] * t[2]),
						-(r[1] * t[0] + r[4] * t[1] + r[7] * t[2]),
						-(r[2] * t[0] + r[5] * t[1] + r[8] * t[2])
					];
					const proj = track(new cv.Mat());
					cv.projectPoints(objM, rvec, tvec, K, dist, proj);
					const p = proj.data64F;
					let se = 0;
					for (let i = 0; i < pix.length; i++) se += (p[i] - pix[i]) ** 2;
					pose = { cam, R: Array.from(r), rms: Math.sqrt(se / (pix.length / 2)) };
					const outM = track(cv.matFromArray(4, 1, cv.CV_64FC3, outline));
					const outP = track(new cv.Mat());
					cv.projectPoints(outM, rvec, tvec, K, dist, outP);
					const o = outP.data64F;
					// One mat corner may be cut off: three still bring two full sides of the ring.
					const inside = [0, 1, 2, 3].filter((i) => o[2 * i] >= 0 && o[2 * i] < w && o[2 * i + 1] >= 0 && o[2 * i + 1] < h);
					inView = inside.length >= 3;
				}
			}
			return { markers: n, inView, sharp, light, clipped, pose };
		} finally {
			for (const m of keep) m.delete();
		}
	}

	root.ScanfitCV = { init, analyse };
})(typeof self !== 'undefined' ? self : globalThis);
