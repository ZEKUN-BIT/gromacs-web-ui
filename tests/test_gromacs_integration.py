"""Small real-engine checks, opt in with GROMACS_INTEGRATION=1 or -m gromacs_integration.

The 64-atom argon fixture is generated locally and has no force-field or download
dependency. All dynamics use one MPI rank, one OpenMP thread and CPU kernels.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.execution import WorkerEngine
from app.job_store import JobStore

pytestmark = pytest.mark.gromacs_integration


@pytest.fixture(scope="module")
def real_gmx(request) -> str:
    explicitly_selected = "gromacs_integration" in request.config.option.markexpr
    if os.environ.get("GROMACS_INTEGRATION") != "1" and not explicitly_selected:
        pytest.skip("Enable real GROMACS checks with GROMACS_INTEGRATION=1 or -m gromacs_integration")
    configured = os.environ.get("GROMACS_TEST_GMX")
    executable = shutil.which(configured or "gmx")
    if not configured and not executable and Path("/usr/local/gromacs/bin/gmx").is_file():
        executable = "/usr/local/gromacs/bin/gmx"
    if not executable:
        pytest.fail("Real GROMACS was requested but gmx is unavailable; install it or set GROMACS_TEST_GMX")
    return executable


def run_gmx(args: list[str], workdir: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=workdir,
        env={
            **os.environ,
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "GMX_DISABLE_GPU_DETECTION": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "GMX_MAXBACKUP": "-1",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    return completed.stdout


@pytest.fixture(scope="module")
def argon_tpr(real_gmx, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("argon-input")
    coordinates = ["Deterministic 64-atom argon integration fixture", "64"]
    for atom in range(64):
        x, y, z = ((atom // 16) * 0.55 + 0.1, ((atom // 4) % 4) * 0.55 + 0.1, (atom % 4) * 0.55 + 0.1)
        coordinates.append(f"{atom + 1:5d}{'AR':<5}{'AR':>5}{atom + 1:5d}{x:8.3f}{y:8.3f}{z:8.3f}")
    (root / "argon.gro").write_text("\n".join([*coordinates, "2.2 2.2 2.2", ""]), encoding="utf-8")
    (root / "argon.top").write_text(
        "[ defaults ]\n1 2 yes 1.0 1.0\n"
        "[ atomtypes ]\nAr 39.948 0.0 A 0.34 0.997\n"
        "[ moleculetype ]\nArgon 1\n"
        "[ atoms ]\n1 Ar 1 AR AR 1 0.0 39.948\n"
        "[ system ]\nTiny argon integration fixture\n"
        "[ molecules ]\nArgon 64\n",
        encoding="utf-8",
    )
    (root / "argon.mdp").write_text(
        "integrator = md\ndt = 0.001\nnsteps = 40\n"
        "nstxout = 0\nnstvout = 0\nnstenergy = 5\nnstlog = 5\nnstxout-compressed = 5\n"
        "cutoff-scheme = Verlet\nnstlist = 10\nrlist = 0.8\n"
        "coulombtype = Cut-off\nrcoulomb = 0.8\nvdwtype = Cut-off\nrvdw = 0.8\n"
        "pbc = xyz\ntcoupl = no\npcoupl = no\nconstraints = none\n"
        "gen-vel = yes\ngen-temp = 90\ngen-seed = 20261016\n"
        "comm-mode = Linear\nnstcomm = 10\nDispCorr = no\n",
        encoding="utf-8",
    )
    run_gmx([real_gmx, "grompp", "-f", "argon.mdp", "-c", "argon.gro", "-p", "argon.top", "-o", "argon.tpr"], root)
    assert (root / "argon.tpr").stat().st_size > 0
    return root / "argon.tpr"


@pytest.fixture
def job_api(real_gmx, tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    settings = {"runtime_root": str(tmp_path), "gmx_bin": real_gmx, "max_parallel": 1, "default_ntmpi": 1, "default_ntomp": 1}
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "current_settings", lambda: settings)
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "GMX_DISABLE_GPU_DETECTION": "1",
        "CUDA_VISIBLE_DEVICES": "",
    }.items():
        monkeypatch.setenv(key, value)
    with TestClient(main.app) as client:
        yield client, store


def submit(client: TestClient, params: dict, files: list[Path]) -> dict:
    response = client.post(
        "/api/jobs",
        data={"params": json.dumps({"ntmpi": 1, "ntomp": 1, "pin": "off", "gpu": False, **params})},
        files=[("files", (path.name, path.read_bytes(), "application/octet-stream")) for path in files],
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "queued", job
    return job


def finish_job(store: JobStore, job: dict) -> dict:
    assert WorkerEngine(store, "worker:real-gromacs-test").run_once()
    completed = store.get(job["id"])
    assert completed["status"] == "completed", store.log_path(job["id"]).read_text(encoding="utf-8")
    assert completed["exit_code"] == 0
    return completed


def checkpoint_step(real_gmx: str, workdir: Path, filename: str) -> int:
    checkpoint = workdir / filename
    assert checkpoint.stat().st_size > 0
    output = run_gmx([real_gmx, "dump", "-cp", filename], workdir)
    match = re.search(r"^\s*step\s*=\s*(\d+)", output, re.MULTILINE)
    assert match, output
    return int(match.group(1))


def test_real_tpr_job_and_rmsd_plot_api(argon_tpr, real_gmx, job_api) -> None:
    client, store = job_api
    job = submit(client, {"name": "Real argon MD", "workflow": "run_tpr", "tpr_file": "argon.tpr"}, [argon_tpr])
    completed = finish_job(store, job)
    workdir = Path(completed["workdir"])
    assert checkpoint_step(real_gmx, workdir, "argon.cpt") == 40
    assert (workdir / "argon.xtc").stat().st_size > 0
    assert (workdir / "argon.edr").stat().st_size > 0
    manifest = json.loads((workdir / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert any(item["path"] == "argon.tpr" and len(item["sha256"]) == 64 for item in manifest["input_files"])

    analysis = submit(
        client,
        {
            "name": "Real argon RMSD",
            "workflow": "analysis_rmsd",
            "tpr_file": "argon.tpr",
            "trajectory_file": "argon.xtc",
            "center_group": "System",
            "fit_group": "System",
            "rmsd_group": "System",
            "time_unit": "ps",
        },
        [workdir / "argon.tpr", workdir / "argon.xtc"],
    )
    analyzed = finish_job(store, analysis)
    assert (Path(analyzed["workdir"]) / "rmsd.xvg").stat().st_size > 0
    response = client.get(f"/api/jobs/{analysis['id']}/plots")
    assert response.status_code == 200, response.text
    plots = response.json()["plots"]
    assert len(plots) == 1, response.json()
    assert plots[0]["sample_count"] == 9
    assert plots[0]["points"][-1][0] == pytest.approx(0.04)
    assert plots[0]["last"] > 0
    assert plots[0]["time_unit"] == "ps"

    # Preflight decodes a real TPR/XTC before any analysis copy is created.
    request = {"tpr_file": "argon.tpr", "trajectory_file": "argon.xtc", "do_research": True, "research_stride": 1}
    preflight = client.post(f"/api/jobs/{job['id']}/analysis-preflight", json=request)
    assert preflight.status_code == 200, preflight.text
    size = preflight.json()["research"]
    assert size["available_frames"] == size["sampled_frames"] == 9
    assert size["atoms"] == 64
    assert size["residues"] == 0 and not size["valid"]  # Argon has no protein C-alpha atoms.
    previous = len(store.list())
    rejected = client.post(f"/api/jobs/{job['id']}/analysis", json=request)
    assert rejected.status_code == 400, rejected.text
    assert len(store.list()) == previous


def test_resume_real_checkpoint_via_api(argon_tpr, real_gmx, job_api) -> None:
    client, store = job_api
    job = submit(
        client,
        {
            "name": "Resume real argon checkpoint",
            "workflow": "custom",
            "custom_command": "gmx mdrun -s argon.tpr -deffnm partial -cpi partial.cpt -ntmpi 1 -ntomp 1 -nb cpu -pme cpu -pin off",
        },
        [argon_tpr],
    )
    claimed = store.claim_next("worker:interrupted-real-gromacs")
    assert claimed and claimed["id"] == job["id"]
    workdir = Path(claimed["workdir"])
    # A bounded first segment deterministically represents interruption after a
    # checkpoint. The saved command retains the TPR's full 40-step target.
    run_gmx([*claimed["commands"][0]["args"], "-nsteps", "20"], workdir)
    assert checkpoint_step(real_gmx, workdir, "partial.cpt") == 20
    store.update(job["id"], status="interrupted", desired_action=None, worker_id=None, step_index=1, current_step="Custom command")
    response = client.post(f"/api/jobs/{job['id']}/resume-checkpoint")
    assert response.status_code == 200, response.text
    resumed = response.json()
    assert resumed["status"] == "queued"
    assert resumed["resumed_from_checkpoint"] == "partial.cpt"
    assert "-append" in resumed["commands"][0]["args"]
    completed = finish_job(store, resumed)
    assert checkpoint_step(real_gmx, workdir, "partial.cpt") == 40
    assert completed["progress_percent"] == 100
    assert "checkpoint" in store.log_path(job["id"]).read_text(encoding="utf-8").lower()
