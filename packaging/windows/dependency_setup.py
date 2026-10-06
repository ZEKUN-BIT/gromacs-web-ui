"""Install Python/scientific dependencies once; reuse verified unchanged environments offline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

MARKER = ".dependencies-ready.json"
SCIENCE_FILES = ("science-bootstrap-requirements.txt", "science-requirements.txt", "science-tools.py")
LOCK_NAME = "windows-python-requirements.lock"
PIP_STAGE_TIMEOUT = 1800


class PythonDependencyError(RuntimeError):
    """Installed Python packages are missing or inconsistent."""


class NativeToolError(RuntimeError):
    """A native scientific tool or its operating-system runtime failed."""


class VerificationIncompleteError(RuntimeError):
    """Verification could not finish; package corruption has not been established."""


def bounded_detail(*outputs: str | bytes | None, limit: int = 3000) -> str:
    return "\n".join(
        output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output for output in outputs if output
    ).strip()[-limit:]


def inputs(payload: Path) -> tuple[bytes, str]:
    with tarfile.open(payload / "application.tar.gz", "r:gz") as archive:
        member = archive.getmember("requirements.txt")
        if not member.isfile() or member.size > 65536:
            raise RuntimeError("Invalid packaged application requirements.")
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError("Packaged application requirements are missing.")
        requirements = stream.read()
    digest = hashlib.sha256()
    for name, content in [("requirements.txt", requirements)] + [(name, (payload / name).read_bytes()) for name in SCIENCE_FILES]:
        digest.update(name.encode() + b"\0" + content + b"\0")
    lock = (payload / LOCK_NAME).read_bytes()
    source_digest = hashlib.sha256()
    for name, content in [("requirements.txt", requirements)] + [(name, (payload / name).read_bytes()) for name in SCIENCE_FILES[:2]]:
        source_digest.update(name.encode() + b"\0" + content + b"\0")
    recorded = re.search(rb"^# source-input-sha256: ([0-9a-f]{64})$", lock, re.MULTILINE)
    if recorded is None or recorded.group(1).decode() != source_digest.hexdigest():
        raise RuntimeError(
            "Packaged Python dependency lock does not match its source requirements. Regenerate the Windows lock before building."
        )
    digest.update(LOCK_NAME.encode() + b"\0" + lock + b"\0")
    digest.update(Path(__file__).read_bytes())
    return requirements, digest.hexdigest()


def environment_fingerprint() -> str:
    packages = sorted((dist.metadata["Name"].lower().replace("_", "-"), dist.version) for dist in importlib.metadata.distributions())
    data = {"python": sys.version, "executable": str(Path(sys.executable).resolve()), "prefix": sys.prefix, "packages": packages}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def python_checks(app_root: Path) -> None:
    """Probe real imports independently of metadata and the readiness marker."""
    python = str(app_root / ".venv/bin/python")
    for command in (
        [python, "-m", "pip", "check"],
        [
            python,
            "-c",
            "import fastapi,uvicorn,multipart,numpy,matplotlib,MDAnalysis,Bio,acpype; "
            "import click,httptools,uvloop,yaml,watchfiles,websockets; from openbabel import openbabel",
        ],
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as error:
            detail = bounded_detail(error.stdout, error.stderr)
            raise VerificationIncompleteError(f"Python dependency check timed out; no package repair was attempted. {detail}") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise VerificationIncompleteError(f"Python dependency check could not run: {error}") from error
        if result.returncode:
            detail = bounded_detail(result.stdout, result.stderr)
            if any(
                message in detail
                for message in ("cannot open shared object file", "error while loading shared libraries", "undefined symbol")
            ):
                raise NativeToolError(f"A Python extension needs a native runtime repair: {detail}")
            raise PythonDependencyError(f"Python dependency check failed (exit {result.returncode}): {detail}")


def native_checks(app_root: Path) -> None:
    python = str(app_root / ".venv/bin/python")
    tools = app_root / "tools"
    if not tools.is_symlink() or not tools.resolve().is_relative_to((app_root / ".venv").resolve()):
        raise NativeToolError(
            "Bundled AmberTools link is missing or points outside the application environment. Rerun scientific verification to restore a missing link."
        )
    try:
        bundled = Path(importlib.metadata.distribution("acpype").locate_file("acpype/amber_linux")).resolve()
    except importlib.metadata.PackageNotFoundError as error:
        raise PythonDependencyError("ACPYPE package metadata is missing.") from error
    if tools.resolve() != bundled:
        raise NativeToolError(
            "The tools link does not point at ACPYPE's bundled AmberTools. Remove the incorrect link before rerunning setup."
        )
    for name in ("antechamber", "parmchk2", "tleap", "sqm"):
        if not os.access(tools / "bin" / name, os.X_OK):
            raise NativeToolError(f"Missing scientific tool: {name}. Repair the ACPYPE wheel's bundled AmberTools files, then rerun setup.")
    for command, accepted_codes, expected_output in (
        (
            [
                python,
                "-c",
                "from openbabel import openbabel; version = openbabel.OBReleaseVersion(); assert version; print('Open Babel ' + version)",
            ],
            {0},
            "Open Babel",
        ),
        ([str(app_root / ".venv/bin/obabel"), "-V"], {0}, "Open Babel"),
        ([str(tools / "bin/antechamber"), "-h"], {0}, "Usage: antechamber"),
        ([str(tools / "bin/parmchk2"), "-h"], {0, 1}, "Usage: parmchk2"),
        ([str(tools / "bin/tleap"), "-h"], {0}, "teLeap"),
        ([str(tools / "bin/sqm"), "-h"], {0}, "sqm [-O]"),
        (["/usr/bin/mkdssp", "--version"], {0}, "mkdssp"),
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as error:
            detail = bounded_detail(error.stdout, error.stderr, limit=1500)
            raise VerificationIncompleteError(f"Scientific executable probe timed out: {command[0]}. {detail}") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise NativeToolError(
                f"Scientific executable probe could not run: {command[0]}: {error}. Repair the affected tool or Ubuntu runtime, then rerun setup."
            ) from error
        output = result.stdout + result.stderr
        if result.returncode not in accepted_codes or expected_output not in output:
            detail = output.strip()[-1500:]
            raise NativeToolError(
                f"Scientific executable probe failed: {Path(command[0]).name} (exit {result.returncode}). {detail}\n"
                "Repair the affected native tool or Ubuntu runtime (DSSP, liblapack3, libblas3, libgfortran5), then rerun setup."
            )


def offline_checks(app_root: Path) -> None:
    print("Checking installed Python dependencies and scientific executables offline.", flush=True)
    python_checks(app_root)
    native_checks(app_root)


def run_stage(command: list[str], *, timeout: float, stage: str) -> None:
    """Stream output, bound the whole stage and reap this stage's process group."""
    process = subprocess.Popen(command, start_new_session=True)
    try:
        result = process.wait(timeout=timeout)
    except BaseException as error:
        # pip and the ligand verifier can have children. Stopping only the parent
        # would let them retain installation locks or write into removed folders.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The parent may have exited before a descendant that ignored TERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if isinstance(error, subprocess.TimeoutExpired):
            raise VerificationIncompleteError(
                f"{stage} exceeded its {timeout:g}-second total time limit. Rerun setup to resume; inspect the output above."
            ) from error
        raise
    if result:
        raise subprocess.CalledProcessError(result, command)


def stage_is_satisfied(command: list[str], requirement: Path, report: Path) -> bool:
    """Only pip's successful, empty offline installation plan proves satisfaction."""
    # --no-index alone still honors find-links from environment variables/config files.
    # os.devnull disables all pip configuration files; only this offline child gets it.
    offline_environment = {name: value for name, value in os.environ.items() if not name.startswith("PIP_")}
    offline_environment["PIP_CONFIG_FILE"] = os.devnull
    try:
        result = subprocess.run(
            [*command, "--dry-run", "--no-index", "--report", str(report), "-r", str(requirement)],
            capture_output=True,
            text=True,
            timeout=60,
            env=offline_environment,
        )
        if result.returncode:
            return False
        plan = json.loads(report.read_text())
    except subprocess.TimeoutExpired as error:
        detail = bounded_detail(error.stdout, error.stderr)
        raise VerificationIncompleteError(f"Offline package plan timed out; no installation was attempted. {detail}") from error
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return isinstance(plan, dict) and isinstance(plan.get("install"), list) and not plan["install"]


def setup(app_root: Path, payload: Path, *, force_verify: bool = False) -> bool:
    """Return True for offline reuse, False when dependencies were installed/verified."""
    _, input_digest = inputs(payload)
    marker = app_root / MARKER
    fingerprint = environment_fingerprint()
    try:
        saved = json.loads(marker.read_text())
    except (OSError, ValueError):
        saved = {}
    reuse = saved == {"inputs": input_digest, "environment": fingerprint}
    python_bad = False
    initially_healthy = False
    try:
        # Metadata and marker files cannot detect a damaged installed .py/.so.
        # Probe imports even when a previous failed installation lost its marker.
        offline_checks(app_root)
    except PythonDependencyError as error:
        print(f"Python dependency verification needs attention: {error}", flush=True)
        python_bad = True
    except NativeToolError as error:
        print(f"Scientific tool verification needs attention: {error}", flush=True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Timeouts and launch failures establish no package corruption.
        marker.unlink(missing_ok=True)
        raise
    else:
        initially_healthy = True
        if reuse and not force_verify:
            print("Reusing verified Python and scientific dependencies; no package downloads or ligand smoke test needed.", flush=True)
            return True
    # A failed installation must not leave a marker claiming this environment is verified.
    marker.unlink(missing_ok=True)
    python = str(app_root / ".venv/bin/python")
    installed = False
    if not reuse or python_bad:
        print("Preparing Python dependencies and scientific tools.", flush=True)
        with tempfile.TemporaryDirectory(prefix="dependency-setup-", dir=app_root) as work:
            requirement = payload / LOCK_NAME
            command = [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--require-hashes",
            ]
            satisfied = stage_is_satisfied(command, requirement, Path(work) / "locked-plan.json")
            # A failed real import plus satisfied metadata proves a repair is
            # needed. A clean venv with missing packages takes a normal install.
            repaired = python_bad and satisfied
            if not satisfied or repaired:
                run_stage(
                    [*command, *(["--force-reinstall"] if repaired else []), "-r", str(requirement)],
                    timeout=PIP_STAGE_TIMEOUT,
                    stage="Locked Python dependency installation",
                )
                installed = True
                try:
                    python_checks(app_root)
                except PythonDependencyError:
                    if repaired:
                        raise
                    # Installing missing packages can expose pre-existing damage
                    # in another installed package. Permit one bounded repair.
                    run_stage(
                        [*command, "--force-reinstall", "-r", str(requirement)],
                        timeout=PIP_STAGE_TIMEOUT,
                        stage="Python dependency repair",
                    )
                    python_checks(app_root)
            else:
                print(f"Installed packages satisfy {requirement.name}; skipping package downloads.", flush=True)
    run_stage([python, str(payload / "science-tools.py"), "--app-root", str(app_root)], timeout=360, stage="Scientific tool verification")
    # If packages were unchanged and preflight was healthy, the real ligand
    # smoke already adds the needed coverage; avoid repeating all imports/probes.
    if installed or not initially_healthy:
        if not installed:
            # A native error during Python import can stop preflight before
            # later imports are checked. Do not publish readiness without them.
            python_checks(app_root)
        native_checks(app_root)
    verified = {"inputs": input_digest, "environment": environment_fingerprint()}
    with tempfile.NamedTemporaryFile(mode="w", prefix="dependency-marker-", dir=app_root, delete=False) as stream:
        temporary_marker = Path(stream.name)
        json.dump(verified, stream, sort_keys=True)
    try:
        os.replace(temporary_marker, marker)
    finally:
        temporary_marker.unlink(missing_ok=True)
    return False


def check_ready(app_root: Path, payload: Path) -> bool:
    """Read-only gate for application-only updates; never repair or download."""
    try:
        _, digest = inputs(payload)
        saved = json.loads((app_root / MARKER).read_text())
        if saved != {"inputs": digest, "environment": environment_fingerprint()}:
            raise RuntimeError("The dependency marker or environment does not match this release.")
        offline_checks(app_root)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Application-only update needs Configure or Update: {bounded_detail(str(error))}", file=sys.stderr, flush=True)
        return False
    return True


def main() -> int:
    def terminate(signum, _frame):
        # Closing the launcher must unwind run_stage so its pip/ligand children
        # are stopped too. SIGINT already raises KeyboardInterrupt by default.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--force-verify", action="store_true")
    parser.add_argument(
        "--check-ready", action="store_true", help="Check readiness without modifying files or downloading; exit 10 if not ready."
    )
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run dependency setup as the unprivileged application user.")
    app_root = args.app_root.resolve()
    if Path(sys.prefix).resolve() != (app_root / ".venv").resolve():
        parser.error("Run dependency setup with the application's virtual environment Python.")
    if args.check_ready:
        return 0 if check_ready(app_root, args.payload_dir.resolve()) else 10
    try:
        setup(app_root, args.payload_dir.resolve(), force_verify=args.force_verify)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Dependency setup failed: {error}. See the scientific tool output above.", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
