#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PATH="/home/lzy/anaconda3/envs/isbench/bin:${PATH}"
export ISBENCH_OMNIGIBSON_PYTHON=/home/lzy/anaconda3/envs/isbench/bin/python
source entrypoints/env.sh

exec entrypoints/omnigibson_python.sh -m og_ego_prim.cli.headless_manual_physical_session "$@"
