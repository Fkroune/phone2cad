# Accuracy

## How ScanFit was tested

Two 3D-printed parts with known CAD were scanned with an Android phone, and
processed on a laptop (8 GB for WSL2, an entry-level NVIDIA GPU (2 GB)):
- a **corner connector**, 39.5 × 39.5 × 36.8 mm, with square 9.6 mm arms and
  small square windows;
- a **black jaw**, 75 × 36 × 31.8 mm, with two Ø3.5 side holes and a threaded
  Ø6.5 top hole.

![The two test parts on the printed mat: the red corner connector and the black jaw (scan frames warped onto the mat)](images/test-parts.jpg)

For each scan, the 3D copy was fitted onto the CAD by shape alone (rotation and
position, no scaling, so size errors stay visible). Then:
- **typical error**: the median distance from the copy's surface to the CAD;
- **surface within 1 mm**: the share of the copy within 1 mm of the CAD;
- **dimensions**: each measured width, height or hole was compared with the
  same measurement taken on the CAD.

Each scan was processed from scratch, once, as a new scan would be. A
measurement paired with the wrong CAD dimension when scoring (for example a
17.5 mm width with the 39.5 mm one) is left out; one that is wrong because part
of the shape is missing counts as a miss.

## Results (October 2026)

| Scan | Typical error | Surface within 1 mm | Dimensions within their ± | Within 1 mm | Holes |
|---|---|---|---|---|---|
| Corner, one side* | 0.25 mm | 87 % | 3 of 5 | 4 of 5 | one false hole |
| Corner, one side (2nd scan)* | 0.36 mm | 75 % | 2 of 2 | 2 of 2 | none (correct) |
| Corner, both sides | 0.27 mm | 93 % | 9 of 9 | 9 of 9 | none (correct) |
| Corner, both sides (2nd scan) | 0.32 mm | 89 % | 5 of 6 | 6 of 6 | two false holes |
| Jaw, both sides, no torch | 0.39 mm | 92 % | 2 of 3 | 2 of 3 | side holes and threaded hole missed |
| Jaw, both sides, torch | 0.42 mm | 79 % | 2 of 3 | 2 of 3 | one real side hole (Ø3.09 vs 3.5), the other side hole and the threaded hole missed |

\* One arm of the part is missing from the 3D copy (overall 33.6 × 19.0 × 39.9
and 33.3 × 21.0 × 36.9 mm, CAD 39.5 × 39.5 × 36.8); the result marks the overall
size "may be incomplete".

- ScanFit's bar is an error under **1 mm**: 25 of 28 dimensions were within
  1 mm of the CAD; 23 of 28 within their own ±.
- Two-sided scans give the most complete surface: all four came out whole.
  The torch jaw scan warned that its two sides' shapes matched poorly (0.54),
  although nothing was missing.
- An automated test scans a synthetic 40 × 30 × 20 mm block with a Ø10 hole and
  must measure it within ±0.5 mm.

### With a stored camera solve

An earlier run of the same six scans reused the camera solve stored with each
scan (the step that works out where each photo was taken from) and re-ran only
the steps after it. It gave better numbers than processing from scratch:

| Scan | Typical error | Surface within 1 mm | Dimensions within their ± | Within 1 mm | Holes |
|---|---|---|---|---|---|
| Corner, one side | 0.22 mm | 83 % | 3 of 4 | 4 of 4 | none (correct) |
| Corner, one side (2nd scan) | 0.21 mm | 82 % | 3 of 3 | 3 of 3 | none (correct) |
| Corner, both sides | 0.21 mm | 95 % | 8 of 9 | 9 of 9 | one false hole |
| Corner, both sides (2nd scan) | 0.25 mm | 90 % | 7 of 8 | 8 of 8 | none (correct) |
| Jaw, both sides, no torch | 0.38 mm | 92 % | 2 of 3 | 2 of 3 | threaded hole missed |
| Jaw, both sides, torch | 0.42 mm | 82 % | 2 of 3 | 2 of 3 | 2 real side holes (Ø3.11, Ø2.80 vs 3.5), one false hole, threaded hole missed |

28 of 30 dimensions were within 1 mm of the CAD, 25 of 30 within their own ±.

![The corner part scanned on one side and on two sides with a stored camera solve, each 3D copy coloured by distance to the CAD](images/one-vs-two-sides.png)

## Known weaknesses

- **Holes** (experimental): real side holes read 0.4–0.7 mm small; processed
  from scratch, the two jaw scans found one of their four side holes between
  them. False holes appeared on two scans. A threaded hole inside a shallow recess was
  always missed.
- **Repeatability**: processing the same frames twice gives slightly different
  camera positions (median 0.14 mm), and that can change the 3D copy much more.
  Each fresh solve changes where the outline step clicks on the part, and a
  share of the photos can then be outlined without a piece of it (an arm, a
  tab). A missing arm or tab shows on the result page's outline photo (see
  [reading the results](reading-results.md#does-this-match-your-part)): if the
  green line misses part of the object, scan again.
- **Outlines that leave out a piece.** ScanFit looks for a lump of texture
  points that most outlines keep but the carved shape lost, and outlines the
  photos that cut it away again, with extra clicks inside the lump. On the first
  two-sided corner scan, which lost an arm in 5 of 6 runs from scratch before
  this step, a third of one side's photos were redone and the arm came back.
  On the first one-sided scan, it brought back one of the two lost arms (all 4
  dimensions within their ±, against 3 of 4 without it); the other stayed
  missing.
- **When the two sides' shapes match poorly**, part of the shape may be missing:
  ScanFit warns below 0.70 on the match score. Whole shapes scored 0.74–0.86,
  except the torch jaw scan (0.54, nothing missing); shapes missing an arm
  scored 0.38–0.66.
- **Sizes read large**: on every test scan processed from scratch, the scale
  came out 1.1–2.3 % large (0.4–1.65 % with a stored camera solve), so sizes
  may read up to ~2 % large. The shape is carved from the part's outlines
  (shape from outlines), which can only err outwards. See the
  [roadmap](dev/roadmap.md), item 2: help with caliper-checked prints is welcome.
- **Quick scans** (one side filmed) may miss parts of the object: the camera
  never sees the underside. Both one-sided test scans lost an arm when
  processed from scratch.
- **Printer scale**: if you skip the mat measurement, every result carries your
  printer's scaling error (often 1–3 %).

## Test it yourself

### With the synthetic block (no printer needed)

```sh
cd worker
python -m pytest            # renders 64 views of the block, scans and measures it (~6 min)
```

The block is described in `artefacts/test_block.scad`.

### With your own part

You need a part with a known size: a CAD model (STL) of a 3D-printed part, or
a part you can measure with a caliper. The corner connector from these tests
(`scanfit-corner-connector.stl`) and the test block (`scanfit-test-block.stl`)
are attached to each [release](https://github.com/Fkroune/scanfit/releases).

1. Scan it (see the [scanning guide](scanning-guide.md)).
2. Download the 3D copy: the job's `result/mesh.ply` on the server.
3. Compare it with the CAD:
   ```sh
   cd worker
   python -m scanfit_worker.compare mesh.ply part.stl --save aligned.ply
   ```
   This prints the scale error, the median distance and the coverage. Open
   `aligned.ply` together with the STL in a viewer (MeshLab, CloudCompare) to
   see where they differ.
4. For single dimensions, compare the measurement list with your CAD or your
   caliper.

Please share your results (part, material, colour, one or two sides, numbers)
in an issue: it helps everyone.
