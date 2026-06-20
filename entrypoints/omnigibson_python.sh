#!/usr/bin/env bash
set -euo pipefail

# Isaac Sim must use one consistent system X11/XCB stack.
unset QT_QPA_PLATFORM_PLUGIN_PATH QT_PLUGIN_PATH
unset PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
unset ROS_DISTRO ROS_ETC_DIR ROS_MASTER_URI ROS_PACKAGE_PATH
unset ROS_PYTHON_VERSION ROS_ROOT ROS_VERSION

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ISBENCH_OMNIGIBSON_PYTHON:-python}"
if [[ "${PYTHON_BIN}" == */* ]]; then
    export PATH="$(dirname "${PYTHON_BIN}"):${PATH:-}"
fi
if [[ -f "${SCRIPT_DIR}/env.sh" ]]; then
    source "${SCRIPT_DIR}/env.sh"
fi

export ISBENCH_OMNIGIBSON_X11_FIX=1
export LD_PRELOAD="${ISBENCH_OMNIGIBSON_X11_PRELOAD:-/usr/lib/x86_64-linux-gnu/libxcb.so.1:/usr/lib/x86_64-linux-gnu/libX11-xcb.so.1:/usr/lib/x86_64-linux-gnu/libX11.so.6}"

exec "${PYTHON_BIN}" "$@"

# ./entrypoints/omnigibson_python.sh -m omnigibson.examples.environments.navigation_env_demo
