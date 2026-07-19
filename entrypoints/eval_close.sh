#!/usr/bin/env bash
set -euo pipefail

export NUM_GPUS=1
export PYTHONPATH="./:${PYTHONPATH:-}"

mkdir -p logs
START_TIME=$(date +%Y%m%d-%H%M%S)
LOG_FILE=logs/exec_$START_TIME.log

if [ -f "entrypoints/env.sh" ]; then
    source entrypoints/env.sh
fi

if [[ -n "${PARTITION:-}" ]]; then
    echo "Submit to $PARTITION"
fi

if [[ $# -lt 1 ]]; then
    echo "Usage: bash entrypoints/eval_close.sh MODEL_NAME [DATA_PARALLEL] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]" >&2
    exit 2
fi

MODEL_NAME=$1
DATA_PARALLEL=${2:-1}
TASK_SPEC=${3:-entrypoints/task_list.txt}
CONFIG=${4:-entrypoints/configs/eval_close.yaml}
EXTRA_ARGS=("${@:5}")
NUM_RETRY=3

if [[ -f "$TASK_SPEC" ]]; then
    TASK_LIST=$TASK_SPEC
else
    TASK_NAME=${TASK_SPEC%.json}
    TASK_NAME=${TASK_NAME#./data/tasks/}
    TASK_NAME=${TASK_NAME#data/tasks/}

    TASK_LIST=$(mktemp "${TMPDIR:-/tmp}/isbench-task-list.XXXXXX")
    trap 'rm -f "$TASK_LIST"' EXIT
    printf '%s\n' "$TASK_NAME" > "$TASK_LIST"
fi

WORK_DIR=./results/$MODEL_NAME

python -m og_ego_prim.cli.check_close_api --model "$MODEL_NAME"

python -m og_ego_prim.cli.online_benchmark_all \
    --config "$CONFIG" \
    --data_parallel $DATA_PARALLEL \
    --num_retry "$NUM_RETRY" \
    --task_list "$TASK_LIST" \
    --work_dir "$WORK_DIR" \
    --model "$MODEL_NAME" \
    --draw_bbox_2d \
    "${EXTRA_ARGS[@]}" \
    2>&1 | tee -a "$LOG_FILE"
