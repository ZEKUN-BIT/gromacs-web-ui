from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import MDAnalysis as mda
import numpy as np
import pytest

from app import analysis_stream, existing_analysis
from app.analysis import preflight
from app.analysis.limits import research_size
from app.existing_analysis import _copy_analysis_input, create_existing_analysis
from app.job_store import JobStore
from app.models import ExistingAnalysisRequest


@pytest.fixture
def molecular_inputs(tmp_path, monkeypatch):
    universe = mda.Universe.empty(6, n_residues=6, atom_resindex=np.arange(6), trajectory=True)
    universe.add_TopologyAttr("names", ["CA"] * 6)
    universe.add_TopologyAttr("resnames", ["ALA"] * 6)
    universe.atoms.positions = np.arange(18).reshape(6, 3)
    monkeypatch.setattr(preflight, "_topology", lambda *_: universe)
    return universe


@pytest.mark.parametrize("extension", ["xtc", "trr"])
def test_header_preflight_counts_window_and_stride_without_source_cache(tmp_path, molecular_inputs, extension):
    trajectory = tmp_path / f"md.{extension}"
    with mda.Writer(str(trajectory), n_atoms=6) as writer:
        for time in [0, 1000, 3000, 6000, 7000, 9000]:
            molecular_inputs.trajectory.ts.time = time
            writer.write(molecular_inputs.atoms)
    request = ExistingAnalysisRequest(
        tpr_file="md.tpr", trajectory_file=trajectory.name, do_research=True, begin_ns=1, end_ns=7, research_stride=1
    )
    result = preflight.inspect_research_inputs({"tpr_file": tmp_path / "md.tpr", "trajectory_file": trajectory}, request, "gmx")
    assert result["valid"]
    assert result["available_frames"] == 6
    assert result["selected_frames"] == result["sampled_frames"] == 4
    assert result["trajectory_window_ns"] == [0, 9]
    assert list(tmp_path.iterdir()) == [trajectory]
    result = preflight.inspect_research_inputs(
        {"tpr_file": tmp_path / "md.tpr", "trajectory_file": trajectory}, request.model_copy(update={"research_stride": 2}), "gmx"
    )
    assert not result["valid"]
    assert "不足 3 帧" in result["message"]


def test_recommended_stride_accounts_for_frame_residue_budget():
    result = research_size(10000, 1000, 1)
    assert not result["valid"]
    assert result["suggested_stride"] == 5
    assert research_size(10000, 1000, 5)["valid"]
    assert not research_size(10, 3001)["valid"]


def test_topology_trajectory_mismatch_fails_before_analysis(tmp_path, molecular_inputs):
    trajectory = tmp_path / "md.xtc"
    with mda.Writer(str(trajectory), n_atoms=3) as writer:
        writer.write(molecular_inputs.atoms[:3])
    with pytest.raises(ValueError, match="原子数与 TPR 不一致"):
        preflight.inspect_research_inputs(
            {"tpr_file": tmp_path / "md.tpr", "trajectory_file": trajectory},
            ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="md.xtc"),
            "gmx",
        )


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr("app.reproducibility.detect_gromacs", lambda *_: {})
    monkeypatch.setattr("app.reproducibility.host_information", lambda: {})
    monkeypatch.setattr("app.reproducibility.gpu_information", lambda: {})
    monkeypatch.setattr(existing_analysis, "require_analysis_disk_space", lambda *_: None)
    store = JobStore(tmp_path / "runtime")
    job = store.create("source", {"workflow": "protein_md"}, [], [])
    for name in ("md.tpr", "md.xtc"):
        (store.job_dir(job["id"]) / name).write_bytes(b"fixture" * 100)
    store.start(job["id"], [])
    store.claim_next("fixture")
    store.update(job["id"], status="completed")
    return store, job["id"]


def test_research_limit_failure_precedes_job_creation_and_copy(source, monkeypatch):
    store, job_id = source
    monkeypatch.setattr(existing_analysis, "inspect_research_inputs", lambda *_: research_size(10000, 1000))
    monkeypatch.setattr(existing_analysis, "_copy_analysis_input", lambda *_: pytest.fail("Must not copy rejected input"))
    request = ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="md.xtc", do_research=True, research_stride=1)
    with pytest.raises(ValueError, match="建议每 5 帧"):
        create_existing_analysis(store, job_id, request, {"gmx_bin": "gmx"})
    assert len(store.list()) == 1


def test_copy_hashes_once_and_progress_is_monotonic(source, monkeypatch):
    store, job_id = source
    events = []
    monkeypatch.setattr("app.reproducibility.sha256_file", lambda *_: pytest.fail("Copied inputs must not be reread for hashing"))
    result = create_existing_analysis(
        store,
        job_id,
        ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="md.xtc", dry_run=True),
        {"gmx_bin": "gmx"},
        progress=events.append,
    )
    copying = [event for event in events if event["phase"] == "copy"]
    assert [event["copied_bytes"] for event in copying] == sorted(event["copied_bytes"] for event in copying)
    assert copying[-1]["copied_bytes"] == copying[-1]["total_bytes"] == 1400
    assert result["status"] == "queued"
    assert all(item["sha256"] == hashlib.sha256(b"fixture" * 100).hexdigest() for item in result["input_hashes"])


def test_changed_source_is_rejected_during_copy(tmp_path):
    source = tmp_path / "source.xtc"
    source.write_bytes(b"before")

    def mutate(_):
        source.write_bytes(b"after!")

    with pytest.raises(ValueError, match="发生变化"):
        _copy_analysis_input(source, tmp_path / "copy.xtc", mutate)


def test_stream_reports_progress_result_and_sanitized_failures(source):
    store, job_id = source
    request = ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="md.xtc", dry_run=True)

    async def collect():
        return [json.loads(line) async for line in analysis_stream.analysis_preparation_events(store, job_id, request, {"gmx_bin": "gmx"})]

    events = asyncio.run(collect())
    assert any(event.get("phase") == "copy" for event in events)
    assert events[-1]["type"] == "result"
    assert events[-1]["job"]["status"] == "queued"


def test_disconnecting_stream_preserves_preparation_and_cleanup(monkeypatch):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def prepare(*args, progress):
        progress({"phase": "copy", "copied_bytes": 1, "total_bytes": 2})
        entered.set()
        assert release.wait(5)
        completed.set()
        return {"id": "test"}

    monkeypatch.setattr(analysis_stream, "create_existing_analysis", prepare)

    async def exercise():
        stream = analysis_stream.analysis_preparation_events(None, "test", None, {})
        assert json.loads(await anext(stream))["type"] == "progress"
        await stream.aclose()
        release.set()
        assert await asyncio.to_thread(completed.wait, 2)
        await asyncio.gather(*list(analysis_stream._preparations))

    asyncio.run(exercise())


def test_preflight_api_rejects_paths_and_returns_size_advice(source, monkeypatch):
    from fastapi import HTTPException

    import app.main as main

    store, job_id = source
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "current_settings", lambda: {"gmx_bin": "gmx"})
    request = ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="../outside.xtc", do_research=True)
    with pytest.raises(HTTPException) as error:
        main.analysis_preflight(job_id, request)
    assert error.value.status_code == 400
    monkeypatch.setattr(existing_analysis, "inspect_research_inputs", lambda *_: research_size(10000, 1000))
    result = main.analysis_preflight(job_id, request.model_copy(update={"trajectory_file": "md.xtc"}))
    assert not result["research"]["valid"]
    assert result["research"]["suggested_stride"] == 5
