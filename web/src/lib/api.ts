import { getServer, getToken } from './pc';

export type FailKind = 'no-server' | 'permission' | 'network' | 'auth' | 'http';

export class ApiError extends Error {
	constructor(
		public kind: FailKind,
		message: string,
		public status?: number
	) {
		super(message);
	}
}

export type NetPermission = 'granted' | 'denied' | 'prompt' | 'unsupported';

/**
 * Chrome asks before a public page may talk to devices on the user's own
 * network ("local network access"). The permission name has changed across
 * versions, so try each and report the first one the browser knows.
 */
export async function localNetworkPermission(): Promise<NetPermission> {
	if (!('permissions' in navigator)) return 'unsupported';
	for (const name of ['local-network-access', 'local-network']) {
		try {
			const st = await navigator.permissions.query({ name } as unknown as PermissionDescriptor);
			return st.state as NetPermission;
		} catch {
			/* unknown name in this browser */
		}
	}
	return 'unsupported';
}

async function call(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<Response> {
	const base = getServer();
	if (!base) throw new ApiError('no-server', 'No PC is set up yet.');
	const token = getToken();
	const headers = new Headers(init.headers);
	if (token) headers.set('authorization', `Bearer ${token}`);
	let res: Response;
	try {
		res = await fetch(base + path, {
			...init,
			headers,
			signal: AbortSignal.timeout(timeoutMs),
			mode: 'cors',
			credentials: 'omit',
			referrerPolicy: 'no-referrer',
			// Hint for Chrome's local network access checks; ignored elsewhere.
			targetAddressSpace: 'local'
		} as RequestInit);
	} catch {
		if ((await localNetworkPermission()) === 'denied') {
			throw new ApiError('permission', 'Your browser is blocking access to your PC.');
		}
		throw new ApiError('network', "Can't reach your PC.");
	}
	if (res.status === 401) throw new ApiError('auth', 'This phone is not paired with this PC.', 401);
	if (!res.ok) {
		let detail = res.statusText;
		try {
			detail = ((await json(res, 64 * 1024)) as { detail?: string }).detail ?? detail;
		} catch {
			/* not JSON */
		}
		throw new ApiError('http', String(detail).slice(0, 300), res.status);
	}
	return res;
}

const MB = 1024 * 1024;
const unreadable = (what = 'answer') => new ApiError('http', `The PC sent an unreadable ${what}.`);

/** Bounded body: a broken or hostile server cannot make the page hold more than it needs. */
async function body(res: Response, maxBytes: number): Promise<ArrayBuffer> {
	const tooMuch = () => new ApiError('http', 'The PC sent too much data.');
	if (Number(res.headers.get('content-length') ?? 0) > maxBytes) throw tooMuch();
	const buf = await res.arrayBuffer();
	if (buf.byteLength > maxBytes) throw tooMuch();
	return buf;
}

async function json(res: Response, maxBytes: number): Promise<unknown> {
	const buf = await body(res, maxBytes);
	try {
		return JSON.parse(new TextDecoder().decode(buf));
	} catch {
		throw unreadable();
	}
}

function isNumbers(a: unknown, max: number): a is number[] {
	return Array.isArray(a) && a.length <= max && a.every((v) => typeof v === 'number' && Number.isFinite(v));
}

/** Is the PC there, is it ScanFit, and does it accept this phone's pairing token? */
export async function health(): Promise<void> {
	const r = (await json(await call('/api/health'), 4096)) as { name?: unknown };
	if (r?.name !== 'scanfit') throw new ApiError('http', 'That address is not a ScanFit PC.');
	await call('/api/session');
}

export async function createJob(): Promise<{ id: string; maxFrames: number }> {
	const r = (await json(await call('/api/jobs', { method: 'POST' }), 4096)) as { id?: unknown; maxFrames?: unknown };
	if (typeof r?.id !== 'string' || !/^[a-f0-9]{32}$/.test(r.id)) throw unreadable();
	return { id: r.id, maxFrames: typeof r.maxFrames === 'number' ? r.maxFrames : 150 };
}

/** side 2: the part filmed again after flipping it (optional second pass). */
export async function putFrame(id: string, index: number, blob: Blob, side: 1 | 2 = 1, tries = 4): Promise<void> {
	for (let a = 1; ; a++) {
		try {
			await call(`/api/jobs/${id}/frames/${index}${side === 2 ? '?side=2' : ''}`, {
				method: 'PUT',
				body: blob,
				headers: { 'content-type': 'image/jpeg' }
			}, 120_000);
			return;
		} catch (e) {
			const retry = e instanceof ApiError && (e.kind === 'network' || (e.status ?? 0) >= 500);
			if (!retry || a >= tries) throw e;
			await new Promise((r) => setTimeout(r, 800 * a));
		}
	}
}

export async function startJob(id: string): Promise<void> {
	await call(`/api/jobs/${id}/start`, { method: 'POST' });
}

export interface JobStatus {
	id: string;
	status: 'uploading' | 'queued' | 'running' | 'done' | 'failed';
	stage?: string;
	label?: string;
	progress?: number;
	position?: number;
	error?: string;
	step?: string; // what the PC is doing right now, in words
	stats?: Record<string, string | number>; // what it has found so far
	preview?: number; // version of the live 3D preview; fetch again when it changes
	frame?: number; // version of the "frame being worked on" thumbnail
	frame_label?: string;
	warnings?: string[]; // things the user should know, in plain words
	elapsed_s?: number;
}

/** Live preview while a job runs: flat xyz (mm) and rgb arrays, camera centres. */
export interface Preview {
	label: string;
	points: number[];
	colors: number[];
	cameras: number[];
	kinds?: number[]; // per camera: 0 placed by the mat, 1 by matching, 2 unclear outline, 3 being worked on
}

export async function jobPreview(id: string): Promise<Preview> {
	const p = (await json(await call(`/api/jobs/${id}/preview`), 4 * MB)) as Partial<Preview> | null;
	const n = 300_000;
	if (!p || !isNumbers(p.points, n) || !isNumbers(p.colors, n) || !isNumbers(p.cameras, n) ||
		(p.kinds !== undefined && !isNumbers(p.kinds, n)))
		throw unreadable('preview');
	return { label: String(p.label ?? ''), points: p.points, colors: p.colors, cameras: p.cameras, kinds: p.kinds };
}

export async function jobStatus(id: string): Promise<JobStatus> {
	const s = (await json(await call(`/api/jobs/${id}`), 256 * 1024)) as JobStatus | null;
	if (!s || typeof s !== 'object' || !['uploading', 'queued', 'running', 'done', 'failed'].includes(s.status))
		throw unreadable();
	for (const k of ['label', 'step', 'error', 'frame_label', 'stage'] as const)
		if (s[k] !== undefined && typeof s[k] !== 'string') delete s[k];
	for (const k of ['progress', 'position', 'preview', 'frame', 'elapsed_s'] as const)
		if (s[k] !== undefined && !Number.isFinite(s[k])) delete s[k];
	if (s.warnings !== undefined && !(Array.isArray(s.warnings) && s.warnings.every((w) => typeof w === 'string')))
		s.warnings = [];
	if (s.stats !== undefined && (typeof s.stats !== 'object' || s.stats === null || Array.isArray(s.stats)))
		s.stats = {};
	return s;
}

/** features.json; the result page checks each feature before showing it. */
export async function resultJson<T>(id: string): Promise<T> {
	const r = (await json(await call(`/api/jobs/${id}/result/features.json`), 16 * MB)) as { features?: unknown } | null;
	if (!r || typeof r !== 'object' || !Array.isArray(r.features)) throw unreadable('result');
	return r as T;
}

/** The frame the PC is working on, as an object URL (revoke the previous one). */
export async function jobFrame(id: string): Promise<string> {
	const buf = await body(await call(`/api/jobs/${id}/frame`), 2 * MB);
	return URL.createObjectURL(new Blob([buf], { type: 'image/jpeg' }));
}

export async function resultMesh(id: string): Promise<ArrayBuffer> {
	const buf = await body(await call(`/api/jobs/${id}/result/mesh.ply`, {}, 120_000), 200 * MB);
	checkPly(buf);
	return buf;
}

/**
 * Refuse a PLY the viewer could choke on before three.js parses it: header
 * within 4 KB, a known format, every element with at least one property, and
 * element counts that can fit in the file.
 */
export function checkPly(buf: ArrayBuffer): void {
	const head = new TextDecoder('latin1').decode(buf.slice(0, 4096));
	const end = head.indexOf('end_header\n');
	if (!head.startsWith('ply\n') || end < 0) throw unreadable('3D copy');
	const lines = head.slice(0, end).split('\n').map((l) => l.trim());
	const format = lines.find((l) => l.startsWith('format '));
	if (!format || !/^format (ascii|binary_little_endian|binary_big_endian) 1\.0$/.test(format)) throw unreadable('3D copy');
	let props = -1;
	let total = 0;
	for (const l of lines) {
		if (l.startsWith('element ')) {
			if (props === 0) throw unreadable('3D copy');
			const n = Number(l.split(/\s+/)[2]);
			if (!Number.isInteger(n) || n < 0) throw unreadable('3D copy');
			total += n;
			props = 0;
		} else if (l.startsWith('property ')) {
			if (props < 0) throw unreadable('3D copy');
			props++;
		}
	}
	if (props === 0 || total > buf.byteLength) throw unreadable('3D copy');
}

/** The 3D copy's outline over one real photo, as an object URL; null for older results. */
export async function resultCheck(id: string): Promise<string | null> {
	try {
		const buf = await body(await call(`/api/jobs/${id}/result/check.jpg`), 8 * MB);
		return URL.createObjectURL(new Blob([buf], { type: 'image/jpeg' }));
	} catch {
		return null;
	}
}

export async function deleteJob(id: string): Promise<void> {
	await call(`/api/jobs/${id}`, { method: 'DELETE' });
}

/** One plain sentence for each failure, with what to do next. */
export function explain(e: unknown): string {
	if (!(e instanceof ApiError)) return 'Something went wrong. Try again.';
	switch (e.kind) {
		case 'no-server':
			return 'Scan the QR code that run-server printed on your PC, or paste its link below.';
		case 'permission':
			return 'Your browser is blocking access to your PC. Open the site settings (tap the icon next to the address), allow "Local network access", then tap Retry.';
		case 'network':
			return "Can't reach your PC. Check that it is on and running run-server, and that Tailscale is on on this phone.";
		case 'auth':
			return 'This phone is not paired with your PC, or the PC has a new pairing code. Scan the QR code that run-server prints on your PC again.';
		default:
			return e.message;
	}
}
