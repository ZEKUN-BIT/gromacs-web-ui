from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")
SOURCE = Path(__file__).resolve().parents[1] / "scripts/linux_desktop.py"
spec = importlib.util.spec_from_file_location("linux_desktop", SOURCE)
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    info = {"url": f"http://127.0.0.1:{port}", "runtime": str(root / "runtime")}
    (root / "server.py").write_text(
        "import json, os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\nfrom pathlib import Path\n"
        "Path('server.pid').write_text(str(os.getpid()))\n"
        "with Path('starts').open('a') as log: log.write('started\\n')\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def log_message(self,*args): pass\n"
        " def do_GET(self):\n"
        "  self.send_response(200);self.end_headers()\n"
        "  self.wfile.write(json.dumps({'version':'test','installation_sha256':os.environ.get('GROMACS_CONSOLE_INSTALLATION_SHA256')}).encode())\n"
        f"HTTPServer(('127.0.0.1',{port}),Handler).serve_forever()\n"
    )
    (root / "start.sh").write_text(
        "#!/bin/bash\nif [ \"${1:-}\" = '--desktop-info' ]; then\n"
        f" printf '%s\\n' {shlex.quote(json.dumps(info))}\n exit\nfi\n"
        f"exec {shlex.quote(sys.executable)} server.py\n"
    )
    yield root, info
    pid_file = root / "server.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text())
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def test_concurrent_clicks_start_one_service_and_reuse_it(installation):
    root, info = installation
    directory = desktop.state_directory(root)
    with ThreadPoolExecutor(max_workers=3) as pool:
        urls = list(pool.map(lambda _: desktop.ensure_service(root, directory), range(3)))
    assert urls == [info["url"]] * 3
    assert (root / "starts").read_text().splitlines() == ["started"]
    assert desktop.ensure_service(root, directory) == info["url"]
    assert (directory / "launch.log").stat().st_mode & 0o777 == 0o600


def test_failed_child_reports_failure_and_keeps_diagnostic_log(installation):
    root, _ = installation
    content = (root / "start.sh").read_text().split("exec ")[0]
    (root / "start.sh").write_text(content + "echo 'missing configured Conda environment' >&2\nexit 1\n")
    directory = desktop.state_directory(root)
    with pytest.raises(desktop.LauncherError, match="服务启动失败"):
        desktop.ensure_service(root, directory)
    assert "Conda" in (directory / "launch.log").read_text()


def test_unrelated_listener_and_redirect_never_count_as_our_service(installation):
    root, info = installation
    port = int(info["url"].rsplit(":", 1)[1])
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", info["url"] + "/redirected")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(desktop.LauncherError, match="其他服务占用"):
            desktop.ensure_service(root, desktop.state_directory(root))
        assert requests == ["/api/version"]
        assert not (root / "server.pid").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_manual_service_requires_kernel_lock_and_matching_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with (runtime / "web.lock").open("w") as lock:
        assert not desktop.manual_service_matches(tmp_path, runtime)
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert desktop.manual_service_matches(tmp_path, runtime)
        assert not desktop.manual_service_matches(tmp_path / "another-project", runtime)
    assert not desktop.manual_service_matches(tmp_path, runtime)


def test_info_reads_env_port_and_runtime_from_outside_project_without_activating_conda(tmp_path):
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(SOURCE, root / "scripts/linux_desktop.py")
    shutil.copyfile(SOURCE.parents[1] / "start.sh", root / "start.sh")
    (root / ".env").write_text("CONDA_ENV=does-not-exist\nPORT=8123\nGROMACS_WEB_ROOT=data\nOPENAI_API_KEY=private-test-value\n")
    result = subprocess.run(["bash", str(root / "start.sh"), "--desktop-info"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"url": "http://127.0.0.1:8123", "runtime": str(root / "data")}
    assert "private-test-value" not in result.stdout + result.stderr


def test_install_is_repeatable_and_exec_handles_shell_characters(tmp_path, monkeypatch):
    root = tmp_path / 'a project "$dollar `tick 100% \\ directory'
    root.mkdir()
    apps = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(apps))
    output = tmp_path / "argv.json"
    python = tmp_path / "fake python"
    python.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    python.chmod(0o755)
    which = shutil.which
    monkeypatch.setattr(desktop.shutil, "which", lambda name: str(python) if name == "python3" else None)
    first = desktop.install(root)
    before = first[0].read_text()
    assert desktop.install(root) == first
    assert first[0].read_text() == before
    assert "Terminal=false" in before
    assert "GROMACS_CONSOLE" not in before
    validator = which("desktop-file-validate")
    if validator:
        subprocess.run([validator, str(first[0])], check=True)
    gio = which("gio")
    if not gio:
        pytest.skip("gio is needed to verify native desktop Exec parsing")
    subprocess.run([gio, "launch", str(first[0])], check=True, capture_output=True)
    deadline = time.monotonic() + 3
    while not output.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert json.loads(output.read_text()) == [str(root / "scripts/linux_desktop.py"), "open"]


@pytest.mark.parametrize("host", ["malformed", "https://example.com", "127.0.0.1/../../bad"])
def test_desktop_url_only_uses_local_hosts(tmp_path, monkeypatch, host):
    monkeypatch.setenv("HOST", host)
    with pytest.raises(desktop.LauncherError):
        desktop.launch_info(tmp_path)
