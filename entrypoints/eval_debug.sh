#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="./:${PYTHONPATH:-}"

mkdir -p logs
START_TIME=$(date +%Y%m%d-%H%M%S)
LOG_FILE=logs/exec_debug_$START_TIME.log

if [[ -f "entrypoints/env.sh" ]]; then
    source entrypoints/env.sh
fi

if [[ $# -lt 1 ]]; then
    cat >&2 <<'USAGE'
Usage:
  bash entrypoints/eval_debug.sh TASK_NAME [SCENE] [MODEL_NAME_OR_PATH] [CONFIG] [EXTRA_ARGS...]

Examples:
  bash entrypoints/eval_debug.sh store_apple_and_tissue_box_in_bottom_cabinet Wainscott_0_int "" entrypoints/configs/eval_debug.yaml
  bash entrypoints/eval_debug.sh store_apple_and_tissue_box_in_bottom_cabinet Wainscott_0_int gpt-4o-mini entrypoints/configs/eval_debug.yaml --plan-max-steps 30
USAGE
    exit 2
fi

TASK_NAME=$1
SCENE_NAME=${2:-}
MODEL_NAME=${3:-}
CONFIG=${4:-entrypoints/configs/eval_debug.yaml}
EXTRA_ARGS=("${@:5}")

MODEL_ARGS=()
if [[ -n "${MODEL_NAME}" ]]; then
    MODEL_ARGS+=(--model "${MODEL_NAME}")
fi

SCENE_ARGS=()
if [[ -n "${SCENE_NAME}" ]]; then
    SCENE_ARGS+=(--scene "${SCENE_NAME}")
fi

python -m og_ego_prim.cli.online_benchmark_debug \
    --config "${CONFIG}" \
    --task "${TASK_NAME}" \
    "${SCENE_ARGS[@]}" \
    "${MODEL_ARGS[@]}" \
    "${EXTRA_ARGS[@]}" \
    2>&1 | tee -a "$LOG_FILE"
