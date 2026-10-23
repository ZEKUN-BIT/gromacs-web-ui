#!/usr/bin/env python3
"""Read NUL-delimited git paths and decide whether to run real GROMACS tests."""

from __future__ import annotations

import fnmatch
import sys

PATTERNS = (
    "app/*.py",
    "scripts/*.py",
    "tests/*.py",
    "tests/fixtures/*",
    "forcefields/*",
    "app/static/examples/files/*",
    ".github/workflows/*",
    "requirements.txt",
    "requirements/*",
    "pyproject.toml",
    "setup.sh",
    "run.sh",
)


def requires_gromacs(paths: list[str]) -> bool:
    # fnmatch's '*' also matches '/', covering nested backend modules and fixtures.
    return any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in PATTERNS)


if __name__ == "__main__":
    paths = sys.stdin.buffer.read().decode("utf-8", errors="surrogateescape").split("\0")
    print(f"required={str(requires_gromacs(paths)).lower()}")
