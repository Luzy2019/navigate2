# for apptainer
# export OMNIGIBSON_ROOT=
# export APPTAINER_IMAGE=
# export MAMBA_ROOT_PREFIX=/micromamba
# export APPTAINER_SITE_PACKAGES=/micromamba/envs/omnigibson/lib/python3.10/site-packages
# export BINDING=

# for slurm
# export PARTITION=AI4Good_L1_p

# Keep credentials in env.local.sh so they are not committed.
ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${ENV_DIR}/env.local.sh" ]]; then
    source "${ENV_DIR}/env.local.sh"
fi

export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-${API_BASE_URL:-}}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-${OPENAI_API_BASE}}"
export OPENAI_EXTRA_BODY=${OPENAI_EXTRA_BODY:-'{"provider":{"sort":"success_rate"}}'}

# Required only for the google-genai backend.
export GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-}"
