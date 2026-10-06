"""Promote application files with recoverable rollback; never touch results or environments."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ITEMS = (
    "app",
    "mdp",
    "scripts",
    "requirements.txt",
    "run.sh",
    "desktop_service.py",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "installed-release.json",
)
JOURNAL = ".application-update.json"


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def save_journal(root: Path, state: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", prefix="update-journal-", dir=root, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(root / JOURNAL)
    finally:
        temporary.unlink(missing_ok=True)


def checked_stage(root: Path, stage: Path) -> Path:
    stage = stage.absolute()
    if stage.is_symlink() or stage.parent.resolve() != root.resolve() or not stage.name.startswith("build."):
        raise ValueError("Update staging must be a build directory directly inside the application directory.")
    return stage


def recover(root: Path) -> None:
    journal = root / JOURNAL
    if not journal.exists():
        return
    state = json.loads(journal.read_text())
    stage = checked_stage(root, Path(state["stage"]))
    started = state["started"]
    previous = state["previous"]
    if (
        state.get("phase") not in {"promoting", "committed"}
        or not isinstance(started, list)
        or not isinstance(previous, list)
        or not set(started).issubset(ITEMS)
        or not set(previous).issubset(ITEMS)
    ):
        raise ValueError("Invalid application update journal; retain backups and inspect the installation log.")
    backup = stage / "previous"
    if backup.is_symlink():
        raise ValueError("Update backup directory must not be a symlink.")
    if state["phase"] == "promoting":
        print("Restoring the previous application after an interrupted update.", flush=True)
        for name in reversed(started):
            saved, installed = backup / name, root / name
            if saved.exists() or saved.is_symlink():
                remove(installed)
                saved.replace(installed)
            elif name not in previous:
                remove(installed)
    # On rollback failure, the journal and remaining backups intentionally survive.
    journal.unlink()
    remove(stage)


def check_space(root: Path, payload: Path, *, app_only: bool, backend: str = "CUDA") -> int:
    with tarfile.open(payload / "application.tar.gz", "r:gz") as archive:
        extracted = sum(member.size for member in archive if member.isfile())
    # Existing files are already reflected in free space. Budget only new staged
    # files, downloads and a margin; no duplicate copy of trajectories is needed.
    budget = 128 * 1024**2 if app_only else (2 if backend.upper() == "CPU" else 3) * 1024**3
    required = extracted + budget
    free = shutil.disk_usage(root).free
    if free < required:
        raise RuntimeError(
            f"Insufficient Linux disk space at {root}: {free / 1024**3:.2f} GiB available; "
            f"at least {required / 1024**3:.2f} GiB required for this {'application update' if app_only else backend + ' setup'}. "
            "Free space on the Windows disk holding this WSL distribution as well, then retry."
        )
    print(f"Linux disk preflight: {free / 1024**3:.2f} GiB available; estimated reserve {required / 1024**3:.2f} GiB.", flush=True)
    return required


def prepare(root: Path, stage: Path, payload: Path) -> None:
    stage = checked_stage(root, stage)
    shutil.copyfile(payload / "desktop_service.py", stage / "desktop_service.py")
    shutil.copyfile(payload / "release.json", stage / "installed-release.json")
    (stage / "run.sh").chmod(0o755)
    for name in ITEMS:
        if not (stage / name).exists() or (stage / name).is_symlink():
            raise ValueError(f"Staged application item is missing or unsafe: {name}")


def check_runtime(root: Path) -> None:
    prefix = root / "gromacs"
    gmx = prefix / "bin/gmx"
    if not (prefix / ".ready").is_file() or not os.access(gmx, os.X_OK) or not (prefix / "share/gromacs/top/amber19sb.ff").is_dir():
        raise RuntimeError("The retained GROMACS runtime is incomplete; run a full environment repair.")
    try:
        result = subprocess.run([str(gmx), "--version"], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("GROMACS version verification timed out; run Configure or Update.") from error
    output = result.stdout + result.stderr
    if (
        result.returncode
        or not re.search(r"^GROMACS version:\s*2026\.3(?:\s|$|-)", output, re.MULTILINE)
        or not re.search(r"^Precision:\s*mixed\s*$", output, re.MULTILINE)
    ):
        raise RuntimeError(f"GROMACS runtime needs repair (expected 2026.3 mixed precision): {output[-1500:]}")


def promote(root: Path, stage: Path) -> None:
    stage = checked_stage(root, stage)
    if (root / JOURNAL).exists():
        raise RuntimeError("Recover the previous application update before promoting another one.")
    backup = stage / "previous"
    backup.mkdir()
    state = {
        "phase": "promoting",
        "stage": str(stage),
        "previous": [name for name in ITEMS if (root / name).exists() or (root / name).is_symlink()],
        "started": [],
    }
    save_journal(root, state)
    try:
        for name in ITEMS:
            state["started"].append(name)
            save_journal(root, state)
            installed = root / name
            if name in state["previous"]:
                installed.replace(backup / name)
            (stage / name).replace(installed)
        state["phase"] = "committed"
        save_journal(root, state)
    except BaseException:
        recover(root)
        raise
    recover(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--payload-dir", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--action", choices=("recover", "check-space", "check-runtime", "prepare", "promote"), required=True)
    parser.add_argument("--app-only", action="store_true")
    parser.add_argument("--backend", choices=("CPU", "CUDA"), default="CUDA")
    args = parser.parse_args()
    root = args.app_root.resolve()
    if args.action == "recover":
        recover(root)
    elif args.action == "check-runtime":
        check_runtime(root)
    elif args.action == "check-space":
        if args.payload_dir is None:
            parser.error("--payload-dir is required")
        check_space(root, args.payload_dir, app_only=args.app_only, backend=args.backend)
    elif args.action == "prepare":
        if args.stage is None or args.payload_dir is None:
            parser.error("--stage and --payload-dir are required")
        prepare(root, args.stage, args.payload_dir)
    else:
        if args.stage is None:
            parser.error("--stage is required")
        promote(root, args.stage)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(
            f"Application update failed: {error}. Previous application backups are retained if recovery needs attention.", file=sys.stderr
        )
        raise SystemExit(1) from error
