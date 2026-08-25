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

mkdir -p logs
START_TIME=$(date +%Y%m%d-%H%M%S)
LOG_FILE=logs/exec_safe_memory_$START_TIME.log

if [[ -f "entrypoints/env.sh" ]]; then
    source entrypoints/env.sh
fi

if [[ $# -lt 1 ]]; then
    cat >&2 <<'USAGE'
Usage:
  bash entrypoints/eval_safe_memory.sh MODEL_NAME [DATA_PARALLEL] [SCENE|all] [TASK_OR_TASK_LIST] [CONFIG] [EXTRA_ARGS...]

Examples:
  # 1) 单个场景单个任务：自动运行 full (sg+rp) 和 baseline (no_sg_no_rp) 两次
  bash entrypoints/eval_safe_memory.sh gpt-4o 1 Beechwood_0_int lifelong_crossroom__beechwood__raw_board_ready_plate_v1

  # 2) 单个场景多个任务：每个任务的两种消融配置依次评估
  bash entrypoints/eval_safe_memory.sh gpt-4o 1 Beechwood_0_int

  # 2b) 单个场景多个任务：只评估任务列表文件里的交集
  bash entrypoints/eval_safe_memory.sh gpt-4o 1 Beechwood_0_int entrypoints/my_safe_memory_tasks.txt

  # 3) 全部场景全部任务
  bash entrypoints/eval_safe_memory.sh gpt-4o 1 all

Notes:
  - SCENE 可用 Beechwood_0_int, Pomaria_1_int, Rs_int, restaurant_diner, Wainscott_0_garden。
  - 为兼容文档命名，Wainscott_0_int 会自动映射到 Wainscott_0_garden。
  - TASK_OR_TASK_LIST 可以是任务名、data/tasks/composite/*.json 路径，或一行一个任务的 txt 文件。
  - CONFIG 默认 entrypoints/configs/eval_safe_memory.yaml。
  - DATA_PARALLEL 默认 1；单任务会先运行 full (sg+rp)，再运行 baseline (no_sg_no_rp)。
  - 若设为大于 1 的值，多个子任务可能并发运行。
  - 默认使用 /home/lzy/anaconda3/envs/isbench/bin/python；可通过 ISBENCH_PYTHON 覆盖。
  - example planning、actions file、视频参数、scene graph 参数都从 YAML 配置读取。
  - 需要临时覆盖时，把 Python 参数放在 CONFIG 后面，例如 --use-example-planning。
  - 如只需运行单一消融配置，可用 --enable-scene-graph 或 --enable-risk-predictor
    作为 EXTRA_ARGS 传入，跳过自动配对。
USAGE
    exit 2
fi

MODEL_NAME=$1
DATA_PARALLEL=${2:-1}
SCENE_SELECTOR=${3:-all}
TASK_SPEC=${4:-}
CONFIG=${5:-entrypoints/configs/eval_safe_memory.yaml}
EXTRA_ARGS=("${@:6}")
WORK_DIR="./results"

TASK_ARGS=()
if [[ "${SCENE_SELECTOR}" != "all" && "${SCENE_SELECTOR}" != "*" ]]; then
    TASK_ARGS+=(--scene "${SCENE_SELECTOR}")
fi
if [[ -n "${TASK_SPEC}" ]]; then
    if [[ -f "${TASK_SPEC}" ]]; then
        TASK_ARGS+=(--task-list "${TASK_SPEC}")
    else
        TASK_NAME=${TASK_SPEC%.json}
        TASK_NAME=${TASK_NAME#./data/tasks/composite/}
        TASK_NAME=${TASK_NAME#data/tasks/composite/}
        TASK_NAME=${TASK_NAME#./data/tasks/}
        TASK_NAME=${TASK_NAME#data/tasks/}
        TASK_ARGS+=(--task "${TASK_NAME}")
    fi
fi

# If the user didn't pass --enable-scene-graph or --enable-risk-predictor
# as EXTRA_ARGS, run the paired comparison: full (sg+rp) then baseline (neither).
USER_PASSED_ABLATION=0
for arg in "${EXTRA_ARGS[@]}"; do
    case "${arg}" in
        --enable-scene-graph|--enable-risk-predictor)
            USER_PASSED_ABLATION=1
            break
            ;;
    esac
done

if [[ "${USER_PASSED_ABLATION}" -eq 1 ]]; then
    # User specified a single ablation profile — run it directly.
    "${PYTHON_BIN}" -m og_ego_prim.cli.safe_memory_benchmark_all \
        --config "${CONFIG}" \
        --model "${MODEL_NAME}" \
        --work-dir "${WORK_DIR}" \
        --data-parallel "${DATA_PARALLEL}" \
        "${TASK_ARGS[@]}" \
        "${EXTRA_ARGS[@]}" \
        2>&1 | tee -a "$LOG_FILE"
else
    # Default: run full (sg+rp) then baseline (neither enabled).
    echo "=== Running full profile (scene graph + risk predictor) ===" | tee -a "$LOG_FILE"
    "${PYTHON_BIN}" -m og_ego_prim.cli.safe_memory_benchmark_all \
        --config "${CONFIG}" \
        --model "${MODEL_NAME}" \
        --work-dir "${WORK_DIR}" \
        --data-parallel "${DATA_PARALLEL}" \
        --enable-scene-graph \
        --enable-risk-predictor \
        "${TASK_ARGS[@]}" \
        "${EXTRA_ARGS[@]}" \
        2>&1 | tee -a "$LOG_FILE"

    echo "=== Running baseline profile (no scene graph, no risk predictor) ===" | tee -a "$LOG_FILE"
    "${PYTHON_BIN}" -m og_ego_prim.cli.safe_memory_benchmark_all \
        --config "${CONFIG}" \
        --model "${MODEL_NAME}" \
        --work-dir "${WORK_DIR}" \
        --data-parallel "${DATA_PARALLEL}" \
        "${TASK_ARGS[@]}" \
        "${EXTRA_ARGS[@]}" \
        2>&1 | tee -a "$LOG_FILE"
fi
