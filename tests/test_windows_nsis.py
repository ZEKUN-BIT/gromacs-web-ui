"""Compile real installers without importing the Linux application or service."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_nsis_builder", ROOT / "packaging/windows/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def nsis_compiler():
    compiler = shutil.which("makensis")
    if compiler:
        return compiler
    if os.environ.get("GROMACS_REQUIRE_NSIS") == "1":
        pytest.fail("GROMACS_REQUIRE_NSIS=1 requires a real makensis compiler on PATH", pytrace=False)
    pytest.skip("Real NSIS compiler is not installed")


@pytest.mark.parametrize("charset", ["UTF8", "CP1252" if sys.platform == "win32" else "CP437"])
def test_real_nsis_compiled_chinese_text(tmp_path, nsis_compiler, charset):
    # Linux iconv rejects undefined CP1252 bytes before producing an installer.
    # CP437 decodes every source byte, allowing the same real corruption check
    # on Linux; Windows retains the native CP1252 regression case.
    builder.build(tmp_path)
    installer = tmp_path / "compiled.exe"
    prefix = "/" if Path(nsis_compiler).suffix.lower() == ".exe" else "-"
    completed = subprocess.run(
        [
            nsis_compiler,
            prefix + "INPUTCHARSET",
            charset,
            prefix + f"DPAYLOAD_DIR={tmp_path / 'windows-payload'}",
            prefix + f"DOUTPUT_FILE={installer}",
            prefix + "DAPP_VERSION=0.0.0",
            str(builder.ASSETS / "installer.nsi"),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    if charset == "UTF8":
        builder.verify_installer_text(installer)
    else:
        # FinishTitle's short text also occurs in NSIS's standard language table,
        # so searching the complete header cannot distinguish its corrupt copy.
        with pytest.raises(ValueError, match=r"WelcomeTitle.*WelcomeText.*FinishText.*RunText.*KeepEnvironment"):
            builder.verify_installer_text(installer)
