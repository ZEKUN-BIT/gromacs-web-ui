from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .files import ensure_under_root
from .gromacs import Step, directory_slug, read_json, utc_now, write_json

JOB_STATES = {"preparing", "queued", "running", "completed", "failed", "cancelled", "interrupted"}

MAX_LOG_FILE_BYTES = int(os.environ.get("GROMACS_WEB_MAX_LOG_BYTES", str(100 * 1024**2)))


class JobStore:
    """Transactional job metadata store shared by the Web and worker processes."""

    def __init__(self, runtime_root: Path, max_parallel: int = 1):
        self.runtime_root = runtime_root
        self.jobs_root = runtime_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.db_path = runtime_root / "jobs.sqlite3"
        self._log_lock = threading.Lock()
        self._max_parallel = max(1, int(max_parallel))
        self._initialize_database()
        self._migrate_json_jobs()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN (
                        'preparing','queued','running','completed','failed','cancelled','interrupted'
                    )),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL CHECK (json_valid(meta_json)),
                    desired_action TEXT CHECK (desired_action IN ('run','cancel','adopt','resume') OR desired_action IS NULL),
                    worker_id TEXT,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status, created_at);
                CREATE TRIGGER IF NOT EXISTS jobs_status_transition
                BEFORE UPDATE OF status ON jobs
                WHEN OLD.status <> NEW.status AND NOT (
                    (OLD.status = 'preparing' AND NEW.status IN ('queued','failed','cancelled')) OR
                    (OLD.status = 'queued' AND NEW.status IN ('running','cancelled','interrupted')) OR
                    (OLD.status = 'running' AND NEW.status IN ('completed','failed','cancelled','interrupted')) OR
                    (OLD.status = 'interrupted' AND NEW.status IN ('queued','running','cancelled')) OR
                    (OLD.status = 'failed' AND NEW.status IN ('queued','cancelled'))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid job status transition');
                END;
                """
            )

    def _migrate_json_jobs(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for meta_path in self.jobs_root.glob("*/metadata.json"):
                    try:
                        meta = read_json(meta_path, {})
                    except (json.JSONDecodeError, OSError):
                        continue
                    if not meta or meta.get("status") not in JOB_STATES:
                        continue
                    meta.setdefault("directory_name", meta_path.parent.name)
                    now = str(meta.get("updated_at") or meta.get("created_at") or utc_now())
                    connection.execute(
                        "INSERT OR IGNORE INTO jobs(id,status,created_at,updated_at,meta_json) VALUES(?,?,?,?,?)",
                        (meta["id"], meta["status"], str(meta.get("created_at") or now), now, json.dumps(meta, ensure_ascii=False)),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def configure(self, max_parallel: int | None = None) -> None:
        if max_parallel is not None:
            self._max_parallel = max(1, int(max_parallel))

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def job_dir(self, job_id: str) -> Path:
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise FileNotFoundError(job_id)
        return ensure_under_root(self.jobs_root / job_id, self.jobs_root)

    def meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "metadata.json"

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "run.log"

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        meta = json.loads(row["meta_json"])
        meta["status"] = row["status"]
        meta["updated_at"] = row["updated_at"]
        if row["desired_action"]:
            meta["desired_action"] = row["desired_action"]
        else:
            meta.pop("desired_action", None)
        if row["worker_id"]:
            meta["worker_id"] = row["worker_id"]
        return meta

    def get(self, job_id: str) -> dict:
        self.job_dir(job_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(job_id)
        meta = self._decode(row)
        meta["skip_step_block_reason"] = self.skip_step_block_reason(meta)
        meta["can_skip_failed_step"] = meta["skip_step_block_reason"] is None
        commands = meta.get("commands") or []
        index = self.current_command_index(meta)
        current = commands[index] if 0 <= index < len(commands) else {}
        args = current.get("args") or []
        meta["can_resume_checkpoint"] = (
            meta["status"] in {"failed", "interrupted"}
            and current.get("kind", "command") == "command"
            and "mdrun" in args
            and "-cpi" in args
        )
        return meta

    @staticmethod
    def current_command_index(meta: dict) -> int:
        commands = meta.get("commands") or []
        offset = max(0, int(meta.get("step_count") or len(commands)) - len(commands))
        return int(meta.get("step_index") or 0) - offset - 1

    @staticmethod
    def skip_step_block_reason(meta: dict) -> str | None:
        if meta.get("status") != "failed":
            return "Only a failed job can skip a step."
        commands = meta.get("commands") or []
        index = JobStore.current_command_index(meta)
        if not 0 <= index < len(commands):
            return "The failed step is unavailable; cannot skip."
        current = commands[index]
        if current.get("parallel_group"):
            return "Parallel analysis must be retried as a group; cannot skip a single step."
        args = current.get("args") or []
        if current.get("kind", "command") == "internal":
            optional = current.get("operation") in {"remove_files", "generate_analysis_figures"}
        else:
            optional = len(args) > 1 and args[1] in {"energy", "rms", "rmsf", "gyrate", "sasa", "hbond", "hbond-legacy", "dssp"}
        if not optional:
            return "Simulation preparation and quality checks cannot be skipped. Correct the cause and retry."

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)

        outputs = set(current.get("outputs") or [])
        for following in commands[index + 1 :]:
            if outputs.intersection(strings([following.get("args", []), following.get("data", {})])):
                return "A later step requires these outputs; correct the cause and retry."
        return None

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def summarize(meta: dict) -> dict:
        fields = (
            "id",
            "name",
            "status",
            "created_at",
            "updated_at",
            "workflow",
            "directory_name",
            "current_step",
            "step_index",
            "step_count",
            "exit_code",
            "error",
            "progress_percent",
            "simulation_progress",
            "performance_ns_per_day",
            "benchmark_results",
        )
        return {field: meta.get(field) for field in fields}

    def list_page(self, limit: int = 50, offset: int = 0, search: str = "", *, search_aliases: tuple[str, ...] = ()) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        filters, parameters = [], []
        search = search.strip()
        if search:
            literal = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            fields = (
                "id",
                "status",
                "json_extract(meta_json,'$.name')",
                "json_extract(meta_json,'$.directory_name')",
                "json_extract(meta_json,'$.workflow')",
            )
            filters.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
            parameters.extend([literal] * len(fields))
            for alias in dict.fromkeys(search_aliases):
                filters.append("(status = ? OR json_extract(meta_json,'$.workflow') = ?)")
                parameters.extend((alias, alias))
        where = " WHERE " + " OR ".join(filters) if filters else ""
        with self._connect() as connection:
            total = int(connection.execute("SELECT count(*) FROM jobs" + where, parameters).fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM jobs" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?", [*parameters, limit, offset]
            ).fetchall()
        return {
            "jobs": [self.summarize(self._decode(row)) for row in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(rows) < total,
        }

    def _persist(
        self, connection: sqlite3.Connection, meta: dict, *, desired_action: str | None = None, worker_id: str | None = None
    ) -> dict:
        now = utc_now()
        meta["updated_at"] = now
        connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,meta_json=?,desired_action=?,worker_id=?,version=version+1 WHERE id=?",
            (meta["status"], now, json.dumps(meta, ensure_ascii=False), desired_action, worker_id, meta["id"]),
        )
        write_json(self.meta_path(meta["id"]), meta)
        return meta

    def update(self, job_id: str, **updates: object) -> dict:
        desired = updates.pop("desired_action", None) if "desired_action" in updates else ...
        worker = updates.pop("worker_id", None) if "worker_id" in updates else ...
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(job_id)
                meta = self._decode(row)
                meta.update(updates)
                self._persist(
                    connection,
                    meta,
                    desired_action=row["desired_action"] if desired is ... else desired,
                    worker_id=row["worker_id"] if worker is ... else worker,
                )
                connection.commit()
                return self.get(job_id)
            except Exception:
                connection.rollback()
                raise

    def create(self, name: str, params: dict, uploaded_files: list[str], steps: list[Step]) -> dict:
        job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{directory_slug(name)}-{uuid4().hex[:8]}"
        workdir = self.job_dir(job_id)
        workdir.mkdir(parents=True, exist_ok=False)
        now = utc_now()
        meta = {
            "id": job_id,
            "name": name,
            "status": "preparing",
            "created_at": now,
            "updated_at": now,
            "workflow": params.get("workflow"),
            "params": params,
            "workdir": str(workdir),
            "directory_name": workdir.name,
            "uploaded_files": uploaded_files,
            "current_step": None,
            "step_index": 0,
            "step_count": len(steps),
            "commands": self.serialize_steps(steps),
            "dry_run": False,
            "exit_code": None,
            "error": None,
        }
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO jobs(id,status,created_at,updated_at,meta_json) VALUES(?,?,?,?,?)",
                    (job_id, "preparing", now, now, json.dumps(meta, ensure_ascii=False)),
                )
            write_json(self.meta_path(job_id), meta)
            self.log_path(job_id).write_text("", encoding="utf-8")
            return meta
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    @staticmethod
    def serialize_steps(steps: list[Step]) -> list[dict]:
        return [
            {
                "title": step.title,
                "args": step.args,
                "outputs": step.outputs,
                "stdin": bool(step.stdin_text),
                "stdin_text": step.stdin_text or "",
                "kind": step.kind,
                "operation": step.operation,
                "data": step.data,
                "parallel_group": step.parallel_group,
            }
            for step in steps
        ]

    def start(self, job_id: str, steps: list[Step], dry_run: bool = False) -> None:
        self.update(job_id, status="queued", commands=self.serialize_steps(steps), dry_run=bool(dry_run), desired_action="run")

    def append_log(self, job_id: str, text: str) -> None:
        path = self.log_path(job_id)
        with self._log_lock:
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(text)
            if path.stat().st_size > MAX_LOG_FILE_BYTES:
                self._rotate_log(path)

    @staticmethod
    def _rotate_log(path: Path) -> None:
        """Keep run.log bounded by dropping the oldest half once it grows too large."""
        keep = MAX_LOG_FILE_BYTES // 2
        with path.open("rb") as handle:
            handle.seek(max(0, handle.seek(0, 2) - keep))
            tail = handle.read(keep).decode("utf-8", errors="replace")
        marker = f"\n[web] log rotated by worker (older output dropped) at {utc_now()}\n"
        path.write_text(marker + tail, encoding="utf-8", errors="replace")

    def cancel(self, job_id: str) -> dict:
        from .execution import live_process_identities

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(job_id)
            meta = self._decode(row)
            live = live_process_identities(meta, self.job_dir(job_id))
            if meta["status"] in {"completed", "failed", "cancelled"} and not live:
                connection.commit()
                return self.get(job_id)
            if meta["status"] == "running" or live:
                meta["cancellation_requested_at"] = utc_now()
                self._persist(connection, meta, desired_action="cancel", worker_id=row["worker_id"])
            else:
                meta.update(status="cancelled", cancelled_at=utc_now())
                self._persist(connection, meta, desired_action=None, worker_id=None)
            connection.commit()
        return self.get(job_id)

    def adopt(self, job_id: str) -> dict:
        meta = self.get(job_id)
        if meta["status"] != "interrupted":
            raise RuntimeError("Only an interrupted job can be re-adopted.")
        return self.update(job_id, desired_action="adopt")

    def resume_from_checkpoint(self, job_id: str) -> dict:
        meta = self.get(job_id)
        if meta["status"] not in {"interrupted", "failed"}:
            raise RuntimeError("Only an interrupted or failed job can resume from a checkpoint.")
        from .execution import live_process_identities

        if live_process_identities(meta, self.job_dir(job_id)):
            raise RuntimeError("A saved process is still active. Re-adopt it instead of starting a second process.")
        commands = list(meta.get("commands") or [])
        step_index = int(meta.get("step_index") or 0)
        command_index = self.current_command_index(meta)
        if not 0 <= command_index < len(commands):
            raise RuntimeError("The interrupted step is unavailable; checkpoint resume cannot be planned.")
        current = dict(commands[command_index])
        args = [str(arg) for arg in current.get("args") or []]
        if current.get("kind", "command") != "command" or "mdrun" not in args or "-cpi" not in args:
            raise RuntimeError("The interrupted step is not a checkpoint-enabled mdrun command.")
        cpi_index = args.index("-cpi") + 1
        checkpoint = ensure_under_root(self.job_dir(job_id) / args[cpi_index], self.job_dir(job_id))
        if not checkpoint.is_file() or checkpoint.stat().st_size == 0:
            raise RuntimeError(f"Checkpoint file is missing or empty: {checkpoint.name}")
        if "-append" not in args:
            args.append("-append")
        current["args"] = args
        recovery = [current, *commands[command_index + 1 :]]
        return self.update(
            job_id,
            status="queued",
            commands=recovery,
            step_index=step_index - 1,
            step_count=(step_index - 1) + len(recovery),
            desired_action="resume",
            error=None,
            exit_code=None,
            resumed_from_checkpoint=checkpoint.name,
            resumed_at=utc_now(),
        )

    def retry_failed_step(self, job_id: str) -> dict:
        """Re-queue a failed job from its failed step (no checkpoint required)."""
        meta = self.get(job_id)
        if meta["status"] != "failed":
            raise RuntimeError("Only a failed job can retry a step.")
        from .execution import live_process_identities

        if live_process_identities(meta, self.job_dir(job_id)):
            raise RuntimeError("A saved process is still active. Cancel it before retrying the failed step.")
        commands = list(meta.get("commands") or [])
        step_index = int(meta.get("step_index") or 0)
        command_index = self.current_command_index(meta)
        if not 0 <= command_index < len(commands):
            raise RuntimeError("The failed step is unavailable; cannot retry.")
        group = commands[command_index].get("parallel_group")
        while group and command_index > 0 and commands[command_index - 1].get("parallel_group") == group:
            command_index -= 1
            step_index -= 1
        step_runs = [run for run in (meta.get("step_runs") or []) if int(run.get("index") or 0) < step_index]
        return self.update(
            job_id,
            status="queued",
            commands=commands[command_index:],
            step_index=step_index - 1,
            step_runs=step_runs,
            desired_action="run",
            error=None,
            exit_code=None,
            current_step=None,
        )

    def skip_failed_step(self, job_id: str) -> dict:
        """Skip only optional work with no downstream file dependencies."""
        self.job_dir(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(job_id)
            meta = self._decode(row)
            reason = self.skip_step_block_reason(meta)
            if reason:
                raise RuntimeError(reason)
            commands = meta["commands"]
            index = self.current_command_index(meta)
            step_index = int(meta["step_index"])
            skipped = {"index": step_index, "title": commands[index]["title"], "at": utc_now(), "error": meta.get("error")}
            meta.setdefault("skipped_steps", []).append(skipped)
            for run in meta.get("step_runs") or []:
                if int(run.get("index") or 0) == step_index:
                    run["status"] = "skipped"
            meta.update(status="queued", commands=commands[index + 1 :], error=None, exit_code=None, current_step=None)
            self._persist(connection, meta, desired_action="run", worker_id=None)
            connection.commit()
        return self.get(job_id)

    @staticmethod
    def _resource_request(meta: dict) -> tuple[int, bool]:
        commands = list(meta.get("commands") or [])
        has_mdrun = any("mdrun" in [str(arg) for arg in command.get("args") or []] for command in commands)
        params = dict(meta.get("params") or {})
        try:
            cpu_threads = max(1, int(params.get("ntmpi") or 1) * int(params.get("ntomp") or 1)) if has_mdrun else 1
        except (TypeError, ValueError):
            cpu_threads = 1
        uses_gpu = bool(params.get("gpu")) or any(
            any(str(arg).lower() == "gpu" for arg in command.get("args") or [])
            for command in commands
            if "mdrun" in [str(arg) for arg in command.get("args") or []]
        )
        return cpu_threads, uses_gpu

    def claim_next(self, worker_id: str) -> dict | None:
        from .execution import live_process_identities

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                running_rows = connection.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
                reserved_rows = connection.execute(
                    "SELECT * FROM jobs WHERE (status IN ('queued','interrupted','failed','cancelled','completed') OR desired_action='cancel') AND "
                    "(json_extract(meta_json,'$.process_pid') IS NOT NULL OR json_array_length(json_extract(meta_json,'$.process_identities')) > 0)"
                ).fetchall()
                live_reserved = {}
                for item in reserved_rows:
                    meta = self._decode(item)
                    if live_process_identities(meta, self.job_dir(item["id"])):
                        live_reserved[item["id"]] = meta
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE (desired_action IN ('run','resume','adopt') AND status IN ('queued','interrupted')) "
                    "OR (desired_action='cancel' AND status <> 'running') ORDER BY created_at"
                ).fetchall()
                # Re-adoption and cancellation control an existing process;
                # they must not wait behind limits intended for new launches.
                row = next((item for item in rows if item["desired_action"] == "cancel" or item["id"] in live_reserved), None)
                action = "cancel" if row is not None and row["desired_action"] == "cancel" else "adopt"
                if row is None:
                    requests = [self._resource_request(self._decode(item)) for item in running_rows]
                    requests.extend(self._resource_request(meta) for meta in live_reserved.values())
                    if len(running_rows) + len(live_reserved) >= self._max_parallel:
                        connection.commit()
                        return None
                    used_cpu_threads = sum(request[0] for request in requests)
                    gpu_busy = any(request[1] for request in requests)
                    logical_cpus = os.cpu_count() or 1
                    for candidate in rows:
                        request = self._resource_request(self._decode(candidate))
                        if request[1] and gpu_busy:
                            continue
                        if requests and used_cpu_threads + request[0] > logical_cpus:
                            continue
                        row, action = candidate, candidate["desired_action"]
                        break
                if row is None:
                    connection.commit()
                    return None
                meta = self._decode(row)
                if meta["status"] not in {"completed", "failed", "cancelled"}:
                    meta["status"] = "running"
                meta["worker_id"] = worker_id
                meta["claimed_action"] = action
                self._persist(connection, meta, desired_action=None, worker_id=worker_id)
                connection.commit()
                return meta
            except Exception:
                connection.rollback()
                raise

    def delete(self, job_id: str) -> dict:
        self.job_dir(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(job_id)
                meta = self._decode(row)
                if meta["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
                    raise RuntimeError("Cancel the job before deleting it.")
                from .execution import live_process_identities

                if live_process_identities(meta, self.job_dir(job_id)):
                    raise RuntimeError("A saved process is still active. Cancel it and wait for the worker before deleting its files.")
                connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        workdir = self.job_dir(job_id)
        if workdir.exists():
            shutil.rmtree(workdir)
        return meta

    def clone(self, job_id: str, name: str | None = None) -> dict:
        source = self.get(job_id)
        commands = list(source.get("commands") or [])
        steps = [
            Step(
                str(command.get("title") or "Cloned step"),
                [str(arg) for arg in command.get("args") or []],
                stdin_text=str(command.get("stdin_text") or "") or None,
                outputs=[str(item) for item in command.get("outputs") or []],
                kind=str(command.get("kind") or "command"),
                operation=command.get("operation"),
                data=dict(command.get("data") or {}),
            )
            for command in commands
        ]
        cloned = self.create(
            name=(name or f"{source.get('name') or 'job'} copy").strip(),
            params=dict(source.get("params") or {}),
            uploaded_files=list(source.get("uploaded_files") or []),
            steps=steps,
        )
        source_dir = self.job_dir(job_id)
        target_dir = self.job_dir(cloned["id"])
        try:
            for filename in source.get("uploaded_files") or []:
                source_file = ensure_under_root(source_dir / str(filename), source_dir)
                if source_file.is_file():
                    shutil.copy2(source_file, target_dir / source_file.name)
            for source_file in source_dir.glob("*.mdp"):
                if source_file.is_file():
                    shutil.copy2(source_file, target_dir / source_file.name)
            for force_field in source_dir.glob("*.ff"):
                if force_field.is_dir() and not force_field.is_symlink():
                    shutil.copytree(force_field, target_dir / force_field.name)
            self.update(
                cloned["id"],
                cloned_from=job_id,
                mdp_templates=source.get("mdp_templates") or [],
                mdp_overrides=source.get("mdp_overrides") or [],
                local_force_fields=source.get("local_force_fields") or [],
            )
            self.start(cloned["id"], steps, dry_run=bool(source.get("dry_run")))
            return self.get(cloned["id"])
        except Exception as exc:
            self.update(cloned["id"], status="failed", error=f"Failed to clone task inputs: {exc}")
            raise
