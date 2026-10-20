<script lang="ts">
	import { version } from '$app/env';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { resolve } from '$app/paths';
	import Dome from '../../lib/Dome.svelte';
	import { createJob, explain, putFrame, startJob } from '../../lib/api';
	import { encodeJpeg, hasTorch, lockExposure, nextFrame, openCamera, setTorch, unlockExposure } from '../../lib/camera';
	import { CAL_FRAMES, KeyframeSelector, MAX_FRAMES, MIN_FRAMES, thumbnail, type Analysis, type MatLayout, type View } from '../../lib/keyframes';
	import { Motion } from '../../lib/motion';
	import { getServer, pcName, rememberJob } from '../../lib/pc';

	const PREVIEW = 640;

	let video: HTMLVideoElement;
	let phase = $state<'starting' | 'capturing' | 'uploading' | 'flip' | 'error'>('starting');
	// Two passes by default: the part flipped for the second, so the PC can close
	// the model underneath. A quick scan (?quick=1) films one side only.
	let side = $state<1 | 2>(1);
	let quick = $state(false);
	let jobId: string | null = null;
	let mat: MatLayout;
	let analyseFn: (img: ImageData, focal: number) => Promise<Analysis>;
	let instruction = $state('Starting the camera…');
	let error = $state('');
	let calViews = $state(0);
	let count = $state(0); // all kept frames
	let far = $state(new Set<number>());
	let close = $state(new Set<number>());
	let current = $state<View | null>(null);
	let target = $state<{ ring: number; sector: number } | null>(null);
	let locked = $state<string[]>([]);
	let torchable = $state(false);
	let torch = $state(false);

	async function toggleTorch() {
		if (track) torch = await setTorch(track, !torch);
	}
	let track: MediaStreamTrack | null = null;
	const motion = new Motion();
	let sent = $state(0);
	let total = $state(0);
	let flash = $state(false);

	let selector: KeyframeSelector;
	const frames: Blob[] = [];
	let running = true;

	function finish() {
		running = false;
		if (torch && track) setTorch(track, false).then((t) => (torch = t));
		upload();
	}

	async function upload() {
		phase = 'uploading';
		const pc = pcName(getServer() ?? '') || 'your PC';
		instruction = side === 1 ? `Sending to ${pc}` : `Sending the other side to ${pc}`;
		total = frames.length;
		sent = 0;
		try {
			jobId ??= (await createJob()).id;
			const id = jobId;
			let next = 0;
			const lane = async () => {
				while (next < frames.length) {
					const i = next++;
					await putFrame(id, i, frames[i], side);
					sent++;
				}
			};
			await Promise.all([lane(), lane(), lane()]);
			frames.length = 0; // sent: free the phone's memory before the second pass
			if (side === 1 && !quick) {
				phase = 'flip';
				instruction = 'Now flip the part';
				return;
			}
			await process();
		} catch (e) {
			phase = 'error';
			error = explain(e);
		}
	}

	async function process() {
		try {
			await startJob(jobId!);
			rememberJob(jobId!);
			await goto(resolve('/result') + `#job=${jobId}`);
		} catch (e) {
			phase = 'error';
			error = explain(e);
		}
	}

	function filmOtherSide() {
		side = 2;
		selector = new KeyframeSelector(mat); // fresh calibration: the part (and maybe the mat) moved
		calViews = 0;
		count = 0;
		far = new Set();
		close = new Set();
		current = null;
		target = null;
		locked = [];
		if (track) unlockExposure(track); // the light may differ after the flip: meter again
		running = true;
		phase = 'capturing';
		instruction = 'Flip the part, then show the whole mat';
		loop(analyseFn);
	}

	onMount(() => {
		quick = new URLSearchParams(location.search).get('quick') === '1';
		let stream: MediaStream | null = null;
		const worker = new Worker(`/cv/cv-worker.js?v=${encodeURIComponent(version)}`);
		let reqId = 0;
		const pending = new Map<number, (a: Analysis) => void>();
		worker.onmessage = (e) => {
			const m = e.data;
			if (m.type === 'result') pending.get(m.id)?.(m);
			if (m.type === 'error') pending.get(m.id)?.({ markers: 0, inView: false, sharp: 0, pose: null });
			if (m.id !== undefined) pending.delete(m.id);
		};
		const analyse = (img: ImageData, focal: number) =>
			new Promise<Analysis>((res) => {
				const id = ++reqId;
				pending.set(id, res);
				worker.postMessage({ type: 'frame', id, width: img.width, height: img.height, focal, buffer: img.data.buffer }, [img.data.buffer]);
			});

		(async () => {
			try {
				mat = await (await fetch('/mat.json')).json();
				analyseFn = analyse;
				selector = new KeyframeSelector(mat);
				const ready = new Promise<void>((res, rej) => {
					const h = (e: MessageEvent) => {
						if (e.data.type === 'ready') res();
						if (e.data.type === 'error') rej(new Error(e.data.message));
					};
					worker.addEventListener('message', h);
				});
				worker.postMessage({ type: 'init', mat });
				stream = await openCamera(video);
				track = stream.getVideoTracks()[0];
				torchable = hasTorch(track);
				motion.start();
				instruction = 'Loading…';
				await ready;
				phase = 'capturing';
				await loop(analyse);
			} catch (e) {
				phase = 'error';
				const name = (e as Error)?.name;
				error = name === 'NotAllowedError'
					? 'Camera access is off. Allow the camera for this site in your browser settings, then reload.'
					: `Could not start: ${(e as Error)?.message ?? e}`;
			}
		})();

		return () => {
			running = false;
			motion.stop();
			worker.terminate();
			stream?.getTracks().forEach((t) => t.stop());
		};
	});

	async function loop(analyse: (img: ImageData, focal: number) => Promise<Analysis>) {
		const canvas = document.createElement('canvas');
		const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
		let lastMove = performance.now();
		while (running) {
			await nextFrame(video);
			if (!running) break;
			const bmp = await createImageBitmap(video);
			const scale = PREVIEW / Math.max(bmp.width, bmp.height);
			canvas.width = Math.round(bmp.width * scale);
			canvas.height = Math.round(bmp.height * scale);
			ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
			const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
			const thumb = thumbnail(img); // before analyse(): its pixel buffer moves to the worker
			// Rough focal length for the live pose (only used for coverage).
			const a = await analyse(img, 0.8 * Math.max(canvas.width, canvas.height));
			const size = `${bmp.width}x${bmp.height}`;
			const now = performance.now();
			// A good mat pose ties the motion sensors to the mat (their drift is reset each time).
			if (a.pose?.R && a.markers >= 8) motion.anchor(a.pose.R);
			const v = selector.view(a, motion.worldFromCam());
			const d = selector.consider(a, v, thumb, size, now, motion.sinceMark());
			if (!d.still) lastMove = now;
			current = v;
			if (d.kind && running) {
				const wasCalibrated = selector.calibrated;
				selector.accept(d.kind, v, thumb, size, now);
				motion.mark();
				frames.push(await encodeJpeg(bmp));
				calViews = selector.kept.length;
				count = selector.total;
				far = new Set(selector.cells.far);
				close = new Set(selector.cells.close);
				flash = true;
				setTimeout(() => (flash = false), 120);
				navigator.vibrate?.(15);
				// Calibration done: same colour from here on (better outlines on the PC).
				if (!wasCalibrated && selector.calibrated && track) lockExposure(track).then((l) => (locked = l));
			}
			bmp.close();
			const t = selector.calibrated ? selector.target(v) : null;
			target = t ? { ring: t.ring, sector: t.sector } : null;
			instruction = selector.instruction(a, v, d.sharp, now - lastMove);
			if (frames.length >= MAX_FRAMES) {
				finish();
				break;
			}
		}
	}
</script>

<div class="stage">
	<!-- svelte-ignore a11y_media_has_caption -->
	<video bind:this={video} class:flash playsinline muted></video>

	<div class="top">
		<p class="say" aria-live="polite">{phase === 'error' ? 'Stopped' : instruction}</p>
	</div>

	<div class="bottom">
		{#if phase === 'capturing' || phase === 'starting'}
			<div class="row">
				<div class="dome">
					<Dome {far} {close} {target} current={current ? { az: current.az, elev: current.elev, sensed: current.sensed } : null} />
				</div>
				<div class="count">
					<span class="tag">{quick ? 'Quick scan' : `Side ${side} of 2`}</span>
					{#if calViews < CAL_FRAMES}
						<b>{calViews}</b><span>/ {CAL_FRAMES} calibration views</span>
					{:else}
						<b>{count}</b><span>frames · calibrated{locked.length ? ' · colour locked' : ''}</span>
					{/if}
				</div>
			</div>
			<div class="row">
				<a class="btn ghost light" href={resolve('/')}>Cancel</a>
				{#if torchable}
					<button class="btn ghost light" class:on={torch} aria-pressed={torch} onclick={toggleTorch}>
						{torch ? 'Light off' : 'Light'}
					</button>
				{/if}
				<button class="btn grow" disabled={calViews < CAL_FRAMES || count < MIN_FRAMES} onclick={finish}>Finish</button>
			</div>
		{:else if phase === 'flip'}
			<p class="choice">
				<b>Side 1 sent.</b> Turn the part over onto another face, put it back in the marked zone,
				then film it all around again.
			</p>
			<div class="row">
				<button class="btn ghost light" onclick={process} title="Parts of the object may be missing">Quick scan: stop here</button>
				<button class="btn grow" onclick={filmOtherSide}>Film side 2</button>
			</div>
		{:else if phase === 'uploading'}
			<p>Sending {sent} of {total}</p>
			<div class="bar"><i style:width={`${(100 * sent) / Math.max(1, total)}%`}></i></div>
		{:else}
			<p class="err">{error}</p>
			<div class="row">
				<a class="btn ghost light" href={resolve('/')}>Back</a>
				{#if count >= MIN_FRAMES}<button class="btn grow" onclick={upload}>Try sending again</button>{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	.btn.on {
		background: #f5c84b;
		color: #111;
		border-color: #f5c84b;
	}
	.tag {
		font-size: 0.78rem;
		font-weight: 650;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		opacity: 0.8;
	}
	.choice {
		margin: 0 0 0.9rem;
		line-height: 1.45;
	}
	.stage {
		position: fixed;
		inset: 0;
		background: #000;
		color: #fff;
	}
	video {
		width: 100%;
		height: 100%;
		object-fit: cover;
		transition: opacity 0.1s;
	}
	video.flash {
		opacity: 0.7;
	}
	.top {
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		padding: calc(1rem + env(safe-area-inset-top)) 1rem 0;
		text-align: center;
	}
	.say {
		display: inline-block;
		margin: 0;
		padding: 0.55rem 1.1rem;
		border-radius: 999px;
		background: rgb(0 0 0 / 0.6);
		font-size: 1.25rem;
		font-weight: 600;
	}
	.bottom {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		padding: 1rem 1rem calc(1rem + env(safe-area-inset-bottom));
		background: linear-gradient(transparent, rgb(0 0 0 / 0.75) 30%);
		display: grid;
		gap: 0.8rem;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.8rem;
	}
	.dome {
		width: 7.5rem;
		height: 7.5rem;
	}
	.count b {
		font-size: 2.2rem;
		display: block;
		line-height: 1;
	}
	.count span {
		opacity: 0.75;
	}
	.grow {
		flex: 1;
	}
	.btn.light {
		color: #fff;
		border-color: rgb(255 255 255 / 0.4);
	}
	.btn:not(.ghost) {
		background: #fff;
		color: #000;
	}
	.err {
		color: #ffb4a6;
	}
</style>
