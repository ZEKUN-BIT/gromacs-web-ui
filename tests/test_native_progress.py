import os
import time

import pytest

from app.progress import enrich_progress, parse_native_log


def test_tpr_native_log_supplies_total_without_grompp():
    result = parse_native_log(
        "Input Parameters:\n  dt = 0.002\n  nsteps = 50000000\n  init-step = 0\n",
        "           Step           Time\n       25915000    51830.00000\n",
    )
    assert result["percent"] == 51.83
    assert result["time_ps"] == 51830
    assert result["total_steps"] == 50000000


def running_meta(tmp_path):
    return {
        "id": "test",
        "status": "running",
        "workdir": str(tmp_path),
        "step_index": 1,
        "step_count": 1,
        "commands": [{"args": ["gmx", "mdrun", "-s", "input.tpr", "-deffnm", "production"]}],
        "progress_percent": 0,
    }


def test_log_progress_updates_without_mutating_job_and_preserves_estimate(tmp_path):
    meta = running_meta(tmp_path)
    path = tmp_path / "production.log"
    path.write_text("dt = 0.002\nnsteps = 10000\ninit-step = 0\nStep Time\n1000 2.0\n")
    started = time.time() - 20
    os.utime(path, (started, started))
    assert enrich_progress(meta)["progress_percent"] == 10
    with path.open("a") as handle:
        handle.write("Step Time\n2000 4.0\n")
    os.utime(path, (started + 10, started + 10))
    detail = enrich_progress(meta)
    assert detail["progress_percent"] == 20
    assert detail["simulation_progress"]["ns_per_day"] == pytest.approx(17.28)
    assert detail["simulation_progress"]["eta_seconds"] == 80
    assert enrich_progress(meta)["simulation_progress"]["eta_seconds"] == 80
    assert meta["progress_percent"] == 0


def test_unknown_total_and_initialization_are_not_zero_progress(tmp_path):
    meta = running_meta(tmp_path)
    assert enrich_progress(meta)["progress_percent"] is None
    (tmp_path / "production.log").write_text("Step Time\n100 0.2\n")
    detail = enrich_progress(meta)
    assert detail["progress_percent"] is None
    assert detail["simulation_progress"]["step"] == 100


def test_restart_and_long_output_interval_do_not_claim_completion(tmp_path):
    meta = running_meta(tmp_path)
    path = tmp_path / "production.log"
    path.write_text("dt = 0.002\ninit-step = 5000\nnsteps = 5000\nStep Time\n7500 15\n")
    old = time.time() - 1200
    os.utime(path, (old, old))
    result = enrich_progress(meta)
    assert result["progress_percent"] == 50
    assert result["progress_observation"]["state"] == "stale"
    assert result["status"] == "running"
    assert "eta_seconds" not in result["simulation_progress"]


def test_log_symlink_cannot_read_outside_job(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("nsteps = 100\nStep Time\n50 0.1\n")
    (root / "production.log").symlink_to(outside)
    assert enrich_progress(running_meta(root))["progress_percent"] is None


def test_resumed_log_uses_latest_parameter_section_and_complete_step():
    result = parse_native_log(
        "dt = 0.002\nnsteps = 1000\ninit-step = 0\n",
        "Restarting\ndt = 0.002\nnsteps = 2000\ninit-step = 1000\nStep Time\n2000 4.0\nStep Time\n",
    )
    assert result["total_steps"] == 3000
    assert result["percent"] == 50


def test_native_command_line_override_and_appended_extension_are_distinguished():
    header = "dt=0.001\nnsteps=1000\ninit-step=0\nOverriding nsteps with value passed on the command line: 500 steps, 0.5 ps\n"
    assert parse_native_log(header, "Step Time\n250 0.25\n")["percent"] == 50
    extended = parse_native_log(header, "Restarting from checkpoint, appending to previous log file.\nStep Time\n750 0.75\n")
    assert extended["step"] == 750
    assert extended["percent"] is None and extended["total_steps"] is None


def test_checkpoint_continuation_keeps_target_unknown_outside_log_window(tmp_path):
    meta = running_meta(tmp_path)
    meta["commands"][0]["args"].extend(["-cpi", "production.cpt"])
    (tmp_path / "production.log").write_text("dt=0.001\nnsteps=1000\nStep Time\n750 0.75\n")
    result = enrich_progress(meta)
    assert result["simulation_progress"]["step"] == 750
    assert result["progress_percent"] is None


@pytest.mark.parametrize("text", ["Step Time\n100 1e999\n", "Step Time\n100 1e+\n"])
def test_nonfinite_or_incomplete_time_does_not_claim_progress(text):
    assert parse_native_log("nsteps=100", text) == {}
