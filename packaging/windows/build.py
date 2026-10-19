"""Build a small Windows distribution from an explicit source allowlist."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import re
import shutil
import struct
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js", ".svg"}
HELP_NAMES = (
    "QUICKSTART",
    "USAGE",
    "WINDOWS",
    "TRAJECTORY-ANALYSIS",
    "RESEARCH-ANALYSIS",
    "PLOTTING",
    "RELEASE-CHECKLIST",
)
EXAMPLE_SUFFIXES = {".html", ".css", ".json", ".xvg", ".png", ".md", ".mdp", ".txt"}
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
NSIS_SIGNATURE = b"\xef\xbe\xad\xdeNullsoftInst"
MAX_INSTALLER_BYTES = 64 * 1024**2


def _nsis_archive(data: bytes, *, flags: int, max_decoded: int) -> tuple[bytes, bytes]:
    """Read this project's solid LZMA NSIS header and data, without extracting files."""
    start = data.find(NSIS_SIGNATURE) - 4
    if start < 0 or start + 28 > len(data):
        raise ValueError("NSIS archive header is missing")
    actual_flags, _, _, _, _, header_size, archive_size = struct.unpack_from("<7I", data, start)
    if actual_flags != flags or not 0 < header_size <= 1024**2:
        raise ValueError("Unexpected NSIS archive flags or header size")
    if archive_size < 37 or start + archive_size > len(data):
        raise ValueError("Invalid NSIS archive size")
    # The archive ends with a four-byte CRC. Solid LZMA starts with five properties bytes.
    compressed = data[start + 28 : start + archive_size - 4]
    properties = compressed[0]
    dictionary = int.from_bytes(compressed[1:5], "little")
    if properties >= 9 * 5 * 5 or not 4096 <= dictionary <= 64 * 1024**2:
        raise ValueError("Unsupported NSIS LZMA properties")
    remainder, lc = divmod(properties, 9)
    pb, lp = divmod(remainder, 5)
    try:
        decoder = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dictionary, "lc": lc, "lp": lp, "pb": pb}],
        )
        decoded = decoder.decompress(compressed[5:], max_length=max_decoded + 1)
    except lzma.LZMAError as exc:
        raise ValueError("Invalid NSIS solid LZMA stream") from exc
    if len(decoded) > max_decoded:
        raise ValueError("NSIS decompressed data exceeds the verification limit")
    if not decoder.eof or decoder.unused_data or len(decoded) < 4 + header_size:
        raise ValueError("Incomplete NSIS solid LZMA stream")
    if struct.unpack_from("<I", decoded)[0] != header_size:
        raise ValueError("NSIS decoded header size does not match")
    return decoded[4 : 4 + header_size], decoded[4 + header_size :]


def verify_installer_text(installer: Path) -> None:
    """Verify every custom Chinese LangString in the compiled install/uninstall headers."""
    # This intentionally supports only our unsigned, solid LZMA Unicode NSIS build.
    with installer.open("rb") as stream:
        data = stream.read(MAX_INSTALLER_BYTES + 1)
    if len(data) > MAX_INSTALLER_BYTES:
        raise ValueError("Installer exceeds the verification size limit")
    install_header, payload = _nsis_archive(data, flags=0, max_decoded=32 * 1024**2)
    uninstall_header, _ = _nsis_archive(payload, flags=1, max_decoded=2 * 1024**2)
    strings = re.findall(
        r'^LangString\s+(\w+)\s+\$\{LANG_SIMPCHINESE\}\s+"(.*)"$',
        (ASSETS / "installer.nsi").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not strings:
        raise ValueError("Installer source has no custom Chinese LangStrings")
    missing = []
    for name, literal in strings:
        literal = literal.replace(r"$\r", "\r").replace(r"$\n", "\n")
        if "$" in literal:
            raise ValueError(f"Unsupported NSIS escape in Chinese LangString: {name}")
        header = uninstall_header if name == "KeepEnvironment" else install_header
        if literal.encode("utf-16-le") + b"\0\0" not in header:
            missing.append(name)
    if missing:
        raise ValueError("Compiled Chinese installer text is missing or corrupted: " + ", ".join(missing))


def source_files(root: Path) -> list[Path]:
    files = [root / "requirements.txt", root / "run.sh", root / "scripts/__init__.py", root / "scripts/md_plot.py"]
    files += [
        path for path in (root / "app").rglob("*") if path.is_file() and path.suffix in SOURCE_SUFFIXES and "__pycache__" not in path.parts
    ]
    files += [path for path in (root / "mdp").rglob("*.mdp") if path.name in MDP_NAMES]
    files += [root / name for name in (*FONT_SOURCES, *LEGAL_SOURCES) if (root / name).is_file()]
    files += [root / "docs" / f"{name}.md" for name in HELP_NAMES if (root / "docs" / f"{name}.md").is_file()]
    files += [path for path in (root / "app/static/examples").rglob("*") if path.is_file() and path.suffix in EXAMPLE_SUFFIXES]
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


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
        for reference in re.findall(r"""(?:url\(\s*["']?|(?:src|href)=["'])(/static/[^\s"'<>\)]+)""", path.read_text(encoding="utf-8")):
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
    for name in (*FONT_SOURCES, *LEGAL_SOURCES, *(f"docs/{name}.md" for name in HELP_NAMES), "app/static/examples/index.html"):
        if not (ROOT / name).is_file():
            raise ValueError(f"Required distribution asset or license is missing: {name}")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output.mkdir(parents=True, exist_ok=True)
    staging = output / "windows-payload"
    staging.mkdir(exist_ok=True)
    payload = staging / "application.tar.gz"
    write_payload(ROOT, payload)
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (staging / "application.sha256").write_text(digest + "  application.tar.gz\n", encoding="ascii", newline="\n")
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
    (staging / "Start.cmd").write_bytes((ASSETS / "Start.cmd").read_text(encoding="ascii").replace("\n", "\r\n").encode("ascii"))
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
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    portable = output / f"GromacsConsole-{version}-windows-x64.zip"
    with zipfile.ZipFile(portable, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in DISTRIBUTION_FILES:
            path = staging / name
            archive.write(path, f"GromacsConsole/{path.name}")
    artifacts = [portable]
    if compiler:
        installer = (output / f"GromacsConsole-{version}-windows-x64-setup.exe").resolve()
        option_prefix = "/" if Path(compiler).suffix.lower() == ".exe" else "-"
        subprocess.run(
            [
                compiler,
                # Unicode True controls output; source decoding still needs an explicit charset on Windows.
                f"{option_prefix}INPUTCHARSET",
                "UTF8",
                f"{option_prefix}DPAYLOAD_DIR={staging.resolve()}",
                f"{option_prefix}DOUTPUT_FILE={installer}",
                f"{option_prefix}DAPP_VERSION={version}",
                str(ASSETS / "installer.nsi"),
            ],
            check=True,
        )
        verify_installer_text(installer)
        artifacts.append(installer)
    for artifact in artifacts:
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_suffix(artifact.suffix + ".sha256").write_text(f"{checksum}  {artifact.name}\n", encoding="ascii", newline="\n")
        print(f"{artifact} ({artifact.stat().st_size:,} bytes)")
    return artifacts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--makensis", help="Optional NSIS compiler; produces a real setup.exe")
    mode.add_argument("--verify-installer", type=Path, help="Check compiled Chinese text without running the installer")
    options = parser.parse_args()
    if options.verify_installer:
        verify_installer_text(options.verify_installer)
        print(f"Verified installer and uninstaller Chinese text: {options.verify_installer}")
    else:
        build(options.output.resolve(), options.makensis)
