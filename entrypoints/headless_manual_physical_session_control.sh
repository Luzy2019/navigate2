#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

exec /home/lzy/anaconda3/envs/isbench/bin/python \
    -m og_ego_prim.cli.headless_manual_physical_session_control "$@"
