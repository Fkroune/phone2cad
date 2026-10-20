// Rear camera at the highest resolution the browser will give us.

export async function openCamera(video: HTMLVideoElement): Promise<MediaStream> {
	if (!navigator.mediaDevices?.getUserMedia) {
		throw new Error('This browser cannot use the camera. Try Chrome or Safari.');
	}
	const stream = await navigator.mediaDevices.getUserMedia({
		audio: false,
		video: {
			facingMode: { ideal: 'environment' },
			width: { ideal: 4096 },
			height: { ideal: 2160 },
			frameRate: { ideal: 30 }
		}
	});
	const track = stream.getVideoTracks()[0];
	// Ask for the largest size the camera reports, if it offers more.
	try {
		const caps = track.getCapabilities?.() as MediaTrackCapabilities & { focusMode?: string[] };
		const s = track.getSettings();
		if (caps?.width?.max && caps?.height?.max && (s.width ?? 0) < caps.width.max) {
			await track.applyConstraints({ width: { ideal: caps.width.max }, height: { ideal: caps.height.max } });
		}
		if (caps?.focusMode?.includes('continuous')) {
			await track.applyConstraints({ advanced: [{ focusMode: 'continuous' } as MediaTrackConstraintSet] });
		}
	} catch {
		/* keep whatever we got */
	}
	video.srcObject = stream;
	video.muted = true;
	video.playsInline = true;
	await video.play();
	return stream;
}

export function nextFrame(video: HTMLVideoElement): Promise<void> {
	return new Promise((r) => {
		const v = video as HTMLVideoElement & { requestVideoFrameCallback?: (cb: () => void) => number };
		if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(() => r());
		else requestAnimationFrame(() => r());
	});
}

/** Full-resolution JPEG of a frame. Canvas output carries no EXIF/GPS. */
export async function encodeJpeg(bmp: ImageBitmap, quality = 0.92): Promise<Blob> {
	if (typeof OffscreenCanvas !== 'undefined') {
		const c = new OffscreenCanvas(bmp.width, bmp.height);
		c.getContext('2d')!.drawImage(bmp, 0, 0);
		return c.convertToBlob({ type: 'image/jpeg', quality });
	}
	const c = document.createElement('canvas');
	c.width = bmp.width;
	c.height = bmp.height;
	c.getContext('2d')!.drawImage(bmp, 0, 0);
	return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error('encode failed'))), 'image/jpeg', quality));
}

type ExposureCaps = MediaTrackCapabilities & {
	exposureMode?: string[];
	whiteBalanceMode?: string[];
	focusMode?: string[];
	torch?: boolean;
};
type ExposureSettings = MediaTrackSettings & { exposureTime?: number; colorTemperature?: number; torch?: boolean };

// Settings this app holds on the camera (colour lock, torch), by name.
const held = new Map<string, Record<string, unknown>>();

/**
 * applyConstraints replaces every earlier constraint, so each call restates the
 * current size, continuous focus and everything in `held`: losing them would
 * change the frame size mid-scan (the PC needs one size per side), freeze the
 * focus, or drop the colour lock when the torch is switched.
 */
async function applyHeld(track: MediaStreamTrack): Promise<boolean> {
	const caps = (track.getCapabilities?.() ?? {}) as ExposureCaps;
	const s = track.getSettings();
	const focus = caps.focusMode?.includes('continuous') ? [{ focusMode: 'continuous' }] : [];
	try {
		await track.applyConstraints({
			width: { ideal: s.width },
			height: { ideal: s.height },
			advanced: [...focus, ...held.values()] as MediaTrackConstraintSet[]
		});
		return true;
	} catch {
		return false;
	}
}

/**
 * Freeze the white balance at its current value (after calibration), so the part
 * keeps the same colour in every frame: the PC finds its outline by comparing
 * colours with the paper. Exposure stays automatic: frozen at the white mat's
 * level, a black part came out at 1-4 of 255 in close-ups (first jaw scan) and its
 * holes could not be seen; the PC evens out brightness per frame anyway.
 * Returns what could be locked. Chrome on Android supports it; elsewhere this
 * quietly does nothing.
 */
export async function lockExposure(track: MediaStreamTrack): Promise<string[]> {
	const caps = (track.getCapabilities?.() ?? {}) as ExposureCaps;
	const s = track.getSettings() as ExposureSettings;
	if (caps.whiteBalanceMode?.includes('manual') && s.colorTemperature) {
		held.set('colour', { whiteBalanceMode: 'manual', colorTemperature: s.colorTemperature });
	} else if (caps.whiteBalanceMode?.includes('single-shot')) {
		held.set('colour', { whiteBalanceMode: 'single-shot' });
	} else {
		return [];
	}
	if (await applyHeld(track)) return ['colour'];
	held.delete('colour');
	return [];
}

/** Back to automatic white balance (a new side starts with a new calibration). */
export async function unlockExposure(track: MediaStreamTrack): Promise<void> {
	const caps = (track.getCapabilities?.() ?? {}) as ExposureCaps;
	held.delete('colour');
	if (caps.whiteBalanceMode?.includes('continuous')) held.set('auto-colour', { whiteBalanceMode: 'continuous' });
	await applyHeld(track);
	held.delete('auto-colour');
}

/** Whether this phone's camera can light the scene (the flash as a torch). */
export function hasTorch(track: MediaStreamTrack | null): boolean {
	return !!track && !!((track.getCapabilities?.() ?? {}) as ExposureCaps).torch;
}

/** Switch the torch on or off; returns the state actually set. */
export async function setTorch(track: MediaStreamTrack, on: boolean): Promise<boolean> {
	if (on) held.set('torch', { torch: true });
	else held.set('torch', { torch: false });
	const ok = await applyHeld(track);
	if (!on) held.delete('torch');
	if (!ok) {
		held.delete('torch');
		return false;
	}
	return on;
}
