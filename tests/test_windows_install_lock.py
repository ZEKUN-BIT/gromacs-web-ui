"""Exercise the actual Linux installation lock without touching a real app home."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")

SCRIPT = Path(__file__).resolve().parents[1] / "packaging/windows/linux-install.sh"
FLOCK = shutil.which("flock")
pytestmark = pytest.mark.skipif(FLOCK is None or sys.platform != "linux", reason="Requires Linux flock")

MOCK_PYTHON = r"""#!PYTHON
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
if args and args[0].endswith('gpu_setup.py'):
    prefix = pathlib.Path(args[args.index('--prefix') + 1])
    try:
        os.fstat(9)
        inherited = True
    except OSError:
        inherited = False
    with open(os.environ['TEST_GPU_STARTED'], 'a') as event:
        event.write(json.dumps({'inherited_fd9': inherited}) + '\n')
    if os.environ.get('TEST_BACKGROUND_PID'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
            close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pathlib.Path(os.environ['TEST_BACKGROUND_PID']).write_text(str(child.pid))
    gate = os.environ.get('TEST_GPU_GATE')
    deadline = time.monotonic() + 8
    while gate and not pathlib.Path(gate).exists():
        if time.monotonic() > deadline:
            raise SystemExit(98)
        time.sleep(0.01)
    if os.environ.get('TEST_GPU_FAILURE'):
        raise SystemExit(int(os.environ['TEST_GPU_FAILURE']))
    (prefix / 'bin').mkdir(parents=True, exist_ok=True)
    (prefix / 'share/gromacs/top/amber19sb.ff').mkdir(parents=True, exist_ok=True)
    gmx = prefix / 'bin/gmx'
    gmx.write_text('#!/bin/sh\necho "GROMACS version: 2026.3"\necho "Precision: mixed"\n'.replace('\\n', '\n'))
    gmx.chmod(0o755)
elif args and args[0].endswith(('application_update.py', 'desktop_service.py')):
    os.execv(sys.executable, [sys.executable, *args])
elif args[:2] == ['-m', 'venv']:
    target = pathlib.Path(args[2]) / 'bin/python'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('#!/bin/sh\nexit 0\n'.replace('\\n', '\n'))
    target.chmod(0o755)
else:
    raise SystemExit('Unexpected installer command: ' + repr(args))
"""


def executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path):
    home, payload, commands = tmp_path / "fake home", tmp_path / "payload", tmp_path / "commands"
    for directory in (home, payload, commands):
        directory.mkdir()
    app_root = home / ".local/share/gromacs-console"
    app_root.mkdir(parents=True)
    files = {
        "app/test.py": b"# mock app\n",
        "mdp/test.mdp": b"; mock mdp\n",
        "scripts/test.py": b"# mock script\n",
        "requirements.txt": b"# no dependencies\n",
        "run.sh": b"#!/bin/sh\nexit 0\n",
        "LICENSE": b"test license\n",
        "THIRD_PARTY_NOTICES.md": b"test notices\n",
    }
    archive = payload / "application.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    (payload / "application.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  application.tar.gz\n")
    shutil.copyfile(SCRIPT.with_name("desktop_service.py"), payload / "desktop_service.py")
    (payload / "release.json").write_text('{"version":"test"}\n')
    shutil.copyfile(SCRIPT.with_name("application_update.py"), payload / "application_update.py")
    executable(commands / "id", "#!/bin/sh\nprintf 'gromacs-console\\n'\n")
    executable(commands / "wslpath", '#!/bin/sh\nprintf "%s\\n" "$2"\n')
    executable(commands / "python3", MOCK_PYTHON.replace("#!PYTHON", "#!" + sys.executable))
    # This shim only shortens the fixed production 60-second wait in the timeout
    # test; acquisition and contention still use the real util-linux flock.
    executable(
        commands / "flock",
        "#!"
        + sys.executable
        + "\n"
        + "import json,os,sys\nargs=sys.argv[1:]\n"
        + "if '--timeout' in args and os.environ.get('TEST_SHORT_LOCK_WAIT'):\n"
        + "    open(os.environ['TEST_FLOCK_ARGUMENTS'],'w').write(json.dumps(args))\n"
        + "    args[args.index('--timeout')+1]='0.15'\n"
        + f"os.execv({FLOCK!r},[{FLOCK!r}]+args)\n",
    )
    env = dict(
        os.environ,
        HOME=str(home),
        PATH=str(commands) + os.pathsep + os.environ["PATH"],
        PAYLOAD_WINDOWS_B64=base64.b64encode(str(payload).encode()).decode(),
        TEST_GPU_STARTED=str(tmp_path / "started"),
    )
    return app_root, env


def launch(env: dict) -> subprocess.Popen:
    return subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"Installer did not create {path.name}"
        time.sleep(0.01)


def waiting_notice(process: subprocess.Popen) -> str:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5), "Installer did not announce lock contention"
    notice = process.stdout.readline()
    assert "Waiting up to 60 seconds" in notice
    return notice


def finish(process: subprocess.Popen) -> str:
    output, _ = process.communicate(timeout=8)
    assert process.returncode == 0, output
    return output


def cleanup(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        process.communicate(timeout=8)


def test_concurrent_installers_wait_without_entering_the_installation_body(installation, tmp_path):
    app_root, env = installation
    release = tmp_path / "release-first"
    first = launch(dict(env, TEST_GPU_GATE=str(release)))
    second = None
    try:
        wait_for_file(Path(env["TEST_GPU_STARTED"]))
        second_marker = tmp_path / "second-started"
        second = launch(dict(env, TEST_GPU_STARTED=str(second_marker)))
        waiting_notice(second)
        assert not second_marker.exists()
        assert not list(app_root.glob("installed-release.json"))
        release.touch()
        finish(first)
        finish(second)
        assert second_marker.exists()
    finally:
        release.touch()
        cleanup(first)
        if second is not None:
            cleanup(second)


@pytest.mark.parametrize("status,action", [("running", "run"), ("interrupted", "cancel")])
def test_full_repair_refuses_active_jobs_even_when_virtual_environment_is_missing(installation, status, action):
    app_root, env = installation
    (app_root / "runtime").mkdir()
    with sqlite3.connect(app_root / "runtime/jobs.sqlite3") as database:
        database.execute("CREATE TABLE jobs(status TEXT, desired_action TEXT)")
        database.execute("INSERT INTO jobs VALUES (?, ?)", (status, action))
    process = launch(env)
    output, _ = process.communicate(timeout=8)
    assert process.returncode == 1, output
    assert "Jobs are still" in output
    assert not Path(env["TEST_GPU_STARTED"]).exists()
    assert not (app_root / "installed-release.json").exists()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_full_repair_refuses_terminal_jobs_with_verified_live_members(installation, status):
    from app.execution import wait_process_identity

    app_root, env = installation
    workdir = app_root / "runtime/jobs/isolated-job"
    workdir.mkdir(parents=True)
    checkpoint = workdir / "simulation.cpt"
    checkpoint.write_bytes(b"preserve checkpoint")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=workdir, start_new_session=True)
    try:
        identity = wait_process_identity(child.pid)
        assert identity is not None
        with sqlite3.connect(app_root / "runtime/jobs.sqlite3") as database:
            database.execute("CREATE TABLE jobs(id TEXT, status TEXT, desired_action TEXT, meta_json TEXT)")
            database.execute(
                "INSERT INTO jobs VALUES (?, ?, NULL, ?)",
                (workdir.name, status, json.dumps({"process_pid": 99999999, "process_identities": [identity]})),
            )
        process = launch(env)
        output, _ = process.communicate(timeout=8)
        assert process.returncode == 1, output
        assert "verified live processes" in output
        assert not Path(env["TEST_GPU_STARTED"]).exists()
        assert not (app_root / "installed-release.json").exists()
        assert not (app_root / "app").exists()
        assert not (app_root / ".venv").exists()
        assert checkpoint.read_bytes() == b"preserve checkpoint"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_application_only_update_skips_gpu_and_dependency_setup(installation):
    app_root, env = installation
    finish(launch(env))
    events = Path(env["TEST_GPU_STARTED"]).read_text()
    python = app_root / ".venv/bin/python"
    before = python.stat().st_mtime_ns
    output = finish(launch(dict(env, APP_ONLY="1")))
    assert "Application-only update" in output
    assert Path(env["TEST_GPU_STARTED"]).read_text() == events
    assert python.stat().st_mtime_ns == before


def test_application_only_missing_environment_requests_full_repair_without_mutation(installation):
    app_root, env = installation
    process = launch(dict(env, APP_ONLY="1"))
    output, _ = process.communicate(timeout=8)
    assert process.returncode == 76, output
    assert not Path(env["TEST_GPU_STARTED"]).exists()
    assert not (app_root / "installed-release.json").exists()


def test_temporary_external_holder_releases_lock_and_waiting_install_succeeds(installation):
    app_root, env = installation
    with (app_root / "install.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        process = launch(env)
        try:
            waiting_notice(process)
            assert not Path(env["TEST_GPU_STARTED"]).exists()
            fcntl.flock(holder, fcntl.LOCK_UN)
            assert "Installation complete" in finish(process)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            cleanup(process)


def test_busy_timeout_keeps_the_same_inode_and_existing_environment(installation, tmp_path):
    app_root, env = installation
    lock = app_root / "install.lock"
    lock.write_text("existing lock file content\n")
    inode = lock.stat().st_ino
    previous = app_root / "previous-environment"
    previous.write_text("preserve existing data")
    recorded = tmp_path / "flock-arguments.json"
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        process = launch(dict(env, TEST_SHORT_LOCK_WAIT="1", TEST_FLOCK_ARGUMENTS=str(recorded)))
        try:
            output, _ = process.communicate(timeout=5)
            assert process.returncode == 75, output
            assert "Waiting up to 60 seconds" in output
            assert "still busy after 60 seconds (exit 75)" in output
            assert not Path(env["TEST_GPU_STARTED"]).exists()
            assert not list(app_root.glob("build.*"))
            assert lock.stat().st_ino == inode
            assert lock.read_text() == "existing lock file content\n"
            assert previous.read_text() == "preserve existing data"
            arguments = json.loads(recorded.read_text())
            assert arguments[arguments.index("--timeout") + 1] == "60"
            assert subprocess.run([FLOCK, "--nonblock", str(lock), "true"]).returncode != 0
        finally:
            cleanup(process)


@pytest.mark.parametrize("first_failure", [None, "99"])
def test_exit_releases_lock_even_while_a_background_child_is_still_alive(installation, tmp_path, first_failure):
    app_root, env = installation
    pid_file = tmp_path / "owned-background.pid"
    first_env = dict(env, TEST_BACKGROUND_PID=str(pid_file))
    if first_failure:
        first_env["TEST_GPU_FAILURE"] = first_failure
    process = launch(first_env)
    child_pid = None
    try:
        output, _ = process.communicate(timeout=8)
        assert process.returncode == (int(first_failure) if first_failure else 0), output
        child_pid = int(pid_file.read_text())
        os.kill(child_pid, 0)  # Our mock's background child remains alive.
        event = json.loads(Path(env["TEST_GPU_STARTED"]).read_text().splitlines()[0])
        assert event["inherited_fd9"] is False
        assert subprocess.run([FLOCK, "--nonblock", str(app_root / "install.lock"), "true"]).returncode == 0
        retry = launch(dict(env, TEST_GPU_STARTED=str(tmp_path / "retry-started")))
        try:
            assert "Installation complete" in finish(retry)
        finally:
            cleanup(retry)
    finally:
        cleanup(process)
        if child_pid is not None:
            # This PID belongs to the background child created by this test.
            os.kill(child_pid, signal.SIGTERM)
