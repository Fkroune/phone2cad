<script lang="ts">
	// Top-down view of the dome around the part: centre = straight above,
	// outer ring = low views. Faint cells have an overview frame (mat in view),
	// solid cells a close-up; the outlined cell is where to go next.
	import { RINGS, SECTORS } from './keyframes';

	let {
		far,
		close,
		current = null,
		target = null
	}: {
		far: Set<number>;
		close: Set<number>;
		current?: { az: number; elev: number; sensed?: boolean } | null;
		target?: { ring: number; sector: number } | null;
	} = $props();

	const R = 46;
	const radius = (elev: number) => (R * (90 - elev)) / (90 - RINGS[0][0]);

	function cellPath(ring: number, sector: number): string {
		const [lo, hi] = RINGS[ring];
		const r0 = radius(hi), r1 = radius(lo);
		const a0 = (sector * 2 * Math.PI) / SECTORS, a1 = ((sector + 1) * 2 * Math.PI) / SECTORS;
		const p = (r: number, a: number) => `${(50 + r * Math.cos(a)).toFixed(2)} ${(50 - r * Math.sin(a)).toFixed(2)}`;
		return `M ${p(r0, a0)} L ${p(r1, a0)} A ${r1} ${r1} 0 0 0 ${p(r1, a1)} L ${p(r0, a1)} A ${r0} ${r0} 0 0 1 ${p(r0, a0)} Z`;
	}

	const all = RINGS.flatMap((_, r) => Array.from({ length: SECTORS }, (_, s) => ({ r, s, id: r * SECTORS + s })));
</script>

<svg viewBox="0 0 100 100" class="dome" role="img" aria-label="Coverage around the part">
	{#each all as c (c.id)}
		<path d={cellPath(c.r, c.s)} class:far={far.has(c.id)} class:on={close.has(c.id)} />
	{/each}
	{#if target}
		<path d={cellPath(target.ring, target.sector)} class="next" />
	{/if}
	<circle cx="50" cy="50" r="2.2" class="part" />
	{#if current && current.elev >= RINGS[0][0]}
		{@const r = radius(current.elev)}
		{@const a = (current.az * Math.PI) / 180}
		<circle cx={50 + r * Math.cos(a)} cy={50 - r * Math.sin(a)} r="3.4" class="you" class:sensed={current.sensed} />
	{/if}
</svg>

<style>
	.dome {
		width: 100%;
		height: 100%;
		display: block;
	}
	path {
		fill: rgb(255 255 255 / 0.08);
		stroke: rgb(255 255 255 / 0.35);
		stroke-width: 0.5;
		transition: fill 0.25s;
	}
	path.far {
		fill: rgb(255 255 255 / 0.3);
	}
	path.on {
		fill: var(--good);
	}
	path.next {
		fill: none;
		stroke: #f59e0b;
		stroke-width: 1.6;
	}
	.part {
		fill: #fff;
	}
	.you {
		fill: #fff;
		stroke: #000;
		stroke-width: 0.8;
	}
	.you.sensed {
		fill: rgb(255 255 255 / 0.6);
		stroke-dasharray: 1.2 0.8;
	}
</style>
