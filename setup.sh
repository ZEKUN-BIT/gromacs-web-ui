#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

profile=runtime
if [ "${1:-}" = "--dev" ]; then
  profile=dev
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "Usage: ./setup.sh [--dev]" >&2
  exit 2
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python scripts/lock_dependencies.py --check
python -m pip install --require-hashes --only-binary=:all: -r "requirements/$profile.lock"
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check

if [ "$(uname -s)" = "Linux" ]; then
  python scripts/linux_desktop.py install || echo "Desktop shortcut registration failed; the terminal launcher remains available." >&2
fi

echo "Environment ready. On Linux, open GROMACS Console from the application menu or desktop; ./run.sh is also available."
