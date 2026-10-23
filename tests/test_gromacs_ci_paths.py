from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_gromacs_changes import requires_gromacs


@pytest.mark.parametrize(
    "path",
    [
        "app/execution.py",
        "app/worker.py",
        "app/workflows/simulation.py",
        "app/analysis/research.py",
        "scripts/md_plot.py",
        "tests/test_gromacs_integration.py",
        "tests/fixtures/nested/example.gro",
        "forcefields/custom.ff/forcefield.itp",
        "app/static/examples/files/example.mdp",
        "requirements/runtime.lock",
        "requirements.txt",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    ],
)
def test_simulation_and_analysis_changes_trigger_real_gromacs(path):
    assert requires_gromacs(["README.md", path])


def test_documentation_and_frontend_changes_do_not_trigger_real_gromacs():
    assert not requires_gromacs(["README.md", "docs/USAGE.md", "app/static/js/jobs.js", "app/templates/index.html"])
    assert not requires_gromacs([])


def test_git_nul_delimited_paths_are_handled_without_shell_interpretation():
    script = Path(__file__).resolve().parents[1] / "scripts/check_gromacs_changes.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        input="docs/some\napp/worker.py\0app/analysis/research.py\0",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == "required=true\n"
