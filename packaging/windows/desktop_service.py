"""Manage only this installation's Web and worker processes on Linux/WSL2."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import runpy
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG_LIMIT = 10 * 1024**2


class ServiceLogHandler(logging.handlers.RotatingFileHandler):
    """Never report a log I/O failure recursively through redirected stderr."""

    reported_error = False

    def handleError(self, record: logging.LogRecord) -> None:
        if self.reported_error:
            return
        self.reported_error = True
        try:
            sys.__stderr__.write(f"Service log write failed; check disk space and permissions: {str(sys.exc_info()[1])[:500]}\n")
            sys.__stderr__.flush()
        except (OSError, ValueError, AttributeError):
            pass


class ServiceLogStream:
    """Bound stdout/stderr throughout a component's lifetime, using separate files."""

    encoding = "utf-8"

    def __init__(self, path: Path, max_bytes: int = LOG_LIMIT):
        self.handler = ServiceLogHandler(path, maxBytes=max_bytes, backupCount=1, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, value: str) -> int:
        for offset in range(0, len(value), 4096):
            self.handler.handle(logging.LogRecord("service", logging.INFO, "", 0, value[offset : offset + 4096].rstrip("\n"), (), None))
        return len(value)

    def flush(self) -> None:
        try:
            self.handler.flush()
        except OSError:
            self.handler.handleError(logging.LogRecord("service", logging.ERROR, "", 0, "Log flush failed", (), None))

    def isatty(self) -> bool:
        return False


def run_component(root: Path, component: str) -> None:
    # Each component owns its own rotating log; concurrent processes never
    # rename one another's open log file. No extra supervising process is needed.
    log = ServiceLogStream(root / ("service.log" if component == "run-web" else "worker-service.log"))
    sys.stdout = sys.stderr = log
    try:
        if component == "run-web":
            import uvicorn

            uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ["PORT"]), timeout_graceful_shutdown=5, access_log=False)
        else:
            previous_args = sys.argv
            try:
                sys.argv = ["gromacs-console-worker"]
                runpy.run_module("app.worker", run_name="__main__")
            finally:
                sys.argv = previous_args
    finally:
        log.flush()


def process_identity(pid: int) -> dict | None:
    try:
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        command = (proc / "cmdline").read_bytes()
        return {"pid": pid, "ticks": int(fields[19]), "command": hashlib.sha256(command).hexdigest()}
    except (OSError, ValueError, IndexError):
        return None


def matches(record: dict) -> bool:
    return process_identity(int(record["pid"])) == record


def job_process_matches(record: dict, workdir: Path) -> bool:
    """Check execution.py identities without requiring installed app code or a venv."""
    pid = record.get("process_pid")
    if type(pid) is not int or pid <= 0 or type(record.get("process_started_ticks")) is not int:
        return False
    if type(record.get("process_pgid")) is not int or not isinstance(record.get("process_command_fingerprint"), str):
        return False
    saved_cwd = record.get("process_cwd") or str(workdir)
    if not isinstance(saved_cwd, str):
        return False
    try:
        cwd = Path(saved_cwd).resolve()
        if not cwd.is_relative_to(workdir):
            return False
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" or int(fields[19]) != record["process_started_ticks"]:
            return False
        args = [item.decode("utf-8", errors="replace") for item in (proc / "cmdline").read_bytes().split(b"\0") if item]
        fingerprint = hashlib.sha256(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        return (
            bool(args)
            and fingerprint == record["process_command_fingerprint"]
            and os.getpgid(pid) == record["process_pgid"]
            and (proc / "cwd").resolve() == cwd
        )
    except (OSError, ValueError, IndexError):
        return False


def active_jobs(root: Path) -> bool:
    database = root / "runtime" / "jobs.sqlite3"
    if not database.exists():
        return False
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        if connection.execute(
            "SELECT 1 FROM jobs WHERE status IN ('preparing','queued','running') "
            "OR (status = 'interrupted' AND desired_action IN ('run','resume','adopt','cancel')) LIMIT 1"
        ).fetchone():
            return True
        jobs_root = (root / "runtime" / "jobs").resolve()
        if not jobs_root.is_relative_to(root.resolve()):
            return False
        rows = connection.execute(
            "SELECT id,meta_json FROM jobs WHERE json_extract(meta_json,'$.process_pid') IS NOT NULL "
            "OR json_array_length(json_extract(meta_json,'$.process_identities')) > 0"
        )
        for job_id, raw in rows:
            if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
                continue
            workdir = (jobs_root / job_id).resolve()
            if not workdir.is_relative_to(jobs_root):
                continue
            meta = json.loads(raw)
            if not isinstance(meta, dict):
                raise ValueError("Invalid job process metadata; inspect the jobs database before stopping/updating.")
            registry = meta.get("process_identities")
            records = [meta, *(registry if isinstance(registry, list) else [])]
            if any(isinstance(record, dict) and job_process_matches(record, workdir) for record in records):
                return True
        return False


def free_port() -> int:
    for port in range(8000, 8100):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("No free port between 8000 and 8099.")


def terminate(record: dict) -> None:
    if not matches(record):
        return
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while matches(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if matches(record):
        os.kill(record["pid"], signal.SIGKILL)


def stop(root: Path, state: dict) -> None:
    if active_jobs(root):
        raise RuntimeError(
            "Jobs are still preparing, queued, running, waiting to resume/adopt/cancel, or have verified live processes. "
            "Finish or cancel them in the UI and wait for their processes to stop before stopping/updating."
        )
    # Stop the Web process before the worker; never signal simulation process groups.
    for record in state.get("processes", []):
        terminate(record)
    (root / "service.json").unlink(missing_ok=True)


def service_environment(root: Path, port: int) -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "GMX_BIN": str(root / "gromacs" / "bin" / "gmx"),
        "GROMACS_WEB_ROOT": str(root / "runtime"),
        "WEB_CONCURRENCY": "1",
        "HOST": "127.0.0.1",
        "PORT": str(port),
    }
    # Both Web diagnostics and worker subprocesses must find the same tools.
    paths = [root / ".venv" / "bin", root / "tools" / "bin", root / "gromacs" / "bin", Path("/usr/lib/wsl/lib")]
    env["PATH"] = os.pathsep.join([*(str(path) for path in paths if path.is_dir()), env.get("PATH", os.defpath)])
    if (root / "tools" / "bin" / "antechamber").is_file():
        env["AMBERHOME"] = str(root / "tools")
    # Do not put tools/lib on the global library path: its bundled libraries
    # can shadow the system libraries used by GROMACS and Python.
    return env


def start(root: Path, state: dict) -> dict:
    processes = state.get("processes", [])
    if len(processes) == 2 and all(matches(record) for record in processes):
        return {"url": state["url"]}
    if any(matches(record) for record in processes):
        stop(root, state)
    # Ensure another run.sh instance cannot be mistaken for this managed service.
    for name in ("web.lock", "worker.lock"):
        lock = root / "runtime" / name
        if lock.exists():
            with lock.open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("An unmanaged service is already running. Stop it first.") from exc
    port = free_port()
    python = str(root / ".venv" / "bin" / "python")
    env = service_environment(root, port)
    # Also migrate a large log written by an older launcher before first startup.
    log_path = root / "service.log"
    if log_path.exists() and log_path.stat().st_size > LOG_LIMIT:
        log_path.replace(root / "service.previous.log")
    children: list[subprocess.Popen] = []
    records = []
    try:
        with (root / "service-bootstrap.log").open("wb") as log:
            for args in (
                [python, str(root / "desktop_service.py"), "run-web"],
                [python, str(root / "desktop_service.py"), "run-worker"],
            ):
                child = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                children.append(child)
                # Popen has completed exec; record the Linux identity before returning.
                record = process_identity(child.pid)
                if record is None:
                    raise RuntimeError(f"Service exited during startup. Check {log_path}.")
                records.append(record)
        url = f"http://127.0.0.1:{port}"
        # Do not report success until this Web process and worker are alive and HTTP is ready.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"Service exited during startup. Check {log_path}.")
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        else:
            raise RuntimeError(f"Service startup timed out. Check {log_path}.")
        new_state = {"url": url, "processes": records}
        temporary = root / "service.json.tmp"
        temporary.write_text(json.dumps(new_state))
        temporary.replace(root / "service.json")
        return {"url": url}
    except BaseException:
        for record in records:
            terminate(record)
        raise


def manage(root: Path, action: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "service.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state_path = root / "service.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if action == "start":
            return start(root, state)
        stop(root, state)
        if action == "remove":
            # Explicit user confirmation is performed by the Windows launcher.
            # Preserve runtime/, settings.json and system/WSL dependencies.
            for name in ("app", "mdp", "scripts", "gromacs", "tools", ".venv"):
                path = root / name
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
            (root / "installed-release.json").unlink(missing_ok=True)
        return {"status": "stopped"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "remove", "run-web", "run-worker"))
    parser.add_argument(
        "--app-root", type=Path, default=Path(__file__).resolve().parent, help="Application directory for installation-time recovery."
    )
    args = parser.parse_args()
    root = args.app_root.resolve()
    try:
        if args.action in {"run-web", "run-worker"}:
            run_component(root, args.action)
        else:
            print(json.dumps(manage(root, args.action)))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
