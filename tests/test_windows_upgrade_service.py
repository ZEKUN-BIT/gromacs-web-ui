"""Run real disposable Web/worker processes across an application promotion.

The preceding application is deliberately small; the promoted application uses
the real packaged sources and APIs. All listeners use ephemeral ports, and all
settings, runtime files and process records live under pytest's temporary root.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sqlite3
import sys
import tarfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="WSL process identities require Linux /proc")


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging/windows" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load("upgrade_test_service", "desktop_service.py")
updater = load("upgrade_test_updater", "application_update.py")
builder = load("upgrade_test_builder", "build.py")


def request(url: str, path: str = "/", *, as_json: bool = False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + path, timeout=5) as response:
        return json.load(response) if as_json else response.read().decode()


@pytest.fixture
def installed_service(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    (root / "app").mkdir(parents=True)
    (root / "app/__init__.py").write_text("")
    (root / "app/main.py").write_text(
        """
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
app = FastAPI()
@app.get('/')
def index():
    return HTMLResponse('<h1>Previous application</h1>')
@app.get('/api/version')
def version():
    return {'version': '0.1.0', 'instance_id': os.environ.get('GROMACS_CONSOLE_INSTANCE_ID', ''),
            'installation_sha256': os.environ.get('GROMACS_CONSOLE_INSTALLATION_SHA256', '')}
""",
        encoding="utf-8",
    )
    (root / "app/worker.py").write_text("import time\nwhile True: time.sleep(1)\n")
    (root / "app/obsolete-file.txt").write_text("old application file")
    (root / "runtime").mkdir()
    (root / "runtime/retained.cpt").write_bytes(b"synthetic checkpoint retained across upgrade")
    (root / "settings.json").write_text('{"max_parallel":1}', encoding="utf-8")
    (root / "installed-release.json").write_text(json.dumps({"version": "0.1.0", "installation_sha256": "old-build"}))
    shutil.copyfile(ROOT / "packaging/windows/desktop_service.py", root / "desktop_service.py")
    (root / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)

    def ephemeral_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    monkeypatch.setattr(service, "free_port", ephemeral_port)
    yield root
    database = root / "runtime/jobs.sqlite3"
    if database.is_file():
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM jobs")
    service.manage(root, "stop")


def test_running_upgrade_exposes_product_api_and_preserves_runtime(installed_service, tmp_path):
    root = installed_service
    old = service.manage(root, "start")
    old_state = json.loads((root / "service.json").read_text())
    assert "Previous application" in request(old["url"])
    with pytest.raises(urllib.error.HTTPError) as missing:
        request(old["url"], "/api/product")
    assert missing.value.code == 404

    payload = tmp_path / "payload"
    payload.mkdir()
    builder.write_payload(ROOT, payload / "application.tar.gz")
    release = {"version": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], "installation_sha256": "new-build"}
    (payload / "release.json").write_text(json.dumps(release))
    shutil.copyfile(ROOT / "packaging/windows/desktop_service.py", payload / "desktop_service.py")
    stage = root / "build.upgrade-test"
    stage.mkdir()
    with tarfile.open(payload / "application.tar.gz") as archive:
        archive.extractall(stage, filter="data")
    updater.prepare(root, stage, payload)
    service.manage(root, "stop")
    assert all(not service.matches(record) for record in old_state["processes"])
    updater.promote(root, stage)

    current = service.manage(root, "start")
    assert current["version"] == release["version"]
    assert current["installation_sha256"] == "new-build"
    assert current["instance_id"] != old["instance_id"]
    identity = request(current["url"], "/api/version", as_json=True)
    assert identity == {key: current[key] for key in ("version", "instance_id", "installation_sha256")}
    assert request(current["url"], "/api/product", as_json=True)["schema_version"] == 1
    page = request(current["url"])
    assert all(text in page for text in ("协议模板", "引导视图", "专业视图", "运行教学示例", "/static/examples/index.html"))
    assert "教学" in request(current["url"], "/static/examples/index.html")
    assert "bindWorkspace" in request(current["url"], "/static/js/workspace.js")
    state = (root / "service.json").read_text()
    assert service.manage(root, "start") == current
    assert (root / "service.json").read_text() == state
    assert (root / "runtime/retained.cpt").read_bytes() == b"synthetic checkpoint retained across upgrade"
    assert (root / "settings.json").read_text() == '{"max_parallel":1}'
    assert (root / ".venv").is_symlink()
    assert not (root / "app/obsolete-file.txt").exists()


@pytest.mark.parametrize("stale", ["missing-identity", "wrong-identity", "new-build"])
def test_start_replaces_stale_idle_service_only_after_identity_check(installed_service, stale):
    root = installed_service
    old = service.manage(root, "start")
    state = json.loads((root / "service.json").read_text())
    if stale == "missing-identity":
        state.pop("instance_id")
    elif stale == "wrong-identity":
        state["instance_id"] = "different-instance"
    else:
        (root / "installed-release.json").write_text(json.dumps({"version": "0.1.0", "installation_sha256": "replacement-build"}))
    (root / "service.json").write_text(json.dumps(state))
    current = service.manage(root, "start")
    assert current["instance_id"] != old["instance_id"]
    assert all(not service.matches(record) for record in state["processes"])
    if stale == "new-build":
        assert current["installation_sha256"] == "replacement-build"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_mismatched_instance_never_restarts_service_with_active_jobs(installed_service, status):
    root = installed_service
    original = service.manage(root, "start")
    state = json.loads((root / "service.json").read_text())
    state["instance_id"] = "stale-instance"
    (root / "service.json").write_text(json.dumps(state))
    with sqlite3.connect(root / "runtime/jobs.sqlite3") as connection:
        connection.execute("CREATE TABLE jobs(id TEXT, status TEXT, desired_action TEXT, meta_json TEXT)")
        connection.execute("INSERT INTO jobs VALUES ('synthetic-job', ?, NULL, '{}')", (status,))
    with pytest.raises(RuntimeError, match="Jobs are still"):
        service.manage(root, "start")
    assert all(service.matches(record) for record in state["processes"])
    assert request(original["url"], "/api/version", as_json=True)["instance_id"] == original["instance_id"]
    assert json.loads((root / "service.json").read_text()) == state


@pytest.mark.parametrize("mode", ["slow-default", "slow-deadline", "oversized", "maximum-size"])
def test_version_probe_bounds_response_time_and_bytes(mode):
    identity = {"version": "test-version", "instance_id": "test-instance", "installation_sha256": "test-installation"}
    body = json.dumps(identity).encode()
    if mode == "oversized":
        body += b" " * 16385
    elif mode == "maximum-size":
        body += b" " * (16384 - len(body))
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if mode.startswith("slow"):
                    for offset in range(0, len(body), 15):
                        if stopped.wait(0.25):
                            return
                        self.wfile.write(body[offset : offset + 15])
                        self.wfile.flush()
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        release = {key: identity[key] for key in ("version", "installation_sha256")}
        if mode.startswith("slow"):
            limit = 0.35 if mode == "slow-deadline" else 1.0
            started = time.monotonic()
            kwargs = {"deadline": started + limit} if mode == "slow-deadline" else {}
            with pytest.raises(TimeoutError, match="timed out"):
                service.verify_service(url, "test-instance", release, **kwargs)
            assert time.monotonic() - started < limit + 0.8, "Trickled bytes must not extend the total verification deadline"
        elif mode == "oversized":
            with pytest.raises(ValueError, match="exceeds 16 KiB"):
                service.verify_service(url, "test-instance", release)
        else:
            assert service.verify_service(url, "test-instance", release) == {"url": url, **identity}
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
