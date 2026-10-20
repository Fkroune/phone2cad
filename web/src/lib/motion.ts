// The phone's motion sensors, for two jobs the camera image alone does badly:
// telling "the phone moved" from "the picture flickered", and knowing which
// way the camera looks when the mat is out of view (close-ups).
//
// Orientation: the browser's fused gyroscope/accelerometer reading
// (deviceorientation, relative: no compass, so no magnetic disturbance from
// metal parts). It drifts slowly, so it is re-anchored to the mat every time
// the mat gives a camera pose. Movement: rotation since a mark, plus a rough
// "how much did it shake or travel" from the acceleration without gravity.

export type Mat3 = number[]; // row-major 3x3

const DEG = Math.PI / 180;

/** W3C DeviceOrientation: earth <- device rotation, R = Rz(alpha) Rx(beta) Ry(gamma). */
function fromEuler(alpha: number, beta: number, gamma: number): Mat3 {
	const [a, b, g] = [alpha * DEG, beta * DEG, gamma * DEG];
	const [ca, sa, cb, sb, cg, sg] = [Math.cos(a), Math.sin(a), Math.cos(b), Math.sin(b), Math.cos(g), Math.sin(g)];
	return [
		ca * cg - sa * sb * sg, -cb * sa, ca * sg + cg * sa * sb,
		cg * sa + ca * sb * sg, ca * cb, sa * sg - ca * cg * sb,
		-cb * sg, sb, cb * cg
	];
}

export function mul(A: Mat3, B: Mat3): Mat3 {
	const C = new Array(9).fill(0);
	for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) for (let k = 0; k < 3; k++) C[3 * i + j] += A[3 * i + k] * B[3 * k + j];
	return C;
}

export function transpose(A: Mat3): Mat3 {
	return [A[0], A[3], A[6], A[1], A[4], A[7], A[2], A[5], A[8]];
}

/** Angle (degrees) between two rotations. */
export function angleBetween(A: Mat3, B: Mat3): number {
	const R = mul(transpose(A), B);
	return Math.acos(Math.max(-1, Math.min(1, (R[0] + R[4] + R[8] - 1) / 2))) / DEG;
}

// Back camera, phone upright (portrait): camera x = device x, camera y and z
// opposite to the device's (OpenCV camera: y down the image, z into the scene).
const CAM_FROM_DEV: Mat3 = [1, 0, 0, 0, -1, 0, 0, 0, -1];

export class Motion {
	available = false;
	private earthFromDev: Mat3 | null = null;
	private worldFromEarth: Mat3 | null = null; // the anchor to the mat
	private markRot: Mat3 | null = null;
	private travel = 0; // integrated |acceleration| (m/s) since the mark
	private lastT = 0;
	private onOrient = (e: DeviceOrientationEvent) => {
		if (e.alpha === null || e.beta === null || e.gamma === null) return;
		this.earthFromDev = fromEuler(e.alpha, e.beta, e.gamma);
		this.available = true;
	};
	private onMotion = (e: DeviceMotionEvent) => {
		const a = e.acceleration;
		const t = e.timeStamp;
		if (a && a.x !== null && a.y !== null && a.z !== null && this.lastT) {
			const dt = Math.min((t - this.lastT) / 1000, 0.1);
			this.travel += Math.hypot(a.x, a.y, a.z) * dt;
		}
		this.lastT = t;
	};

	start(): void {
		addEventListener('deviceorientation', this.onOrient);
		addEventListener('devicemotion', this.onMotion);
	}

	stop(): void {
		removeEventListener('deviceorientation', this.onOrient);
		removeEventListener('devicemotion', this.onMotion);
	}

	/** Only portrait is modelled; in landscape the sensors are not used for direction. */
	private get upright(): boolean {
		return (screen.orientation?.angle ?? 0) === 0;
	}

	/** The mat gave a camera pose (world -> camera rotation, row-major): tie the sensors to the mat. */
	anchor(camFromWorld: Mat3): void {
		if (!this.earthFromDev || !this.upright) return;
		// world <- earth = (world <- cam)(cam <- dev)(dev <- earth)
		this.worldFromEarth = mul(mul(transpose(camFromWorld), CAM_FROM_DEV), transpose(this.earthFromDev));
	}

	/** Camera -> world rotation from the sensors alone (null until anchored once). */
	worldFromCam(): Mat3 | null {
		if (!this.earthFromDev || !this.worldFromEarth || !this.upright) return null;
		return mul(mul(this.worldFromEarth, this.earthFromDev), CAM_FROM_DEV);
	}

	/** Start measuring movement from now (called when a frame is kept). */
	mark(): void {
		this.markRot = this.earthFromDev ? [...this.earthFromDev] : null;
		this.travel = 0;
	}

	/** Degrees turned and a rough travel score since the mark; null without sensors. */
	sinceMark(): { turned: number; travel: number } | null {
		if (!this.available || !this.earthFromDev || !this.markRot) return null;
		return { turned: angleBetween(this.markRot, this.earthFromDev), travel: this.travel };
	}
}
