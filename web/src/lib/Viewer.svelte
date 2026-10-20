<script lang="ts">
	// three.js view of the 3D copy. World units are millimetres, Z up, the mat
	// surface is z = 0 (drawn as a faint 10 mm grid).
	import { onMount } from 'svelte';
	import * as THREE from 'three';
	import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
	import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';
	import type { Highlight } from './types';

	let { ply, highlight = null }: { ply: ArrayBuffer; highlight?: Highlight | null } = $props();

	let host: HTMLDivElement;
	let overlay: THREE.Group;

	onMount(() => {
		const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
		renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
		host.appendChild(renderer.domElement);

		const scene = new THREE.Scene();
		const camera = new THREE.PerspectiveCamera(35, 1, 0.5, 5000);
		camera.up.set(0, 0, 1);

		const geom = new PLYLoader().parse(ply);
		// WebGL has no 64-bit floats: a PLY with "property double" must be narrowed.
		for (const [name, attr] of Object.entries(geom.attributes)) {
			if (attr.array instanceof Float64Array) {
				geom.setAttribute(name, new THREE.BufferAttribute(new Float32Array(attr.array), attr.itemSize, attr.normalized));
			}
		}
		geom.computeVertexNormals();
		geom.computeBoundingBox();
		const box = geom.boundingBox!;
		const centre = box.getCenter(new THREE.Vector3());
		const size = box.getSize(new THREE.Vector3()).length();

		const mesh = new THREE.Mesh(
			geom,
			new THREE.MeshStandardMaterial({
				vertexColors: geom.hasAttribute('color'),
				color: geom.hasAttribute('color') ? 0xffffff : 0xd8d4cc,
				roughness: 0.85,
				metalness: 0,
				side: THREE.DoubleSide
			})
		);
		scene.add(mesh);

		const grid = new THREE.GridHelper(Math.ceil(size / 10) * 20, Math.ceil(size / 10) * 2, 0x888888, 0x888888);
		grid.rotation.x = Math.PI / 2;
		grid.position.set(centre.x, centre.y, 0);
		(grid.material as THREE.Material).transparent = true;
		(grid.material as THREE.Material).opacity = 0.25;
		scene.add(grid);

		overlay = new THREE.Group();
		scene.add(overlay);

		scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.6));
		const sun = new THREE.DirectionalLight(0xffffff, 1.4);
		sun.position.set(1, -1.5, 2);
		scene.add(sun);

		camera.position.copy(centre).add(new THREE.Vector3(0.9, -1.4, 1.1).multiplyScalar(size));
		const controls = new OrbitControls(camera, renderer.domElement);
		controls.target.copy(centre);
		controls.enableDamping = true;
		controls.update();

		const resize = () => {
			const w = host.clientWidth, h = host.clientHeight;
			renderer.setSize(w, h, false);
			camera.aspect = w / h;
			camera.updateProjectionMatrix();
		};
		const ro = new ResizeObserver(resize);
		ro.observe(host);
		resize();

		let raf = 0;
		const tick = () => {
			controls.update();
			renderer.render(scene, camera);
			raf = requestAnimationFrame(tick);
		};
		tick();

		return () => {
			cancelAnimationFrame(raf);
			ro.disconnect();
			controls.dispose();
			geom.dispose();
			renderer.dispose();
			renderer.domElement.remove();
		};
	});

	$effect(() => {
		if (!overlay) return;
		overlay.clear();
		if (highlight?.kind === 'faces') {
			const mark = 0x22c3e6;
			for (const f of highlight.faces) {
				const [u, v, c] = [new THREE.Vector3(...f.u), new THREE.Vector3(...f.v), new THREE.Vector3(...f.centre)];
				const n = new THREE.Vector3().crossVectors(u, v);
				const quad = new THREE.Mesh(
					new THREE.PlaneGeometry(2 * f.half[0], 2 * f.half[1]),
					new THREE.MeshBasicMaterial({ color: mark, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthTest: false })
				);
				quad.matrixAutoUpdate = false;
				quad.matrix.makeBasis(u, v, n).setPosition(c.clone().addScaledVector(n, 0.15));
				const edge = new THREE.LineSegments(
					new THREE.EdgesGeometry(quad.geometry),
					new THREE.LineBasicMaterial({ color: mark, depthTest: false })
				);
				edge.matrixAutoUpdate = false;
				edge.matrix.copy(quad.matrix);
				quad.renderOrder = edge.renderOrder = 2;
				overlay.add(quad, edge);
			}
			if (highlight.line) {
				const line = new THREE.Line(
					new THREE.BufferGeometry().setFromPoints(highlight.line.map((p) => new THREE.Vector3(...p))),
					new THREE.LineBasicMaterial({ color: 0xffffff, depthTest: false })
				);
				line.renderOrder = 3;
				overlay.add(line);
			}
			return;
		}
		if (highlight?.kind !== 'cylinder') return;
		const { axis, point, radius, length } = highlight;
		const cyl = new THREE.Mesh(
			new THREE.CylinderGeometry(radius, radius, Math.max(length, 1), 48, 1, true),
			new THREE.MeshBasicMaterial({ color: 0x1f9d6b, transparent: true, opacity: 0.45, side: THREE.DoubleSide })
		);
		cyl.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(...axis).normalize());
		cyl.position.set(point[0], point[1], point[2]);
		overlay.add(cyl);
	});
</script>

<div class="viewer" bind:this={host}></div>

<style>
	.viewer {
		width: 100%;
		height: 100%;
		touch-action: none;
	}
	.viewer :global(canvas) {
		width: 100%;
		height: 100%;
		display: block;
	}
</style>
