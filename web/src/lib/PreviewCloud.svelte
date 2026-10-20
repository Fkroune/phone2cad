<script lang="ts">
	// Live view while the PC works: the mat outline, the camera positions it has
	// worked out, and the 3D points found so far. World units are millimetres,
	// Z up, the mat at z = 0. Turns slowly so it is visibly alive; drag to look.
	import { onMount } from 'svelte';
	import * as THREE from 'three';
	import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
	import type { Preview } from './api';

	let { data }: { data: Preview | null } = $props();

	let host: HTMLDivElement;
	let points: THREE.Points;
	let cams: THREE.Points;
	let active: THREE.Points;

	// Camera colours by kind (see api.ts Preview.kinds); kind 3 is also drawn large.
	const KIND_RGB = [
		[0.13, 0.66, 0.35], // placed by the mat: green
		[0.23, 0.51, 0.96], // placed by matching: blue
		[0.9, 0.2, 0.2], // unclear outline: red
		[0.98, 0.62, 0.1] // being worked on: orange
	];
	let fit: (() => void) | null = null;

	// The printed mat's board (12 x 18 squares of 15 mm), centred on the origin.
	const MAT_W = 180, MAT_H = 270;

	onMount(() => {
		const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
		renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
		host.appendChild(renderer.domElement);
		const scene = new THREE.Scene();
		const camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
		camera.up.set(0, 0, 1);

		const fg = getComputedStyle(host).color;
		const outline = new THREE.LineLoop(
			new THREE.BufferGeometry().setFromPoints([
				new THREE.Vector3(-MAT_W / 2, -MAT_H / 2, 0),
				new THREE.Vector3(MAT_W / 2, -MAT_H / 2, 0),
				new THREE.Vector3(MAT_W / 2, MAT_H / 2, 0),
				new THREE.Vector3(-MAT_W / 2, MAT_H / 2, 0)
			]),
			new THREE.LineBasicMaterial({ color: new THREE.Color(fg), transparent: true, opacity: 0.35 })
		);
		scene.add(outline);

		points = new THREE.Points(
			new THREE.BufferGeometry(),
			new THREE.PointsMaterial({ size: 1.6, vertexColors: true, sizeAttenuation: true })
		);
		cams = new THREE.Points(
			new THREE.BufferGeometry(),
			new THREE.PointsMaterial({ size: 6, vertexColors: true, sizeAttenuation: false })
		);
		active = new THREE.Points(
			new THREE.BufferGeometry(),
			new THREE.PointsMaterial({ size: 12, color: 0xf59e0b, sizeAttenuation: false })
		);
		scene.add(points, cams, active);

		const controls = new OrbitControls(camera, renderer.domElement);
		controls.enableDamping = true;
		controls.autoRotate = true;
		controls.autoRotateSpeed = 0.8;
		controls.addEventListener('start', () => (controls.autoRotate = false));

		let framed = false;
		fit = () => {
			// Frame the mat and whatever has been found, once; then let the user move.
			const box = new THREE.Box3(new THREE.Vector3(-MAT_W / 2, -MAT_H / 2, 0), new THREE.Vector3(MAT_W / 2, MAT_H / 2, 20));
			if (points.geometry.getAttribute('position')) box.expandByObject(points);
			const centre = box.getCenter(new THREE.Vector3());
			const size = box.getSize(new THREE.Vector3()).length();
			if (!framed) {
				camera.position.copy(centre).add(new THREE.Vector3(0.55, -0.8, 0.75).multiplyScalar(size));
				framed = true;
			}
			controls.target.copy(centre);
		};
		fit();

		const resize = () => {
			const w = host.clientWidth, h = host.clientHeight;
			renderer.setSize(w, h, false);
			camera.aspect = w / h;
			camera.updateProjectionMatrix();
		};
		const ro = new ResizeObserver(resize);
		ro.observe(host);
		resize();

		// Render only while visible: no work when scrolled away or in a background tab.
		let visible = true;
		const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting));
		io.observe(host);
		let raf = 0;
		const tick = () => {
			if (visible && !document.hidden) {
				controls.update();
				renderer.render(scene, camera);
			}
			raf = requestAnimationFrame(tick);
		};
		tick();

		return () => {
			cancelAnimationFrame(raf);
			ro.disconnect();
			io.disconnect();
			controls.dispose();
			points.geometry.dispose();
			cams.geometry.dispose();
			active.geometry.dispose();
			renderer.dispose();
			renderer.domElement.remove();
		};
	});

	$effect(() => {
		if (!data || !points) return;
		const g = new THREE.BufferGeometry();
		if (data.points.length) {
			g.setAttribute('position', new THREE.Float32BufferAttribute(data.points, 3));
			const rgb = data.colors.length === data.points.length
				? data.colors.map((c) => c / 255)
				: new Array(data.points.length).fill(0.6);
			g.setAttribute('color', new THREE.Float32BufferAttribute(rgb, 3));
		}
		points.geometry.dispose();
		points.geometry = g;
		const n = data.cameras.length / 3;
		const kinds = data.kinds?.length === n ? data.kinds : new Array(n).fill(1);
		const c = new THREE.BufferGeometry();
		const a = new THREE.BufferGeometry();
		if (n) {
			c.setAttribute('position', new THREE.Float32BufferAttribute(data.cameras, 3));
			c.setAttribute('color', new THREE.Float32BufferAttribute(kinds.flatMap((k) => KIND_RGB[k] ?? KIND_RGB[1]), 3));
			const on = kinds.flatMap((k, i) => (k === 3 ? data.cameras.slice(3 * i, 3 * i + 3) : []));
			if (on.length && on.length < data.cameras.length) a.setAttribute('position', new THREE.Float32BufferAttribute(on, 3));
		}
		cams.geometry.dispose();
		cams.geometry = c;
		active.geometry.dispose();
		active.geometry = a;
		fit?.();
	});
</script>

<div class="preview" bind:this={host} aria-label="Live 3D preview of the scan being processed"></div>

<style>
	.preview {
		width: 100%;
		aspect-ratio: 4 / 3;
		border-radius: var(--radius);
		border: 1px solid var(--line);
		background: var(--card);
		overflow: hidden;
		color: var(--fg);
		touch-action: none;
	}
	.preview :global(canvas) {
		display: block;
		width: 100%;
		height: 100%;
	}
</style>
