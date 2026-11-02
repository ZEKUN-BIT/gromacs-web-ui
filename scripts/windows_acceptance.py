"""Collect Windows acceptance evidence locally; validate manually completed records.

Collection never installs, updates or runs a simulation, and never marks a case
passed. A validated record is a maintainer attestation, not automated proof of
physical hardware testing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CASES = {
    "win10-fresh-cpu": ("install", "reboot", "launch", "tutorial"),
    "win11-fresh-nvidia": ("install", "gpu_detection", "gpu_simulation"),
    "win11-existing-cpu": ("reuse_environment", "launch", "tutorial"),
    "win11-upgrade-nvidia": ("upgrade", "read_old_jobs", "resume_checkpoint"),
    "win-recovery-cpu": ("network_interruption", "low_disk", "retry", "diagnostics"),
}
MAX_EVIDENCE_BYTES = 64 * 1024**2


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def probe(command: list[str]) -> dict:
    try:
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)

        # WSL uses UTF-16LE for its Windows-side list output on some versions.
        def decode(data: bytes) -> str:
            encoding = "utf-16-le" if b"\x00" in data[:100] else "utf-8-sig"
            return data.decode(encoding, errors="replace")[:32000]

        return {"exit_code": result.returncode, "stdout": decode(result.stdout), "stderr": decode(result.stderr)}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exit_code": None, "error": type(error).__name__}


def collect(installer: Path, output: Path, *, case: str, tester: str, version: str, source_commit: str, distro: str, gmx: str) -> dict:
    if platform.system() != "Windows":
        raise ValueError("Evidence collection requires a real Windows host; Linux/WSL is not a Windows acceptance run.")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing acceptance record.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise ValueError("source_commit must be the full 40-character release commit.")
    installer_hash = digest(installer)
    output.parent.mkdir(parents=True, exist_ok=True)
    probes = {
        "wsl": probe(["wsl.exe", "--list", "--verbose"]),
        "gromacs": probe(["wsl.exe", "--distribution", distro, "--exec", gmx, "--version"]),
        "gpu": probe(["nvidia-smi.exe", "--query-gpu=name,driver_version", "--format=csv,noheader"]),
    }
    evidence = []
    for name, result in probes.items():
        path = output.with_name(f"{output.stem}-{name}.json")
        with path.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        evidence.append({"path": path.name, "sha256": digest(path)})
    record = {
        "schema_version": 1,
        "case": case,
        "status": "unverified",
        "version": version,
        "source_commit": source_commit.lower(),
        "installer_sha256": installer_hash,
        "tester": tester,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(), "machine": platform.machine()},
        "distro": distro,
        "hardware": "",
        "driver": "",
        "gromacs_version": "",
        "installation_minutes": None,
        "recovery_minutes": None,
        "steps": {name: {"status": "unverified", "actual": "", "evidence": []} for name in CASES[case]},
        "evidence": evidence,
        "notes": "Read-only probes only. Complete the named steps on this machine, attach their evidence, then record the actual outcome.",
    }
    with output.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return record


def validate_record(path: Path, *, installer_sha256: str, version: str, source_commit: str, require_passed: bool) -> dict:
    if path.stat().st_size > 1024**2:
        raise ValueError("Acceptance record exceeds 1 MiB.")
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or not isinstance(record.get("case"), str)
        or record["case"] not in CASES
    ):
        raise ValueError("Unknown acceptance schema or case.")
    for key, expected in (("installer_sha256", installer_sha256), ("version", version), ("source_commit", source_commit)):
        if record.get(key) != expected:
            raise ValueError(f"{key} does not match this release.")
    if record.get("status") not in {"unverified", "passed", "failed"}:
        raise ValueError("Invalid acceptance status.")
    if not isinstance(record.get("os"), dict) or record["os"].get("system") != "Windows":
        raise ValueError("A Windows host is required.")
    for key in ("tester", "tested_at"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"Missing {key}.")
    datetime.fromisoformat(record["tested_at"])
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 50:
        raise ValueError("Invalid evidence list.")
    evidence_names = set()
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Invalid evidence entry.")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in item["path"] or ":" in item["path"] or not relative.parts:
            raise ValueError("Evidence must stay inside the report directory.")
        target = path.parent
        for part in relative.parts:
            target = target / part
            if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                raise ValueError("Symlink or junction evidence is not accepted.")
        if not target.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError("Evidence must stay inside the report directory.")
        if not target.is_file() or target.stat().st_size > MAX_EVIDENCE_BYTES or digest(target) != item.get("sha256"):
            raise ValueError("Evidence is missing, oversized or its hash changed.")
        if item["path"] in evidence_names:
            raise ValueError("Duplicate evidence path.")
        evidence_names.add(item["path"])
    steps = record.get("steps", {})
    if not isinstance(steps, dict) or set(steps) != set(CASES[record["case"]]):
        raise ValueError("The case must include every required acceptance step.")
    for step in steps.values():
        if not isinstance(step, dict) or step.get("status") not in {"unverified", "passed", "failed"}:
            raise ValueError("Invalid step status.")
        if not isinstance(step.get("evidence", []), list) or any(
            not isinstance(name, str) or name not in evidence_names for name in step.get("evidence", [])
        ):
            raise ValueError("Step references unknown evidence.")
    if record["status"] == "passed":
        for key in ("hardware", "driver", "gromacs_version"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise ValueError(f"Passed records require {key} (use n/a with a reason when appropriate).")
        if any(
            step["status"] != "passed" or not isinstance(step.get("actual"), str) or not step["actual"].strip() or not step.get("evidence")
            for step in steps.values()
        ):
            raise ValueError("Passed records require actual results and evidence for every step.")
    if require_passed and record["status"] != "passed":
        raise ValueError(f"{record['case']} remains {record['status']}.")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="Collect read-only probes on Windows; always creates an unverified record")
    check_parser = commands.add_parser("check", help="Verify release identity, evidence hashes and manual completion")
    for command in (collect_parser, check_parser):
        command.add_argument("--installer", type=Path, required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--source-commit", required=True)
    collect_parser.add_argument("--case", choices=CASES, required=True)
    collect_parser.add_argument("--tester", required=True)
    collect_parser.add_argument("--distro", default="Ubuntu-24.04")
    collect_parser.add_argument("--gmx", default="gmx")
    collect_parser.add_argument("--output", type=Path, required=True)
    check_parser.add_argument("--require-complete", action="store_true", help="Require all five cases to have passed")
    check_parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            collect(
                args.installer,
                args.output,
                case=args.case,
                tester=args.tester,
                version=args.version,
                source_commit=args.source_commit,
                distro=args.distro,
                gmx=args.gmx,
            )
            print(f"Unverified evidence record saved to {args.output}")
        else:
            records = [
                validate_record(
                    path,
                    installer_sha256=digest(args.installer),
                    version=args.version,
                    source_commit=args.source_commit,
                    require_passed=args.require_complete,
                )
                for path in args.reports
            ]
            cases = [record["case"] for record in records]
            if len(cases) != len(set(cases)):
                raise ValueError("Duplicate acceptance case.")
            if args.require_complete and set(cases) != set(CASES):
                raise ValueError("All five Windows acceptance cases are required.")
            print(json.dumps({record["case"]: record["status"] for record in records}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Acceptance verification failed: {error}\n")


if __name__ == "__main__":
    main()
