#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="./:${PYTHONPATH:-}"
export OMNIGIBSON_HEADLESS="${OMNIGIBSON_HEADLESS:-1}"

PYTHON_BIN=${ISBENCH_PYTHON:-/home/lzy/anaconda3/envs/isbench/bin/python}
if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "isbench interpreter is not executable: ${PYTHON_BIN}" >&2
    exit 2
fi
export PATH="$(dirname "${PYTHON_BIN}"):${PATH}"

if [[ -f "entrypoints/env.sh" ]]; then
    source entrypoints/env.sh
fi

if [[ $# -lt 4 ]]; then
    cat >&2 <<'USAGE'
Usage:
  bash entrypoints/eval_safe_memory_once.sh MEMORY_MODE MODEL_NAME SCENE TASK_OR_JSON [CONFIG] [WORK_DIR] [EXTRA_ARGS...]

Examples:
  bash entrypoints/eval_safe_memory_once.sh with_memory gpt-4o-mini Beechwood_0_int lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3
  bash entrypoints/eval_safe_memory_once.sh without_memory gpt-4o-mini Beechwood_0_int data/tasks/composite/lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3.json

Notes:
  - MEMORY_MODE must be with_memory or without_memory.
  - Each invocation uses a separate default work directory, so the two modes can run concurrently.
  - If CONFIG is omitted, the Python runner selects the task-specific safe-memory YAML when one exists.
  - Set ISBENCH_PYTHON to override the default isbench interpreter.
USAGE
    exit 2
fi

MEMORY_MODE=$1
MODEL_NAME=$2
SCENE_NAME=$3
TASK_SPEC=$4
CONFIG_ARGS=()
if [[ $# -ge 5 ]]; then
    CONFIG_ARGS=(--config "$5")
fi

case "${MEMORY_MODE}" in
    with_memory|without_memory) ;;
    *)
        echo "MEMORY_MODE must be with_memory or without_memory, got: ${MEMORY_MODE}" >&2
        exit 2
        ;;
esac

TASK_NAME=${TASK_SPEC%.json}
TASK_NAME=${TASK_NAME#./data/tasks/composite/}
TASK_NAME=${TASK_NAME#data/tasks/composite/}
TASK_NAME=${TASK_NAME#./data/tasks/}
TASK_NAME=${TASK_NAME#data/tasks/}

START_TIME=$(date +%Y%m%d-%H%M%S)
WORK_DIR=${6:-"./results/${TASK_NAME}_${START_TIME}_${MEMORY_MODE}"}
EXTRA_ARGS=("${@:7}")

mkdir -p "${WORK_DIR}"
LOG_FILE="${WORK_DIR}/console.log"

ISBENCH_LOG_FILE_ONLY=1 "${PYTHON_BIN}" -m og_ego_prim.cli.safe_memory_benchmark_once \
    "${CONFIG_ARGS[@]}" \
    --model "${MODEL_NAME}" \
    --memory-mode "${MEMORY_MODE}" \
    --work-dir "${WORK_DIR}" \
    --scene "${SCENE_NAME}" \
    --task "${TASK_NAME}" \
    --no-timestamp-work-dir \
    "${EXTRA_ARGS[@]}" \
    >> "${LOG_FILE}" 2>&1
