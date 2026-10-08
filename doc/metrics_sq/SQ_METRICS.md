# Metrics Explanation for Superquadrics Generator Module

This document introduces the metrics selected to test the segmentation and point clouds generator module and shows the results obtained for each model/method used as basis.

Tests where done using the [`eval_segmentation.py`](../../scripts/eval_segmentation.py) script, which evaluated models/methods on GraspNet-1B (RealSense D435, validation scenes 90-99, resampled to 640x480) and generated three CSV files.

| File | Rows | Columns |
| :--- | :--- | :--- |
| `segmentation_frames.csv` | One per model × scene × view | Every metric for that frame (use it for paired tests, e.g. Wilcoxon) |
| `segmentation_summary.csv` | One per model | Every metric: mean, `_std` and bootstrap 95% CI (`_ci_lo`, `_ci_hi`; 1000 resamples over frames) |
| `segmentation_summary_core.csv` | One per model | Core metrics only (listed below): mean and `_std` |

> ***Evaluated models**: Depth + RANSAC (Geometric method), U²-Net, U²-NetP, FastSAM-s and FastSAM-x. If a node changes, its copy in the script must be updated too.*

## 1. Foreground vs. background (`fg_*`)

> **Notation**: $\hat M$ predicted mask, $M$ ground-truth mask, $\lvert\cdot\rvert$ number of pixels/points. ↑ higher is better, ↓ lower is better.

 Instances are ignored: anything that is not background counts as object ($\hat M = \text{labels}>0$, $M = \text{gt}>0$). Answers *does the method separate objects from the table?*

| Metric | Column | Formula | Direction | Description |
| :---:  | :---: | :---: | :---: | :--- |
| **IoU** | `fg_iou` | $\dfrac{\lvert\hat M\cap M\rvert}{\lvert\hat M\cup M\rvert}$ | ↑ | Jaccard index, $\mathcal{J}$ in DAVIS (Perazzi et al., CVPR 2016). Penalises false positives and false negatives together. |
| **MAE** | `fg_mae` | $\dfrac{1}{HW}\sum_{u,v}\lvert\hat M - M\rvert$ | ↓ | Mean absolute error; for binary masks it is the fraction of misclassified pixels (1 − accuracy). Depends on how much of the image the objects cover, so read it together with IoU. 
## 2. Instance segmentation (`inst_*`)

Follows the *Unseen Object Instance Segmentation* protocol (UOIS-Net, Xie et al., T-RO 2021; UCN, Xiang et al., CoRL 2020), the usual one on OCID/OSD. Answers *does the method separate one object from another?*

**Matching.** With the intersection matrix $I_{ij}=\lvert\hat M_i\cap M_j\rvert$, the pairwise F-measure is $F_{ij}=\dfrac{2I_{ij}}{\lvert\hat M_i\rvert+\lvert M_j\rvert}$. The Hungarian algorithm (`linear_sum_assignment`, maximising) finds the one-to-one assignment with the highest total F; pairs with $F=0$ are dropped. The same matching is used for the point-cloud metrics.

| Metric | Column | Formula | Direction | Description |
| :---: | :---: | :---: | :---: | :--- |
| **Overlap F** | `inst_overlap_f` | $\dfrac{2PR}{P+R}$, with $P=\dfrac{\sum_{\text{match}} I_{ij}}{\sum_i\lvert\hat M_i\rvert}$, $R=\dfrac{\sum_{\text{match}} I_{ij}}{\sum_j\lvert M_j\rvert}$ | ↑ | **Main UOIS metric.** Unlike `fg_iou`, it penalises merging two objects into one, because half of the merged blob has no match. |
| **%F ≥ 0.75** | `inst_f075` | $\dfrac{1}{G}\,\#\{j : F_{ij}\ge 0.75\}$ over matched pairs ($G$ = number of GT objects) | ↑ | Fraction of objects segmented well enough to be usable. The most interpretable one for manipulation ("the robot can use 55% of the objects"). |
| **Under-segmentation** | `inst_under_seg` | $\#\{i : \#\{j : I_{ij}/\lvert M_j\rvert\ge 0.5\}\ge 2\}$ | ↓ | Predictions per frame that swallow at least half of two or more objects: touching objects merged into one blob. Expected failure of `connectedComponentsWithStats`. |

## 3. Point-cloud quality (`pc_*`)

Evaluates what SuperDec actually consumes: the points of `cloud_labels` (for the geometric method it can differ from the 2D mask).

**Reference cloud.** Sensor depth under the GT mask eroded 2 px, inside [`--min-depth`, `--max-depth`]. The erosion keeps the reference free of edge *flying pixels*. It is the observed surface, not the CAD mesh, so these metrics measure segmentation rather than sensor noise. The `oracle` model gives the floor of the reference: ~1% outliers.

For each matched pair, $\hat X$ is the predicted cloud, $X$ the reference, $d(\cdot,\cdot)$ the nearest-neighbour distance (`cKDTree`) and $\tau$ = `--dist-tol`. Values are averaged over the matched objects of the frame; missed objects are already penalised in section 2.

| Metric | Column | Formula | Direction | Description |
| :---: | :---: | :---: | :---: | :--- |
| **Outlier ratio** | `pc_outlier_ratio` | $\dfrac{1}{\lvert\hat X\rvert}\#\{x\in\hat X : d(x,X)>\tau\}$ | ↓ | Geometric contamination (flying pixels, tails, curtains, table points). Key metric: `normalize_points` uses $2\max\lvert p\rvert$, so **a single distant outlier shrinks the whole cloud**. |
| **Completeness** | `pc_completeness` | $\dfrac{1}{\lvert X\rvert}\#\{y\in X : d(y,\hat X)\le\tau\}$ | ↑ | Fraction of the real visible surface that was covered. Low values mean a cropped object (e.g. `plane_clearance` cutting its base), so SuperDec gets an incomplete shape. |

---

## 4. Efficiency

| Metric | Column | Formula | Direction | Description |
| :---: | :---: | :---: | :---: | :--- |
| **Parameters** | `params_m` | Trainable parameters [M] | ↓ | 0 for the geometric method. |
| **Peak VRAM** | `vram_peak_mb` | `max_memory_allocated` − memory in use before loading the model [MB] | ↓ | Weights + activations. Matters because the GPU is shared with SuperDec and the VLA. |
| **Throughput** | `fps` | $1000/\overline{\text{latency}}$ | ↑ | Sustained throughput. Latency is the wall time of one segmenter call (RANSAC + network + morphology + labelling), wrapped in `torch.cuda.synchronize()`, `--warmup` frames excluded. Excludes PNG reading and ROS transport. |
| **Latency p95** | `latency_p95_ms` | 95th percentile of the latency [ms] | ↓ | For real-time control the p95 matters, not the mean: occasional spikes are what drop frames. |

## 5. Superquadric quality (`sd_*`, `--superdec` only)

Task-based evaluation: *does the segmentation produce better superquadrics?* Every `--superdec-every` frames, the predicted clouds of the matched objects (≥ 32 points) go through `SuperDecRunner` with the adopted configuration (denoise + complete + canonical + uniform + merge) and the **plane estimated by the method itself**, as on the robot.

**Solina-Bajcsy radial distance.** For a superquadric with semi-axes $(a_1,a_2,a_3)$, profile exponent $\varepsilon_1$ (`e_prof`) and section exponent $\varepsilon_2$ (`e_sect`), a point $x$ in the primitive's local frame gives the inside-outside function

$$F(x)=\left[\left(\frac{x}{a_1}\right)^{2/\varepsilon_2}+\left(\frac{y}{a_2}\right)^{2/\varepsilon_2}\right]^{\varepsilon_2/\varepsilon_1}+\left(\frac{z}{a_3}\right)^{2/\varepsilon_1}$$

($F<1$ inside, $F=1$ on the surface, $F>1$ outside). The radial distance approximates the Euclidean distance to the surface along the ray from the centre, and each point is assigned to its closest primitive:

$$d_r(x)=\lVert x\rVert\,\left|F(x)^{-\varepsilon_1/2}-1\right|,\qquad E(X)=\frac{1}{\lvert X\rvert}\sum_{x\in X}\min_k d_r^{(k)}(x)$$

It is evaluated in log space (`logaddexp`) because the $2/\varepsilon$ exponents reach 20 and overflow to NaN otherwise.

| Metric | Column | Formula | Direction | Description |
| :---: | :---: | :---: | :---: | :--- |
| **Radial error (reference cloud)** | `sd_radial_gt_mm` | $E(X)$ [mm], primitives fitted on $\hat X$ | ↓ | **Task metric**: how well the superquadrics describe the real object. Table leaking in deforms the primitives; a cropped object leaves real parts far from them. Both errors show up here. |
| **Flatness** | `sd_flatness` | $\dfrac{1}{K}\sum_{k=1}^{K}\dfrac{\min(a_1^{(k)},a_2^{(k)},a_3^{(k)})}{\max(a_1^{(k)},a_2^{(k)},a_3^{(k)})}$, over the $K$ primitives of an object | ↑ (up to a point) | Ratio between the shortest and longest semi-axis of each primitive. With single-view clouds SuperDec tends to fit flat slabs glued ad of volumes (PROGRESS.md, steps 7-8): < 0.15 means slab-like primitives, > 0.35 means primitives with real volume. 1 is a cube-like proportion, so it is not a quantity to maximise blindly; read it together with the radial error. |

## Results

GraspNet-1Billion RealSense, scenes 90-99, stride 8 (320 frames per model), 640×480. Mean ± std over frames (`segmentation_summary_core.csv`). Best value per row in bold.

| Metric | Geometric | U²-Net | U²-NetP | FastSAM-s | FastSAM-x |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Parameters [M] ↓ | **0** | 44.01 | 1.13 | 11.79 | 72.23 |
| Peak VRAM [MB] ↓ | **0** | 329.7 | 153.6 | 152.1 | 394.2 |
| FPS ↑ | 30.6 | 38.4 | 43.7 | **57.4** | 24.6 |
| Latency p95 [ms] ↓ | 37.0 | 35.2 | 30.3 | **20.7** | 44.4 |
| IoU ↑ | 0.649 ± 0.076 | 0.490 ± 0.229 | 0.585 ± 0.207 | 0.617 ± 0.127 | **0.714 ± 0.100** |
| MAE ↓ | 0.144 ± 0.042 | 0.166 ± 0.077 | 0.136 ± 0.072 | 0.124 ± 0.047 | **0.096 ± 0.038** |
| Overlap F ↑ | 0.422 ± 0.175 | 0.393 ± 0.170 | 0.429 ± 0.178 | 0.663 ± 0.107 | **0.689 ± 0.087** |
| %F ≥ 0.75 ↑ | 0.247 ± 0.185 | 0.175 ± 0.134 | 0.180 ± 0.162 | 0.570 ± 0.152 | **0.615 ± 0.157** |
| Under-segmentation ↓ | 1.40 ± 0.64 | 0.92 ± 0.76 | 1.08 ± 0.71 | 0.04 ± 0.19 | **0.02 ± 0.12** |
| Outlier ratio ↓ | 0.290 ± 0.202 | 0.220 ± 0.201 | 0.240 ± 0.197 | 0.035 ± 0.045 | **0.022 ± 0.032** |
| Completeness ↑ | **0.908 ± 0.086** | 0.838 ± 0.143 | 0.838 ± 0.150 | 0.810 ± 0.082 | 0.842 ± 0.066 |
| Radial error [mm] ↓ | 8.9 ± 5.447 | 9.982 ± 7.182 | 10.85 ± 8.2 | 10.844 ± 5.531 | **8.363 ± 3.166** |
| Flatness | **0.478 ± 0.073** | 0.471 ± 0.106 | 0.464 ± 0.101 | 0.476 ± 0.070 | 0.475 ± 0.065 |

>**Note:.** *U²-Net was trained on GraspNet scenes 0-89, and scenes 90-99 may have been used to select `best.pth`, which would give it a slight bias. U²-Net does not apply `reject_flying_pixels`, so its 3D metrics also reflect that design choice. The temporal stability of the masks is not measured, because the GraspNet camera moves between views.*
