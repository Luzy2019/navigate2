# Visual Scene Graph Dependencies

This integration keeps the IS-Bench runtime code in the repo, but the full
UniGoal/SAMJAM perception backend still needs heavy dependencies inside the
`isbench` conda environment.

## Python Packages

Install the package list:

```bash
conda activate isbench
pip install -r requirements-scene-graph.txt
```

Important compatibility pins:

- `numpy==1.26.4`, because OmniGibson / IsaacSim require `numpy<2.0`.
- `opencv-python-headless==4.11.0.86`, because newer headless OpenCV wheels can
  require `numpy>=2`.
- LightGlue is installed from GitHub, not PyPI:
  `git+https://github.com/cvg/LightGlue.git`.
- SAM2 requires `iopath`; it is included in `requirements-scene-graph.txt`.

## Local Editable Packages

After the base dependencies, install the vendored local packages:

```bash
pip install -e og_ego_prim/scene_graph/vendor/unigoal/third_party/Grounded-Segment-Anything/segment_anything
pip install -e og_ego_prim/scene_graph/vendor/unigoal/third_party/Grounded-Segment-Anything/GroundingDINO
SAM2_BUILD_CUDA=0 pip install --no-build-isolation -e og_ego_prim/scene_graph/vendor/samjam
```

`SAM2_BUILD_CUDA=0` is the safer first install path. SAM2 can still run without
its optional CUDA post-processing extension.

## Checkpoints

The default model directory is:

```text
data/models
```

Expected files:

```text
data/models/unigoal/groundingdino_swint_ogc.pth
data/models/unigoal/sam_vit_h_4b8939.pth
data/models/samjam/sam2.1_hiera_large.pt
data/models/samjam/sam2.1_hiera_l.yaml
```

Override with:

```bash
export ISBENCH_SCENE_GRAPH_MODEL_DIR=/path/to/models
```

## Backend Selection

```bash
export ISBENCH_SCENE_GRAPH_BACKEND=unigoal_grounded_sam
export ISBENCH_SCENE_GRAPH_UPDATE_EVERY=5
export ISBENCH_SCENE_GRAPH_IMAGE_WIDTH=256
export ISBENCH_SCENE_GRAPH_IMAGE_HEIGHT=256
```

Available backends:

- `unigoal_grounded_sam`: GroundingDINO + SAM + UniGoal Graph memory.
- `samjam_sam2`: SAM2 mask memory backend.
- `truth`: lightweight OmniGibson truth-backed debug backend.

## Dependency Check

```bash
python scripts/check_scene_graph_dependencies.py
```
