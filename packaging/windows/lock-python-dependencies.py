#!/usr/bin/env python3
"""Generate the Ubuntu 24.04 x64 / Python 3.12 dependency lock from a pip report.

Run with Python 3.12 on Linux x64 and --resolve to create a fresh pip report,
or pass an existing full report with --report. This generator only resolves
and writes a lock; it installs no packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = ("requirements.txt", "science-bootstrap-requirements.txt", "science-requirements.txt")
LOCK_NAME = "windows-python-requirements.lock"


def source_digest(requirements: bytes, assets: Path) -> str:
    digest = hashlib.sha256()
    for name, content in [("requirements.txt", requirements)] + [(name, (assets / name).read_bytes()) for name in SOURCE_FILES[1:]]:
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def render_lock(report: dict, requirements: bytes, assets: Path) -> str:
    environment = report.get("environment", {})
    if (
        environment.get("python_version") != "3.12"
        or environment.get("sys_platform") != "linux"
        or environment.get("platform_machine") not in {"x86_64", "AMD64"}
    ):
        raise ValueError("The report must resolve Linux x64 / Python 3.12 dependencies.")
    entries = {}
    metadata = {}
    hashes = {}
    for item in report.get("install", []):
        name = re.sub(r"[-_.]+", "-", item["metadata"]["name"]).lower()
        version = item["metadata"]["version"]
        download = item["download_info"]
        parsed = urlparse(download["url"])
        wheel = unquote(Path(parsed.path).name)
        if parsed.scheme != "https" or not wheel.endswith(".whl"):
            raise ValueError(f"{name} must be resolved to an HTTPS wheel.")
        # Ubuntu 24.04 has glibc 2.39; a report from a newer host must not
        # accidentally lock wheels requiring the host's newer glibc.
        if any(int(minor) > 39 for minor in re.findall(r"manylinux_2_(\d+)", wheel)):
            raise ValueError(f"{name}'s wheel needs a newer glibc than Ubuntu 24.04.")
        if "linux" in wheel and "manylinux" not in wheel:
            raise ValueError(f"{name}'s wheel does not declare portable manylinux compatibility.")
        _, _, _, tags = parse_wheel_filename(wheel)
        if not any(
            (tag.platform == "any" and tag.interpreter in {"py3", "py312"} and tag.abi == "none")
            or (
                tag.platform.startswith("manylinux")
                and tag.platform.endswith("_x86_64")
                and (
                    (tag.interpreter == "cp312" and tag.abi in {"cp312", "abi3", "none"})
                    or (tag.abi == "abi3" and re.fullmatch(r"cp3\d{1,2}", tag.interpreter) and int(tag.interpreter[3:]) <= 12)
                    or (tag.interpreter in {"py3", "py312"} and tag.abi == "none")
                )
            )
            for tag in tags
        ):
            raise ValueError(f"{name}'s wheel is incompatible with Ubuntu x64 / CPython 3.12.")
        sha256 = download["archive_info"].get("hashes", {}).get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or name in entries:
            raise ValueError(f"Invalid or duplicate hash entry for {name}.")
        extras = "[standard]" if name == "uvicorn" else ""
        entries[name] = f"{name}{extras}=={version} \\\n    --hash=sha256:{sha256}"
        metadata[name] = item["metadata"]
        hashes[name] = sha256
    if not entries:
        raise ValueError("The report must contain the full fresh-environment installation plan.")
    active_extras = {}

    def validate_requirement(requirement: Requirement, allowed_hashes: set[str] | None = None) -> None:
        name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        if name not in metadata or metadata[name]["version"] not in requirement.specifier:
            raise ValueError(f"The report is missing a compatible dependency: {requirement}.")
        if allowed_hashes and hashes[name] not in allowed_hashes:
            raise ValueError(f"The report's {name} hash differs from its fixed source manifest.")
        active_extras.setdefault(name, set()).update(requirement.extras)

    for content in [requirements] + [(assets / name).read_bytes() for name in SOURCE_FILES[1:]]:
        for line in content.decode().replace("\\\n", " ").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            allowed_hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
            requirement = Requirement(re.sub(r"\s*--hash=sha256:[0-9a-f]{64}", "", line).strip())
            if requirement.marker is None or requirement.marker.evaluate(environment):
                validate_requirement(requirement, allowed_hashes)
    # A report made without --ignore-installed can omit already-present
    # transitives. Refuse to produce an incomplete lock in that situation.
    previous = None
    while previous != {name: frozenset(extras) for name, extras in active_extras.items()}:
        previous = {name: frozenset(extras) for name, extras in active_extras.items()}
        for name, extras in list(previous.items()):
            for dependency in metadata[name].get("requires_dist", []):
                requirement = Requirement(dependency)
                if requirement.marker is None or any(
                    requirement.marker.evaluate({**environment, "extra": extra}) for extra in {"", *extras}
                ):
                    validate_requirement(requirement)
    return (
        "# Ubuntu 24.04 x64 / CPython 3.12. Generated; do not edit manually.\n"
        "# All application, bootstrap and scientific dependencies are resolved together.\n"
        f"# source-input-sha256: {source_digest(requirements, assets)}\n\n" + "\n\n".join(entries[name] for name in sorted(entries)) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--report", type=Path)
    choice.add_argument("--resolve", action="store_true", help="Resolve all source requirements together without installing packages.")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name(LOCK_NAME))
    args = parser.parse_args()
    requirements = (ROOT / "requirements.txt").read_bytes()
    assets = Path(__file__).parent
    if args.resolve:
        if sys.version_info[:2] != (3, 12) or sys.platform != "linux" or platform.machine() != "x86_64":
            parser.error("Resolve on Linux x64 with Python 3.12.")
        with tempfile.TemporaryDirectory(prefix="windows-python-lock-") as work:
            input_file = Path(work) / "requirements.in"
            report_file = Path(work) / "report.json"
            sources = [requirements.decode()] + [(assets / name).read_text() for name in SOURCE_FILES[1:]]
            input_file.write_text(re.sub(r"\s*--hash=sha256:[0-9a-f]{64}", "", "\n".join(sources).replace("\\\n", " ")))
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--dry-run",
                    "--ignore-installed",
                    "--only-binary=:all:",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--report",
                    str(report_file),
                    "-r",
                    str(input_file),
                ],
                check=True,
                timeout=1800,
            )
            report = json.loads(report_file.read_text())
    else:
        report = json.loads(args.report.read_text())
    args.output.write_text(render_lock(report, requirements, assets))


if __name__ == "__main__":
    main()
