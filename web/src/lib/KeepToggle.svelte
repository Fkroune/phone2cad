<script lang="ts">
	import { onMount } from 'svelte';
	import { getKeepPhotos, setKeepPhotos } from './pc';

	let on = $state(false);
	onMount(() => (on = getKeepPhotos()));
</script>

<label class="keep">
	<input type="checkbox" bind:checked={on} onchange={() => setKeepPhotos(on)} />
	<span>
		Keep this scan's photos on my PC for 24 hours
		<small>To check a bad result. Off by default; applies to the next scan only.</small>
	</span>
</label>

<style>
	.keep {
		display: flex;
		gap: 0.6rem;
		align-items: flex-start;
		margin-bottom: 0.6rem;
		font-size: 0.9rem;
	}
	.keep input {
		margin-top: 0.2rem;
		flex: none;
	}
	small {
		display: block;
		color: var(--muted);
	}
</style>
