// Decides which preview frames become keyframes, tracks coverage around the
// part, and picks the single instruction to show.
//
// Two phases. Calibration: a dozen views with the whole mat in view, from
// different angles; the PC solves the lens, the scale and the first camera
// positions from them. Detail: sharp, well-lit frames that show something new,
// mat or not; the PC locates those by matching them to each other and to the
// calibration views. Close-ups are welcome there: they carry the detail.
//
// "Something new" (after keyframe rules in visual SLAM such as ORB-SLAM): the
// frame must differ from every frame kept so far, not just the last one, and
// the phone must actually have moved (motion sensors), so standing still or
// coming back to an angle already filmed does not use up frames. Each viewing
// direction gets a quota; past it a frame needs to be clearly different.
//
// Coverage: a dome of 3 elevation rings x 12 directions, in two distance
// bands (overview with the mat, close-up). Direction comes from the mat when
// it is in view, otherwise from the phone's orientation sensor anchored to the
// last mat sighting (see motion.ts).

import type { Mat3 } from './motion';

export interface Analysis {
	markers: number;
	inView: boolean;
	sharp: number;
	light?: number; // mean grey of the frame centre, 0..1
	clipped?: number; // share of blown-out pixels in the centre
	pose: { cam: [number, number, number]; R?: Mat3; rms: number } | null;
}

export interface MatLayout {
	dictionary: string;
	partZone: [number, number, number, number];
	markers: { id: number; corners: [number, number][] }[];
}

export const RINGS = [
	[12, 35],
	[35, 55],
	[55, 82]
] as const;
export const SECTORS = 12;
export const CAL_FRAMES = 12; // whole-mat views first
export const MAX_DETAILS = 120;
export const MAX_FRAMES = CAL_FRAMES + MAX_DETAILS; // server accepts up to 150
export const MIN_FRAMES = 30; // in total, calibration included, before Finish
// With the whole mat in view (see cv-core.js `inView`) the PC finds 40+ corners even
// when the 640 px preview only resolves ~15 markers.
export const MIN_MARKERS = 8;
// Close-up directions to cover (of 12) per ring, low to high, before suggesting Finish.
export const COVER_TARGET = [8, 8, 5] as const;

const CAL_ANGLE_DEG = 10; // calibration views must differ by this much
const CAL_INTERVAL_MS = 250;
const DETAIL_INTERVAL_MS = 400;
const MIN_CHANGE = 0.05; // mean |difference| of 32x24 grey thumbnails (0..1) from every kept frame
const MIN_CHANGE_CROWDED = 0.12; // ... once this direction already has CELL_QUOTA frames
const CELL_QUOTA = 5;
const MIN_TURN_DEG = 3; // sensors: the phone turned at least this much since the last kept frame...
const MIN_TRAVEL = 0.15; // ...or moved (integrated |acceleration|, m/s)
// A close-up is a frame where the whole mat no longer fits: then the part fills
// more of the picture, whatever its size. (A distance limit failed: on the first
// phone scan 90 frames at 225-280 mm still showed the whole mat around a 40 mm part.)
const DARK = 0.18;
const GLARE = 0.03;
const STILL_HINT_MS = 2000;

export type Kind = 'anchor' | 'detail';
export type Band = 'far' | 'close';

export interface View {
	dir: [number, number, number]; // from the part towards the camera, unit
	elev: number; // degrees
	az: number; // degrees, 0..360
	dist: number | null; // mm, when the mat gave the position
	band: Band;
	sensed: boolean; // direction from the motion sensors, not the mat
}

export const THUMB_W = 32;
export const THUMB_H = 24;

/**
 * 32x24 grey thumbnail by true block averaging (like an area resize). The
 * browser's own canvas shrink samples a few pixels per cell instead, so the
 * mat's fine print aliases and a hand tremor looks like a new view (first phone
 * scan: a third of the detail frames were near-repeats).
 */
export function thumbnail(img: { data: Uint8ClampedArray; width: number; height: number }): Float32Array {
	const { data, width: w, height: h } = img;
	const sum = new Float32Array(THUMB_W * THUMB_H);
	const cnt = new Float32Array(THUMB_W * THUMB_H);
	for (let y = 0; y < h; y++) {
		const row = Math.min(THUMB_H - 1, Math.floor((y * THUMB_H) / h)) * THUMB_W;
		for (let x = 0; x < w; x++) {
			const i = 4 * (y * w + x);
			const c = row + Math.min(THUMB_W - 1, Math.floor((x * THUMB_W) / w));
			sum[c] += data[i] + data[i + 1] + data[i + 2];
			cnt[c] += 765;
		}
	}
	for (let c = 0; c < sum.length; c++) sum[c] /= cnt[c] || 1;
	return sum;
}

/** Mean absolute difference of two equal-size grey thumbnails, 0..1. */
export function change(a: Float32Array, b: Float32Array | null): number {
	if (!b || a.length !== b.length) return 1;
	let s = 0;
	for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
	return s / a.length;
}

export interface Moved {
	turned: number;
	travel: number;
}

export class KeyframeSelector {
	kept: View[] = []; // calibration views
	details = 0;
	cells = { far: new Set<number>(), close: new Set<number>() };
	private perCell = new Map<string, number>();
	private thumbs: Float32Array[] = [];
	private sharpHistory: number[] = [];
	private lastKeep = 0;
	private size: string | null = null;
	readonly centre: [number, number, number];

	constructor(mat: MatLayout) {
		const [x0, x1, y0, y1] = mat.partZone;
		this.centre = [(x0 + x1) / 2, (y0 + y1) / 2, 15];
	}

	get calibrated(): boolean {
		return this.kept.length >= CAL_FRAMES;
	}

	get total(): number {
		return this.kept.length + this.details;
	}

	/** Where the camera is, seen from the part: from the mat pose, else from the sensors. */
	view(a: Analysis, worldFromCam: Mat3 | null): View | null {
		if (a.pose) {
			const d = a.pose.cam.map((v, i) => v - this.centre[i]) as [number, number, number];
			const dist = Math.hypot(...d);
			return this.fromDir(d.map((v) => v / dist) as [number, number, number], dist, a.inView ? 'far' : 'close', false);
		}
		if (worldFromCam) {
			// Close-ups aim at the part: the camera sits opposite its viewing axis.
			const R = worldFromCam;
			return this.fromDir([-R[2], -R[5], -R[8]], null, 'close', true);
		}
		return null;
	}

	private fromDir(dir: [number, number, number], dist: number | null, band: Band, sensed: boolean): View {
		const elev = (Math.asin(Math.max(-1, Math.min(1, dir[2]))) * 180) / Math.PI;
		const az = ((Math.atan2(dir[1], dir[0]) * 180) / Math.PI + 360) % 360;
		return { dir, elev, az, dist, band, sensed };
	}

	cellOf(v: View): number | null {
		const ring = RINGS.findIndex(([lo, hi]) => v.elev >= lo && v.elev < hi);
		if (ring < 0) return null;
		return ring * SECTORS + Math.floor(v.az / (360 / SECTORS));
	}

	sharpEnough(sharp: number): boolean {
		const h = this.sharpHistory;
		h.push(sharp);
		if (h.length > 40) h.shift();
		if (h.length < 5) return true;
		const sorted = [...h].sort((a, b) => a - b);
		const p80 = sorted[Math.floor(0.8 * (sorted.length - 1))];
		return sharp >= 0.55 * p80;
	}

	private novelView(v: View): boolean {
		const cosMin = Math.cos((CAL_ANGLE_DEG * Math.PI) / 180);
		return this.kept.every((k) => k.dir[0] * v.dir[0] + k.dir[1] * v.dir[1] + k.dir[2] * v.dir[2] <= cosMin);
	}

	/** How different this thumbnail is from the closest frame already kept. */
	novelty(thumb: Float32Array): number {
		let best = 1;
		for (const t of this.thumbs) best = Math.min(best, change(thumb, t));
		return best;
	}

	static lighting(a: Analysis): 'dark' | 'glare' | null {
		if (a.light !== undefined && a.light < DARK) return 'dark';
		if (a.clipped !== undefined && a.clipped > GLARE) return 'glare';
		return null;
	}

	/** Which kind of frame to keep, if any. thumb: 32x24 grey thumbnail of the frame. */
	consider(a: Analysis, v: View | null, thumb: Float32Array, frameSize: string, now: number,
		moved: Moved | null): { kind: Kind | null; sharp: boolean; still: boolean } {
		const sharp = this.sharpEnough(a.sharp);
		const still = moved !== null && moved.turned < MIN_TURN_DEG && moved.travel < MIN_TRAVEL;
		const no = { kind: null, sharp, still };
		if (!sharp || (this.size && frameSize !== this.size)) return no;
		if (!this.calibrated) {
			if (!v || v.sensed || !a.inView || a.markers < MIN_MARKERS || v.elev < RINGS[0][0]) return no;
			if (v.dist === null || v.dist < 90 || v.dist > 900) return no;
			if (now - this.lastKeep < CAL_INTERVAL_MS || !this.novelView(v)) return no;
			return { kind: 'anchor', sharp, still };
		}
		if (this.details >= MAX_DETAILS || now - this.lastKeep < DETAIL_INTERVAL_MS) return no;
		if (KeyframeSelector.lighting(a)) return no;
		if (still) return no;
		const c = v ? this.cellOf(v) : null;
		const crowded = c !== null && (this.perCell.get(`${v!.band}${c}`) ?? 0) >= CELL_QUOTA;
		if (this.novelty(thumb) < (crowded ? MIN_CHANGE_CROWDED : MIN_CHANGE)) return no;
		return { kind: 'detail', sharp, still };
	}

	accept(kind: Kind, v: View | null, thumb: Float32Array, frameSize: string, now: number): void {
		this.size ??= frameSize;
		this.lastKeep = now;
		this.thumbs.push(thumb);
		if (kind === 'anchor' && v) this.kept.push(v);
		else this.details++;
		const c = v ? this.cellOf(v) : null;
		if (c !== null && v) {
			this.cells[v.band].add(c);
			const key = `${v.band}${c}`;
			this.perCell.set(key, (this.perCell.get(key) ?? 0) + 1);
		}
	}

	/** Close-up directions covered per ring (low, mid, high). */
	coverage(): number[] {
		return RINGS.map((_, r) => {
			let n = 0;
			for (let s = 0; s < SECTORS; s++) if (this.cells.close.has(r * SECTORS + s)) n++;
			return n;
		});
	}

	get covered(): boolean {
		const c = this.coverage();
		return c.every((n, r) => n >= COVER_TARGET[r]);
	}

	/**
	 * The next close-up direction to film: in the ring furthest behind its target
	 * (usually the low one, which people skip), the sector nearest to the phone.
	 */
	target(v: View | null): { ring: number; sector: number; az: number; elev: number } | null {
		if (!v) return null;
		const cov = this.coverage();
		const behind = cov.map((n, r) => (COVER_TARGET[r] - n) / COVER_TARGET[r]);
		const ring = behind.indexOf(Math.max(...behind));
		if (behind[ring] <= 0) return null;
		let best: { ring: number; sector: number; az: number; elev: number } | null = null;
		let bestD = Infinity;
		RINGS.forEach(([lo, hi], r) => {
			if (r !== ring) return;
			for (let s = 0; s < SECTORS; s++) {
				if (this.cells.close.has(r * SECTORS + s)) continue;
				const az = (s + 0.5) * (360 / SECTORS), elev = (lo + hi) / 2;
				const dAz = Math.abs(((az - v.az + 540) % 360) - 180);
				const d = dAz + 1.5 * Math.abs(elev - v.elev);
				if (d < bestD) [bestD, best] = [d, { ring: r, sector: s, az, elev }];
			}
		});
		return best;
	}

	instruction(a: Analysis | null, v: View | null, sharp: boolean, stillFor: number): string {
		if (!this.calibrated) {
			if (!a || !a.pose || a.markers < 3) return 'Point at the whole mat';
			if (a.markers < MIN_MARKERS || !a.inView) return 'Step back: whole mat in view';
			if (!sharp) return 'Hold steady';
			return `Calibrating ${this.kept.length}/${CAL_FRAMES}: move round the mat`;
		}
		if (this.total >= MAX_FRAMES) return 'All set. Tap Finish';
		if (!sharp) return 'Move slower';
		const light = a ? KeyframeSelector.lighting(a) : null;
		if (light === 'dark') return 'Too dark: add light';
		if (light === 'glare') return 'Glare on the part: tilt the phone a little';
		if (this.covered && this.total >= MIN_FRAMES) return 'Covered all round. Tap Finish, or add close-ups';
		if (a?.inView && !this.covered) return 'Get closer: fill the screen with the part';
		const t = this.target(v);
		if (t && v) {
			const dAz = ((t.az - v.az + 540) % 360) - 180; // moving right raises the azimuth
			const side = Math.abs(dAz) < 20 ? '' : dAz > 0 ? 'right' : 'left';
			const height = Math.abs(t.elev - v.elev) < 12 ? '' : t.elev > v.elev ? 'higher' : 'lower';
			const way = [side, height].filter(Boolean).join(' and ');
			if (way) return `Next: move ${way}`;
		}
		if (stillFor > STILL_HINT_MS) return 'Keep moving slowly round the part';
		return 'Close up: circle the part slowly';
	}
}
