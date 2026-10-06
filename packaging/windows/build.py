"""Build a small Windows distribution from an explicit source allowlist."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js", ".svg"}
FONT_SOURCES = ("app/static/fonts/ibm-plex-sans-var-latin.woff2", "app/static/fonts/jetbrains-mono-var-latin.woff2")
LEGAL_SOURCES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "app/static/licenses/project-MIT.txt",
    "app/static/icons/lucide/LICENSE",
    "app/static/fonts/IBM-Plex-OFL.txt",
    "app/static/fonts/JetBrains-Mono-OFL.txt",
)
ENVIRONMENT_FILES = (
    "linux-system.sh",
    "gpu_setup.py",
    "dependency_setup.py",
    "science-tools.py",
    "science-requirements.txt",
    "science-bootstrap-requirements.txt",
    "windows-python-requirements.lock",
)
MDP_NAMES = {"ions.mdp", "em_pre.mdp", "minim.mdp", "nvt.mdp", "npt.mdp", "md.mdp"}
DISTRIBUTION_FILES = (
    "application.tar.gz",
    "application.sha256",
    "Console.ps1",
    "WslSetup.ps1",
    "WslPrerequisites.ps1",
    "PowerShellHost.ps1",
    "ubuntu-rootfs.json",
    "Start.cmd",
    "GromacsConsole.ico",
    "linux-install.sh",
    "application_update.py",
    "linux-system.sh",
    "desktop_service.py",
    "gpu_setup.py",
    "dependency_setup.py",
    "science-tools.py",
    "science-requirements.txt",
    "science-bootstrap-requirements.txt",
    "windows-python-requirements.lock",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "release.json",
)


def source_files(root: Path) -> list[Path]:
    files = [root / "requirements.txt", root / "run.sh", root / "scripts/__init__.py", root / "scripts/md_plot.py"]
    files += [
        path for path in (root / "app").rglob("*") if path.is_file() and path.suffix in SOURCE_SUFFIXES and "__pycache__" not in path.parts
    ]
    files += [path for path in (root / "mdp").rglob("*.mdp") if path.name in MDP_NAMES]
    files += [root / name for name in (*FONT_SOURCES, *LEGAL_SOURCES) if (root / name).is_file()]
    return sorted(files)


def write_payload(root: Path, target: Path) -> None:
    files = source_files(root)
    included = {p.relative_to(root).as_posix() for p in files}
    for path in files:
        relative = path.relative_to(root)
        if any(root.joinpath(*relative.parts[:depth]).is_symlink() for depth in range(1, len(relative.parts) + 1)):
            raise ValueError(f"Refusing symlink in distribution: {path}")
    # Check the final allowlist, not just whether a resource exists in the checkout.
    for path in files:
        if path.suffix not in {".html", ".css"}:
            continue
        for reference in re.findall(r"""(?:url\(\s*["']?|(?:src|href)=["'])(/static/[^\s"'<>\)]+)""", path.read_text()):
            resource = "app" + reference.split("?", 1)[0].split("#", 1)[0]
            if resource not in included:
                raise ValueError(f"Packaged static resource is missing: {reference} (referenced by {path.relative_to(root)})")
    # Fixed metadata makes identical source trees produce identical archives.
    with target.open("wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in files:
                data = path.read_bytes()
                info = tarfile.TarInfo(path.relative_to(root).as_posix())
                info.size = len(data)
                info.mode = 0o755 if path.name == "run.sh" else 0o644
                archive.addfile(info, io.BytesIO(data))


def build(output: Path, compiler: str | None = None) -> list[Path]:
    for name in (*FONT_SOURCES, *LEGAL_SOURCES):
        if not (ROOT / name).is_file():
            raise ValueError(f"Required distribution asset or license is missing: {name}")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output.mkdir(parents=True, exist_ok=True)
    staging = output / "windows-payload"
    staging.mkdir(exist_ok=True)
    payload = staging / "application.tar.gz"
    write_payload(ROOT, payload)
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (staging / "application.sha256").write_text(digest + "  application.tar.gz\n", encoding="ascii")
    for name in (
        "Console.ps1",
        "WslSetup.ps1",
        "WslPrerequisites.ps1",
        "PowerShellHost.ps1",
        "ubuntu-rootfs.json",
        "Start.cmd",
        "GromacsConsole.ico",
        "linux-install.sh",
        "application_update.py",
        "linux-system.sh",
        "desktop_service.py",
        "gpu_setup.py",
        "dependency_setup.py",
        "science-tools.py",
        "science-requirements.txt",
        "science-bootstrap-requirements.txt",
        "windows-python-requirements.lock",
    ):
        shutil.copyfile(ASSETS / name, staging / name)
    (staging / "Start.cmd").write_bytes((ASSETS / "Start.cmd").read_text().replace("\n", "\r\n").encode("ascii"))
    shutil.copyfile(ROOT / "docs" / "WINDOWS.md", staging / "README.md")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, staging / name)
    installation_digest = hashlib.sha256(payload.read_bytes())
    for name in ("desktop_service.py", "linux-install.sh", "application_update.py", *ENVIRONMENT_FILES):
        installation_digest.update((staging / name).read_bytes())
    environment_digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes())
    for name in ENVIRONMENT_FILES:
        environment_digest.update(name.encode() + b"\0" + (staging / name).read_bytes() + b"\0")
    (staging / "release.json").write_text(
        json.dumps(
            {
                "version": version,
                "payload_sha256": digest,
                "installation_sha256": installation_digest.hexdigest(),
                "environment_sha256": environment_digest.hexdigest(),
            }
        )
        + "\n"
    )
    portable = output / f"GromacsConsole-{version}-windows-x64.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in DISTRIBUTION_FILES:
            path = staging / name
            archive.write(path, f"GromacsConsole/{path.name}")
    artifacts = [portable]
    if compiler:
        installer = (output / f"GromacsConsole-{version}-windows-x64-setup.exe").resolve()
        define_prefix = "/D" if Path(compiler).suffix.lower() == ".exe" else "-D"
        subprocess.run(
            [
                compiler,
                f"{define_prefix}PAYLOAD_DIR={staging.resolve()}",
                f"{define_prefix}OUTPUT_FILE={installer}",
                f"{define_prefix}APP_VERSION={version}",
                str(ASSETS / "installer.nsi"),
            ],
            check=True,
        )
        artifacts.append(installer)
    for artifact in artifacts:
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_suffix(artifact.suffix + ".sha256").write_text(f"{checksum}  {artifact.name}\n")
        print(f"{artifact} ({artifact.stat().st_size:,} bytes)")
    return artifacts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--makensis", help="Optional NSIS compiler; produces a real setup.exe")
    options = parser.parse_args()
    build(options.output.resolve(), options.makensis)
