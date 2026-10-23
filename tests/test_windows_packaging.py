from __future__ import annotations

import importlib.util
import io
import json
import lzma
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.diagnostics import environment_diagnostics

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging/windows" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("windows_builder", "build.py")
service = load_module("windows_service", "desktop_service.py")
CHINESE_LANGSTRINGS = ("WelcomeTitle", "WelcomeText", "FinishTitle", "FinishText", "RunText", "KeepEnvironment")


def nsis_archive(header: bytes, payload: bytes = b"", *, flags: int = 0) -> bytes:
    """Small solid LZMA fixture using the NSIS archive wire format."""
    compressed = b"\x5d\x00\x00\x01\x00" + lzma.compress(
        struct.pack("<I", len(header)) + header + payload,
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 65536, "lc": 3, "lp": 0, "pb": 2}],
    )
    return (
        struct.pack("<I", flags)
        + b"\xef\xbe\xad\xdeNullsoftInst"
        + struct.pack("<II", len(header), 32 + len(compressed))
        + compressed
        + b"\0" * 4
    )


def installer_fixture(*, corrupt: str | None = None) -> bytes:
    source = (builder.ASSETS / "installer.nsi").read_text(encoding="utf-8")
    strings = re.findall(r'^LangString (\w+) \$\{LANG_SIMPCHINESE\} "(.*)"$', source, re.MULTILINE)
    assert tuple(name for name, _ in strings) == CHINESE_LANGSTRINGS
    install, uninstall = bytearray(), bytearray()
    for name, literal in strings:
        text = literal.replace(r"$\r", "\r").replace(r"$\n", "\n")
        if name == corrupt:
            text = text.encode("utf-8").decode("cp1252", errors="replace")
        target = uninstall if name == "KeepEnvironment" else install
        target.extend(text.encode("utf-16-le") + b"\0\0")
    return b"MZ" + nsis_archive(bytes(install), nsis_archive(bytes(uninstall), flags=1))


def test_compiled_installer_checks_install_and_uninstall_chinese_text(tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(installer_fixture())
    builder.verify_installer_text(installer)


@pytest.mark.parametrize("name", CHINESE_LANGSTRINGS)
def test_compiled_installer_rejects_each_corrupted_chinese_string(tmp_path, name):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(installer_fixture(corrupt=name))
    with pytest.raises(ValueError, match=name):
        builder.verify_installer_text(installer)


@pytest.mark.parametrize("compiler, prefix", [("makensis", "-"), ("makensis.exe", "/")])
def test_build_explicitly_decodes_nsis_source_as_utf8_and_checks_result(tmp_path, monkeypatch, compiler, prefix):
    calls = []

    def compile_installer(command, *, check):
        assert check
        calls.append(command)
        output = next(value.split("=", 1)[1] for value in command if value.startswith(prefix + "DOUTPUT_FILE="))
        Path(output).write_bytes(installer_fixture())

    monkeypatch.setattr(builder.subprocess, "run", compile_installer)
    artifacts = builder.build(tmp_path, compiler)
    assert calls[0][:3] == [compiler, prefix + "INPUTCHARSET", "UTF8"]
    assert Path(calls[0][-1]).is_absolute()
    assert artifacts[-1].suffix == ".exe"
    assert artifacts[-1].with_suffix(".exe.sha256").is_file()


def test_build_rejects_corrupted_compiler_output_before_checksums(tmp_path, monkeypatch):
    def compile_installer(command, *, check):
        output = next(value.split("=", 1)[1] for value in command if value.startswith("/DOUTPUT_FILE="))
        Path(output).write_bytes(installer_fixture(corrupt="KeepEnvironment"))

    monkeypatch.setattr(builder.subprocess, "run", compile_installer)
    with pytest.raises(ValueError, match="KeepEnvironment"):
        builder.build(tmp_path, "makensis.exe")
    assert not list(tmp_path.glob("*.sha256"))


def test_installer_verification_limits_input_size(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_INSTALLER_BYTES", 64)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x" * 65)
    with pytest.raises(ValueError, match="size limit"):
        builder.verify_installer_text(installer)


def test_installer_verification_limits_decompressed_size():
    archive = nsis_archive(b"header", b"x" * 10000)
    with pytest.raises(ValueError, match="decompressed data exceeds"):
        builder._nsis_archive(archive, flags=0, max_decoded=100)


@pytest.mark.parametrize("damage", ["signature", "length", "dictionary", "truncated_stream", "flags", "header_size"])
def test_installer_verification_rejects_invalid_archive(damage):
    archive = bytearray(nsis_archive(b"header"))
    if damage == "signature":
        archive[4] = 0
    elif damage == "length":
        struct.pack_into("<I", archive, 24, len(archive) + 1)
    elif damage == "dictionary":
        struct.pack_into("<I", archive, 29, 2**31)
    elif damage == "truncated_stream":
        del archive[-8:-4]
        struct.pack_into("<I", archive, 24, len(archive))
    elif damage == "flags":
        struct.pack_into("<I", archive, 0, 1)
    elif damage == "header_size":
        struct.pack_into("<I", archive, 20, 3)
    with pytest.raises(ValueError, match="NSIS"):
        builder._nsis_archive(bytes(archive), flags=0, max_decoded=1024)


@pytest.mark.skipif(shutil.which("makensis") is None, reason="Real NSIS compiler is not installed")
@pytest.mark.parametrize(
    "charset",
    [
        "UTF8",
        pytest.param("CP1252", marks=pytest.mark.skipif(sys.platform != "win32", reason="Linux iconv rejects undefined CP1252 bytes")),
    ],
)
def test_real_nsis_compiled_chinese_text(tmp_path, charset):
    builder.build(tmp_path)
    installer = tmp_path / "compiled.exe"
    compiler = shutil.which("makensis")
    prefix = "/" if Path(compiler).suffix.lower() == ".exe" else "-"
    subprocess.run(
        [
            compiler,
            prefix + "INPUTCHARSET",
            charset,
            prefix + f"DPAYLOAD_DIR={tmp_path / 'windows-payload'}",
            prefix + f"DOUTPUT_FILE={installer}",
            prefix + "DAPP_VERSION=0.0.0",
            str(builder.ASSETS / "installer.nsi"),
        ],
        check=True,
        capture_output=True,
    )
    if charset == "UTF8":
        builder.verify_installer_text(installer)
    else:
        with pytest.raises(ValueError, match="WelcomeTitle.*WelcomeText.*FinishText.*RunText.*KeepEnvironment"):
            builder.verify_installer_text(installer)


def test_windows_entrypoints_share_the_power_shell_host_selector():
    assets = ROOT / "packaging/windows"
    launcher = (assets / "Start.cmd").read_text(encoding="utf-8")
    installer = (assets / "installer.nsi").read_text(encoding="utf-8")
    setup = (assets / "WslSetup.ps1").read_text(encoding="utf-8")
    assert 'PowerShellHost.ps1" -LaunchAction %GROMACS_ACTION%' in launcher
    assert 'set "GROMACS_ACTION=Start"' in launcher
    for action in ("Install", "Stop", "Files", "Diagnose", "RemoveEnvironment"):
        assert f'set "GROMACS_ACTION={action}"' in launcher
        assert f'"$INSTDIR\\Start.cmd" "{action}"' in installer
    action_shortcuts = [
        line for line in installer.splitlines() if "CreateShortcut" in line and ".lnk" in line and "Uninstall.lnk" not in line
    ]
    assert action_shortcuts and all('"$INSTDIR\\Start.cmd"' in line for line in action_shortcuts)
    assert "$powershell = Get-PreferredPowerShellHost" in setup
    assert 'File "${PAYLOAD_DIR}\\PowerShellHost.ps1"' in installer
    assert 'Delete "$INSTDIR\\PowerShellHost.ps1"' in installer


def test_payload_omits_private_data_and_includes_runtime_plotting(tmp_path):
    payload = tmp_path / "application.tar.gz"
    builder.write_payload(ROOT, payload)
    with tarfile.open(payload) as archive:
        names = set(archive.getnames())
        assert {
            "app/main.py",
            "app/worker.py",
            "app/agent_api.py",
            "app/agent_provider.py",
            "app/agent_service.py",
            "app/agent_tools.py",
            "app/static/agent.css",
            "app/static/js/agent.js",
            "app/static/js/agent-content.js",
            "app/templates/index.html",
            "scripts/md_plot.py",
            "scripts/__init__.py",
            "mdp/complex/md.mdp",
            "requirements.txt",
            "run.sh",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "app/static/licenses/project-MIT.txt",
            "app/static/fonts/ibm-plex-sans-var-latin.woff2",
            "app/static/fonts/jetbrains-mono-var-latin.woff2",
            "app/static/fonts/IBM-Plex-OFL.txt",
            "app/static/fonts/JetBrains-Mono-OFL.txt",
            "app/static/icons/lucide/LICENSE",
            "docs/QUICKSTART.md",
            "docs/RELEASE-CHECKLIST.md",
            "docs/USAGE.md",
            "docs/AI-AGENT.md",
            "app/static/examples/index.html",
            "app/static/examples/summary.json",
        } <= names
        assert not any(name.split("/")[0] in {"runtime", ".venv", "tests", ".env", "settings.json"} for name in names)
        assert not any("__pycache__" in name or "mdout" in name or "aki" in name for name in names)
        assert all(item.isreg() and not item.name.startswith("/") and ".." not in Path(item.name).parts for item in archive)
    assert payload.stat().st_size < 1024**2


def test_payload_limits_example_assets_and_help_to_public_allowlists(tmp_path):
    for folder in ("app/static/examples/files", "docs", "scripts", "mdp"):
        (tmp_path / folder).mkdir(parents=True)
    for name in ("requirements.txt", "run.sh", "scripts/__init__.py", "scripts/md_plot.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    for name in (
        "docs/QUICKSTART.md",
        "docs/private-notes.md",
        "app/static/settings.json",
        "app/static/examples/input.tpr",
        "app/static/examples/files/energy.xvg",
        "app/static/examples/files/energy.png",
    ):
        (tmp_path / name).write_text("example", encoding="utf-8")
    payload = tmp_path / "application.tar.gz"
    builder.write_payload(tmp_path, payload)
    with tarfile.open(payload) as archive:
        names = set(archive.getnames())
    assert {"docs/QUICKSTART.md", "app/static/examples/files/energy.xvg", "app/static/examples/files/energy.png"} <= names
    assert not {"docs/private-notes.md", "app/static/settings.json", "app/static/examples/input.tpr"} & names


def test_payload_is_reproducible(tmp_path):
    first, second = tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"
    builder.write_payload(ROOT, first)
    builder.write_payload(ROOT, second)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("launcher_newline", [b"\n", b"\r\n"])
def test_build_preserves_unicode_and_script_newlines_with_windows_text_defaults(tmp_path, monkeypatch, launcher_newline):
    root = tmp_path / "中文源码"
    for path in [*builder.source_files(ROOT), ROOT / "pyproject.toml"]:
        target = root / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    assets = tmp_path / "assets"
    shutil.copytree(builder.ASSETS, assets)
    launcher = (assets / "Start.cmd").read_bytes().replace(b"\r\n", b"\n")
    (assets / "Start.cmd").write_bytes(launcher.replace(b"\n", launcher_newline))
    html = '<p>中文数据</p><link href="/static/例子.css">'.encode("utf-8")
    css = "/* 中文数据 */ body { color: green; }".encode("utf-8")
    (root / "app/static/说明.html").write_bytes(html)
    (root / "app/static/例子.css").write_bytes(css)
    project = root / "pyproject.toml"
    project.write_bytes(project.read_bytes() + "\n# 中文数据\n".encode("utf-8"))
    original_open = Path.open

    def windows_text_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode:
            if encoding in (None, "locale"):
                encoding = "cp1252"
            if newline is None and any(flag in mode for flag in "wax"):
                newline = "\r\n"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "ASSETS", assets)
    with monkeypatch.context() as context:
        context.setattr(Path, "open", windows_text_open)
        # Prove the fixture reproduces the Windows default-decoding failure.
        with pytest.raises(UnicodeDecodeError):
            (root / "app/static/说明.html").read_text()
        artifact = builder.build(tmp_path / "中文输出")[0]
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read("GromacsConsole/Start.cmd") == launcher.replace(b"\n", b"\r\n")
        assert b"\r" not in archive.read("GromacsConsole/linux-install.sh")
        assert archive.read("GromacsConsole/README.md") == (root / "docs/WINDOWS.md").read_bytes()
        assert b"\r" not in archive.read("GromacsConsole/release.json")
        assert b"\r" not in archive.read("GromacsConsole/application.sha256")
        with tarfile.open(fileobj=io.BytesIO(archive.read("GromacsConsole/application.tar.gz"))) as payload:
            assert payload.getnames() == sorted(payload.getnames())
            assert payload.extractfile("app/static/说明.html").read() == html
            assert payload.extractfile("app/static/例子.css").read() == css
            assert payload.extractfile("docs/QUICKSTART.md").read() == (root / "docs/QUICKSTART.md").read_bytes()
            assert b"\r" not in payload.extractfile("run.sh").read()
    assert b"\r" not in artifact.with_suffix(artifact.suffix + ".sha256").read_bytes()


def test_windows_archive_includes_provisioning_helpers_and_excludes_stale_files(tmp_path):
    staging = tmp_path / "windows-payload"
    staging.mkdir()
    (staging / "private-settings.json").write_text('{"private":true}', encoding="utf-8")
    artifacts = builder.build(tmp_path)
    with zipfile.ZipFile(artifacts[0]) as archive:
        names = set(archive.namelist())
        assert {
            "GromacsConsole/Console.ps1",
            "GromacsConsole/WslSetup.ps1",
            "GromacsConsole/WslPrerequisites.ps1",
            "GromacsConsole/PowerShellHost.ps1",
            "GromacsConsole/GromacsConsole.ico",
        } <= names
        assert {
            "GromacsConsole/gpu_setup.py",
            "GromacsConsole/dependency_setup.py",
            "GromacsConsole/science-tools.py",
            "GromacsConsole/science-requirements.txt",
            "GromacsConsole/windows-python-requirements.lock",
            "GromacsConsole/application_update.py",
            "GromacsConsole/LICENSE",
            "GromacsConsole/THIRD_PARTY_NOTICES.md",
        } <= names
        rootfs_spec = json.loads(archive.read("GromacsConsole/ubuntu-rootfs.json"))
        assert rootfs_spec["url"].startswith("https://releases.ubuntu.com/24.04/")
        assert len(rootfs_spec["sha256"]) == 64
        assert "GromacsConsole/private-settings.json" not in names


def test_dependency_helper_change_triggers_environment_update_without_application_change(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    shutil.copytree(builder.ASSETS, assets)
    monkeypatch.setattr(builder, "ASSETS", assets)
    first = builder.build(tmp_path / "first")[0]
    helper = assets / "dependency_setup.py"
    helper.write_bytes(helper.read_bytes() + b"\n# Changed dependency verification.\n")
    second = builder.build(tmp_path / "second")[0]
    with zipfile.ZipFile(first) as old, zipfile.ZipFile(second) as new:
        previous = json.loads(old.read("GromacsConsole/release.json"))
        current = json.loads(new.read("GromacsConsole/release.json"))
        assert previous["payload_sha256"] == current["payload_sha256"]
        assert previous["installation_sha256"] != current["installation_sha256"]
        assert previous["environment_sha256"] != current["environment_sha256"]
        assert new.read("GromacsConsole/dependency_setup.py") == helper.read_bytes()


def test_payload_refuses_source_symlink(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "mdp").mkdir()
    (tmp_path / "scripts").mkdir()
    for name in ("requirements.txt", "run.sh", "scripts/__init__.py", "scripts/md_plot.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "secret").write_text("sensitive", encoding="utf-8")
    (tmp_path / "app/main.py").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="symlink"):
        builder.write_payload(tmp_path, tmp_path / "payload.tar.gz")


def test_payload_refuses_parent_symlink_to_external_private_files(tmp_path):
    source, private = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    private.mkdir()
    (private / "main.py").write_text("private configuration", encoding="utf-8")
    (source / "app").symlink_to(private, target_is_directory=True)
    (source / "scripts").mkdir()
    (source / "mdp").mkdir()
    for name in ("requirements.txt", "run.sh", "scripts/__init__.py", "scripts/md_plot.py"):
        (source / name).write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        builder.write_payload(source, tmp_path / "payload.tar.gz")


@pytest.mark.parametrize("gpu_support, expected", [("disabled", False), ("CUDA", True), ("", False)])
def test_gpu_recommendation_checks_gromacs_build(gpu_support, expected, tmp_path):
    gmx = {"available": True, "version": f"GROMACS version: 2026.3\nGPU support: {gpu_support}\n"}
    with (
        patch("app.diagnostics.discover_local_force_fields", return_value=[]),
        patch("app.diagnostics.installed_water_model_selection", return_value="1"),
        patch("app.diagnostics.host_information", return_value={"cpu_count": 8, "physical_core_count": 4}),
        patch("app.diagnostics.gpu_information", return_value={"available": True, "devices": ["NVIDIA GPU"]}),
        patch("app.diagnostics.cuda_runtime_information", return_value={"available": True, "device_count": 1, "reason": ""}),
        patch("app.diagnostics._probe", return_value=gmx),
    ):
        result = environment_diagnostics(tmp_path, {"gmx_bin": "gmx"})
    assert result["performance_recommendation"]["gpu"] is expected
    assert result["performance_recommendation"]["gpu_count"] == int(expected)


@pytest.mark.skipif(sys.platform != "linux", reason="WSL service uses Linux process identity")
def test_stale_pid_record_does_not_signal_an_unrelated_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        record = service.process_identity(child.pid)
        record["ticks"] -= 1
        service.terminate(record)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_stop_refuses_active_jobs_and_preserves_service_record(tmp_path):
    (tmp_path / "runtime").mkdir()
    with sqlite3.connect(tmp_path / "runtime/jobs.sqlite3") as database:
        database.execute("CREATE TABLE jobs(status TEXT, desired_action TEXT)")
        database.execute("INSERT INTO jobs VALUES ('queued', 'run')")
    (tmp_path / "service.json").write_text('{"processes": []}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Jobs are still"):
        service.manage(tmp_path, "stop")
    assert (tmp_path / "service.json").exists()


def test_remove_keeps_simulation_results_and_settings(tmp_path):
    for folder in ("app", "mdp", "scripts", ".venv", "gromacs", "runtime"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "sample").write_text("data", encoding="utf-8")
    (tmp_path / "settings.json").write_text('{"gmx_bin":"gmx"}', encoding="utf-8")
    (tmp_path / "installed-release.json").write_text("{}", encoding="utf-8")
    service.manage(tmp_path, "remove")
    assert (tmp_path / "runtime/sample").read_text(encoding="utf-8") == "data"
    assert (tmp_path / "settings.json").exists()
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / "installed-release.json").exists()


def test_service_log_rotates_during_continuous_writes(tmp_path):
    stream = service.ServiceLogStream(tmp_path / "service.log", max_bytes=2048)
    try:
        for index in range(100):
            stream.write(f"{index}: " + "x" * 500 + "\n")
        stream.flush()
        assert (tmp_path / "service.log").stat().st_size <= 2048
        assert (tmp_path / "service.log.1").stat().st_size <= 2048
        assert "99:" in (tmp_path / "service.log").read_text(encoding="utf-8")
        assert "0:" not in (tmp_path / "service.log").read_text(encoding="utf-8")
    finally:
        stream.handler.close()


def test_log_disk_failure_cannot_recurse_through_redirected_stderr(tmp_path, monkeypatch):
    stream = service.ServiceLogStream(tmp_path / "service.log", max_bytes=2048)
    original_stream = stream.handler.stream
    fallback = io.StringIO()

    class BrokenFile:
        writes = 0

        def write(self, text):
            self.writes += 1
            raise OSError("disk full")

        def flush(self):
            pass

        def seek(self, *args):
            pass

        def tell(self):
            return 0

    broken = BrokenFile()
    stream.handler.stream = broken
    try:
        with monkeypatch.context() as patch:
            patch.setattr(service.sys, "stderr", stream)
            patch.setattr(service.sys, "__stderr__", fallback)
            assert stream.write("first") == 5
            assert stream.write("second") == 6
        assert broken.writes == 2
        assert fallback.getvalue().count("disk full") == 1
    finally:
        stream.handler.stream = original_stream
        stream.handler.close()


def test_application_only_payload_change_preserves_environment_hash(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in ("app", "mdp", "scripts", "docs"):
        shutil.copytree(ROOT / name, root / name)
    for name in ("requirements.txt", "run.sh", "pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, root / name)
    monkeypatch.setattr(builder, "ROOT", root)
    first = builder.build(tmp_path / "before")[0]
    page = root / "app/templates/index.html"
    page.write_bytes(page.read_bytes() + b"\n<!-- presentation update -->\n")
    second = builder.build(tmp_path / "after")[0]
    with zipfile.ZipFile(first) as old, zipfile.ZipFile(second) as new:
        previous = json.loads(old.read("GromacsConsole/release.json"))
        current = json.loads(new.read("GromacsConsole/release.json"))
        assert previous["environment_sha256"] == current["environment_sha256"]
        assert previous["installation_sha256"] != current["installation_sha256"]


def test_build_rejects_unshipped_static_reference(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in ("app", "mdp", "scripts"):
        (root / name).mkdir(parents=True)
    for name in ("requirements.txt", "run.sh", "scripts/__init__.py", "scripts/md_plot.py"):
        (root / name).write_text("", encoding="utf-8")
    (root / "app/page.html").write_text('<img src="/static/missing.png">', encoding="utf-8")
    with pytest.raises(ValueError, match="missing.png"):
        builder.write_payload(root, tmp_path / "bad.tar.gz")


@pytest.mark.skipif(sys.platform != "linux", reason="WSL service integration test")
def test_packaged_service_start_reuse_http_and_stop(tmp_path):
    payload = tmp_path / "application.tar.gz"
    builder.write_payload(ROOT, payload)
    with tarfile.open(payload) as archive:
        archive.extractall(tmp_path, filter="data")
    shutil.copyfile(ROOT / "packaging/windows/desktop_service.py", tmp_path / "desktop_service.py")
    (tmp_path / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    # Occupy 8000: the launcher must use another port rather than an existing server.
    with socket.socket() as occupied:
        try:
            occupied.bind(("127.0.0.1", 8000))
            occupied.listen()
        except OSError:
            pass
        try:
            result = service.manage(tmp_path, "start")
            assert result["url"] != "http://127.0.0.1:8000"
            state = json.loads((tmp_path / "service.json").read_text(encoding="utf-8"))
            assert service.manage(tmp_path, "start") == result
            assert json.loads((tmp_path / "service.json").read_text(encoding="utf-8")) == state
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(result["url"]) as response:
                assert b"GROMACS" in response.read()
            with opener.open(result["url"] + "/api/jobs") as response:
                assert json.load(response) == {"jobs": [], "total": 0, "limit": 50, "offset": 0, "has_more": False}
            # Verify a deferred plotting dependency also survived the allowlist.
            subprocess.run(
                [sys.executable, "-c", "from scripts.md_plot import generate_directory"],
                cwd=tmp_path,
                env={**os.environ, "PYTHONPATH": str(tmp_path)},
                check=True,
            )
        finally:
            service.manage(tmp_path, "stop")
        assert all(not service.matches(record) for record in state["processes"])
        assert not (tmp_path / "service.json").exists()
