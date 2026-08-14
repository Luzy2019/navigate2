#!/usr/bin/env bash
set -euo pipefail

# Isaac Sim must use one consistent system X11/XCB stack (inlined from
# omnigibson_python.sh so this entrypoint is self-contained and does not
# need the separate wrapper script).
unset QT_QPA_PLATFORM_PLUGIN_PATH QT_PLUGIN_PATH
unset LD_LIBRARY_PATH LD_PRELOAD
unset ROS_DISTRO ROS_ETC_DIR ROS_MASTER_URI ROS_PACKAGE_PATH
unset ROS_PYTHON_VERSION ROS_ROOT ROS_VERSION

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

# Mark the X11-safe wrapper as applied so the Python runner's
# maybe_reexec_with_omnigibson_python() returns immediately instead of
# re-executing through omnigibson_python.sh, and install the same LD_PRELOAD
# stack that wrapper would have set.
export ISBENCH_OMNIGIBSON_X11_FIX=1
export LD_PRELOAD="${ISBENCH_OMNIGIBSON_X11_PRELOAD:-/usr/lib/x86_64-linux-gnu/libxcb.so.1:/usr/lib/x86_64-linux-gnu/libX11-xcb.so.1:/usr/lib/x86_64-linux-gnu/libX11.so.6}"

if [[ $# -lt 4 ]]; then
    cat >&2 <<'USAGE'
Usage:
  bash entrypoints/eval_safe_memory_once.sh ABLATION_PROFILE MODEL_NAME SCENE TASK_OR_JSON [CONFIG] [WORK_DIR] [EXTRA_ARGS...]

Examples:
  bash entrypoints/eval_safe_memory_once.sh full gpt-4o-mini Beechwood_0_int lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3
  bash entrypoints/eval_safe_memory_once.sh no_sg_no_rp gpt-4o-mini Beechwood_0_int data/tasks/composite/lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3.json

Notes:
  - ABLATION_PROFILE must be one of: full, no_sg, no_rp, no_sg_no_rp.
    full = scene graph + risk predictor enabled (default).
    no_sg = scene graph disabled (risk predictor enabled).
    no_rp = risk predictor disabled (scene graph enabled).
    no_sg_no_rp = both disabled (baseline).
  - Each invocation uses a separate default work directory, so different profiles can run concurrently.
  - If CONFIG is omitted, the Python runner selects the task-specific safe-memory YAML when one exists.
  - Set ISBENCH_PYTHON to override the default isbench interpreter.
USAGE
    exit 2
fi

ABLATION_PROFILE=$1
MODEL_NAME=$2
SCENE_NAME=$3
TASK_SPEC=$4
CONFIG_ARGS=()
if [[ $# -ge 5 ]]; then
    CONFIG_ARGS=(--config "$5")
fi

case "${ABLATION_PROFILE}" in
    full)       EXTRA_FLAGS=() ;;
    no_sg)      EXTRA_FLAGS=(--no-enable-scene-graph) ;;
    no_rp)      EXTRA_FLAGS=(--no-enable-risk-predictor) ;;
    no_sg_no_rp) EXTRA_FLAGS=(--no-enable-scene-graph --no-enable-risk-predictor) ;;
    *)
        echo "ABLATION_PROFILE must be full, no_sg, no_rp, or no_sg_no_rp, got: ${ABLATION_PROFILE}" >&2
        exit 2
        ;;
esac

TASK_NAME=${TASK_SPEC%.json}
TASK_NAME=${TASK_NAME#./data/tasks/composite/}
TASK_NAME=${TASK_NAME#data/tasks/composite/}
TASK_NAME=${TASK_NAME#./data/tasks/}
TASK_NAME=${TASK_NAME#data/tasks/}

START_TIME=$(date +%Y%m%d-%H%M%S)
WORK_DIR=${6:-"./results/${TASK_NAME}_${START_TIME}_${ABLATION_PROFILE}"}
EXTRA_ARGS=("${@:7}")

mkdir -p "${WORK_DIR}"
LOG_FILE="${WORK_DIR}/console.log"

ISBENCH_LOG_FILE_ONLY=1 "${PYTHON_BIN}" -m og_ego_prim.cli.safe_memory_benchmark_once \
    "${CONFIG_ARGS[@]}" \
    --model "${MODEL_NAME}" \
    "${EXTRA_FLAGS[@]}" \
    --work-dir "${WORK_DIR}" \
    --scene "${SCENE_NAME}" \
    --task "${TASK_NAME}" \
    --no-timestamp-work-dir \
    "${EXTRA_ARGS[@]}" \
    >> "${LOG_FILE}" 2>&1
