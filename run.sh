#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Virtual environment not found. Run ./setup.sh first." >&2
  exit 1
fi

source .venv/bin/activate
python -c "import fastapi, uvicorn, multipart" >/dev/null 2>&1 || {
  echo "Dependencies are missing. Run ./setup.sh first." >&2
  exit 1
}

export PYTHONPATH="$PWD"
export GMX_BIN="${GMX_BIN:-gmx}"

web_workers="${WEB_CONCURRENCY:-1}"
expect_worker_value=false
for argument in "$@"; do
  if [ "$expect_worker_value" = true ]; then
    web_workers="$argument"
    expect_worker_value=false
    continue
  fi
  case "$argument" in
    --workers) expect_worker_value=true ;;
    --workers=*) web_workers="${argument#--workers=}" ;;
  esac
done
if [ "$expect_worker_value" = true ]; then
  echo "--workers requires a value; only --workers 1 is supported." >&2
  exit 2
fi
if [ "$web_workers" != "1" ]; then
  echo "Multiple Web workers are not supported. Use the dedicated execution worker and --workers 1." >&2
  exit 2
fi

python -m app.worker &
execution_worker_pid=$!
cleanup_worker() {
  kill "$execution_worker_pid" 2>/dev/null || true
  wait "$execution_worker_pid" 2>/dev/null || true
}
trap cleanup_worker EXIT INT TERM

uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --timeout-graceful-shutdown 5 "$@"
