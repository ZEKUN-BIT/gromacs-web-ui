import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.job_file_listing as listing
import app.job_store as jobs
import app.main as main
from app.gromacs import Step
from app.job_store import JobStore


@pytest.mark.parametrize("action", ["retry", "skip", "checkpoint"])
def test_clone_after_recovery_uses_original_steps_and_inputs(tmp_path, action, monkeypatch):
    for detector in ("detect_gromacs", "host_information", "gpu_information"):
        monkeypatch.setattr(f"app.reproducibility.{detector}", lambda *args: {})
    store = JobStore(tmp_path)
    steps = [
        Step("prepare", ["gmx", "grompp", "-o", "intermediate.tpr"], outputs=["intermediate.tpr"]),
        Step("simulate", ["gmx", "mdrun", "-s", "intermediate.tpr", "-cpi", "md.cpt"]),
        Step("optional", ["gmx", "rms"], stdin_text="0\n0\n"),
    ]
    meta = store.create("protocol", {}, ["input.dat"], steps)
    job_id = meta["id"]
    for file in ["input.dat", "intermediate.tpr", "md.cpt"]:
        (store.job_dir(job_id) / file).write_text(file)
    store.start(job_id, steps)
    store.claim_next("worker")
    store.update(job_id, status="failed", step_index=3 if action == "skip" else 2)
    if action == "checkpoint":
        assert "-append" in store.resume_from_checkpoint(job_id)["commands"][0]["args"]
        store.claim_next("worker")
        store.update(job_id, status="completed", desired_action=None)
    else:
        recovered = store.retry_failed_step(job_id) if action == "retry" else store.skip_failed_step(job_id)
        assert recovered["commands"] != meta["commands"]
    # A new Web process must also see the immutable plan.
    cloned = JobStore(tmp_path).clone(job_id)
    assert cloned["commands"] == meta["commands"] and cloned["step_index"] == 0
    assert cloned["status"] == "queued" and not cloned["dry_run"]
    folder = Path(cloned["workdir"])
    assert (folder / "input.dat").read_text() == "input.dat"
    assert not (folder / "intermediate.tpr").exists() and not (folder / "md.cpt").exists()


def test_legacy_clone_refuses_ambiguous_preparation_mode(tmp_path):
    store = JobStore(tmp_path)
    job_id = store.create("legacy", {}, [], [])["id"]
    store.update(job_id, status="failed", error="failed during preparation")
    with store._connect() as connection:
        connection.execute("DELETE FROM job_plans WHERE job_id=?", (job_id,))
    with pytest.raises(RuntimeError, match="旧任务"):
        store.clone(job_id)
    assert len(store.list()) == 1


def test_xvg_archive_skips_oversize_files_and_explains_limits(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(listing, "MAX_XVG_ARCHIVE_BYTES", 8)
    monkeypatch.setattr(listing, "MAX_XVG_ARCHIVE_FILES", 1)
    job_id = store.create("xvg", {}, [], [])["id"]
    folder = store.job_dir(job_id)
    url = f"/api/jobs/{job_id}/plots/download"
    client = TestClient(main.app)
    assert listing.xvg_archive(folder / "absent") is None
    (folder / "invalid.xvg").symlink_to(tmp_path / "jobs.sqlite3")
    assert client.get(url).status_code == 404
    (folder / "a.xvg").write_bytes(b"X" * 9)
    result = client.get(url)
    assert result.status_code == 413 and "单独下载" in result.json()["detail"]
    (folder / "b.XVG").write_bytes(b"benefit")
    (folder / "c.xvg").touch()
    with zipfile.ZipFile(io.BytesIO(client.get(url).content)) as archive:
        assert archive.namelist() == ["b.XVG", "manifest.json"]
        assert archive.read("b.XVG") == b"benefit"
        assert json.loads(archive.read("manifest.json")) == {
            "included": 1,
            "skipped": 2,
            "skipped_examples": [{"path": "a.xvg", "reason": "byte_limit"}, {"path": "c.xvg", "reason": "file_count_limit"}],
        }


def test_log_reconnect_detects_rotation_even_after_regrowth(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(jobs, "MAX_LOG_FILE_BYTES", 1024)
    job_id = store.create("log", {}, [], [])["id"]
    store.append_log(job_id, "A" * 300)
    old = main.job_log(job_id, offset=0)
    store.append_log(job_id, "B" * 1024)
    new = main.job_log(job_id, offset=old["offset"], log_generation=old["generation"])
    assert new["generation"] != old["generation"] and new["reset"] and new["start"] == 0
    assert new["size"] > old["offset"] and new["text"].endswith("B" * 512)
    store.append_log(job_id, "尾声")
    next_chunk = main.job_log(job_id, offset=new["offset"], log_generation=new["generation"])
    assert next_chunk["text"] == "尾声" and not next_chunk["reset"]

    class Connected:
        async def is_disconnected(self):
            return False

    async def reconnect():
        response = await main.job_events(Connected(), job_id=job_id, log_offset=old["offset"], log_generation=old["generation"])
        try:
            async for event in response.body_iterator:
                if event.startswith("event: log\n"):
                    return json.loads(event.split("data: ", 1)[1])
        finally:
            await response.body_iterator.aclose()

    event = asyncio.run(reconnect())
    assert event["reset"] and event["generation"] == new["generation"] and event["text"] == new["text"] + "尾声"
