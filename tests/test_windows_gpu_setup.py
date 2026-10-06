from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_gpu_setup", ROOT / "packaging/windows/gpu_setup.py")
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)


@pytest.fixture(autouse=True)
def isolated_cuda_visibility(monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


class FakeCuda:
    def __init__(self, *, driver=12090, capabilities=((8, 6),), init_code=0):
        self.driver = driver
        self.capabilities = capabilities
        self.init_code = init_code

    def cuInit(self, flags):
        return self.init_code

    def cuDriverGetVersion(self, version):
        version._obj.value = self.driver
        return 0

    def cuDeviceGetCount(self, count):
        count._obj.value = len(self.capabilities)
        return 0

    def cuDeviceGet(self, device, index):
        device._obj.value = index
        return 0

    def cuDeviceComputeCapability(self, major, minor, device):
        major._obj.value, minor._obj.value = self.capabilities[device.value]
        return 0

    def cuDeviceGetName(self, name, length, device):
        name.value = b"Detected NVIDIA GPU"
        return 0


@pytest.mark.parametrize(
    ("cuda", "reason"),
    [
        (FakeCuda(driver=12080), "Update the Windows"),
        (FakeCuda(init_code=100), "initialization failed"),
        (FakeCuda(capabilities=()), "No CUDA device"),
        (FakeCuda(capabilities=((5, 2),)), "No supported CUDA GPU"),
    ],
)
def test_driver_and_hardware_failures_cannot_claim_gpu_success(cuda, reason):
    report = gpu.probe_cuda(lambda _: cuda)
    assert report["backend"] == "CPU"
    assert reason in report["reason"]


def test_gpu_probe_uses_capabilities_and_handles_multiple_models():
    report = gpu.probe_cuda(lambda _: FakeCuda(capabilities=((5, 2), (8, 9), (12, 0))))
    assert report["backend"] == "CUDA"
    assert [(item["index"], item["compute_capability"]) for item in report["devices"]] == [(1, "8.9"), (2, "12.0")]


def test_explicitly_disabled_cuda_is_respected(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    report = gpu.probe_cuda(lambda _: pytest.fail("Disabled GPU must not load its driver"))
    assert report["backend"] == "CPU"
    assert "CUDA_VISIBLE_DEVICES" in report["reason"]


def test_probe_missing_driver_and_timeout_have_clear_cpu_reasons(monkeypatch):
    def missing(_):
        raise OSError("not found")

    assert "No NVIDIA CUDA driver" in gpu.probe_cuda(missing)["reason"]

    def hung(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 20)

    monkeypatch.setattr(gpu.subprocess, "run", hung)
    assert gpu.probe_cuda_safely()["backend"] == "CPU"
    assert "timed out" in gpu.probe_cuda_safely()["reason"]


def test_bad_download_hash_is_rejected_before_install(monkeypatch, tmp_path):
    target = tmp_path / "download"

    class CorruptDownload:
        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    def corrupt(*args, **kwargs):
        target.write_bytes(b"corrupted vendor package")
        return CorruptDownload()

    monkeypatch.setattr(gpu.subprocess, "Popen", corrupt)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        gpu.download_verified("https://vendor.example/package", "0" * 64, target)


def test_growing_download_reports_package_bytes_percentage_speed_and_elapsed(monkeypatch, tmp_path, capsys):
    target = tmp_path / "cufft.whl"
    data = b"a" * 1048576
    original_popen = subprocess.Popen
    commands = []
    producer = (
        "import pathlib,sys,time\n"
        "with pathlib.Path(sys.argv[1]).open('wb') as stream:\n"
        " for i in range(4):\n"
        "  stream.write(b'a'*262144); stream.flush(); time.sleep(0.04)\n"
    )

    def transfer(command, **kwargs):
        commands.append(command)
        return original_popen([sys.executable, "-c", producer, str(target)], **kwargs)

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_PROGRESS_INTERVAL", 0.02)
    monkeypatch.setattr(gpu, "DOWNLOAD_SIZES", {gpu.CUFFT_URL: len(data)})
    gpu.download_verified(gpu.CUFFT_URL, hashlib.sha256(data).hexdigest(), target)
    output = capsys.readouterr().out
    progress = [line for line in output.splitlines() if line.startswith("cufft.whl:")]
    assert len(progress) > 1
    sizes = [float(line.split(": ")[1].split("/")[0]) for line in progress]
    assert max(sizes) > min(sizes)
    assert all("MiB (" in line and "%" in line and "MiB/s" in line and "s elapsed" in line for line in progress)
    assert "Verified cufft.whl." in output
    assert target.read_bytes() == data
    assert commands[0][commands[0].index("--speed-limit") + 1] == "1024"
    assert commands[0][commands[0].index("--speed-time") + 1] == "60"
    assert commands[0][commands[0].index("--proto-redir") + 1] == "=https"


def test_stall_retries_are_explicit_bounded_and_resume_only_current_download(monkeypatch, tmp_path, capsys):
    target = tmp_path / "cufft.whl"
    target.write_bytes(b"stale bytes from before this call")
    attempts = []

    class StalledDownload:
        def wait(self, **kwargs):
            return 28

        def poll(self):
            return 28

    def transfer(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            assert not target.exists(), "Only partial files from this call may be resumed"
            target.write_bytes(b"this installation partial data")
        else:
            assert target.read_bytes() == b"this installation partial data"
        kwargs["stderr"].write(b"curl: (28) Operation too slow. Less than 1024 bytes/sec transferred the last 60 seconds\n")
        return StalledDownload()

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_RETRY_DELAY", 0)
    with pytest.raises(RuntimeError, match="failed after 4 attempt.*stalled"):
        gpu.download_verified(gpu.CUFFT_URL, "0" * 64, target)
    assert len(attempts) == 4
    assert all(command[command.index("--continue-at") + 1] == "-" for command in attempts)
    output = capsys.readouterr().out
    assert "retry 2/4" in output and "retry 4/4" in output
    assert "stalled below 1 KiB/s for 60 seconds" in output
    assert "Verified" not in output


def test_interrupted_transfer_resumes_and_hashes_the_complete_file(monkeypatch, tmp_path, capsys):
    target = tmp_path / "cufft.whl"
    data, calls = b"complete vendor archive", []

    class Transfer:
        def __init__(self, code):
            self.code = code

        def wait(self, **kwargs):
            return self.code

        def poll(self):
            return self.code

    def transfer(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            target.write_bytes(data[:8])
            return Transfer(28)
        assert target.read_bytes() == data[:8]
        with target.open("ab") as stream:
            stream.write(data[8:])
        return Transfer(0)

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_RETRY_DELAY", 0)
    gpu.download_verified(gpu.CUFFT_URL, hashlib.sha256(data).hexdigest(), target)
    assert len(calls) == 2
    assert "Verified cufft.whl." in capsys.readouterr().out


@pytest.mark.parametrize(("range_code", "range_error"), [(33, b""), (22, b"curl: (22) The requested URL returned error: 416")])
def test_resume_unsupported_restarts_only_that_temporary_file(monkeypatch, tmp_path, capsys, range_code, range_error):
    target = tmp_path / "cufft.whl"
    data, calls = b"complete vendor archive", []

    class Transfer:
        def __init__(self, code):
            self.code = code

        def wait(self, **kwargs):
            return self.code

        def poll(self):
            return self.code

    def transfer(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            target.write_bytes(data[:8])
            return Transfer(28)
        if len(calls) == 2:
            assert target.exists()
            kwargs["stderr"].write(range_error)
            return Transfer(range_code)
        assert not target.exists()
        target.write_bytes(data)
        return Transfer(0)

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_RETRY_DELAY", 0)
    gpu.download_verified(gpu.CUFFT_URL, hashlib.sha256(data).hexdigest(), target)
    assert len(calls) == 3
    assert "resume was rejected; restarting this temporary download" in capsys.readouterr().out


def test_stalled_real_downloader_cleans_all_parallel_packages_and_preserves_prefix(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / "cpu-marker").touch()
    original_download = gpu.download_verified
    attempts = []

    class StalledTransfer:
        def wait(self, **kwargs):
            return 28

        def poll(self):
            return 28

    def transfer(command, **kwargs):
        attempts.append(command)
        target = Path(command[command.index("-o") + 1])
        target.write_bytes(b"partial CUDA runtime")
        return StalledTransfer()

    def download(url, digest, target, **kwargs):
        if target.name == "cufft.whl":
            return original_download(url, digest, target, **kwargs)
        target.write_bytes(b"other verified vendor package")

    monkeypatch.setattr(gpu, "download_verified", download)
    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_RETRY_DELAY", 0)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args: pytest.fail("A stalled package must prevent all extraction"))
    with pytest.raises(RuntimeError, match="failed after 4 attempt.*stalled"):
        gpu.install(prefix, work, {"devices": [{"index": 0}]})
    assert len(attempts) == 4
    assert not list(work.iterdir())
    assert (prefix / "cpu-marker").exists()
    assert not list(tmp_path.glob("gpu-runtime.*"))


def test_extract_rejects_archive_path_escape(monkeypatch, tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        entry = tarfile.TarInfo("bin.SSE2/../../outside")
        entry.size = 3
        archive.addfile(entry, io.BytesIO(b"bad"))

    raw_tar = tmp_path / "raw.tar"
    raw_tar.write_bytes(stream.getvalue())
    package = tmp_path / "package.conda"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("pkg-gromacs.tar.zst", b"compressed placeholder")
    original_popen = subprocess.Popen
    processes = []

    def decompressor(command, **kwargs):
        assert command == ["zstd", "--decompress", "--stdout"]
        process = original_popen(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                str(raw_tar),
            ],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(gpu.subprocess, "Popen", decompressor)
    with pytest.raises(tarfile.OutsideDestinationError):
        gpu.extract_gromacs(package, tmp_path / "install")
    assert not (tmp_path / "outside").exists()
    assert all(process.poll() is not None for process in processes)
    assert not any(thread.name == "gromacs-conda-stream" for thread in threading.enumerate())


def test_cuda_wheel_extracts_only_runtime_and_preserves_license(tmp_path):
    wheel = tmp_path / "cufft.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("nvidia/cufft/lib/libcufft.so.11", b"runtime")
        archive.writestr("nvidia/cufft/include/cufft.h", b"unnecessary header")
        archive.writestr("nvidia/cufft/License.txt", b"vendor license")
        archive.writestr("../../outside", b"bad")
    destination = tmp_path / "prefix"
    destination.mkdir()
    gpu.extract_cuda_library(wheel, destination, "cufft", "libcufft.so.11")
    assert (destination / "lib/libcufft.so.11").read_bytes() == b"runtime"
    assert (destination / "share/licenses/cufft/License.txt").read_bytes() == b"vendor license"
    assert not (destination / "include").exists()
    assert not (tmp_path / "outside").exists()


def test_existing_cuda_runtime_skips_all_downloads_but_checks_execution(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / ".ready").touch()
    (prefix / ".cuda-runtime.json").write_text(json.dumps(gpu.RUNTIME_MANIFEST))
    calls = []
    monkeypatch.setattr(gpu, "download_verified", lambda *args: pytest.fail("Reusable runtime must skip downloads"))
    monkeypatch.setattr(gpu, "validate_runtime", lambda *args: calls.append(args))
    gpu.install(prefix, work, {"devices": [{"index": 2}]})
    assert calls == [(prefix, work, 2)]


def test_download_failure_preserves_existing_cpu_runtime(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / "cpu-marker").write_text("keep simulation environment")

    def fail(*args, **kwargs):
        raise ConnectionError("vendor download unavailable")

    monkeypatch.setattr(gpu, "download_verified", fail)
    with pytest.raises(ConnectionError):
        gpu.install(prefix, work, {"devices": [{"index": 0}]})
    assert (prefix / "cpu-marker").read_text() == "keep simulation environment"
    assert not list(tmp_path.glob("gpu-runtime.*"))


def test_gpu_validation_failure_preserves_cpu_prefix_and_cleans_downloads(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / "cpu-marker").touch()
    monkeypatch.setattr(gpu, "download_verified", lambda url, digest, target, **kwargs: target.write_bytes(b"verified mock package"))
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args, **kwargs: None)
    monkeypatch.setattr(gpu, "extract_cuda_library", lambda *args, **kwargs: None)

    def rejected(*args):
        raise RuntimeError("GPU kernels cannot run on this architecture")

    monkeypatch.setattr(gpu, "validate_runtime", rejected)
    with pytest.raises(RuntimeError, match="GPU kernels"):
        gpu.install(prefix, work, {"devices": [{"index": 0}]})
    assert (prefix / "cpu-marker").exists()
    assert list(work.iterdir()) == []


def test_gpu_upgrade_replaces_cpu_only_after_successful_gpu_execution(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / "cpu-marker").touch()
    monkeypatch.setattr(gpu, "download_verified", lambda *args, **kwargs: None)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args, **kwargs: None)
    monkeypatch.setattr(gpu, "extract_cuda_library", lambda *args, **kwargs: None)

    def validated(staged, *_):
        assert (prefix / "cpu-marker").exists()
        assert (staged / "bin/gmx").exists()

    monkeypatch.setattr(gpu, "validate_runtime", validated)
    gpu.install(prefix, work, {"devices": [{"index": 0}]})
    assert (prefix / ".backend").read_text().strip() == "CUDA"
    assert json.loads((prefix / ".cuda-runtime.json").read_text()) == gpu.RUNTIME_MANIFEST
    assert not (prefix / "cpu-marker").exists()
    assert not prefix.with_name("gromacs.previous").exists()


def test_swap_failure_restores_existing_prefix(monkeypatch, tmp_path):
    prefix, staged = tmp_path / "gromacs", tmp_path / "stage"
    prefix.mkdir()
    staged.mkdir()
    (prefix / "cpu-marker").touch()
    original_rename = Path.rename

    def blocked(path, destination):
        if path == staged:
            raise OSError("disk error")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", blocked)
    with pytest.raises(OSError):
        gpu.replace_prefix(staged, prefix)
    assert (prefix / "cpu-marker").exists()


def test_no_gpu_reports_cpu_without_modifying_existing_runtime(monkeypatch, tmp_path):
    prefix = tmp_path / "gromacs"
    prefix.mkdir()
    (prefix / "cpu-marker").touch()
    monkeypatch.setattr(gpu.sys, "argv", ["gpu_setup.py", "--prefix", str(prefix), "--work-dir", str(tmp_path)])
    monkeypatch.setattr(gpu, "probe_cuda_safely", lambda: {"backend": "CPU", "reason": "No supported GPU"})
    monkeypatch.setattr(gpu, "install", lambda *args: pytest.fail("CPU path must not download/install GPU runtime"))
    assert gpu.main() == 10
    assert json.loads((tmp_path / "gpu-status.json").read_text())["backend"] == "CPU"
    assert (prefix / "cpu-marker").exists()


def test_downloads_overlap_with_four_workers_and_all_hashes_finish_before_extraction(monkeypatch, tmp_path):
    barrier, lock = threading.Barrier(4), threading.Lock()
    active, peak, completed = 0, 0, []

    def parallel(url, digest, destination, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        destination.write_bytes(b"hash-verified mock package")
        with lock:
            completed.append(destination.name)
            active -= 1

    expected_remaining = {
        "gromacs-gpu.conda": {"gromacs-gpu.conda", "cufft.whl", "nvjitlink.whl", "nvrtc.whl"},
        "cufft.whl": {"cufft.whl", "nvjitlink.whl", "nvrtc.whl"},
        "nvjitlink.whl": {"nvjitlink.whl", "nvrtc.whl"},
        "nvrtc.whl": {"nvrtc.whl"},
    }

    def extract(package, *args):
        assert len(completed) == 4, "Extraction must wait until every package is hash-verified"
        assert {path.name for path in tmp_path.iterdir() if path.suffix in {".whl", ".conda"}} == expected_remaining[package.name]

    def validate(*args):
        assert not list(tmp_path.glob("*.whl"))
        assert not list(tmp_path.glob("*.conda")), "Release archives before running the full installed runtime"

    monkeypatch.setattr(gpu, "download_verified", parallel)
    monkeypatch.setattr(gpu, "extract_gromacs", extract)
    monkeypatch.setattr(gpu, "extract_cuda_library", extract)
    monkeypatch.setattr(gpu, "validate_runtime", validate)
    gpu.install(tmp_path / "gromacs", tmp_path, {"devices": [{"index": 0}]})
    assert peak == 4
    assert active == 0
    assert not list(tmp_path.glob("*.whl"))
    assert not list(tmp_path.glob("*.conda"))


def test_parallel_download_failure_waits_for_workers_and_cleans_every_file(monkeypatch, tmp_path):
    barrier = threading.Barrier(4)
    finished = []
    prefix = tmp_path / "gromacs"
    prefix.mkdir()
    (prefix / "existing-runtime").touch()

    def download(url, digest, destination, **kwargs):
        destination.write_bytes(b"partial download")
        barrier.wait(timeout=3)
        finished.append(destination.name)
        if destination.name == "cufft.whl":
            raise ValueError("SHA-256 mismatch")

    monkeypatch.setattr(gpu, "download_verified", download)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args: pytest.fail("Unverified package must never be extracted"))
    with pytest.raises(ValueError, match="SHA-256"):
        gpu.install(prefix, tmp_path, {"devices": [{"index": 0}]})
    assert len(finished) == 4
    assert (prefix / "existing-runtime").exists()
    assert not list(tmp_path.glob("*.whl"))
    assert not list(tmp_path.glob("*.conda"))


@pytest.mark.parametrize("backend", ["CPU", "CUDA"])
def test_cpu_mode_reuses_legacy_cpu_and_gpu_runtime_without_download(monkeypatch, tmp_path, backend):
    prefix = tmp_path / "gromacs"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/gmx").touch()
    (prefix / ".ready").touch()
    (prefix / ".backend").write_text(backend)
    calls = []
    monkeypatch.setattr(gpu, "validate_runtime", lambda *args: calls.append(args))
    monkeypatch.setattr(gpu, "download_verified", lambda *args: pytest.fail("Reusable CPU runtime must skip downloads"))
    gpu.install_cpu(prefix, tmp_path)
    assert calls == [(prefix, tmp_path, None)]
    assert (prefix / ".backend").read_text() == backend


def test_cpu_prebuilt_install_keeps_version_precision_and_validates_before_replacing(monkeypatch, tmp_path):
    prefix = tmp_path / "gromacs"
    prefix.mkdir()
    (prefix / "old-runtime").touch()
    downloads = []

    def download(url, digest, target, **kwargs):
        assert "2026.3-nompi_h26635d9_100" in url
        assert digest == gpu.CPU_SHA256
        downloads.append(target)
        target.write_bytes(b"verified mixed-precision CPU package")

    def validate(candidate, work, device):
        assert device is None
        assert (prefix / "old-runtime").exists()
        assert (candidate / "bin/gmx").exists()
        assert not downloads[0].exists(), "CPU archive should be released before runtime verification"

    monkeypatch.setattr(gpu, "download_verified", download)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args, **kwargs: None)
    monkeypatch.setattr(gpu, "validate_runtime", validate)
    gpu.install_cpu(prefix, tmp_path)
    assert len(downloads) == 1
    assert not downloads[0].exists()
    assert (prefix / ".ready").exists()
    assert (prefix / ".backend").read_text().strip() == "CPU"
    assert json.loads((prefix / ".cpu-runtime.json").read_text())["version"] == "2026.3"
    assert not (prefix / "old-runtime").exists()


def test_cpu_smoke_failure_preserves_old_runtime_and_cleans_download(monkeypatch, tmp_path):
    prefix = tmp_path / "gromacs"
    prefix.mkdir()
    (prefix / "old-runtime").touch()
    monkeypatch.setattr(gpu, "download_verified", lambda url, digest, target, **kwargs: target.write_bytes(b"download"))
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args, **kwargs: None)

    def fail(*args):
        raise ValueError("CPU runtime has wrong precision")

    monkeypatch.setattr(gpu, "validate_runtime", fail)
    with pytest.raises(ValueError, match="precision"):
        gpu.install_cpu(prefix, tmp_path)
    assert (prefix / "old-runtime").exists()
    assert not (tmp_path / "gromacs-cpu.conda").exists()
    assert not list(tmp_path.glob("cpu-runtime.*"))


def test_cpu_mode_repairs_unusable_existing_wrapper(monkeypatch, tmp_path):
    prefix = tmp_path / "gromacs"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/gmx").touch()
    (prefix / ".ready").touch()
    checks = []

    def validate(candidate, *args):
        checks.append(candidate)
        if candidate == prefix:
            raise PermissionError("Existing gmx wrapper is not executable")

    monkeypatch.setattr(gpu, "validate_runtime", validate)
    monkeypatch.setattr(gpu, "download_verified", lambda *args, **kwargs: None)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args, **kwargs: None)
    gpu.install_cpu(prefix, tmp_path)
    assert len(checks) == 2
    assert checks[0] == prefix
    assert (prefix / "bin/gmx").stat().st_mode & 0o111


def test_cpu_cli_bypasses_cuda_probe_and_preserves_diagnostic_reason(monkeypatch, tmp_path):
    status = tmp_path / "gpu-status.json"
    status.write_text('{"backend":"CPU","reason":"Existing NVIDIA driver is too old"}')
    monkeypatch.setattr(gpu.sys, "argv", ["gpu_setup.py", "--cpu", "--prefix", str(tmp_path / "gromacs"), "--work-dir", str(tmp_path)])
    monkeypatch.setattr(gpu, "probe_cuda_safely", lambda: pytest.fail("Explicit CPU install must not probe CUDA again"))
    calls = []
    monkeypatch.setattr(gpu, "install_cpu", lambda *args, **kwargs: calls.append(args))
    assert gpu.main() == 0
    assert calls == [(tmp_path / "gromacs", tmp_path)]
    assert "too old" in json.loads(status.read_text())["reason"]


@pytest.mark.parametrize(
    ("version", "precision", "support", "device"),
    [
        ("2025.4", "mixed", "OpenCL", None),
        ("2026.30", "mixed", "CUDA", None),
        ("2026.3", "double", "OpenCL", None),
        ("2026.3-conda_forge", "mixed", "OpenCL", 0),
    ],
)
def test_runtime_validation_rejects_wrong_version_precision_and_gpu_backend(monkeypatch, tmp_path, version, precision, support, device):
    calls = []

    def version_output(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args[0], 0, stdout=f"GROMACS version: {version}\nPrecision: {precision}\nGPU support: {support}\n"
        )

    monkeypatch.setattr(gpu.subprocess, "run", version_output)
    with pytest.raises(ValueError):
        gpu.validate_runtime(tmp_path, tmp_path, device)
    assert len(calls) == 1, "An incompatible runtime must fail before attempting simulation"


def test_later_failed_download_cancels_only_owned_processes_and_cleans_after_workers_exit(monkeypatch, tmp_path):
    prefix, work = tmp_path / "gromacs", tmp_path / "work"
    prefix.mkdir()
    work.mkdir()
    (prefix / "old-runtime").touch()
    original_popen = subprocess.Popen
    unrelated = original_popen([sys.executable, "-c", "import time; time.sleep(30)"])
    barrier, processes = threading.Barrier(4), []
    producer = (
        "import pathlib,sys,time\n"
        "target = pathlib.Path(sys.argv[1]); target.write_bytes(b'bad hash')\n"
        "if target.name != 'cufft.whl': time.sleep(30)\n"
    )

    def transfer(command, **kwargs):
        target = command[command.index("-o") + 1]
        process = original_popen([sys.executable, "-c", producer, target], **kwargs)
        processes.append(process)
        barrier.wait(timeout=3)
        return process

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "extract_gromacs", lambda *args: pytest.fail("A failed hash must prevent extraction"))
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match="SHA-256 mismatch for cufft.whl"):
            gpu.install(prefix, work, {"devices": [{"index": 0}]})
        assert time.monotonic() - started < 5, "The first submitted slow download must not hide a later failed hash"
        assert len(processes) == 4
        assert all(process.poll() is not None for process in processes)
        assert unrelated.poll() is None, "Cancelling this install must not kill unrelated processes"
        assert not list(work.iterdir())
        assert (prefix / "old-runtime").exists()
        assert not list(tmp_path.glob("gpu-runtime.*"))
    finally:
        for process in [*processes, unrelated]:
            gpu.stop_process(process)


def test_retries_share_one_deadline_and_reduce_curl_budget(monkeypatch, tmp_path):
    now, commands = [0.0], []

    class Transfer:
        def wait(self, **kwargs):
            return 28

        def poll(self):
            return 28

    def transfer(command, **kwargs):
        commands.append(command)
        now[0] += 4
        return Transfer()

    monkeypatch.setattr(gpu.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    monkeypatch.setattr(gpu, "DOWNLOAD_RETRY_DELAY", 0)
    monkeypatch.setenv(gpu.DOWNLOAD_TIMEOUT_ENV, "7")
    with pytest.raises(TimeoutError, match="total download time budget"):
        gpu.download_verified(gpu.CUFFT_URL, "0" * 64, tmp_path / "cufft.whl")
    assert len(commands) == 2
    assert [float(command[command.index("--max-time") + 1]) for command in commands] == [7.0, 3.0]


def test_total_budget_terminates_an_active_transfer(monkeypatch, tmp_path):
    original_popen, processes = subprocess.Popen, []

    def transfer(command, **kwargs):
        process = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(gpu.subprocess, "Popen", transfer)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="total download time budget"):
            gpu.download_verified(gpu.CUFFT_URL, "0" * 64, tmp_path / "cufft.whl", deadline=started + 0.15)
        assert time.monotonic() - started < 3
        assert processes[0].poll() is not None
    finally:
        for process in processes:
            gpu.stop_process(process)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-seconds"])
def test_invalid_download_budget_is_rejected_before_starting_processes(monkeypatch, tmp_path, value):
    monkeypatch.setenv(gpu.DOWNLOAD_TIMEOUT_ENV, value)
    monkeypatch.setattr(gpu.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("An invalid budget must not launch curl"))
    with pytest.raises(ValueError):
        gpu.download_verified(gpu.CUFFT_URL, "0" * 64, tmp_path / "cufft.whl")


@pytest.mark.parametrize("stage", ["solvate", "grompp"])
def test_smoke_preparation_timeout_names_failed_stage_and_preserves_timeout_cause(monkeypatch, tmp_path, stage):
    (tmp_path / "share/gromacs/top/amber19sb.ff").mkdir(parents=True)
    calls = []

    def command(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "--version":
            return subprocess.CompletedProcess(
                arguments, 0, stdout=f"GROMACS version: {gpu.VERSION}\nPrecision: mixed\nGPU support: CUDA\n"
            )
        if arguments[1] == stage:
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"], output=b"stage-specific native output")
        if arguments[1] == "solvate":
            (kwargs["cwd"] / "water.gro").write_text("Smoke water\n3\n")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(gpu.subprocess, "run", command)
    with pytest.raises(TimeoutError, match=f"'{stage}' timed out after 60s") as failed:
        gpu.validate_runtime(tmp_path, tmp_path, 0)
    assert isinstance(failed.value.__cause__, subprocess.TimeoutExpired)
    assert failed.value.__cause__.output == b"stage-specific native output"
    assert all("timeout" in options for _, options in calls)
    assert not any(arguments[1] == "mdrun" for arguments, _ in calls)


def test_gpu_smoke_still_exercises_nonbonded_pme_and_fft_with_time_limits(monkeypatch, tmp_path):
    (tmp_path / "share/gromacs/top/amber19sb.ff").mkdir(parents=True)
    calls = []

    def command(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "--version":
            return subprocess.CompletedProcess(
                arguments, 0, stdout=f"GROMACS version: {gpu.VERSION}\nPrecision: mixed\nGPU support: CUDA\n"
            )
        if arguments[1] == "solvate":
            (kwargs["cwd"] / "water.gro").write_text("Smoke water\n3\n")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(gpu.subprocess, "run", command)
    gpu.validate_runtime(tmp_path, tmp_path, 2)
    simulation, options = calls[-1]
    assert simulation[1] == "mdrun"
    assert all(simulation[simulation.index(flag) + 1] == "gpu" for flag in ("-nb", "-pme", "-pmefft"))
    assert simulation[simulation.index("-gpu_id") + 1] == "2"
    assert [options["timeout"] for _, options in calls] == [30, 60, 60, 120]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="Streaming decompressor test needs the existing zstd executable")
def test_conda_extraction_streams_archive_and_discards_unused_avx_variants(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in {
            "bin.SSE2/gmx": b"runtime",
            "lib.SSE2/library": b"library",
            "share/license": b"license",
            "bin.AVX2/gmx": b"unused",
        }.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    compressed = subprocess.run(["zstd", "--compress", "--stdout"], input=stream.getvalue(), capture_output=True, check=True).stdout
    package, prefix = tmp_path / "runtime.conda", tmp_path / "gromacs"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("pkg-gromacs.tar.zst", compressed)
    gpu.extract_gromacs(package, prefix)
    assert (prefix / "bin.SSE2/gmx").read_bytes() == b"runtime"
    assert (prefix / "lib.SSE2/library").read_bytes() == b"library"
    assert (prefix / "share/license").read_bytes() == b"license"
    assert not (prefix / "bin.AVX2").exists()
    assert not list(tmp_path.glob("*.tar.zst"))
    assert not any(thread.name == "gromacs-conda-stream" for thread in threading.enumerate())


@pytest.mark.skipif(shutil.which("zstd") is None, reason="Streaming decompressor test needs the existing zstd executable")
def test_broken_conda_input_stream_propagates_error_and_reaps_writer(monkeypatch, tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        entry = tarfile.TarInfo("bin.SSE2/gmx")
        entry.size = 3
        archive.addfile(entry, io.BytesIO(b"gmx"))
    compressed = subprocess.run(["zstd", "--compress", "--stdout"], input=stream.getvalue(), capture_output=True, check=True).stdout
    package = tmp_path / "runtime.conda"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("pkg-gromacs.tar.zst", compressed)
    original_copy = shutil.copyfileobj

    def broken_feed(source, target, **kwargs):
        original_copy(source, target, **kwargs)
        raise OSError("damaged ZIP member")

    monkeypatch.setattr(gpu.shutil, "copyfileobj", broken_feed)
    with pytest.raises(ValueError, match="archive streaming failed: damaged ZIP member"):
        gpu.extract_gromacs(package, tmp_path / "gromacs")
    assert not list(tmp_path.glob("*.tar.zst"))
    assert not any(thread.name == "gromacs-conda-stream" for thread in threading.enumerate())


@pytest.mark.skipif(sys.platform != "linux", reason="WSL signal cleanup requires Linux")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
@pytest.mark.parametrize("backend", ["CPU", "CUDA"])
def test_cli_launcher_cancellation_reaps_owned_transfers_before_cleaning_archives(tmp_path, backend, signum):
    prefix, work, marker = tmp_path / "gromacs", tmp_path / "work", tmp_path / "child-pids"
    prefix.mkdir()
    work.mkdir()
    (prefix / "old-runtime").write_text("retain")
    script = r"""
import importlib.util, pathlib, subprocess, sys
spec = importlib.util.spec_from_file_location('gpu', sys.argv[1])
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)
prefix, work, marker, backend = sys.argv[2:]
original_popen = subprocess.Popen
producer = (
    "import os,pathlib,sys,time\n"
    "pathlib.Path(sys.argv[1]).write_bytes(b'partial runtime')\n"
    "with pathlib.Path(sys.argv[2]).open('a') as stream: stream.write(str(os.getpid()) + '\\n')\n"
    "time.sleep(30)\n"
)
def transfer(command, **kwargs):
    if command[0] != 'curl':
        raise RuntimeError('Unexpected native command: ' + repr(command))
    target = command[command.index('-o') + 1]
    return original_popen([sys.executable, '-c', producer, target, marker], **kwargs)
gpu.subprocess.Popen = transfer
gpu.probe_cuda_safely = lambda: {'backend': 'CUDA', 'devices': [{'index': 0}]}
sys.argv = ['gpu_setup.py', '--prefix', prefix, '--work-dir', work]
if backend == 'CPU':
    sys.argv.append('--cpu')
raise SystemExit(gpu.main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ROOT / "packaging/windows/gpu_setup.py"), str(prefix), str(work), str(marker), backend],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pids = []
    try:
        expected, deadline = (1 if backend == "CPU" else 4), time.monotonic() + 5
        while time.monotonic() < deadline:
            pids = [int(value) for value in marker.read_text().splitlines()] if marker.exists() else []
            if len(pids) == expected:
                break
            if process.poll() is not None:
                pytest.fail(f"CLI exited before starting transfers: {process.communicate()[0]}")
            time.sleep(0.01)
        assert len(pids) == expected, "The child PID list must prove all transfer processes are running before cancellation"
        process.send_signal(signum)
        output, _ = process.communicate(timeout=10)
        assert process.returncode == 128 + signum, output
        assert not any((Path("/proc") / str(pid)).exists() for pid in pids), "The CLI must reap each owned transfer"
        assert unrelated.poll() is None
        assert (prefix / "old-runtime").read_text() == "retain"
        assert not list(work.iterdir())
        assert not list(tmp_path.glob("*-runtime.*"))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        gpu.stop_process(unrelated)
        # These PIDs come only from this disposable CLI's producer. Clean them
        # if an assertion failed before its graceful signal handler ran.
        for pid in pids:
            try:
                command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                if str(marker).encode() in command:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
