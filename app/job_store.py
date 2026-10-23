from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .files import ensure_under_root
from .gromacs import Step, directory_slug, read_json, utc_now

JOB_STATES = {"preparing", "queued", "running", "completed", "failed", "cancelled", "interrupted"}

MAX_LOG_FILE_BYTES = int(os.environ.get("GROMACS_WEB_MAX_LOG_BYTES", str(100 * 1024**2)))


class JobStore:
    """Transactional job metadata store shared by the Web and worker processes."""

    def __init__(self, runtime_root: Path, max_parallel: int = 1, analysis_parallel: int = 2):
        self.runtime_root = runtime_root
        self.jobs_root = runtime_root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.db_path = runtime_root / "jobs.sqlite3"
        self._log_lock = threading.Lock()
        self._max_parallel = max(1, int(max_parallel))
        self._analysis_parallel = max(1, min(16, int(analysis_parallel)))
        self._initialize_database()
        self._migrate_json_jobs()
        self._index_existing_research_reports()
        self._recover_maintenance_jobs()

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
                CREATE TABLE IF NOT EXISTS store_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_reports (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS protocol_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK (json_valid(payload)),
                    UNIQUE(name, revision)
                );
                CREATE TABLE IF NOT EXISTS protocol_template_revisions (
                    name TEXT PRIMARY KEY,
                    last_revision INTEGER NOT NULL
                );
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
                if connection.execute("SELECT 1 FROM store_migrations WHERE name='json_import_v1'").fetchone():
                    connection.commit()
                    return
                for meta_path in self.jobs_root.glob("*/metadata.json"):
                    try:
                        meta = read_json(meta_path, {})
                    except (json.JSONDecodeError, OSError):
                        continue
                    if not meta or meta.get("status") not in JOB_STATES:
                        continue
                    if meta.get("id") != meta_path.parent.name or meta_path.is_symlink():
                        continue
                    meta["workdir"] = str(self.job_dir(meta["id"]))
                    meta.setdefault("directory_name", meta_path.parent.name)
                    now = str(meta.get("updated_at") or meta.get("created_at") or utc_now())
                    connection.execute(
                        "INSERT OR IGNORE INTO jobs(id,status,created_at,updated_at,meta_json) VALUES(?,?,?,?,?)",
                        (meta["id"], meta["status"], str(meta.get("created_at") or now), now, json.dumps(meta, ensure_ascii=False)),
                    )
                connection.execute("INSERT INTO store_migrations VALUES('json_import_v1', ?)", (utc_now(),))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def configure(self, max_parallel: int | None = None, analysis_parallel: int | None = None) -> None:
        if max_parallel is not None:
            self._max_parallel = max(1, int(max_parallel))
        if analysis_parallel is not None:
            self._analysis_parallel = max(1, min(16, int(analysis_parallel)))

    def _index_existing_research_reports(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM store_migrations WHERE name='research_index_v1'").fetchone():
                return
            for row in connection.execute("SELECT id FROM jobs WHERE status='completed'").fetchall():
                path = self.job_dir(row["id"]) / "research-report.json"
                if path.is_file() and not path.is_symlink():
                    connection.execute("INSERT OR IGNORE INTO research_reports VALUES(?)", (row["id"],))
            connection.execute("INSERT INTO store_migrations VALUES('research_index_v1', ?)", (utc_now(),))
            connection.commit()

    def research_page(self, limit: int = 50, offset: int = 0, search: str = "") -> dict:
        limit = max(1, min(200, limit))
        offset = max(0, offset)
        literal = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = (
            " FROM jobs j JOIN research_reports r ON r.job_id=j.id WHERE j.status='completed' "
            "AND coalesce(json_extract(j.meta_json,'$.deletion_pending'),0)=0 "
            "AND (j.id LIKE ? ESCAPE '\\' OR json_extract(j.meta_json,'$.name') LIKE ? ESCAPE '\\')"
        )
        with self._connect() as connection:
            total = connection.execute("SELECT count(*)" + where, (literal, literal)).fetchone()[0]
            rows = connection.execute(
                "SELECT j.id,json_extract(j.meta_json,'$.name') AS name,j.created_at"
                + where
                + " ORDER BY j.created_at DESC,j.id DESC LIMIT ? OFFSET ?",
                (literal, literal, limit, offset),
            ).fetchall()
        return {
            "jobs": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def export_metadata(self, job_id: str) -> Path:
        """An explicit, rebuildable export; SQLite remains authoritative."""
        import tempfile

        with self.job_files_lock(job_id):
            meta = self.get(job_id)
            path = self.meta_path(job_id)
            if path.is_symlink():
                raise RuntimeError("Metadata export target must not be a symbolic link.")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, prefix=".metadata-export-", suffix=".tmp", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return path

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def job_dir(self, job_id: str) -> Path:
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise FileNotFoundError(job_id)
        return ensure_under_root(self.jobs_root / job_id, self.jobs_root)

    @contextmanager
    def job_files_lock(self, job_id: str):
        """Reserve one task's files across threads and Web/worker processes."""
        if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id:
            raise FileNotFoundError(job_id)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(self.runtime_root, flags)
        try:
            try:
                os.mkdir("job-locks", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            locks_fd = os.open("job-locks", flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        name = hashlib.sha256(job_id.encode("utf-8")).hexdigest() + ".lock"
        try:
            lock_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=locks_fd)
        finally:
            os.close(locks_fd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise RuntimeError("Task file lock must be a regular file.")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Task files are busy; retry after the current operation completes.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _recover_maintenance_jobs(self) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE json_extract(meta_json,'$.maintenance_pending') IS NOT NULL").fetchall()
        for row in rows:
            try:
                with self.job_files_lock(row["id"]):
                    self._finish_maintenance(row["id"], error="Previous maintenance was interrupted; preview files before retrying.")
            except (OSError, RuntimeError, FileNotFoundError):
                # A live owner keeps its marker. An unavailable directory stays
                # visible for manual recovery rather than changing task state.
                continue

    def meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "metadata.json"

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "run.log"

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        meta = json.loads(row["meta_json"])
        meta["status"] = row["status"]
        meta["updated_at"] = row["updated_at"]
        meta["version"] = row["version"]
        meta.setdefault("storage_revision", 0)
        if row["desired_action"]:
            meta["desired_action"] = row["desired_action"]
        else:
            meta.pop("desired_action", None)
        if row["worker_id"]:
            meta["worker_id"] = row["worker_id"]
        return meta

    def get(self, job_id: str) -> dict:
        if (self.jobs_root / job_id).is_symlink():
            raise FileNotFoundError(f"Task directory is a symbolic link: {job_id}")
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
            "project",
            "tags",
            "pinned",
            "is_example",
        )
        return {field: meta.get(field) for field in fields}

    def list_page(
        self, limit: int = 50, offset: int = 0, search: str = "", *, search_aliases: tuple[str, ...] = (), project: str = ""
    ) -> dict:
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
                "json_extract(meta_json,'$.project')",
                "json_extract(meta_json,'$.tags')",
                "json_extract(meta_json,'$.notes')",
            )
            filters.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
            parameters.extend([literal] * len(fields))
            for alias in dict.fromkeys(search_aliases):
                filters.append("(status = ? OR json_extract(meta_json,'$.workflow') = ?)")
                parameters.extend((alias, alias))
        conditions = ["(" + " OR ".join(filters) + ")"] if filters else []
        if project:
            conditions.append("json_extract(meta_json,'$.project') = ?")
            parameters.append(project)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as connection:
            total = int(connection.execute("SELECT count(*) FROM jobs" + where, parameters).fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM jobs"
                + where
                + " ORDER BY coalesce(json_extract(meta_json,'$.pinned'),0) DESC,created_at DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
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
        if (meta.get("deletion_pending") or meta.get("maintenance_pending")) and (
            meta["status"] in {"queued", "running"} or desired_action is not None
        ):
            raise RuntimeError("Task maintenance or deletion is pending; finish it before using this task.")
        meta["updated_at"] = now
        connection.execute(
            "UPDATE jobs SET status=?,updated_at=?,meta_json=?,desired_action=?,worker_id=?,version=version+1 WHERE id=?",
            (meta["status"], now, json.dumps(meta, ensure_ascii=False), desired_action, worker_id, meta["id"]),
        )
        if meta["status"] == "completed":
            report = self.job_dir(meta["id"]) / "research-report.json"
            if report.is_file() and not report.is_symlink():
                connection.execute("INSERT OR IGNORE INTO research_reports VALUES(?)", (meta["id"],))
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
            "project": str(params.get("project") or "").strip(),
            "tags": params.get("tags") or [],
            "notes": str(params.get("notes") or "").strip(),
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

    def _resource_reservation(self, meta: dict) -> dict:
        """Freeze the peak CPU demand at claim time, including bounded analysis batches."""
        commands = list(meta.get("commands") or [])
        has_mdrun = any("mdrun" in [str(arg) for arg in command.get("args") or []] for command in commands)
        params = dict(meta.get("params") or {})
        try:
            cpu_threads = max(1, int(params.get("ntmpi") or 1) * int(params.get("ntomp") or 1)) if has_mdrun else 1
        except (TypeError, ValueError):
            cpu_threads = 1
        analysis_parallel = min(self._analysis_parallel, os.cpu_count() or 1)
        group, group_size, largest_group = "", 0, 1
        for command in commands:
            current = str(command.get("parallel_group") or "") if command.get("kind", "command") == "command" else ""
            group_size = group_size + 1 if current and current == group else 1
            group = current
            largest_group = max(largest_group, group_size)
        analysis_parallel = min(analysis_parallel, largest_group)
        return {"cpu_threads": max(cpu_threads, analysis_parallel), "analysis_parallel": analysis_parallel}

    def _resource_request(self, meta: dict, *, new_reservation: dict | None = None) -> tuple[int, bool]:
        commands = list(meta.get("commands") or [])
        params = dict(meta.get("params") or {})
        reservation = new_reservation if new_reservation is not None else meta.get("resource_reservation")
        reservation = reservation or self._resource_reservation(meta)
        cpu_threads = max(1, int(reservation["cpu_threads"]))
        if new_reservation is None:
            # Older workers could launch an entire analysis group. Keep its
            # registered processes reserved until recovery has stopped them.
            cpu_threads = max(cpu_threads, len(meta.get("process_identities") or []))
        uses_gpu = bool(params.get("gpu")) or any(
            any(str(arg).lower() == "gpu" for arg in command.get("args") or [])
            for command in commands
            if "mdrun" in [str(arg) for arg in command.get("args") or []]
        )
        return cpu_threads, uses_gpu

    def claim_next(self, worker_id: str) -> dict | None:
        from .execution import live_process_identities

        with self._connect() as connection, ExitStack() as file_locks:
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
                reservation = None
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
                        candidate_meta = self._decode(candidate)
                        if candidate_meta.get("deletion_pending") or candidate_meta.get("maintenance_pending"):
                            continue
                        candidate_reservation = self._resource_reservation(candidate_meta)
                        request = self._resource_request(candidate_meta, new_reservation=candidate_reservation)
                        if request[1] and gpu_busy:
                            continue
                        if requests and used_cpu_threads + request[0] > logical_cpus:
                            continue
                        try:
                            file_locks.enter_context(self.job_files_lock(candidate["id"]))
                        except RuntimeError:
                            continue
                        row, action, reservation = candidate, candidate["desired_action"], candidate_reservation
                        break
                if row is None:
                    connection.commit()
                    return None
                meta = self._decode(row)
                if action not in {"adopt", "cancel"}:
                    meta["resource_reservation"] = reservation
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

    def _finish_maintenance(self, job_id: str, *, result: dict | None = None, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(job_id)
            meta = self._decode(row)
            if not meta.get("maintenance_pending") or meta.get("deletion_pending"):
                return
            meta.pop("maintenance_pending", None)
            # Even a failed operation may already have removed some files.
            meta["storage_revision"] += 1
            if error is None:
                meta["last_cleanup"] = {"at": utc_now(), **(result or {})}
                meta.pop("last_cleanup_error", None)
            else:
                meta["last_cleanup_error"] = {"at": utc_now(), "error": error}
            self._persist(connection, meta)
            connection.commit()

    def maintain_job(
        self, job_id: str, operation: Callable[[dict, Path], dict], *, validate: Callable[[dict, Path], None] | None = None
    ) -> dict:
        """Reserve task files while allowing unrelated database writers to proceed."""
        from .execution import live_process_identities

        with self.job_files_lock(job_id):
            workdir = self.job_dir(job_id)
            if (self.jobs_root / job_id).is_symlink():
                raise RuntimeError("Cannot maintain a task directory that is a symbolic link.")
            initial = self.get(job_id)
            # Reject stale previews before recording an operation that might
            # modify files, and keep directory scans outside the writer lock.
            if validate:
                validate(initial, workdir)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(job_id)
                meta = self._decode(row)
                if (
                    meta["status"] not in {"completed", "cancelled"}
                    or row["desired_action"]
                    or meta.get("deletion_pending")
                    or live_process_identities(meta, workdir)
                ):
                    raise RuntimeError("Only completed or cancelled tasks without active processes can be cleaned.")
                if row["version"] != initial["version"]:
                    raise RuntimeError("Task changed since the cleanup preview; preview again.")
                meta["maintenance_pending"] = True
                self._persist(connection, meta)
                connection.commit()
            try:
                result = operation(meta, workdir)
            except BaseException as exc:
                try:
                    self._finish_maintenance(job_id, error=str(exc))
                except Exception:
                    # The durable marker can be recovered once storage is writable.
                    pass
                raise
            self._finish_maintenance(job_id, result=result)
            return result

    def delete(
        self,
        job_id: str,
        *,
        allowed_states: set[str] | None = None,
        expected_version: int | None = None,
        validate: Callable[[dict, Path], None] | None = None,
    ) -> dict:
        from .execution import live_process_identities

        with self.job_files_lock(job_id):
            if self.jobs_root.is_symlink() or (self.jobs_root / job_id).is_symlink():
                raise RuntimeError("Cannot delete a task directory that is a symbolic link.")
            workdir = self.job_dir(job_id)
            initial = self.get(job_id)
            # Preview validation can walk a large directory and must not hold
            # SQLite's writer lock while other jobs are reporting progress.
            if validate:
                validate(initial, workdir)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(job_id)
                meta = self._decode(row)
                if meta["status"] not in (allowed_states or {"completed", "failed", "cancelled", "interrupted"}):
                    raise RuntimeError("Cancel the job before deleting it.")
                if live_process_identities(meta, workdir):
                    raise RuntimeError("A saved process is still active. Cancel it and wait for the worker before deleting its files.")
                if row["desired_action"]:
                    raise RuntimeError("A worker action is pending; wait before deleting this task.")
                if row["version"] != initial["version"] or (expected_version is not None and row["version"] != expected_version):
                    raise RuntimeError("Task changed since the deletion preview; preview again.")
                meta.pop("maintenance_pending", None)
                meta["deletion_pending"] = True
                self._persist(connection, meta)
                connection.commit()
            # Keep the pending marker until removal succeeds. The file lock
            # serializes copies, other maintenance and new worker launches.
            current = self.get(job_id)
            if validate:
                validate(current, workdir)
            if (self.jobs_root / job_id).is_symlink():
                raise RuntimeError("Task directory changed during deletion.")
            if workdir.exists():
                shutil.rmtree(workdir)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(job_id)
                current = self._decode(row)
                if not current.get("deletion_pending") or row["desired_action"]:
                    raise RuntimeError("Task state changed during deletion.")
                connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                connection.commit()
            return meta

    def clone(self, job_id: str, name: str | None = None) -> dict:
        from .execution import live_process_identities
        from .reproducibility import write_experiment_manifest

        with self.job_files_lock(job_id):
            source = self.get(job_id)
            source_dir = self.job_dir(job_id)
            if (
                source["status"] in {"preparing", "running"}
                or source.get("deletion_pending")
                or source.get("maintenance_pending")
                or source.get("desired_action") in {"cancel", "adopt", "resume"}
                or live_process_identities(source, source_dir)
            ):
                raise RuntimeError("Task inputs are not available for cloning while preparation, execution or maintenance is active.")
            if not source_dir.is_dir():
                raise RuntimeError("Clone source directory is missing.")
            input_files = []
            for filename in source.get("uploaded_files") or []:
                source_file = ensure_under_root(source_dir / str(filename), source_dir)
                if not source_file.is_file() or source_file.is_symlink():
                    raise RuntimeError(f"Clone source input is missing or is not a regular file: {filename}")
                input_files.append(source_file)
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
                    parallel_group=str(command.get("parallel_group") or ""),
                )
                for command in commands
            ]
            cloned = self.create(
                name=(name or f"{source.get('name') or 'job'} copy").strip(),
                params=dict(source.get("params") or {}),
                uploaded_files=list(source.get("uploaded_files") or []),
                steps=steps,
            )
            target_dir = self.job_dir(cloned["id"])
            try:
                with self.job_files_lock(cloned["id"]):
                    if self.get(cloned["id"])["status"] != "preparing":
                        raise RuntimeError("Clone preparation was cancelled before inputs were ready.")
                    for source_file in input_files:
                        shutil.copy2(source_file, target_dir / source_file.name)
                    for source_file in source_dir.glob("*.mdp"):
                        if source_file.is_file():
                            shutil.copy2(ensure_under_root(source_file, source_dir), target_dir / source_file.name)
                    for force_field in source_dir.glob("*.ff"):
                        if force_field.is_dir() and not force_field.is_symlink():
                            shutil.copytree(force_field, target_dir / force_field.name)
                    self.update(
                        cloned["id"],
                        cloned_from=job_id,
                        project=source.get("project") or "",
                        tags=source.get("tags") or [],
                        notes=source.get("notes") or "",
                        is_example=bool(source.get("is_example")),
                        mdp_templates=source.get("mdp_templates") or [],
                        mdp_overrides=source.get("mdp_overrides") or [],
                        local_force_fields=source.get("local_force_fields") or [],
                    )
                    manifest = write_experiment_manifest(self.get(cloned["id"]))
                    self.update(cloned["id"], experiment_manifest="experiment-manifest.json", input_hashes=manifest["input_files"])
                    if self.get(cloned["id"])["status"] != "preparing":
                        raise RuntimeError("Clone preparation was cancelled before inputs were ready.")
                    self.start(cloned["id"], steps, dry_run=bool(source.get("dry_run")))
                    return self.get(cloned["id"])
            except Exception as exc:
                try:
                    if self.get(cloned["id"])["status"] == "preparing":
                        self.update(cloned["id"], status="failed", error=f"Failed to clone task inputs: {exc}")
                except FileNotFoundError:
                    pass
                raise
