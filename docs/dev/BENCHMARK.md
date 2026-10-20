# ScanFit benchmark and comparison with other scanning tools

## 1. Benchmark dataset

**corner-v1** (kept locally only, never committed or uploaded):
100 hand-held frames (an Android phone, 12 MP, portrait) of a 3D-printed corner connector
on the A4 mat, filmed unguided: mostly close-ups, the mat often cut off, the paper curled
1-4 mm at its edges. Ground truth: the part's CAD (`reference.stl`, 39.5 x 39.5 x 36.8 mm).
It is the realistic case the app must handle, so every pipeline change is scored on it.

Run it: `cd worker && python -m scanfit_worker.benchmark --note "what changed"`.
Each run appends a row to a local results file (see `worker/scanfit_worker/benchmark.py`). Metrics:

- **coverage**: share of the CAD surface that has scan points within 1 mm / 0.5 mm;
- **median distance**: scan points to the true surface;
- **scale error**: best-fit scale between scan and CAD (also absorbs any uniform
  surface offset, so treat ±0.5 % as noise on a 40 mm part);
- **dimension errors**: every reported plane-to-plane distance and height against the
  same measurement taken on the CAD placed where the scan put the part.

### Results so far (corner-v1)

| Pipeline version | Frames used | Coverage @1 mm | Median dist. | Worst dimension error | Time (entry-level NVIDIA GPU, 2 GB) |
|---|---|---|---|---|---|
| v0: frames located by the mat only | 16 / 100 | 27 % | 0.22 mm | n/a (too few faces) | not timed |
| + SIFT SfM on all frames, fast mode (sequential + global mapper) | 100 / 100 | 62 % | 0.20 mm | 0.29 mm | ~24 min |
| + SIFT SfM, accurate mode (all pairs + incremental mapper) | 100 / 100 | 66-76 % | 0.22 mm | **0.16 mm** | ~40 min (estimate from stage times) |
| Dense on crops of the part (1200 px / 800 px), accurate SfM, 48 views | 100 / 100 | 50 % / 57 % | 0.26 / 0.29 mm | 0.12 / 0.04 mm (2 dims) | ~50 / ~35 min |
| **Official run 1** (`benchmark`): accurate SfM, adaptive crop (chose whole frames: part ~461 px), deterministic planes, footprint isolation | 100 / 100 | **62 %** | **0.23 mm** | **0.06 mm** (2 heights; no widths found) | 42.5 min |

Notes from these runs:

- **Cropping is not always better.** On this close-up-heavy capture the part is already
  ~460-530 px in whole-frame dense images, and its plain red plastic has little texture:
  at higher resolution PatchMatch's 11x11 px window covers less physical texture, so
  coverage dropped (50-57 % vs 62 %). Crops pay off only when the part is small in the
  frame (filmed from afar). The pipeline now crops only below ~400 px.
- **Measurement must be repeatable.** Open3D's multi-threaded RANSAC gave 4 to 7 faces on
  the same cloud; plane fitting now uses a seeded numpy RANSAC (identical runs).
- **What limits the score now:** face detection on the noisy surface (4 faces found of
  ~8 visible, so arm widths are often missing) and coverage (62 %; the base is never
  seen and inner corners are occluded).

Second real scan (128 frames, filmed at 35-50 cm as then instructed, not kept): the part
was only ~75 px wide in the dense step (whole frames shrunk to 1000 px for the 2 GB GPU),
coverage fell to 52 %. That is what the crop step addresses.

## 2. How other tools do it

| Tool | Capture | Metric scale | Camera solve | Surface | Object isolation | Reported accuracy |
|---|---|---|---|---|---|---|
| **Qlone** (iOS/Android) | Printed checkerboard mat + AR dome; on-device | From the mat | Mat tracking | Live on-device mesh | Mat region | Weak on small items; smooth, low-detail meshes (reviews) |
| **Apple Object Capture** | Guided capture (iPhone), turntable or walk-around; flip-and-merge for the underside | Device depth / LiDAR, or user scale | SfM | MVS + mesh | Automatic object masking | Within 1 mm on heritage objects; 0.23 mm on total length (PLOS ONE 2024) |
| **RealityScan** (Epic) | Guided phone capture | User / AR | RealityCapture engine, cloud | MVS | Masking | Struggles on shiny / featureless objects |
| **KIRI Engine** | Phone, up to ~200 photos | User | Cloud photogrammetry; "featureless object" mode; 3DGS-to-mesh | MVS or Gaussian splatting | Object masking | 5 mm mean error in a breast-phantom study (different use case) |
| **Polycam** | Phone LiDAR or photos | LiDAR / AR | Cloud | MVS | Masking | 21 mm in the same phantom study (LiDAR mode) |
| **Agisoft Metashape** (desktop, pro) | Any camera | **Coded targets + calibrated scale bars** (targets located to ~1/20 px; bars certified to 0.1 mm) | SfM with camera self-calibration | MVS | Manual / auto masks | ~1 mm and better at close range |
| **Research: Gaussian / neural surfaces** | Any capture | From the camera solve | (needs SfM poses) | PGSR 0.52 mm, GOF 0.74, 2DGS 0.80, NeuS 0.84 mean Chamfer on DTU | Masks used in training | Needs a 10-24 GB GPU (2DGS: 11 min on an RTX 3090) |

Matching research: learned features and matchers (SuperPoint / ALIKED + LightGlue, also
SIFT + LightGlue) find more and more reliable matches than plain SIFT on low-texture and
difficult scenes, at the cost of a GPU network per image pair.

## 3. What fits ScanFit (a phone, a printed mat, a PC with an entry-level NVIDIA GPU (2 GB), no cloud, no paid services)

Ranked by expected gain on our benchmark for the effort:

1. **Keep the mat, but only as a scale and lens reference** (done). Our ChArUco mat is
   the same idea as Metashape's coded targets: metric scale and lens calibration you can
   trust, which no cloud app offers out of the box. Calibrate on ~12 whole-mat views,
   then locate every other frame by feature matching (done: 16 -> 100 of 100 frames).
2. **Crop the dense step to the part** (in progress). Object Capture and KIRI restrict
   reconstruction to the object; for us it is also the only way to use the phone's full
   resolution on a 2 GB GPU.
3. **Automatic object masking** (SAM 2 or a lighter salient-object model), as Apple and
   KIRI do. Masking cut mean error and RMSE by 10-15 % in a published workflow
   (SlicerMorph), removes the table and background from matching and fusion, and is what
   makes **objects larger than the mat** (handlebars) possible: the part no longer has
   to stand inside the mat's clear zone. Runs on CPU (slow) or within ~1 GB of GPU.
4. **Printable scale strips** for large objects: a strip of coded markers with a known
   spacing, laid next to the object (Metashape's scale-bar practice). Gives metric scale
   anywhere along a handlebar instead of only near the mat, and limits scale drift.
5. **Flip-and-merge** (Apple, Qlone): a second pass with the part on its side, merged by
   shape. The underside is never seen today (the base of the part is always missing).
6. **SIFT + LightGlue or ALIKED + LightGlue matching**: worth a benchmark run once the
   above is in; most useful on plain, low-texture parts like the red corner.
7. **Gaussian / neural surfaces (PGSR, 2DGS)**: best accuracy in research, but they need
   a much bigger GPU than an entry-level 2 GB one, and cloud training would break the privacy rule.
   Revisit if the server PC gets a >= 8-12 GB GPU.

Not adopted: cloud processing (privacy rule: frames never leave the PC; no paid
services), LiDAR (not on the target phone, and too coarse for millimetre features).

## Sources

- Qlone: [3D Scan Expert review](https://3dscanexpert.com/qlone-smartphone-3d-scanning-app-ios-android-review/), [App Store](https://apps.apple.com/us/app/qlone-3d-scanner/id1229460906), [SolidSmack](https://www.solidsmack.com/cad/3d-scan-print-objects-qlone-mobile-app/)
- Apple Object Capture: [PLOS ONE 2024 assessment](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0314560), [WWDC24 area mode](https://developer.apple.com/videos/play/wwdc2024/10107/)
- App comparisons: [ISPRS 2025 low-cost documentation](https://isprs-archives.copernicus.org/articles/XLVIII-M-9-2025/1167/2025/isprs-archives-XLVIII-M-9-2025-1167-2025.pdf), [befores & afters](https://beforesandafters.com/2024/07/02/comparing-the-top-5-3d-scanner-apps/), [SwiftWand 2026](https://swiftwand.com/en/smartphone-3d-scanning-app-comparison-2026-en/), [KIRI vs RealityScan](https://www.kiriengine.app/blog/comparison/kiri-engine-vs-reality-scan), [breast phantom study](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12349111/)
- Metashape targets and scale bars: [coded circular targets](https://photogrammetrycommunity.github.io/metashape-expert-manual/workflow/markers-gcps/coded-circular-targets/), [CHI scale bars guide](https://culturalheritageimaging.org/What_We_Offer/Gear/Scale_Bars/ScaleBars_UG_v4.pdf)
- Surfaces: [PGSR](https://arxiv.org/html/2406.06521), [2DGS](https://arxiv.org/html/2403.17888), [Gaussian surfels](https://arxiv.org/pdf/2404.17774)
- Matching: [LightGlue](https://arxiv.org/pdf/2306.13643), [ISPRS 2025 SfM comparison](https://isprs-archives.copernicus.org/articles/XLVIII-2-W10-2025/199/2025/isprs-archives-XLVIII-2-W10-2025-199-2025.pdf)
- Masking: [SlicerMorph photogrammetry](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12421799/), [SAM 2](https://arxiv.org/pdf/2408.06305)
