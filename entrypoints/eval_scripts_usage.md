# Eval Entrypoints

This project now keeps benchmark/runtime tuning in YAML config files under
`entrypoints/configs/`. Shell environment variables are reserved for credentials,
local paths, and launcher/container settings.

## Shared Config

Default config files:

- `entrypoints/configs/common.yaml`: runtime, scene graph, navigation, starter primitives, artifacts.
- `entrypoints/configs/task.yaml`: task/planner defaults.
- `entrypoints/configs/eval_close.yaml`: closed-model batch defaults.
- `entrypoints/configs/eval_open.yaml`: local/open-model batch defaults.
- `entrypoints/configs/eval_debug.yaml`: single-task debug defaults.
- `entrypoints/configs/eval_safe_memory.yaml`: safe-memory lifelong defaults.

Merge order:

```text
built-in defaults < includes < eval yaml < explicit CLI args
```

Do not export benchmark parameters such as scene graph backend, navigation
tolerances, primitive mode, video FPS, or safe-memory video settings. Put those
in YAML. Keep only credentials and local runtime settings in
`entrypoints/env.sh` / `entrypoints/env.local.sh`, such as `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`, `PARTITION`, `NUM_GPUS`,
`APPTAINER_IMAGE`, `BINDING`, `OMNIGIBSON_*`, `PYTHONPATH`, and library paths.

## eval_close.sh

Usage:

```bash
bash entrypoints/eval_close.sh MODEL_NAME [DATA_PARALLEL] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]
```

Defaults:

- `TASK_OR_TASK_LIST`: `entrypoints/task_list.txt`
- `CONFIG`: `entrypoints/configs/eval_close.yaml`
- Output root: `results/MODEL_NAME`

Examples:

```bash
bash entrypoints/eval_close.sh gpt-4o 1 entrypoints/task_list.txt
bash entrypoints/eval_close.sh gpt-4o 1 store_apple_and_tissue_box_in_bottom_cabinet
bash entrypoints/eval_close.sh gpt-4o 2 entrypoints/task_list.txt entrypoints/configs/eval_close.yaml --num_retry 1
```

## eval_open.sh

Usage:

```bash
bash entrypoints/eval_open.sh MODEL_NAME_OR_PATH SERVER_IP [DATA_PARALLEL] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]
```

Defaults:

- `TASK_OR_TASK_LIST`: `entrypoints/task_list.txt`
- `CONFIG`: `entrypoints/configs/eval_open.yaml`
- Output root: `results/basename(MODEL_NAME_OR_PATH)`

Examples:

```bash
bash entrypoints/eval_open.sh /models/Qwen2.5-VL 127.0.0.1 1 entrypoints/task_list.txt
bash entrypoints/eval_open.sh /models/Qwen2.5-VL 127.0.0.1 1 store_apple_and_tissue_box_in_bottom_cabinet
```

## eval_debug.sh

Usage:

```bash
bash entrypoints/eval_debug.sh TASK_NAME [SCENE] [MODEL_NAME_OR_PATH] [CONFIG] [EXTRA_ARGS...]
```

When `MODEL_NAME_OR_PATH` is empty, the debug runner uses task
`example_planning`. It writes `report.json`, `runtime_config.json`, step
observations when enabled, and `video.mp4` when `artifacts.save_video` is true.

Examples replacing the old `scripts/test_all.py` style:

```bash
bash entrypoints/eval_debug.sh \
  store_apple_and_tissue_box_in_bottom_cabinet \
  Wainscott_0_int \
  "" \
  entrypoints/configs/eval_debug.yaml

bash entrypoints/eval_debug.sh \
  store_apple_and_tissue_box_in_bottom_cabinet \
  Wainscott_0_int \
  "" \
  entrypoints/configs/eval_debug.yaml \
  --plan-max-steps 30 \
  --scene-graph-backend samjam_unigoal \
  --scene-graph-step-interval 30 \
  --nav-stuck-waypoint-tolerance 0.25

python -m og_ego_prim.cli.online_benchmark_debug \
  --config entrypoints/configs/eval_debug.yaml \
  --validate-only
```

## eval_safe_memory.sh

Usage:

```bash
bash entrypoints/eval_safe_memory.sh MODEL_NAME [DATA_PARALLEL] [SCENE|all] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]
```

Defaults:

- `SCENE`: `all`
- `CONFIG`: `entrypoints/configs/eval_safe_memory.yaml`
- Output root: `results/MODEL_NAME`

Examples:

```bash
bash entrypoints/eval_safe_memory.sh \
  scripted \
  1 \
  Beechwood_0_int \
  lifelong_crossroom__beechwood__raw_board_ready_plate_v1 \
  entrypoints/configs/eval_safe_memory.yaml \
  --use-example-planning

bash entrypoints/eval_safe_memory.sh gpt-4o 1 Beechwood_0_int
bash entrypoints/eval_safe_memory.sh gpt-4o 1 all
```

Safe-memory output remains quiet in the terminal. Each task/memory-mode pair
writes a separate log under `WORK_DIR/safe_memory_logs`; failures print the log
path and extracted exception summary.
