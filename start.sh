#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

if [ -n "${CONDA_ENV:-}" ]; then
  if ! command -v conda >/dev/null 2>&1; then
    echo "Conda is not available in PATH; unset CONDA_ENV or initialize Conda first." >&2
    exit 1
  fi
  eval "$(conda shell.bash hook)"
  conda activate "$CONDA_ENV"
fi

if [ -n "${AMBER_SH:-}" ]; then
  if [ ! -f "$AMBER_SH" ]; then
    echo "Amber initialization script not found: $AMBER_SH" >&2
    exit 1
  fi
  source "$AMBER_SH"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -f "$CONDA_PREFIX/amber.sh" ]; then
  source "$CONDA_PREFIX/amber.sh"
fi

export GMX_BIN="${GMX_BIN:-gmx}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"

echo "Starting GROMACS Web UI at http://${HOST}:${PORT}"
exec "$PROJECT_DIR/run.sh"
