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

if [[ $# -lt 2 ]]; then
    echo "Usage: bash entrypoints/eval_open.sh MODEL_NAME_OR_PATH SERVER_IP [DATA_PARALLEL] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]" >&2
    exit 2
fi

MODEL_NAME_OR_PATH=$1
SERVER_IP=$2
DATA_PARALLEL=${3:-1}
TASK_SPEC=${4:-entrypoints/task_list.txt}
CONFIG=${5:-entrypoints/configs/eval_open.yaml}
EXTRA_ARGS=("${@:6}")
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

MODEL_NAME=$(basename $MODEL_NAME_OR_PATH)
WORK_DIR=./results/$MODEL_NAME

python -m og_ego_prim.cli.online_benchmark_all \
    --config "$CONFIG" \
    --data_parallel $DATA_PARALLEL \
    --num_retry "$NUM_RETRY" \
    --task_list "$TASK_LIST" \
    --work_dir $WORK_DIR \
    --model $MODEL_NAME_OR_PATH \
    --local_llm_serve \
    --local_serve_ip $SERVER_IP \
    --draw_bbox_2d \
    "${EXTRA_ARGS[@]}" \
    2>&1 | tee -a "$LOG_FILE"
