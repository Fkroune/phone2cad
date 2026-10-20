// Where the user's PC (the ScanFit server) lives, and the pairing token it
// asks for. Never baked into the bundle: both arrive in the URL fragment of
// the link printed by `run-server` (fragments are not sent to Cloudflare) and
// are kept in this browser only. A link never re-points the phone silently:
// the home page shows the PC's name and stores it only when the user agrees.

const KEY = 'scanfit.server';
const TOKEN_KEY = 'scanfit.token';
const JOB_KEY = 'scanfit.lastJob';
const DEFAULT_PORT = '8443';
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;

export interface Pairing {
	origin: string;
	token: string;
}

export function normaliseServer(input: string): string | null {
	let s = input.trim();
	if (!s) return null;
	if (!/^[a-z]+:\/\//i.test(s)) s = `https://${s}`;
	try {
		const u = new URL(s);
		const local = u.hostname === 'localhost' || u.hostname === '127.0.0.1';
		if (u.protocol !== 'https:' && !(local && import.meta.env.DEV)) return null;
		// run-server listens on 8443; people naturally paste the bare MagicDNS name.
		if (u.hostname.endsWith('.ts.net') && !u.port) u.port = DEFAULT_PORT;
		return u.origin;
	} catch {
		return null;
	}
}

function param(text: string, name: string): string | null {
	const m = new RegExp(`(?:^|[#&?])${name}=([^&#]+)`).exec(text);
	if (!m) return null;
	try {
		return decodeURIComponent(m[1]);
	} catch {
		return null;
	}
}

/** The pairing in a run-server link (or just its `#server=…&token=…` part); null if incomplete. */
export function parsePairing(text: string): Pairing | null {
	const hash = text.includes('#') ? text.slice(text.indexOf('#')) : text;
	const server = param(hash, 'server');
	const token = param(hash, 'token');
	const origin = server ? normaliseServer(server) : null;
	if (!origin || !token || !TOKEN.test(token)) return null;
	return { origin, token };
}

/** A pairing link the page was opened with (not stored yet); cleans the address bar. */
export function pairingFromHash(): Pairing | 'invalid' | null {
	if (!/(?:^#|&)server=/.test(location.hash)) return null;
	const p = parsePairing(location.hash);
	history.replaceState(null, '', location.pathname + location.search);
	return p ?? 'invalid';
}

/** "my-pc" for https://my-pc.tail1234.ts.net:8443 */
export function pcName(origin: string): string {
	try {
		return new URL(origin).hostname.split('.')[0];
	} catch {
		return origin;
	}
}

export function pcHost(origin: string): string {
	try {
		return new URL(origin).host;
	} catch {
		return origin;
	}
}

function read(key: string): string | null {
	try {
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}

function write(key: string, value: string | null): boolean {
	try {
		if (value === null) localStorage.removeItem(key);
		else localStorage.setItem(key, value);
		return true;
	} catch {
		return false;
	}
}

export function getServer(): string | null {
	return read(KEY);
}

export function getToken(): string | null {
	return read(TOKEN_KEY);
}

export function isPaired(p: Pairing): boolean {
	return getServer() === p.origin && getToken() === p.token;
}

/** Store a pairing the user confirmed. */
export function pair(p: Pairing): boolean {
	return write(KEY, p.origin) && write(TOKEN_KEY, p.token);
}

/** Forget the PC, its token and the last result. */
export function forgetPc(): void {
	write(KEY, null);
	write(TOKEN_KEY, null);
	write(JOB_KEY, null);
}

export function lastJob(): string | null {
	const id = read(JOB_KEY);
	return id && /^[a-f0-9]{32}$/.test(id) ? id : null;
}

export function rememberJob(id: string): void {
	write(JOB_KEY, id);
}

export function forgetJob(id: string): void {
	if (read(JOB_KEY) === id) write(JOB_KEY, null);
}
