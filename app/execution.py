from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .files import ensure_under_root
from .gromacs import Step, execute_internal_step, utc_now
from .job_store import JobStore

MDRUN_STEP_RE = re.compile(r"\bstep\s+(\d+)(?:.*?\btime\s+([0-9.eE+\-]+))?", re.IGNORECASE)
MDRUN_PERFORMANCE_RE = re.compile(r"\bPerformance:\s*([0-9.eE+\-]+)", re.IGNORECASE)
PROGRESS_UPDATE_INTERVAL_SECONDS = 1.0

MIN_FREE_DISK_BYTES = int(os.environ.get("GROMACS_WEB_MIN_FREE_DISK_BYTES", str(2 * 1024**3)))
TERMINATION_GRACE_SECONDS = 5.0


def command_fingerprint(args: list[str]) -> str:
    payload = json.dumps([str(arg) for arg in args], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_start_ticks(pid: int) -> int | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = stat[stat.rfind(")") + 2 :].split()
        return int(fields[19])
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None


def process_identity(pid: int) -> dict | None:
    proc_root = Path("/proc") / str(pid)
    try:
        raw = (proc_root / "cmdline").read_bytes()
        cwd = (proc_root / "cwd").resolve()
        args = [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
        pgid = os.getpgid(pid)
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    if not args:
        return None
    return {
        "process_pid": pid,
        "process_pgid": pgid,
        "process_started_ticks": process_start_ticks(pid),
        "process_command_fingerprint": command_fingerprint(args),
        "process_args": args,
        "process_cwd": str(cwd),
    }


def wait_process_identity(pid: int, timeout: float = 0.5) -> dict | None:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        identity = process_identity(pid)
        # Installed tool wrappers immediately exec the real binary. Require
        # two samples so their transient shell command is not saved as identity.
        if identity is not None and identity == previous:
            return identity
        previous = identity
        time.sleep(0.01)
    return process_identity(pid)


def identity_matches(meta: dict, workdir: Path) -> bool:
    try:
        pid = int(meta.get("process_pid") or 0)
    except (TypeError, ValueError):
        return False
    if type(meta.get("process_started_ticks")) is not int or not isinstance(meta.get("process_command_fingerprint"), str):
        return False
    try:
        expected_cwd = workdir.resolve()
        if Path(meta.get("process_cwd") or expected_cwd).resolve() != expected_cwd:
            return False
    except (OSError, TypeError, ValueError):
        return False
    identity = process_identity(pid) if pid > 0 else None
    if not identity:
        return False
    return (
        identity["process_pgid"] == meta.get("process_pgid")
        and identity["process_started_ticks"] == meta.get("process_started_ticks")
        and identity["process_command_fingerprint"] == meta.get("process_command_fingerprint")
        and Path(identity["process_cwd"]).resolve() == expected_cwd
    )


def registered_process_identities(meta: dict) -> list[dict]:
    """Read both the legacy single process and every member of a parallel batch."""
    parallel = meta.get("process_identities")
    records = [meta, *(parallel if isinstance(parallel, list) else [])]
    found, identities = set(), []
    for record in records:
        if not isinstance(record, dict) or not record.get("process_pid"):
            continue
        if not isinstance(record.get("process_pid"), int) or not isinstance(record.get("process_command_fingerprint"), (str, type(None))):
            continue
        if record.get("process_started_ticks") is not None and type(record["process_started_ticks"]) is not int:
            continue
        key = (record.get("process_pid"), record.get("process_started_ticks"), record.get("process_command_fingerprint"))
        if key not in found:
            found.add(key)
            fields = ("process_pid", "process_pgid", "process_started_ticks", "process_command_fingerprint", "process_args", "process_cwd")
            identities.append({name: record.get(name) for name in fields})
    return identities


def live_process_identities(meta: dict, workdir: Path) -> list[dict]:
    root, live = workdir.resolve(), []
    for record in registered_process_identities(meta):
        try:
            cwd = Path(record.get("process_cwd") or root).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if cwd.is_relative_to(root) and identity_matches(record, cwd):
            live.append(record)
    return live


def _process_resources(pid: int) -> tuple[int, float]:
    peak_rss_bytes = 0
    cpu_seconds = 0.0
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith(("VmHWM:", "VmRSS:")):
                peak_rss_bytes = max(peak_rss_bytes, int(line.split()[1]) * 1024)
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
        cpu_seconds = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        pass
    return peak_rss_bytes, cpu_seconds


def _mdrun_mdp_path(workdir: Path, args: list[str], commands: list[dict]) -> Path | None:
    if "mdrun" not in args:
        return None
    tpr = None
    if "-s" in args and args.index("-s") + 1 < len(args):
        tpr = args[args.index("-s") + 1]
    elif "-deffnm" in args and args.index("-deffnm") + 1 < len(args):
        tpr = f"{args[args.index('-deffnm') + 1]}.tpr"
    if not tpr:
        return None
    mdp_name = None
    for candidate in commands:
        candidate_args = [str(arg) for arg in candidate.get("args") or []]
        if all(flag in candidate_args for flag in ("grompp", "-o", "-f")):
            if candidate_args[candidate_args.index("-o") + 1] == tpr:
                mdp_name = candidate_args[candidate_args.index("-f") + 1]
    if not mdp_name:
        return None
    return workdir / mdp_name


def _mdrun_mdp_number(workdir: Path, args: list[str], commands: list[dict], key: str) -> float | None:
    path = _mdrun_mdp_path(workdir, args, commands)
    if path is None:
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            clean = line.split(";", 1)[0]
            if clean.strip().lower().startswith(key.lower()) and "=" in clean:
                return float(clean.split("=", 1)[1].strip())
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    return None


def _mdrun_total_steps(workdir: Path, args: list[str], commands: list[dict]) -> int | None:
    value = _mdrun_mdp_number(workdir, args, commands, "nsteps")
    return max(1, int(value)) if value is not None else None


class WorkerEngine:
    def __init__(self, store: JobStore, worker_id: str):
        self.store = store
        self.worker_id = worker_id
        self._progress_samples: dict[tuple[str, int], tuple[float, int]] = {}

    @staticmethod
    def steps_from_commands(commands: list[dict]) -> list[Step]:
        return [
            Step(
                str(command.get("title") or "Recovered step"),
                [str(arg) for arg in command.get("args") or []],
                stdin_text=str(command.get("stdin_text") or "") or None,
                outputs=[str(item) for item in command.get("outputs") or []],
                kind=str(command.get("kind") or "command"),
                operation=command.get("operation"),
                data=dict(command.get("data") or {}),
                parallel_group=str(command.get("parallel_group") or ""),
            )
            for command in commands
        ]

    def _current_step(self, meta: dict) -> dict | None:
        commands = list(meta.get("commands") or [])
        index = self.store.current_command_index(meta)
        if 0 <= index < len(commands):
            return commands[index]
        return None

    @staticmethod
    def _is_mdrun_step(step: dict | None) -> bool:
        return bool(step) and "mdrun" in [str(arg) for arg in (step or {}).get("args") or []]

    def _terminate_leftover(self, meta: dict) -> None:
        """Stop only registered processes/groups whose complete identity still matches."""
        workdir = self.store.job_dir(meta["id"])
        records = registered_process_identities(meta)
        groups = {int(record["process_pgid"]) for record in live_process_identities(meta, workdir)}
        if not groups:
            return
        # Remember descendants before TERM: a parent can exit while a child
        # ignores TERM. Their recorded identities still permit safe escalation.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.getpgid(int(entry.name)) not in groups:
                    continue
            except (ProcessLookupError, PermissionError, OSError):
                continue
            identity = process_identity(int(entry.name))
            if identity and Path(identity["process_cwd"]).resolve().is_relative_to(workdir.resolve()):
                records.append(identity)
        registry = {"process_identities": records}
        records = registered_process_identities(registry)
        persistence_error = None
        try:
            self.store.update(meta["id"], process_identities=records)
        except Exception as error:
            # An unavailable database must not leave our verified subprocesses
            # running merely because the expanded registry could not be saved.
            persistence_error = error
        registry = {"process_identities": records}
        for signum, grace in ((signal.SIGTERM, TERMINATION_GRACE_SECONDS), (signal.SIGKILL, 1.0)):
            signalled = set()
            for record in live_process_identities(registry, workdir):
                group = int(record["process_pgid"])
                if group in signalled or not identity_matches(record, Path(record["process_cwd"])):
                    continue
                try:
                    os.killpg(group, signum)
                except ProcessLookupError:
                    continue
                except OSError as error:
                    raise RuntimeError(f"Unable to stop a verified job process group {group}: {error}") from error
                signalled.add(group)
            deadline = time.monotonic() + grace
            while live_process_identities(registry, workdir) and time.monotonic() < deadline:
                time.sleep(0.05)
            if not live_process_identities(registry, workdir):
                if persistence_error:
                    raise RuntimeError(
                        f"Verified processes stopped, but their identity registry could not be saved: {persistence_error}"
                    ) from persistence_error
                return
        raise RuntimeError("A verified job process is still active after cancellation; its files and resource reservation were retained.")

    def _cancel_job(self, meta: dict) -> None:
        try:
            self._terminate_leftover(self.store.get(meta["id"]))
        except Exception as error:
            status = meta["status"] if meta["status"] in {"completed", "failed", "cancelled"} else "interrupted"
            self.store.update(meta["id"], status=status, desired_action=None, worker_id=None, error=str(error))
            return
        self.store.update(
            meta["id"],
            status="completed" if meta["status"] == "completed" else "cancelled",
            desired_action=None,
            worker_id=None,
            current_step=None,
            cancelled_at=utc_now(),
        )

    def _reinclude_current_step(self, meta: dict) -> tuple[list[dict], int]:
        """Return (commands, step_index) with the interrupted step re-included for a re-run."""
        commands = list(meta.get("commands") or [])
        step_index = int(meta.get("step_index") or 0)
        index = self.store.current_command_index(meta)
        if 0 <= index < len(commands):
            group = commands[index].get("parallel_group")
            while group and index > 0 and commands[index - 1].get("parallel_group") == group:
                index -= 1
                step_index -= 1
            return commands[index:], step_index - 1
        return commands, 0

    def recover_orphaned_jobs(self) -> None:
        for meta in self.store.list():
            if meta.get("status") != "running":
                continue
            if meta.get("desired_action") == "cancel":
                self.store.update(meta["id"], status="interrupted", desired_action="cancel", worker_id=None)
                continue
            workdir = self.store.job_dir(meta["id"])
            step = self._current_step(meta)
            if self._is_mdrun_step(step) and not (step or {}).get("parallel_group"):
                if identity_matches(meta, workdir):
                    self.store.update(
                        meta["id"],
                        status="interrupted",
                        desired_action="adopt",
                        worker_id=None,
                        error="Worker restarted; verified process identity and queued re-adoption.",
                    )
                else:
                    self.store.update(
                        meta["id"],
                        status="interrupted",
                        desired_action=None,
                        worker_id=None,
                        error="Worker restarted and no process with the saved identity was found.",
                    )
            else:
                # Non-mdrun tools (trjconv, rms, ...) overwrite their -o output, so
                # the safe recovery is to stop any leftover process and re-run the step.
                self._terminate_leftover(meta)
                commands, step_index = self._reinclude_current_step(meta)
                self.store.update(
                    meta["id"],
                    status="interrupted",
                    desired_action="run",
                    worker_id=None,
                    commands=commands,
                    step_index=step_index,
                    error="Worker restarted; non-mdrun step will be re-run.",
                )

    def run_once(self) -> bool:
        meta = self.store.claim_next(self.worker_id)
        if meta is None:
            return False
        if meta.get("claimed_action") == "cancel":
            self._cancel_job(meta)
        elif meta.get("claimed_action") == "adopt":
            self._adopt(meta)
        else:
            start_index = int(meta.get("step_index") or 0)
            self._run_job(meta, self.steps_from_commands(meta.get("commands") or []), start_index)
        return True

    def _cancel_requested(self, job_id: str) -> bool:
        try:
            return self.store.get(job_id).get("desired_action") == "cancel"
        except FileNotFoundError:
            return True

    def _refresh_owned_process(self, job_id: str, proc: subprocess.Popen) -> None:
        """Follow an exec only for our own live Popen and its unchanged birth/cwd/group."""
        if proc.poll() is not None:
            return
        current = process_identity(proc.pid)
        if current is None or current.get("process_started_ticks") is None:
            return
        meta = self.store.get(job_id)
        records = registered_process_identities(meta)
        for index, record in enumerate(records):
            same_birth = all(
                current[name] == record.get(name) for name in ("process_pid", "process_pgid", "process_started_ticks", "process_cwd")
            )
            if same_birth and current["process_command_fingerprint"] != record.get("process_command_fingerprint"):
                records[index] = current
                updates = current if meta.get("process_pid") == proc.pid else {}
                self.store.update(job_id, process_identities=records, **updates)
                return

    def _watch_cancel(self, job_id: str, proc: subprocess.Popen, stop: threading.Event) -> None:
        while not stop.wait(0.25) and proc.poll() is None:
            if not self._cancel_requested(job_id):
                continue
            self._refresh_owned_process(job_id, proc)
            meta = self.store.get(job_id)
            try:
                self._terminate_leftover(meta)
            except RuntimeError as error:
                self.store.update(job_id, error=str(error))
                continue
            if proc.poll() is not None:
                return

    def _watch_resources(self, job_id: str, proc: subprocess.Popen, stop: threading.Event, usage: dict) -> None:
        while not stop.wait(0.25) and proc.poll() is None:
            self._refresh_owned_process(job_id, proc)
            peak_rss, cpu_seconds = _process_resources(proc.pid)
            usage["peak_rss_bytes"] = max(int(usage.get("peak_rss_bytes") or 0), peak_rss)
            usage["cpu_seconds"] = max(float(usage.get("cpu_seconds") or 0), cpu_seconds)

    def _record_progress(
        self,
        job_id: str,
        line: str,
        index: int,
        step_count: int,
        total_steps: int | None,
        dt_ps: float | None,
    ) -> None:
        performance_match = MDRUN_PERFORMANCE_RE.search(line)
        if performance_match:
            ns_per_day = float(performance_match.group(1))
            current = self.store.get(job_id)
            simulation = dict(current.get("simulation_progress") or {})
            simulation["ns_per_day"] = round(ns_per_day, 3)
            simulation["eta_seconds"] = 0
            self.store.update(job_id, performance_ns_per_day=round(ns_per_day, 3), simulation_progress=simulation)
            return
        match = MDRUN_STEP_RE.search(line)
        if not match:
            return
        current_step = int(match.group(1))
        now = time.monotonic()
        sample_key = (job_id, index)
        previous = self._progress_samples.get(sample_key)
        if previous and now - previous[0] < PROGRESS_UPDATE_INTERVAL_SECONDS and current_step < (total_steps or current_step + 1):
            return
        fraction = min(1.0, current_step / total_steps) if total_steps else 0.0
        percent = min(99.9, ((index - 1 + fraction) / max(1, step_count)) * 100)
        time_ps = float(match.group(2)) if match.group(2) else (current_step * dt_ps if dt_ps else None)
        simulation = {
            "step": current_step,
            "time_ps": time_ps,
            "total_steps": total_steps,
        }
        ns_per_day = None
        if previous and current_step > previous[1] and now > previous[0] and dt_ps:
            steps_per_second = (current_step - previous[1]) / (now - previous[0])
            ns_per_day = steps_per_second * dt_ps * 86.4
            simulation["ns_per_day"] = round(ns_per_day, 3)
            if total_steps:
                simulation["eta_seconds"] = max(0, round((total_steps - current_step) / steps_per_second))
        self._progress_samples[sample_key] = (now, current_step)
        self.store.update(
            job_id,
            progress_percent=round(percent, 2),
            simulation_progress=simulation,
            **({"performance_ns_per_day": round(ns_per_day, 3)} if ns_per_day is not None else {}),
        )

    @staticmethod
    def _check_disk_space(workdir: Path) -> None:
        free = shutil.disk_usage(workdir).free
        if free < MIN_FREE_DISK_BYTES:
            raise RuntimeError(
                f"Low disk space: {free / 1e9:.1f} GB free (below {MIN_FREE_DISK_BYTES / 1e9:.1f} GB); aborting to avoid filling the disk."
            )

    @staticmethod
    def _batch_steps(steps: list[Step]) -> list[list[Step]]:
        """Group consecutive command steps sharing a parallel_group into batches."""
        batches: list[list[Step]] = []
        index = 0
        while index < len(steps):
            step = steps[index]
            group = step.parallel_group if step.kind == "command" else ""
            if group:
                batch = [step]
                cursor = index + 1
                while cursor < len(steps) and steps[cursor].kind == "command" and steps[cursor].parallel_group == group:
                    batch.append(steps[cursor])
                    cursor += 1
                batches.append(batch)
                index = cursor
            else:
                batches.append([step])
                index += 1
        return batches

    def _run_sequential_step(self, job_id: str, workdir: Path, meta: dict, step: Step, index: int, step_count: int, dry_run: bool) -> bool:
        """Run a single step (command or internal). Returns True to continue, False to stop."""
        step_started_at = utc_now()
        step_started_monotonic = time.monotonic()
        step_runs = list(self.store.get(job_id).get("step_runs") or [])
        step_run = {"index": index, "title": step.title, "started_at": step_started_at, "status": "running"}
        step_runs.append(step_run)
        self.store.update(
            job_id,
            current_step=step.title,
            step_index=index,
            step_runs=step_runs,
            progress_percent=round(((index - 1) / step_count) * 100, 2),
        )
        command_text = " ".join(shlex.quote(arg) for arg in step.args)
        self.store.append_log(job_id, f"\n$ {command_text}\n")
        if step.stdin_text:
            self.store.append_log(job_id, f"[stdin]\n{step.stdin_text}")
        if dry_run:
            self.store.append_log(job_id, "[dry-run] command not executed\n")
            step_run.update(ended_at=utc_now(), duration_seconds=round(time.monotonic() - step_started_monotonic, 3), status="completed")
            self.store.update(job_id, step_runs=step_runs)
            return True
        if step.kind == "internal":
            execute_internal_step(workdir, step)
            self.store.append_log(job_id, f"[internal] {step.operation} completed\n")
            step_run.update(ended_at=utc_now(), duration_seconds=round(time.monotonic() - step_started_monotonic, 3), status="completed")
            self.store.update(job_id, step_runs=step_runs)
            return True
        self._check_disk_space(workdir)
        proc = subprocess.Popen(
            step.args,
            cwd=workdir,
            text=True,
            stdin=subprocess.PIPE if step.stdin_text else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True,
        )
        identity = wait_process_identity(proc.pid)
        if identity is None:
            identity = {
                "process_pid": proc.pid,
                "process_pgid": proc.pid,
                "process_started_ticks": None,
                "process_command_fingerprint": command_fingerprint(step.args),
                "process_args": step.args,
                "process_cwd": str(workdir),
            }
        try:
            self.store.update(job_id, process_started_at=utc_now(), process_identities=[identity], **identity)
        except BaseException:
            try:
                self._terminate_leftover({"id": job_id, "process_identities": [identity]})
            finally:
                proc.wait(timeout=2)
            raise
        stop = threading.Event()
        usage = {"peak_rss_bytes": 0, "cpu_seconds": 0.0}
        watcher = threading.Thread(target=self._watch_cancel, args=(job_id, proc, stop), daemon=True)
        resource_watcher = threading.Thread(target=self._watch_resources, args=(job_id, proc, stop, usage), daemon=True)
        watcher.start()
        resource_watcher.start()
        total_steps = _mdrun_total_steps(workdir, step.args, list(meta.get("commands") or []))
        dt_ps = _mdrun_mdp_number(workdir, step.args, list(meta.get("commands") or []), "dt")
        try:
            if step.stdin_text and proc.stdin:
                proc.stdin.write(step.stdin_text)
                proc.stdin.close()
            assert proc.stdout is not None
            with proc.stdout:
                for line in proc.stdout:
                    self.store.append_log(job_id, line)
                    self._record_progress(job_id, line, index, step_count, total_steps, dt_ps)
            exit_code = proc.wait()
        except BaseException:
            try:
                self._refresh_owned_process(job_id, proc)
                latest = self.store.get(job_id)
                self._terminate_leftover({**latest, "process_identities": [*registered_process_identities(latest), identity]})
            finally:
                proc.wait(timeout=2)
            raise
        finally:
            peak_rss, cpu_seconds = _process_resources(proc.pid)
            usage["peak_rss_bytes"] = max(int(usage["peak_rss_bytes"]), peak_rss)
            usage["cpu_seconds"] = max(float(usage["cpu_seconds"]), cpu_seconds)
            stop.set()
            watcher.join(timeout=1)
            resource_watcher.join(timeout=1)
        step_run.update(
            ended_at=utc_now(),
            duration_seconds=round(time.monotonic() - step_started_monotonic, 3),
            peak_rss_bytes=int(usage["peak_rss_bytes"]),
            cpu_seconds=round(float(usage["cpu_seconds"]), 3),
            exit_code=exit_code,
            status="completed" if exit_code == 0 else "failed",
        )
        self.store.update(job_id, step_runs=step_runs)
        if self._cancel_requested(job_id):
            step_run["status"] = "cancelled"
            self.store.update(job_id, step_runs=step_runs)
            self.store.update(
                job_id,
                status="cancelled",
                desired_action=None,
                worker_id=None,
                current_step=None,
                exit_code=exit_code,
                cancelled_at=utc_now(),
            )
            return False
        if exit_code != 0:
            self.store.update(
                job_id,
                status="failed",
                desired_action=None,
                worker_id=None,
                exit_code=exit_code,
                error=f"{step.title} exited with {exit_code}",
            )
            return False
        return True

    def _run_parallel_batch(self, job_id: str, workdir: Path, steps: list[Step], start_index: int, step_count: int, dry_run: bool) -> bool:
        """Run several independent command steps concurrently. Returns True to continue, False to stop."""
        started_at = utc_now()
        batch_started_monotonic = time.monotonic()
        step_runs = list(self.store.get(job_id).get("step_runs") or [])
        for offset, step in enumerate(steps):
            step_runs.append({"index": start_index + offset, "title": step.title, "started_at": started_at, "status": "running"})
        self.store.update(
            job_id,
            current_step=" + ".join(step.title for step in steps),
            step_index=start_index + len(steps) - 1,
            step_runs=step_runs,
            progress_percent=round(((start_index - 1) / step_count) * 100, 2),
        )
        for step in steps:
            self.store.append_log(job_id, f"\n$ {' '.join(shlex.quote(arg) for arg in step.args)}\n")
            if step.stdin_text:
                self.store.append_log(job_id, f"[stdin]\n{step.stdin_text}")
        if dry_run:
            for run in step_runs[-len(steps) :]:
                run.update(ended_at=utc_now(), duration_seconds=0.0, status="completed")
            self.store.update(job_id, step_runs=step_runs)
            return True

        self._check_disk_space(workdir)
        procs: list[subprocess.Popen] = []
        identities: list[dict] = []
        readers: list[threading.Thread] = []
        stop = threading.Event()
        reader_error = threading.Event()
        cancel_watcher = None
        results: dict[int, dict] = {}

        def drain(proc: subprocess.Popen, offset: int) -> None:
            exit_code = None
            try:
                assert proc.stdout is not None
                with proc.stdout:
                    for line in proc.stdout:
                        self.store.append_log(job_id, line)
                exit_code = proc.wait()
            except Exception as error:
                results[offset] = {"error": str(error)}
                reader_error.set()
            finally:
                peak_rss, cpu_seconds = _process_resources(proc.pid)
                results.setdefault(offset, {}).update(exit_code=exit_code, peak_rss_bytes=int(peak_rss), cpu_seconds=round(cpu_seconds, 3))

        def watch_cancel() -> None:
            while not stop.wait(0.25) and any(reader.is_alive() for reader in readers):
                if self._cancel_requested(job_id):
                    for proc in procs:
                        self._refresh_owned_process(job_id, proc)
                    try:
                        self._terminate_leftover(self.store.get(job_id))
                    except RuntimeError as error:
                        self.store.update(job_id, error=str(error))
                        continue
                    if all(proc.poll() is not None for proc in procs):
                        return

        try:
            for offset, step in enumerate(steps):
                proc = subprocess.Popen(
                    step.args,
                    cwd=workdir,
                    text=True,
                    stdin=subprocess.PIPE if step.stdin_text else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    start_new_session=True,
                )
                procs.append(proc)
                identity = wait_process_identity(proc.pid)
                if identity:
                    identities.append(identity)
                self.store.update(job_id, process_started_at=utc_now(), process_identities=list(identities), **(identity or {}))
                reader = threading.Thread(target=drain, args=(proc, offset), daemon=True)
                readers.append(reader)
                reader.start()
                if step.stdin_text and proc.stdin:
                    proc.stdin.write(step.stdin_text)
                    proc.stdin.close()
            cancel_watcher = threading.Thread(target=watch_cancel, daemon=True)
            cancel_watcher.start()
            for reader in readers:
                while True:
                    reader.join(timeout=0.05)
                    if reader_error.is_set():
                        detail = next((value["error"] for value in results.copy().values() if value.get("error")), "unknown reader error")
                        raise RuntimeError(f"Parallel output reader failed: {detail}")
                    if not reader.is_alive():
                        break
        finally:
            stop.set()
            if cancel_watcher:
                cancel_watcher.join(timeout=TERMINATION_GRACE_SECONDS + 2)
            # Includes partially launched batches and reader/stdin failures.
            # Persisted identities also let a restarted worker stop the group.
            # Keep an independent local registry: a database failure must not
            # skip cleanup of Popen children created by this invocation.
            local = list(identities)
            for proc in procs:
                current = process_identity(proc.pid) if proc.poll() is None else None
                if current and current.get("process_started_ticks") is not None:
                    fields = ("process_pid", "process_pgid", "process_started_ticks", "process_cwd")
                    if any(all(current[name] == record.get(name) for name in fields) for record in identities):
                        local.append(current)
            latest = {"id": job_id, "process_identities": local}
            try:
                for proc in procs:
                    self._refresh_owned_process(job_id, proc)
                saved = self.store.get(job_id)
                latest = {**saved, "process_identities": [*registered_process_identities(saved), *local]}
            except Exception:
                # Local birth/group/cwd checks still establish ownership; the
                # stopper performs the complete identity check before signalling.
                pass
            try:
                self._terminate_leftover(latest)
            finally:
                for proc in procs:
                    proc.wait(timeout=2)
                for reader in readers:
                    reader.join(timeout=2)
                for proc in procs:
                    for stream in (proc.stdin, proc.stdout):
                        if stream is not None and not stream.closed:
                            stream.close()

        cancelled = self._cancel_requested(job_id)
        failed_title = None
        duration = round(time.monotonic() - batch_started_monotonic, 3)
        for offset, step in enumerate(steps):
            run = step_runs[len(step_runs) - len(steps) + offset]
            info = results.get(offset, {})
            if cancelled:
                run.update(status="cancelled", ended_at=utc_now())
            elif info.get("exit_code") == 0:
                run.update(
                    status="completed",
                    ended_at=utc_now(),
                    duration_seconds=duration,
                    peak_rss_bytes=info.get("peak_rss_bytes"),
                    cpu_seconds=info.get("cpu_seconds"),
                    exit_code=0,
                )
            else:
                failed_title = failed_title or step.title
                run.update(status="failed", ended_at=utc_now(), duration_seconds=duration, exit_code=info.get("exit_code"))
        self.store.update(job_id, step_runs=step_runs)
        if cancelled:
            self.store.update(job_id, status="cancelled", desired_action=None, worker_id=None, current_step=None, cancelled_at=utc_now())
            return False
        if failed_title:
            self.store.update(job_id, status="failed", desired_action=None, worker_id=None, exit_code=1, error=f"{failed_title} failed")
            return False
        return True

    def _run_job(self, meta: dict, steps: list[Step], start_index: int) -> None:
        job_id = meta["id"]
        workdir = self.store.job_dir(job_id)
        dry_run = bool(meta.get("dry_run"))
        try:
            self._check_disk_space(workdir)
            self.store.update(job_id, started_at=meta.get("started_at") or utc_now(), error=None)
            flat = list(steps)
            step_count = int(meta.get("step_count") or len(flat) or 1)
            index = start_index + 1
            for batch in self._batch_steps(flat):
                if self._cancel_requested(job_id):
                    self.store.update(
                        job_id, status="cancelled", desired_action=None, worker_id=None, current_step=None, cancelled_at=utc_now()
                    )
                    return
                if len(batch) == 1:
                    if not self._run_sequential_step(job_id, workdir, meta, batch[0], index, step_count, dry_run):
                        return
                    index += 1
                else:
                    if not self._run_parallel_batch(job_id, workdir, batch, index, step_count, dry_run):
                        return
                    index += len(batch)
            completion_updates = {}
            benchmark_path = workdir / "benchmark-results.json"
            if benchmark_path.is_file():
                benchmark_results = json.loads(benchmark_path.read_text(encoding="utf-8"))
                completion_updates["benchmark_results"] = benchmark_results
                best = benchmark_results.get("best") or {}
                if best.get("ns_per_day") is not None:
                    completion_updates["performance_ns_per_day"] = float(best["ns_per_day"])
            self.store.update(
                job_id,
                status="completed",
                desired_action=None,
                worker_id=None,
                exit_code=0,
                current_step=None,
                completed_at=utc_now(),
                progress_percent=100.0,
                **completion_updates,
            )
            self.store.append_log(job_id, "\n[worker] completed\n")
        except Exception as exc:
            message = f"Executable not found: {exc.filename}" if isinstance(exc, FileNotFoundError) else str(exc)
            try:
                self._terminate_leftover(self.store.get(job_id))
            except RuntimeError as cleanup_error:
                message += f"; process cleanup needs attention: {cleanup_error}"
            self.store.update(job_id, status="failed", desired_action=None, worker_id=None, error=message)
            self.store.append_log(job_id, f"\n[worker] error: {exc}\n")

    def _adopt(self, meta: dict) -> None:
        job_id = meta["id"]
        workdir = self.store.job_dir(job_id)
        current = self.store.get(job_id)
        step = self._current_step(current)
        if not self._is_mdrun_step(step) or (step or {}).get("parallel_group"):
            # Non-mdrun steps overwrite their -o output, so re-run them instead of
            # trying to read an exit code from a detached process we are no longer
            # the parent of.
            self._terminate_leftover(current)
            self.store.append_log(job_id, f"\n[worker] re-running interrupted step: {step.get('title') if step else 'unknown'}\n")
            commands, step_index = self._reinclude_current_step(current)
            self._run_job(current, self.steps_from_commands(commands), step_index)
            return
        if not identity_matches(current, workdir):
            self.store.update(
                job_id,
                status="interrupted",
                desired_action=None,
                worker_id=None,
                error="Saved process identity no longer matches a live process.",
            )
            return
        pid = int(current["process_pid"])
        self.store.append_log(job_id, f"\n[worker] re-adopted verified process pid={pid} pgid={current['process_pgid']}\n")
        while identity_matches(self.store.get(job_id), workdir):
            if self._cancel_requested(job_id):
                self._cancel_job(self.store.get(job_id))
                return
            time.sleep(1)
        current = self.store.get(job_id)
        args = [str(arg) for arg in current.get("process_args") or []]
        deffnm = args[args.index("-deffnm") + 1] if "-deffnm" in args and args.index("-deffnm") + 1 < len(args) else None
        gromacs_log = ensure_under_root(workdir / f"{deffnm}.log", workdir) if deffnm else None
        log_text = gromacs_log.read_text(encoding="utf-8", errors="replace") if gromacs_log and gromacs_log.is_file() else ""
        if "Finished mdrun" not in log_text:
            self.store.update(
                job_id,
                status="interrupted",
                desired_action=None,
                worker_id=None,
                current_step=None,
                error="The adopted process exited without a normal GROMACS completion marker.",
            )
            return
        commands = list(current.get("commands") or [])
        completed_index = int(current.get("step_index") or 0)
        remaining = self.steps_from_commands(commands[self.store.current_command_index(current) + 1 :])
        if remaining:
            self._run_job(current, remaining, completed_index)
        else:
            self.store.update(job_id, status="completed", desired_action=None, worker_id=None, current_step=None, completed_at=utc_now())
