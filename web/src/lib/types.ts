export interface Cylinder {
	kind: 'cylinder';
	axis: number[];
	point: number[];
	radius: number;
	length: number;
}

/** A measured face, drawn as a rectangle: centre, in-plane axes, half sizes (mm). */
export interface FaceRect {
	centre: number[];
	u: number[];
	v: number[];
	half: number[];
}

/** The faces a distance or height was measured between, and the measured span. */
export interface Faces {
	kind: 'faces';
	faces: FaceRect[];
	line: [number[], number[]] | null;
}

export type Highlight = Cylinder | Faces;
