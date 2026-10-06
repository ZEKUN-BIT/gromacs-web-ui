"""Install a verified, portable GROMACS runtime without compilers or drivers."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VERSION = "2026.3"
GROMACS_URL = "https://conda.anaconda.org/conda-forge/linux-64/gromacs-2026.3-nompi_cuda_h39c90b0_0.conda"
GROMACS_SHA256 = "f29842f556ff1bc351f3a64bbaebc2c010574912263f7cc136107f94d6451913"
CPU_URL = "https://conda.anaconda.org/conda-forge/linux-64/gromacs-2026.3-nompi_h26635d9_100.conda"
CPU_SHA256 = "7a8f22f6254e60b0d151d43ccfb599af7a6c5141ee7e9926a50614a7b8a0b827"
CUFFT_URL = (
    "https://files.pythonhosted.org/packages/95/f4/61e6996dd20481ee834f57a8e9dca28b1869366a135e0d42e2aa8493bdd4/"
    "nvidia_cufft_cu12-11.4.1.4-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
)
CUFFT_SHA256 = "c67884f2a7d276b4b80eb56a79322a95df592ae5e765cf1243693365ccab4e28"
MINIMUM_CUDA_DRIVER = 12090
NVJITLINK_URL = (
    "https://files.pythonhosted.org/packages/46/0c/c75bbfb967457a0b7670b8ad267bfc4fffdf341c074e0a80db06c24ccfd4/"
    "nvidia_nvjitlink_cu12-12.9.86-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"
)
NVJITLINK_SHA256 = "e3f1171dbdc83c5932a45f0f4c99180a70de9bd2718c1ab77d14104f6d7147f9"
NVRTC_URL = (
    "https://files.pythonhosted.org/packages/b8/85/e4af82cc9202023862090bfca4ea827d533329e925c758f0cde964cb54b7/"
    "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-manylinux2010_x86_64.manylinux_2_12_x86_64.whl"
)
NVRTC_SHA256 = "210cf05005a447e29214e9ce50851e83fc5f4358df8b453155d5e1918094dcb4"
RUNTIME_MANIFEST = {
    "version": VERSION,
    "gromacs_sha256": GROMACS_SHA256,
    "cufft_sha256": CUFFT_SHA256,
    "nvjitlink_sha256": NVJITLINK_SHA256,
    "nvrtc_sha256": NVRTC_SHA256,
}
DOWNLOAD_SIZES = {
    GROMACS_URL: 62875131,
    CPU_URL: 35242981,
    CUFFT_URL: 200877592,
    NVJITLINK_URL: 39748338,
    NVRTC_URL: 89568129,
}
DOWNLOAD_PROGRESS_INTERVAL = 10
DOWNLOAD_RETRY_DELAY = 2
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_POLL_INTERVAL = 0.5
DOWNLOAD_TIMEOUT = 3600
DOWNLOAD_TIMEOUT_ENV = "GROMACS_CONSOLE_DOWNLOAD_TIMEOUT_SECONDS"
DOWNLOAD_OUTPUT_LOCK = threading.Lock()


def probe_cuda(loader=ctypes.CDLL) -> dict:
    """Query the Windows-provided WSL CUDA driver, rather than guessing a GPU model."""
    result = {"backend": "CPU", "version": VERSION, "devices": [], "cuda_driver_version": 0}
    if os.environ.get("CUDA_VISIBLE_DEVICES") in {"", "-1"}:
        result["reason"] = "CUDA_VISIBLE_DEVICES disables GPU access for this installation."
        return result
    try:
        library = "/usr/lib/wsl/lib/libcuda.so.1" if Path("/usr/lib/wsl/lib/libcuda.so.1").exists() else "libcuda.so.1"
        cuda = loader(library)
    except OSError:
        result["reason"] = "No NVIDIA CUDA driver is available in WSL. AMD/Intel GPUs use the CPU backend."
        return result
    code = cuda.cuInit(0)
    if code != 0:
        result["reason"] = f"CUDA initialization failed ({code}). Check the Windows NVIDIA driver and WSL GPU support."
        return result
    driver = ctypes.c_int()
    if cuda.cuDriverGetVersion(ctypes.byref(driver)) != 0:
        result["reason"] = "The CUDA driver version could not be queried."
        return result
    result["cuda_driver_version"] = driver.value
    if driver.value < MINIMUM_CUDA_DRIVER:
        result["reason"] = "Update the Windows NVIDIA driver: this GPU runtime requires CUDA driver API 12.9 or newer."
        return result
    count = ctypes.c_int()
    if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0 or count.value < 1:
        result["reason"] = "No CUDA device is visible inside WSL. Check the Windows driver and WSL2."
        return result
    for ordinal in range(count.value):
        device, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        name = ctypes.create_string_buffer(256)
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        if cuda.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), device) != 0:
            continue
        if major.value < 6:
            continue  # NVIDIA's WSL CUDA support starts at Pascal.
        if cuda.cuDeviceGetName(name, len(name), device) != 0:
            continue
        result["devices"].append(
            {"index": ordinal, "name": name.value.decode("utf-8", errors="replace"), "compute_capability": f"{major.value}.{minor.value}"}
        )
    if not result["devices"]:
        result["reason"] = "No supported CUDA GPU was found. WSL CUDA requires NVIDIA Pascal (compute capability 6.0) or newer."
        return result
    result.update(backend="CUDA", reason="A compatible CUDA driver and GPU were detected; GPU runtime validation is pending.")
    return result


def download_message(message: str) -> None:
    with DOWNLOAD_OUTPUT_LOCK:
        print(message, flush=True)


def download_deadline(timeout_seconds: float | None = None) -> float:
    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get(DOWNLOAD_TIMEOUT_ENV, DOWNLOAD_TIMEOUT))
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(f"Download timeout must be a positive finite number of seconds ({DOWNLOAD_TIMEOUT_ENV}).")
    return time.monotonic() + timeout_seconds


def check_download_state(target: Path, cancellation: threading.Event, deadline: float) -> float:
    if cancellation.is_set():
        raise RuntimeError(f"Download {target.name} cancelled after another package failed.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"Download {target.name} exceeded the total download time budget. "
            f"Increase --download-timeout or {DOWNLOAD_TIMEOUT_ENV} for a slow connection."
        )
    return remaining


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def download_verified(
    url: str, digest: str, target: Path, *, cancellation: threading.Event | None = None, deadline: float | None = None
) -> None:
    cancellation = cancellation if cancellation is not None else threading.Event()
    deadline = deadline if deadline is not None else download_deadline()
    check_download_state(target, cancellation, deadline)
    expected = DOWNLOAD_SIZES.get(url)
    size_label = f" ({expected / 1048576:.1f} MiB)" if expected else ""
    download_message(f"Downloading {target.name}{size_label}...")
    # Only retry partial files created by this call, never retain a download cache.
    target.unlink(missing_ok=True)
    started = time.monotonic()
    for attempt in range(DOWNLOAD_ATTEMPTS):
        remaining = check_download_state(target, cancellation, deadline)
        previous_size = target.stat().st_size if target.exists() else 0
        previous_time = time.monotonic()
        next_progress = previous_time + DOWNLOAD_PROGRESS_INTERVAL
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--continue-at",
            "-",
            "--connect-timeout",
            "20",
            "--max-time",
            f"{min(1800, remaining):.3f}",
            "--speed-limit",
            "1024",
            "--speed-time",
            "60",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--tlsv1.2",
            url,
            "-o",
            str(target),
        ]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                while True:
                    remaining = check_download_state(target, cancellation, deadline)
                    try:
                        code = process.wait(timeout=min(DOWNLOAD_POLL_INTERVAL, max(0.001, next_progress - time.monotonic()), remaining))
                        break
                    except subprocess.TimeoutExpired:
                        now = time.monotonic()
                        remaining = check_download_state(target, cancellation, deadline)
                        if now < next_progress:
                            continue
                        current_size = target.stat().st_size if target.exists() else 0
                        rate = max(0, current_size - previous_size) / max(now - previous_time, 0.001) / 1048576
                        downloaded = f"{current_size / 1048576:.1f}"
                        if expected:
                            downloaded += f"/{expected / 1048576:.1f} MiB ({min(100, current_size / expected * 100):.0f}%)"
                        else:
                            downloaded += " MiB"
                        download_message(
                            f"{target.name}: {downloaded}, {rate:.2f} MiB/s, {now - started:.0f}s elapsed, "
                            f"{remaining:.0f}s remaining in the download budget."
                        )
                        previous_size, previous_time = current_size, now
                        next_progress = now + DOWNLOAD_PROGRESS_INTERVAL
            finally:
                stop_process(process)
            errors.seek(0)
            error_text = errors.read().decode("utf-8", errors="replace").strip()[-1000:]
        if code == 0:
            break
        restart = code == 33 or (code == 22 and "error: 416" in error_text)
        if restart:
            target.unlink(missing_ok=True)
        transient_http = code == 22 and any(f"error: {status}" in error_text for status in (408, 429, 500, 502, 503, 504))
        retryable = code in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92} or restart or transient_http
        cause = "connection timed out or download stalled below 1 KiB/s for 60 seconds" if code == 28 else f"curl error {code}"
        if retryable and attempt + 1 < DOWNLOAD_ATTEMPTS:
            resume_size = target.stat().st_size if target.exists() else 0
            action = (
                "resume was rejected; restarting this temporary download" if restart else f"resuming at {resume_size / 1048576:.1f} MiB"
            )
            download_message(f"{target.name}: {cause}; retry {attempt + 2}/{DOWNLOAD_ATTEMPTS}, {action}.")
            remaining = check_download_state(target, cancellation, deadline)
            cancellation.wait(min(DOWNLOAD_RETRY_DELAY, remaining))
            continue
        raise RuntimeError(f"Download {target.name} failed after {attempt + 1} attempt(s): {cause}. {error_text}")
    check_download_state(target, cancellation, deadline)
    with target.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    check_download_state(target, cancellation, deadline)
    if actual != digest:
        raise ValueError(f"SHA-256 mismatch for {target.name}; refusing to install.")
    download_message(f"Verified {target.name}.")


def download_all(packages: list[tuple[str, str, Path]], *, timeout_seconds: float | None = None) -> None:
    # No extraction starts until every independent download has passed SHA-256.
    cancellation = threading.Event()
    deadline = download_deadline(timeout_seconds)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(download_verified, *package, cancellation=cancellation, deadline=deadline) for package in packages]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            cancellation.set()
            for future in futures:
                future.cancel()
            # The executor waits for running workers to reap their own curl
            # processes before install() can remove any partial downloads.
            raise


def probe_cuda_safely() -> dict:
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe-only"], check=True, capture_output=True, text=True, timeout=20
        )
        return json.loads(process.stdout)
    except (subprocess.SubprocessError, ValueError) as error:
        return {
            "backend": "CPU",
            "version": VERSION,
            "devices": [],
            "cuda_driver_version": 0,
            "reason": f"CUDA driver probing failed or timed out: {error}",
        }


def extract_gromacs(package: Path, destination: Path) -> None:
    # Keep a portable SSE2 build; AVX variants duplicate the same CUDA kernels.
    with zipfile.ZipFile(package) as archive:
        components = [name for name in archive.namelist() if name.startswith("pkg-") and name.endswith(".tar.zst")]
        if len(components) != 1:
            raise ValueError("Invalid GROMACS conda archive.")
        # Feed the compressed ZIP member straight into zstd. A writer thread
        # keeps both pipes flowing without a second compressed file on disk.
        with archive.open(components[0]) as source, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["zstd", "--decompress", "--stdout"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            feed_errors = []

            def feed() -> None:
                try:
                    shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
                except Exception as error:
                    feed_errors.append(error)
                finally:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass

            writer = threading.Thread(target=feed, name="gromacs-conda-stream")
            try:
                writer.start()
                with tarfile.open(fileobj=process.stdout, mode="r|") as unpacked:
                    for member in unpacked:
                        if member.name.split("/", 1)[0] in {"bin.SSE2", "lib.SSE2", "share", "info"}:
                            unpacked.extract(member, destination, filter="data")
                # tar stops at its end marker; drain any remaining padding so
                # zstd can finish instead of blocking on a full output pipe.
                while process.stdout.read(1024 * 1024):
                    pass
                code = process.wait(timeout=30)
                if code != 0:
                    errors.seek(0)
                    detail = errors.read().decode("utf-8", errors="replace").strip()[-1000:]
                    raise ValueError(f"GROMACS decompression failed (exit {code}): {detail}")
            finally:
                # Terminating only this decompressor releases a blocked writer
                # before the ZIP stream or staged installation is removed.
                stop_process(process)
                process.stdout.close()
                if writer.ident is not None:
                    writer.join()
                else:
                    process.stdin.close()
            if feed_errors:
                raise ValueError(f"GROMACS conda archive streaming failed: {feed_errors[0]}") from feed_errors[0]


def extract_cuda_library(wheel: Path, destination: Path, component: str, required: str) -> None:
    library_dir = destination / "lib"
    library_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        library_names = [
            name
            for name in archive.namelist()
            if Path(name).parent.as_posix() == f"nvidia/{component}/lib" and Path(name).name.startswith("lib")
        ]
        if f"nvidia/{component}/lib/{required}" not in library_names:
            raise ValueError(f"{component} wheel does not contain the required CUDA runtime.")
        for name in library_names:
            with archive.open(name) as source, (library_dir / Path(name).name).open("wb") as target:
                shutil.copyfileobj(source, target)
        licenses = destination / "share" / "licenses" / component
        licenses.mkdir(parents=True, exist_ok=True)
        for name in archive.namelist():
            if "/" in name and Path(name).name.startswith(("LICENSE", "License")):
                (licenses / Path(name).name).write_bytes(archive.read(name))


def write_wrapper(prefix: Path) -> None:
    (prefix / "bin").mkdir()
    wrapper = prefix / "bin" / "gmx"
    wrapper.write_text(
        '#!/bin/sh\nset -eu\nprefix=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        'export GMXLIB="$prefix/share/gromacs/top"\n'
        'export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'exec "$prefix/bin.SSE2/gmx" "$@"\n'
    )
    wrapper.chmod(0o755)


def validate_runtime(prefix: Path, work: Path, device: int | None) -> None:
    gmx = str(prefix / "bin" / "gmx")
    version = subprocess.run([gmx, "--version"], check=True, capture_output=True, text=True, timeout=30)
    print(version.stdout, flush=True)
    details = {key.strip(): value.strip() for line in version.stdout.splitlines() if ":" in line for key, value in [line.split(":", 1)]}
    release = details.get("GROMACS version", "")
    if (release != VERSION and not release.startswith(VERSION + "-")) or details.get("Precision") != "mixed":
        raise ValueError("Installed GROMACS must be version 2026.3 with mixed precision.")
    if device is not None and details.get("GPU support") != "CUDA":
        raise ValueError("Installed GROMACS does not report CUDA support.")
    if not (prefix / "share/gromacs/top/amber19sb.ff").is_dir():
        raise ValueError("The required amber19sb force field is missing.")
    smoke = Path(tempfile.mkdtemp(prefix="runtime-smoke.", dir=work))
    run_smoke_stage([gmx, "solvate", "-cs", "spc216.gro", "-box", "1.8", "1.8", "1.8", "-o", "water.gro"], smoke, "solvate", 60)
    atoms = int((smoke / "water.gro").read_text().splitlines()[1])
    (smoke / "water.top").write_text(
        '#include "amber19sb.ff/forcefield.itp"\n#include "amber19sb.ff/tip3p.itp"\n'
        f"[ system ]\nRuntime installation check\n[ molecules ]\nSOL {atoms // 3}\n"
    )
    (smoke / "check.mdp").write_text(
        "integrator = md\ndt = 0.001\nnsteps = 2\ncutoff-scheme = Verlet\n"
        "coulombtype = PME\nrcoulomb = 0.6\nrvdw = 0.6\nrlist = 0.6\n"
        "constraints = h-bonds\ntcoupl = no\npcoupl = no\ngen-vel = yes\ngen-temp = 300\ngen-seed = 20261005\n"
    )
    run_smoke_stage([gmx, "grompp", "-f", "check.mdp", "-c", "water.gro", "-p", "water.top", "-o", "check.tpr"], smoke, "grompp", 60)
    backend = "gpu" if device is not None else "cpu"
    arguments = [
        gmx,
        "mdrun",
        "-s",
        "check.tpr",
        "-deffnm",
        "check",
        "-nb",
        backend,
        "-pme",
        backend,
        "-pmefft",
        backend,
        "-ntmpi",
        "1",
        "-ntomp",
        "1",
    ]
    if device is not None:
        arguments += ["-gpu_id", str(device)]
    run_smoke_stage(arguments, smoke, "mdrun with GPU PME/FFT" if device is not None else "mdrun on CPU", 120)


def run_smoke_stage(arguments: list[str], work: Path, stage: str, timeout: int) -> None:
    print(f"Validating GROMACS: {stage} (up to {timeout}s)...", flush=True)
    try:
        subprocess.run(arguments, cwd=work, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"GROMACS validation stage '{stage}' timed out after {timeout}s.") from error


def replace_prefix(staged: Path, prefix: Path) -> None:
    backup = prefix.with_name(prefix.name + ".previous")
    if backup.exists():
        raise ValueError(f"Previous installation backup exists: {backup}. Refusing to overwrite it.")
    existed = prefix.exists()
    if existed:
        prefix.rename(backup)
    try:
        staged.rename(prefix)
    except BaseException:
        if existed:
            backup.rename(prefix)
        raise
    if existed:
        shutil.rmtree(backup)


def install(prefix: Path, work: Path, report: dict, *, download_timeout: float | None = None) -> None:
    manifest = prefix / ".cuda-runtime.json"
    try:
        reusable = manifest.is_file() and json.loads(manifest.read_text()) == RUNTIME_MANIFEST
    except ValueError:
        reusable = False
    if reusable and (prefix / ".ready").is_file():
        print("Reusing the installed CUDA runtime; checking GPU execution before skipping downloads.", flush=True)
        validate_runtime(prefix, work, report["devices"][0]["index"])
        return
    with tempfile.TemporaryDirectory(prefix="gpu-runtime.", dir=prefix.parent) as temporary:
        staged = Path(temporary) / "gromacs"
        staged.mkdir()
        gromacs, cufft = work / "gromacs-gpu.conda", work / "cufft.whl"
        nvjitlink, nvrtc = work / "nvjitlink.whl", work / "nvrtc.whl"
        try:
            print("Installing GROMACS CUDA runtime for the detected GPU; no CUDA SDK or Linux NVIDIA driver is installed.", flush=True)
            download_all(
                [
                    (GROMACS_URL, GROMACS_SHA256, gromacs),
                    (CUFFT_URL, CUFFT_SHA256, cufft),
                    (NVJITLINK_URL, NVJITLINK_SHA256, nvjitlink),
                    (NVRTC_URL, NVRTC_SHA256, nvrtc),
                ],
                timeout_seconds=download_timeout,
            )
            # Release each verified archive as soon as extraction succeeds so
            # the complete runtime and all downloads do not occupy disk together.
            extract_gromacs(gromacs, staged)
            gromacs.unlink(missing_ok=True)
            extract_cuda_library(cufft, staged, "cufft", "libcufft.so.11")
            cufft.unlink(missing_ok=True)
            extract_cuda_library(nvjitlink, staged, "nvjitlink", "libnvJitLink.so.12")
            nvjitlink.unlink(missing_ok=True)
            extract_cuda_library(nvrtc, staged, "cuda_nvrtc", "libnvrtc.so.12")
            nvrtc.unlink(missing_ok=True)
            write_wrapper(staged)
            validate_runtime(staged, work, report["devices"][0]["index"])
            (staged / ".ready").touch()
            (staged / ".backend").write_text("CUDA\n")
            (staged / ".cuda-runtime.json").write_text(json.dumps(RUNTIME_MANIFEST) + "\n")
            replace_prefix(staged, prefix)
        finally:
            gromacs.unlink(missing_ok=True)
            cufft.unlink(missing_ok=True)
            nvjitlink.unlink(missing_ok=True)
            nvrtc.unlink(missing_ok=True)


def install_cpu(prefix: Path, work: Path, *, download_timeout: float | None = None) -> None:
    if (prefix / ".ready").is_file() and (prefix / "bin/gmx").is_file():
        try:
            validate_runtime(prefix, work, None)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            print(f"Existing runtime cannot be reused: {error}", flush=True)
        else:
            print("Reusing the installed GROMACS 2026.3 runtime for CPU execution; skipping downloads.", flush=True)
            return
    with tempfile.TemporaryDirectory(prefix="cpu-runtime.", dir=prefix.parent) as temporary:
        staged = Path(temporary) / "gromacs"
        staged.mkdir()
        package = work / "gromacs-cpu.conda"
        try:
            print("Installing prebuilt mixed-precision CPU GROMACS 2026.3; no source compilation is needed.", flush=True)
            download_verified(CPU_URL, CPU_SHA256, package, deadline=download_deadline(download_timeout))
            extract_gromacs(package, staged)
            package.unlink(missing_ok=True)
            write_wrapper(staged)
            validate_runtime(staged, work, None)
            (staged / ".ready").touch()
            (staged / ".backend").write_text("CPU\n")
            (staged / ".cpu-runtime.json").write_text(json.dumps({"version": VERSION, "gromacs_sha256": CPU_SHA256}) + "\n")
            replace_prefix(staged, prefix)
        finally:
            package.unlink(missing_ok=True)


def main() -> int:
    def terminate(signum, _frame):
        # Unwind download/extraction cleanup when the launcher is closed. Each
        # worker reaps only its own child before temporary files are removed.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--cpu", action="store_true", help="Install/reuse prebuilt GROMACS for CPU execution")
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--download-timeout", type=float, help=f"Total download budget in seconds (default {DOWNLOAD_TIMEOUT}; env {DOWNLOAD_TIMEOUT_ENV})"
    )
    options = parser.parse_args()
    if options.download_timeout is not None and (not math.isfinite(options.download_timeout) or options.download_timeout <= 0):
        parser.error("--download-timeout must be a positive finite number of seconds")
    if options.probe_only:
        print(json.dumps(probe_cuda()))
        return 0
    if options.prefix is None or options.work_dir is None:
        parser.error("--prefix and --work-dir are required for installation")
    if options.cpu:
        try:
            install_cpu(options.prefix, options.work_dir, download_timeout=options.download_timeout)
        except Exception as error:
            print(f"CPU installation failed; the previous runtime was preserved: {error}", flush=True)
            return 1
        return 0
    report = probe_cuda_safely()
    status = options.prefix.parent / "gpu-status.json"
    if report["backend"] != "CUDA":
        print("CPU backend: " + report["reason"], flush=True)
        status.write_text(json.dumps(report, ensure_ascii=False) + "\n")
        return 10
    try:
        install(options.prefix, options.work_dir, report, download_timeout=options.download_timeout)
    except Exception as error:
        print(f"GPU installation failed; the previous GROMACS installation was preserved: {error}", flush=True)
        return 1
    report["reason"] = "CUDA runtime installed and a short MD simulation completed with GPU nonbonded and PME/FFT kernels."
    status.write_text(json.dumps(report, ensure_ascii=False) + "\n")
    print(report["reason"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
