<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import Viewer from '../../lib/Viewer.svelte';
	import PreviewCloud from '../../lib/PreviewCloud.svelte';
	import type { FaceRect, Highlight } from '../../lib/types';
	import { forgetJob } from '../../lib/pc';
	import { deleteJob, explain, jobFrame, jobPreview, jobStatus, resultCheck, resultJson, resultMesh, type JobStatus, type Preview } from '../../lib/api';

	interface Feature {
		id: string;
		kind: 'hole' | 'boss' | 'distance' | 'height' | 'overall';
		text: string;
		pm: number | null;
		grade: string;
		detail: string;
		geometry: Record<string, unknown>;
		experimental?: boolean;
	}
	interface PlaneInfo {
		normal: number[];
		offset: number;
		extent?: FaceRect | null;
	}

	// How sure each measurement is, from its ± (mm). Words only: nothing to do about it.
	type Grade = 'precise' | 'good' | 'approx' | 'rough';
	const GRADES: { id: Grade; upTo: number; label: string; colour: string }[] = [
		{ id: 'precise', upTo: 0.3, label: 'Precise', colour: '#2e9e5b' },
		{ id: 'good', upTo: 0.6, label: 'Good', colour: '#8cc24a' },
		{ id: 'approx', upTo: 1.0, label: 'Approximate', colour: '#f0b429' },
		{ id: 'rough', upTo: Infinity, label: 'Rough', colour: '#e5543d' }
	];
	/** From the ± itself (older results used other grade names); the PC may only lower
	 * it (a hole seen on part of its circle is never "precise"). */
	function gradeOf(f: Feature) {
		if (f.pm === null || f.pm === undefined) return null;
		const byPm = GRADES.find((x) => f.pm! <= x.upTo)!;
		const named = GRADES.find((x) => x.id === f.grade);
		return named && GRADES.indexOf(named) > GRADES.indexOf(byPm) ? named : byPm;
	}
	const summary = $derived.by(() => {
		const counted = features.map(gradeOf).filter((g) => g !== null);
		if (!counted.length) return '';
		const parts = GRADES.map((g) => [counted.filter((c) => c!.id === g.id).length, g.label.toLowerCase()] as const)
			.filter(([n]) => n > 0)
			.map(([n, l]) => `${n} ${l}`);
		return `${counted.length} measurements: ${parts.join(', ')}`;
	});

	let job = $state('');
	let status = $state<JobStatus | null>(null);
	let error = $state('');
	let features = $state<Feature[]>([]);
	let planes = $state<PlaneInfo[]>([]);
	let mesh = $state<ArrayBuffer | null>(null);
	let selected = $state<string | null>(null);
	let removed = $state(false);
	let preview = $state<Preview | null>(null);
	let previewVersion = 0;
	let frameUrl = $state<string | null>(null);
	let frameVersion = 0;
	let warnings = $state<string[]>([]);
	let checkUrl = $state<string | null>(null);
	let quick = $state(false); // one side filmed: parts of the object may be missing

	// What the PC does, in order (stage ids from the worker's progress.json).
	const STEPS = [
		{ id: 'mat', label: 'Checking the mat' },
		{ id: 'shape', label: 'Locating every frame' },
		{ id: 'dense', label: 'Building the 3D copy' },
		{ id: 'measure', label: 'Measuring' }
	];
	const stepIndex = $derived(STEPS.findIndex((s) => s.id === status?.stage));

	function clock(s = 0): string {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	}

	const highlight = $derived.by((): Highlight | null => {
		const f = features.find((x) => x.id === selected);
		if (!f) return null;
		if (f.kind === 'hole' || f.kind === 'boss') {
			const g = f.geometry as { axis: number[]; point: number[]; radius: number; length: number };
			return { kind: 'cylinder', ...g };
		}
		const g = f.geometry as { planes?: number[]; plane?: number; points?: number[][] };
		if (g.points?.length === 2) return { kind: 'faces', faces: [], line: [g.points[0], g.points[1]] }; // hole to hole
		const ids = g.planes ?? (g.plane !== undefined ? [g.plane] : []);
		const faces = ids.map((i) => planes[i]?.extent).filter((e): e is FaceRect => !!e);
		if (!faces.length) return null;
		let line: [number[], number[]] | null = null;
		const c = faces[0].centre;
		if (f.kind === 'distance' && ids.length === 2 && planes[ids[1]]) {
			// From the first face straight across to the second one's plane.
			const { normal: n, offset: d } = planes[ids[1]];
			const t = n[0] * c[0] + n[1] * c[1] + n[2] * c[2] + d;
			line = [c, [c[0] - t * n[0], c[1] - t * n[1], c[2] - t * n[2]]];
		} else if (f.kind === 'height') {
			line = [c, [c[0], c[1], 0]]; // down to the mat
		}
		return { kind: 'faces', faces, line };
	});

	async function load() {
		const r = await resultJson<{ features: Feature[]; planes?: PlaneInfo[]; mode?: string; check?: string }>(job);
		// Show only well-formed measurements (the server is trusted, but a broken file must not break the page).
		features = r.features.filter(
			(f) => f && typeof f === 'object' && typeof f.text === 'string' && typeof f.kind === 'string' &&
				(f.pm === null || f.pm === undefined || Number.isFinite(f.pm))
		);
		planes = Array.isArray(r.planes) ? r.planes : [];
		quick = r.mode === 'quick';
		mesh = await resultMesh(job);
		if (r.check) checkUrl = await resultCheck(job);
	}

	async function remove() {
		try {
			await deleteJob(job);
			forgetJob(job);
			removed = true;
		} catch (e) {
			error = explain(e);
		}
	}

	onMount(() => {
		// #job= keeps the id out of requests to the app's host; ?job= is read for older links.
		job = /(?:^#|&)job=([a-f0-9]{32})(?:&|$)/.exec(location.hash)?.[1] ?? new URLSearchParams(location.search).get('job') ?? '';
		if (location.search.includes('job=') && /^[a-f0-9]{32}$/.test(job))
			history.replaceState(null, '', `${location.pathname}#job=${job}`);
		if (!/^[a-f0-9]{32}$/.test(job)) {
			error = 'This link has no scan in it.';
			return;
		}
		let timer: ReturnType<typeof setTimeout>;
		let alive = true;
		const poll = async () => {
			try {
				status = await jobStatus(job);
				if (status.warnings) warnings = status.warnings;
				if (status.status === 'done') return await load();
				if ((status.frame ?? 0) > frameVersion) {
					frameVersion = status.frame ?? 0;
					jobFrame(job)
						.then((u) => {
							if (frameUrl) URL.revokeObjectURL(frameUrl);
							frameUrl = u;
						})
						.catch(() => {});
				}
				if ((status.preview ?? 0) > previewVersion) {
					previewVersion = status.preview ?? 0;
					jobPreview(job).then((p) => (preview = p)).catch(() => {});
				}
				if (status.status === 'failed') return;
			} catch (e) {
				error = explain(e);
				return;
			}
			if (alive) timer = setTimeout(poll, 2000);
		};
		poll();
		return () => {
			alive = false;
			clearTimeout(timer);
			if (frameUrl) URL.revokeObjectURL(frameUrl);
			if (checkUrl) URL.revokeObjectURL(checkUrl);
		};
	});
</script>

{#if mesh}
	<div class="view"><Viewer ply={mesh} {highlight} /></div>
{/if}

<main class:with-view={!!mesh}>
	{#if error}
		<h1>Result</h1>
		<p class="bad">{error}</p>
	{:else if !status}
		<h1>Result</h1>
		<p class="muted">Loading…</p>
	{:else if status.status === 'queued' || status.status === 'running' || status.status === 'uploading'}
		<h1>Working on it</h1>
		{#if status.status === 'queued'}
			<p>Waiting for the PC{status.position ? ` (number ${status.position} in line)` : ''}</p>
		{:else}
			<ol class="steps">
				{#each STEPS as s, i (s.id)}
					<li class:done={i < stepIndex} class:now={i === stepIndex}>
						<span class="dot" aria-hidden="true"></span>
						<span>{s.label}</span>
						{#if i === stepIndex && status.step}<span class="step">{status.step}</span>{/if}
					</li>
				{/each}
			</ol>
		{/if}
		<div class="bar"><i style:width={`${Math.round(100 * (status.progress ?? 0))}%`}></i></div>
		<p class="muted small">
			{#if status.elapsed_s}{clock(status.elapsed_s)} so far · {/if}usually 30 to 50 minutes. You can close this page and come back.
		</p>
		{#if preview}
			<PreviewCloud data={preview} />
			<p class="muted small caption">
				{preview.label}{preview.points.length ? ` · ${Math.round(preview.points.length / 3).toLocaleString()} points shown` : ''}{preview.cameras.length ? ` · ${preview.cameras.length / 3} cameras` : ''}
			</p>
			<p class="legend small">
				<span><i style:background="#22a85a"></i>placed by the mat</span>
				<span><i style:background="#3b82f6"></i>placed by matching</span>
				<span><i style:background="#f59e0b"></i>working on it</span>
				<span><i style:background="#e53333"></i>unclear outline</span>
			</p>
		{/if}
		{#if frameUrl}
			<figure class="frame">
				<img src={frameUrl} alt="The frame the PC is working on" />
				<figcaption class="muted small">{status.frame_label}</figcaption>
			</figure>
		{/if}
		{#if warnings.length}
			<ul class="warnings">
				{#each warnings as w (w)}<li>{w}</li>{/each}
			</ul>
		{/if}
		{#if status.stats && Object.keys(status.stats).length}
			<dl class="stats">
				{#each Object.entries(status.stats) as [k, v] (k)}
					<dt>{k}</dt>
					<dd>{typeof v === 'number' ? v.toLocaleString() : v}</dd>
				{/each}
			</dl>
		{/if}
	{:else if status.status === 'failed'}
		<h1>That scan didn't work</h1>
		<p>{status.error}</p>
		<p class="muted">Film again, slowly, keeping the mat in view.</p>
		{#if warnings.length}
			<ul class="warnings">
				{#each warnings as w (w)}<li>{w}</li>{/each}
			</ul>
		{/if}
	{:else if status.status === 'done' && !mesh}
		<h1>Result</h1>
		<p class="muted">Downloading the 3D copy…</p>
	{:else}
		<h2>Measurements{#if quick}<span class="x quick">Quick scan</span>{/if}</h2>
		{#if quick}<p class="muted small">Only one side was filmed: parts of the object may be missing from the 3D copy.</p>{/if}
		{#if summary}<p class="muted small">{summary}. Tap one to see it on the part.</p>{/if}
		{#if checkUrl}
			<figure class="frame check">
				<img src={checkUrl} alt="One of your photos with the outline of the 3D copy drawn over it" />
				<figcaption><b>Does this match your part?</b> <span class="muted small">The green line is the 3D copy's outline, drawn over one of your photos.</span></figcaption>
			</figure>
		{/if}
		<p class="legend small">
			{#each GRADES as g (g.id)}
				<span><i style:background={g.colour}></i>{g.label} {g.upTo === Infinity ? '(over ±1 mm)' : `(±${g.upTo} mm)`}</span>
			{/each}
			<span><i style:background="#9a9a9a"></i>not measured</span>
		</p>
		<ul class="feats">
			{#each features as f (f.id)}
				{@const g = gradeOf(f)}
				<li>
					<button class:sel={selected === f.id} onclick={() => (selected = selected === f.id ? null : f.id)}>
						<span class="t">{f.text}{#if f.experimental}<span class="x">experimental</span>{/if}</span>
						{#if g}<span class="g"><i style:background={g.colour}></i>{g.label}</span>{/if}
						<span class="d">{f.detail}</span>
					</button>
				</li>
			{/each}
		</ul>
		<p class="muted small">
			Estimates, not certified measurements. Check critical sizes before relying on them.
			<a href="https://github.com/Fkroune/scanfit#disclaimer-experimental-software-use-at-your-own-risk"
				target="_blank" rel="noopener noreferrer">Disclaimer</a>
		</p>
		{#if warnings.length}
			<h2>Notes for your next scan</h2>
			<ul class="warnings">
				{#each warnings as w (w)}<li>{w}</li>{/each}
			</ul>
		{/if}
		{#if removed}
			<p class="muted small">Removed from your PC.</p>
		{:else}
			<p class="muted small">
				Your PC keeps this result for 24 hours. <button class="link" onclick={remove}>Remove it now</button>
			</p>
		{/if}
	{/if}
</main>

<div class="dock"><a class="btn wide" href={resolve('/scan')}>New scan</a></div>

<style>
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem 0.9rem;
		margin: 0.2rem 0 0.8rem;
		color: var(--muted);
	}
	.legend i {
		display: inline-block;
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 50%;
		margin-right: 0.3rem;
		vertical-align: middle;
	}
	.frame {
		margin: 0 0 1rem;
	}
	.frame img {
		display: block;
		width: 100%;
		max-width: 22rem;
		border-radius: var(--radius);
		border: 1px solid var(--line);
	}
	.check img {
		/* fits below the sticky 3D view on a phone */
		max-width: 100%;
		max-height: 24vh;
		width: auto;
	}
	.check figcaption {
		margin-top: 0.4rem;
	}
	.quick {
		font-size: 0.6em;
		font-weight: 600;
	}
	.warnings {
		margin: 0 0 1rem;
		padding: 0.7rem 0.9rem 0.7rem 2rem;
		border: 1px solid var(--line);
		border-left: 4px solid #f59e0b;
		border-radius: var(--radius);
		background: var(--card);
	}
	.warnings li + li {
		margin-top: 0.4rem;
	}
	.view {
		position: sticky;
		top: 0;
		height: 52vh;
		background: var(--card);
		border-bottom: 1px solid var(--line);
		z-index: 1;
	}
	main.with-view {
		padding-top: 1rem;
	}
	.feats {
		list-style: none;
		padding: 0;
		margin: 0.5rem 0 1rem;
	}
	.feats button {
		all: unset;
		box-sizing: border-box;
		width: 100%;
		display: grid;
		grid-template-columns: 1fr auto;
		gap: 0.1rem 0.6rem;
		padding: 0.75rem 0.9rem;
		margin-bottom: 0.5rem;
		border: 1px solid var(--line);
		border-radius: 12px;
		background: var(--card);
		cursor: pointer;
	}
	.feats button.sel {
		border-color: var(--good);
	}
	.x {
		margin-left: 0.5em;
		padding: 0 0.45em;
		border: 1px solid #b26a00;
		border-radius: 999px;
		color: #b26a00;
		font-size: 0.75em;
		vertical-align: middle;
	}
	.t {
		font-weight: 650;
		font-variant-numeric: tabular-nums;
	}
	.d {
		grid-column: 1 / -1;
		color: var(--muted);
		font-size: 0.88rem;
	}
	.g {
		font-size: 0.85rem;
		font-weight: 600;
		align-self: center;
	}
	.g {
		color: var(--muted);
		white-space: nowrap;
	}
	.g i {
		display: inline-block;
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 50%;
		margin-right: 0.35rem;
		vertical-align: middle;
	}
	.bad {
		color: var(--bad);
	}
	.small {
		font-size: 0.88rem;
	}
	.bar {
		margin: 0.8rem 0;
	}
	.link {
		all: unset;
		text-decoration: underline;
		cursor: pointer;
	}
	.steps {
		list-style: none;
		padding: 0;
		margin: 0.8rem 0 0;
		display: grid;
		gap: 0.45rem;
	}
	.steps li {
		display: grid;
		grid-template-columns: 1.1rem 1fr;
		align-items: center;
		column-gap: 0.55rem;
		color: var(--muted);
	}
	.steps li.done,
	.steps li.now {
		color: var(--fg);
	}
	.steps li.now {
		font-weight: 650;
	}
	.dot {
		width: 0.7rem;
		height: 0.7rem;
		border-radius: 50%;
		border: 2px solid var(--line);
		justify-self: center;
	}
	.done .dot {
		background: var(--good);
		border-color: var(--good);
	}
	.now .dot {
		border-color: var(--fg);
		animation: pulse 1.4s ease-in-out infinite;
	}
	.step {
		grid-column: 2;
		font-weight: 400;
		font-size: 0.88rem;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
	@keyframes pulse {
		50% {
			background: var(--fg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.now .dot {
			animation: none;
			background: var(--fg);
		}
	}
	.caption {
		margin-top: 0.4rem;
	}
	.stats {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.25rem 0.9rem;
		margin: 0.8rem 0 0;
		font-size: 0.92rem;
	}
	.stats dt {
		color: var(--muted);
	}
	.stats dd {
		margin: 0;
		font-variant-numeric: tabular-nums;
		text-align: right;
	}
</style>
