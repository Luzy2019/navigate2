#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# eval_safe_memory_matrix.sh — 4-task x {full, sap, no_sg} x N-rep comparison matrix
#
# Tasks (all run in Beechwood_0_int):
#   lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v3
#   lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3
#   lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3
#   lifelong_crossroom__beechwood__mold_rag_dining_reuse_v3
#
# Profiles (explicit flags are ALWAYS passed on purpose):
#   full  = scene graph + risk predictor
#           (--enable-scene-graph --enable-risk-predictor)
#   no_sg = scene graph disabled, risk predictor enabled
#           (--no-enable-scene-graph --enable-risk-predictor)
#   sap   = scene graph + risk predictor disabled, SAP cognition prompt enabled
#           (--no-enable-scene-graph --no-enable-risk-predictor --prompt-setting sap)
#
# PROFILES is env-overridable, e.g. run only a subset:
#   PROFILES="full no_sg" bash entrypoints/eval_safe_memory_matrix.sh ...
#
#   IMPORTANT: the Python CLI defaults both module flags to False. Each profile
#   passes explicit flags so its behavior does not depend on parser defaults.
#
# Runs are sequential: each OmniGibson/Isaac instance needs ~13-16 GB VRAM, so
# concurrent instances are not feasible on a single RTX 4080 SUPER.
#
# Resumable: a (task, profile, rep) whose success report.json already exists is
# skipped (and re-recorded into summary.csv), so you can interrupt the batch
# and restart it safely.
#
# Usage:
#   bash entrypoints/eval_safe_memory_matrix.sh [MODEL] [SCENE] [REPS] [BATCH_TAG] [PER_RUN_TIMEOUT_SECONDS]
#
# Examples:
#   bash entrypoints/eval_safe_memory_matrix.sh gpt-4o Beechwood_0_int 10
#   bash entrypoints/eval_safe_memory_matrix.sh gpt-4o Beechwood_0_int 10 my_tag
#   REPS=0 bash entrypoints/eval_safe_memory_matrix.sh          # plan only
#   NO_WAIT=1 bash entrypoints/eval_safe_memory_matrix.sh ...   # skip GPU wait
#   nohup bash entrypoints/eval_safe_memory_matrix.sh gpt-4o Beechwood_0_int 10 \
#         > results/matrix_launch.log 2>&1 &
#
# Output layout:
#   results/<BATCH_TAG>/
#     batch.log                       <- progress log (tee)
#     summary.csv                     <- per-run metrics (SR_L/SSR_L/Vio ...)
#     <task>_<profile>_rep<NN>/       <- WORK_DIR per run (console.log + videos
#                                        + safe_memory_benchmark/.../report.json)
# =============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

MODEL="${1:-gpt-4o}"
SCENE="${2:-Beechwood_0_int}"
REPS="${3:-10}"
BATCH_TAG="${4:-matrix_$(date +%Y%m%d_%H%M%S)}"
PER_RUN_TIMEOUT_SECONDS="${5:-10800}"   # 3h safety net per run

if [[ ! "${REPS}" =~ ^[0-9]+$ ]]; then
    echo "REPS must be a non-negative integer, got: ${REPS}" >&2
    exit 2
fi

TASKS=(
    "lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3"
    "lifelong_crossroom__beechwood__mold_rag_dining_reuse_v3"
    # "lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v3"
    # "lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3"
)

PROFILES=(${PROFILES:-full sap no_sg})

# Canonical task-specific safe-memory configs (same mapping as
# TASK_SAFE_MEMORY_CONFIGS in og_ego_prim/cli/safe_memory_benchmark_once.py).
declare -A TASK_CONFIG=(
    ["lifelong_crossroom__beechwood__hot_water_container_fragile_vase_v3"]="entrypoints/configs/eval_safe_memory_hot_water.yaml"
    ["lifelong_crossroom__beechwood__mold_rag_dining_reuse_v3"]="entrypoints/configs/eval_safe_memory_mold_rag.yaml"
    # ["lifelong_crossroom__beechwood__cleaner_food_cabinet_location_v3"]="entrypoints/configs/eval_safe_memory_cleaner_food.yaml"
    # ["lifelong_crossroom__beechwood__knife_hidden_in_hamper_v3"]="entrypoints/configs/eval_safe_memory_knife_hidden_hamper.yaml"
)

BATCH_DIR="results/${BATCH_TAG}"
mkdir -p "${BATCH_DIR}"
BATCH_LOG="${BATCH_DIR}/batch.log"
SUMMARY_CSV="${BATCH_DIR}/summary.csv"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${BATCH_LOG}"; }

# ---- preflight ------------------------------------------------------------
if [[ ! -f "entrypoints/eval_safe_memory_once.sh" ]]; then
    echo "entrypoint not found: entrypoints/eval_safe_memory_once.sh" >&2
    exit 2
fi
for task in "${TASKS[@]}"; do
    if [[ ! -f "${TASK_CONFIG[${task}]}" ]]; then
        echo "task config missing: ${TASK_CONFIG[${task}]} (task=${task})" >&2
        exit 2
    fi
done
if [[ ! -s "og_ego_prim/prompting/sap_cognitions.txt" ]]; then
    echo "SAP cognition prompt missing or empty: og_ego_prim/prompting/sap_cognitions.txt" >&2
    exit 2
fi

TOTAL=$(( ${#TASKS[@]} * ${#PROFILES[@]} * REPS ))
log "batch tag:    ${BATCH_TAG}"
log "model:        ${MODEL}"
log "scene:        ${SCENE}"
log "reps/profile: ${REPS}"
log "jobs total:   ${#TASKS[@]} tasks x ${#PROFILES[@]} profiles x ${REPS} = ${TOTAL}"
log "output dir:   ${BATCH_DIR}"

if (( REPS <= 0 )); then
    log "dry run: REPS=${REPS}; printing plan only"
    for task in "${TASKS[@]}"; do
        for profile in "${PROFILES[@]}"; do
            log "  plan: ${task} ${profile} x${REPS}"
        done
    done
    exit 0
fi

# ---- wait for GPU (another benchmark may still be running, e.g. manual test) -
if [[ "${NO_WAIT:-0}" != "1" ]]; then
    while pgrep -f "og_ego_prim.cli.safe_memory_benchmark_once" >/dev/null 2>&1; do
        log "another benchmark process is running; waiting 60s for it to finish ..."
        sleep 60
    done
fi

# ---------------------------------------------------------------------------
# GPU hygiene helpers.
#
# A crashed Isaac/Vulkan instance often leaves the process alive (or a zombie)
# still holding ~11-14 GB of VRAM. The next instance then dies within seconds
# with ``rc=139`` / ``ERROR_OUT_OF_DEVICE_MEMORY``. So before every cell we:
#   1. kill any leftover safe-memory processes from previous cells;
#   2. wait until free VRAM is above a threshold;
#   3. if VRAM is still short, wait longer (bounded) instead of starting a
#      doomed instance.
# ---------------------------------------------------------------------------

GPU_TOTAL_MIB=16376
GPU_FREE_MIN_MIB=8000          # require >= ~8 GB free before starting a cell
GPU_RECOVER_TIMEOUT_SECONDS=900

_kill_leftover_benchmarks() {
    local pids
    pids="$(pgrep -f "og_ego_prim.cli.safe_memory_benchmark_once" 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
        log "killing leftover benchmark processes: $(echo ${pids} | tr '\n' ' ')"
        # SIGTERM first (lets the runner flush an aborted report), then SIGKILL.
        kill ${pids} 2>/dev/null || true
        sleep 5
        pids="$(pgrep -f "og_ego_prim.cli.safe_memory_benchmark_once" 2>/dev/null || true)"
        if [[ -n "${pids}" ]]; then
            log "force-killing still-alive benchmark processes: $(echo ${pids} | tr '\n' ' ')"
            kill -9 ${pids} 2>/dev/null || true
            sleep 3
        fi
    fi
}

_gpu_free_mib() {
    # Free VRAM on the first GPU (the one Isaac uses). Falls back to 0 so a
    # broken nvidia-smi makes us wait instead of launching into OOM.
    nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null \
        | head -1 | tr -d ' ' || echo 0
}

_wait_gpu_ready() {
    local waited=0 try
    while true; do
        local free_mib
        free_mib="$(_gpu_free_mib)"
        if (( free_mib >= GPU_FREE_MIN_MIB )); then
            log "GPU ready: ${free_mib} MiB free (need >= ${GPU_FREE_MIN_MIB})"
            return 0
        fi
        if (( waited >= GPU_RECOVER_TIMEOUT_SECONDS )); then
            log "ERROR: GPU still has only ${free_mib} MiB free after ${waited}s; skipping this cell"
            return 1
        fi
        log "GPU busy: ${free_mib} MiB free; waiting 30s (waited ${waited}s)"
        sleep 30
        waited=$(( waited + 30 ))
    done
}

# ---- summary csv -----------------------------------------------------------
if [[ ! -f "${SUMMARY_CSV}" ]]; then
    printf 'task,profile,rep,status,return_code,elapsed_seconds,SR_L,SSR_L,Vio,episode_task_success,episode_safe_success,safety_condition_recall,num_safety_conditions,num_satisfied_safety_conditions,scene_graph_enabled,risk_predictor_enabled,prompt_setting\n' > "${SUMMARY_CSV}"
fi

# ---- helpers ---------------------------------------------------------------
_report_file() {
    local work_dir="$1"
    find "${work_dir}/safe_memory_benchmark" -type f -path "*/${MODEL}/report.json" 2>/dev/null | head -1
}

# A run counts as completed only if its safe-memory report is VALID (parseable
# JSON with real metrics). A truncated report (e.g. a crash during report
# serialization) or a top-level aborted report must NOT be treated as success.
_report_is_valid() {
    local report="$1"
    [[ -n "${report}" && -f "${report}" ]] || return 1
    python3 - "${report}" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    m = d.get("metrics")
    ok = isinstance(m, dict) and m.get("N") not in (None, 0)
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
PY
}

_append_row() {
    local task="$1" profile="$2" rep="$3" status="$4" rc="$5" elapsed="$6" report="$7"
    local fields
    if [[ -n "${report}" && -f "${report}" ]]; then
        fields="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
m = d.get("metrics") or {}
a = d.get("runtime_ablation") or {}
print(",".join(str(x) for x in [
    m.get("SR_L"), m.get("SSR_L"), m.get("Vio"),
    m.get("episode_task_success"), m.get("episode_safe_success"),
    m.get("safety_condition_recall"),
    m.get("num_safety_conditions"), m.get("num_satisfied_safety_conditions"),
    a.get("scene_graph_enabled"), a.get("risk_predictor_enabled"),
    a.get("prompt_setting"),
]))
' "${report}")"
    else
        fields=",,,,,,,,,,"
    fi
    printf '%s,%s,%s,%s,%s,%s,%s\n' \
        "${task}" "${profile}" "${rep}" "${status}" "${rc}" "${elapsed}" "${fields}" \
        >> "${SUMMARY_CSV}"
}

# ---- one cell ----------------------------------------------------------------
run_cell() {
    local task="$1" profile="$2" rep="$3"
    local work_dir="${BATCH_DIR}/${task}_${profile}_rep${rep}"
    local report rc elapsed status
    local -a flags=()

    report="$(_report_file "${work_dir}")"
    if _report_is_valid "${report}"; then
        log "SKIP  ${task} ${profile} rep${rep} (already completed)"
        _append_row "${task}" "${profile}" "${rep}" "ok" 0 "" "${report}"
        return 0
    fi
    if [[ -f "${work_dir}/report.json" ]]; then
        log "RESUME-RERUN ${task} ${profile} rep${rep} (previous run aborted/crashed; safe-memory report invalid)"
    fi

    case "${profile}" in
        full)  flags=(--enable-scene-graph --enable-risk-predictor) ;;
        no_sg) flags=(--no-enable-scene-graph --enable-risk-predictor) ;;
        sap) flags=(--no-enable-scene-graph --no-enable-risk-predictor --prompt-setting sap) ;;
        *)
            log "ERROR: unknown profile '${profile}'"
            return 2
            ;;
    esac

    mkdir -p "${work_dir}"
    log "START ${task} ${profile} rep${rep} -> ${work_dir}"
    local start_ts end_ts
    start_ts="$(date +%s)"
    set +e
    timeout --kill-after=30s "${PER_RUN_TIMEOUT_SECONDS}" \
        bash entrypoints/eval_safe_memory_once.sh \
            "${profile}" "${MODEL}" "${SCENE}" "${task}" \
            "${TASK_CONFIG[${task}]}" "${work_dir}" \
            "${flags[@]}" \
        >> "${BATCH_LOG}" 2>&1
    rc=$?
    set -e
    end_ts="$(date +%s)"
    elapsed=$(( end_ts - start_ts ))

    report="$(_report_file "${work_dir}")"
    if _report_is_valid "${report}"; then
        status="ok"
    elif [[ -f "${work_dir}/report.json" ]]; then
        # Episode crashed mid-run (run_error written top-level). Even if a
        # truncated safe-memory report exists, this is NOT a successful run.
        status="aborted"
        report=""
    elif (( rc == 124 )); then
        status="timeout"
        report=""
    else
        status="failed"
        report=""
    fi
    _append_row "${task}" "${profile}" "${rep}" "${status}" "${rc}" "${elapsed}" "${report}"
    log "DONE  ${task} ${profile} rep${rep} status=${status} rc=${rc} elapsed=${elapsed}s"
    if [[ "${status}" != "ok" ]]; then
        log "      inspect: ${work_dir}/console.log"
    fi
    return 0
}

# ---- matrix -------------------------------------------------------------------
done_count=0
for task in "${TASKS[@]}"; do
    for profile in "${PROFILES[@]}"; do
        for (( rep = 1; rep <= REPS; rep++ )); do
            done_count=$(( done_count + 1 ))
            # Clean up any leftover process/VRAM from a crashed previous cell
            # BEFORE starting the next one.
            _kill_leftover_benchmarks
            if ! _wait_gpu_ready; then
                log "SKIP-GPU ${task} ${profile} rep${rep} (GPU not ready)"
                _append_row "${task}" "${profile}" "${rep}" "gpu_unavailable" 1 "" ""
                continue
            fi
            run_cell "${task}" "${profile}" "${rep}" || true
        done
    done
done

log "================================================================"
log "batch finished: ${BATCH_DIR}"
log "summary csv:    ${SUMMARY_CSV}"
python3 -c '
import csv, collections
rows = list(csv.DictReader(open("'"${SUMMARY_CSV}"'")))
counts = collections.Counter(r["status"] for r in rows)
print("[summary] total=%d by_status=%s" % (len(rows), dict(counts)))
' 2>/dev/null || true
