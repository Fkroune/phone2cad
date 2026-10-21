<script lang="ts">
	import { onMount } from 'svelte';
	import KeepToggle from '../lib/KeepToggle.svelte';
	import { resolve } from '$app/paths';
	import { explain, health, localNetworkPermission, type NetPermission } from '../lib/api';
	import { forgetPc, getServer, isPaired, lastJob, pair, pairingFromHash, parsePairing, pcHost, pcName, type Pairing } from '../lib/pc';

	type Conn = 'none' | 'confirm' | 'ask' | 'checking' | 'ok' | 'failed';

	let server = $state<string | null>(null);
	let conn = $state<Conn>('none');
	let message = $state('');
	let perm = $state<NetPermission>('unsupported');
	let link = $state('');
	let previous = $state<string | null>(null);
	// A pairing link waiting for the user's yes: a link never re-points the phone silently.
	let pending = $state<Pairing | null>(null);

	async function connect() {
		conn = 'checking';
		try {
			await health();
			conn = 'ok';
		} catch (e) {
			conn = 'failed';
			message = explain(e);
		}
	}

	function offer(p: Pairing) {
		if (isPaired(p)) return start();
		pending = p;
		conn = 'confirm';
	}

	function useLink(e: SubmitEvent) {
		e.preventDefault();
		const p = parsePairing(link);
		if (!p) {
			message = 'Paste the whole link that run-server printed: it ends with a pairing code (…&token=…).';
			conn = 'failed';
			return;
		}
		offer(p);
	}

	function accept() {
		if (!pending || !pair(pending)) {
			message = 'This browser could not save the pairing (private mode?).';
			conn = 'failed';
			return;
		}
		pending = null;
		link = '';
		start();
	}

	function decline() {
		pending = null;
		start();
	}

	function forget() {
		forgetPc();
		server = previous = null;
		conn = 'none';
	}

	async function start() {
		server = getServer();
		previous = lastJob();
		if (!server) {
			conn = 'none';
			return;
		}
		perm = await localNetworkPermission();
		// Explain the browser's "local network" question before it appears.
		if (perm === 'prompt') conn = 'ask';
		else connect();
	}

	onMount(() => {
		const p = pairingFromHash();
		if (p === 'invalid') {
			message = 'That pairing link is incomplete. Scan the QR code that run-server prints again.';
			conn = 'failed';
			server = getServer();
		} else if (p) offer(p);
		else start();
	});
</script>

<main>
	<h1>Phone2CAD</h1>

	<section class="card">
		<h2><span class="n">1</span> Print the mat</h2>
		<p class="muted">Print at 100 % (actual size) on A4 and lay it flat on a table.</p>
		<p><a href="/phone2cad-mat-a4.pdf" download>Download the mat (PDF)</a></p>
	</section>

	<section class="card">
		<h2><span class="n">2</span> Connect to your PC</h2>
		{#if conn === 'confirm' && pending}
			<p>Pair this phone with the PC <strong>{pcName(pending.origin)}</strong>?</p>
			<p class="muted small host">{pcHost(pending.origin)}</p>
			{#if server && server !== pending.origin}
				<p class="warn small">
					This replaces <strong>{pcName(server)}</strong> ({pcHost(server)}). Only pair with a PC you
					run yourself: your scans will be sent to it.
				</p>
			{:else}
				<p class="muted small">Only pair with a PC you run yourself: your scans will be sent to it.</p>
			{/if}
			<div class="row">
				<button class="btn" onclick={accept}>Pair</button>
				<button class="btn ghost" onclick={decline}>Cancel</button>
			</div>
		{:else if conn === 'ok' && server}
			<p class="ok">● Connected to {pcName(server)}</p>
			<p class="muted small host">{pcHost(server)} · <button class="link" onclick={forget}>Forget this PC</button></p>
		{:else if conn === 'checking'}
			<p class="muted">Checking…</p>
		{:else if conn === 'ask'}
			<p class="muted">
				Your browser will ask to let this page reach devices on your network. Tap Allow: this is how
				your photos go straight to your own PC.
			</p>
			<button class="btn ghost" onclick={connect}>Connect</button>
		{:else if conn === 'failed'}
			<p class="warn">{message}</p>
			<button class="btn ghost" onclick={connect}>Retry</button>
		{:else}
			<p class="muted">Scan the QR code that <code>run-server</code> printed on your PC.</p>
		{/if}
		{#if server && conn !== 'ok' && conn !== 'confirm'}
			<p class="muted small host">PC: {pcName(server)} ({pcHost(server)}) · <button class="link" onclick={forget}>Forget this PC</button></p>
		{/if}

		{#if conn !== 'ok' && conn !== 'checking' && conn !== 'confirm'}
			<form onsubmit={useLink}>
				<label class="muted" for="addr">Or paste the link it printed</label>
				<input id="addr" type="url" inputmode="url" autocomplete="off" spellcheck="false"
					placeholder="https://…/#server=…&token=…" bind:value={link} />
			</form>
		{/if}
	</section>

	<section class="card">
		<h2><span class="n">3</span> Scan</h2>
		<p class="muted">
			Put the part in the marked zone. Film slowly all around it, high then low. Then turn it over
			and film the other side, so the 3D copy is complete.
		</p>
		<p class="muted small">
			Part you can't turn over? <a href={conn === 'ok' ? resolve('/scan') + '?quick=1' : undefined}>Quick scan</a>
			films one side only; it may miss parts of the object.
		</p>
		{#if previous}
			<p><a href={resolve('/result') + `#job=${previous}`}>Open your last result</a></p>
		{/if}
	</section>
</main>

<div class="dock">
	<KeepToggle />
	<a class="btn wide" class:off={conn !== 'ok'} aria-disabled={conn !== 'ok'} href={conn === 'ok' ? resolve('/scan') : undefined}>
		Start scanning
	</a>
</div>

<style>
	.n {
		display: inline-grid;
		place-items: center;
		width: 1.5rem;
		height: 1.5rem;
		margin-right: 0.35rem;
		border-radius: 50%;
		background: var(--fg);
		color: var(--bg);
		font-size: 0.85rem;
	}
	.small {
		font-size: 0.88rem;
	}
	.ok {
		color: var(--good);
		font-weight: 600;
	}
	.warn {
		color: var(--bad);
	}
	form {
		margin-top: 0.8rem;
	}
	label {
		display: block;
		font-size: 0.9rem;
		margin-bottom: 0.3rem;
	}
	.off {
		opacity: 0.4;
		pointer-events: none;
	}
	button {
		margin-top: 0.6rem;
	}
	.row {
		display: flex;
		gap: 0.6rem;
	}
	.host {
		overflow-wrap: anywhere;
	}
	button.link {
		margin: 0;
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-decoration: underline;
		cursor: pointer;
	}
</style>
