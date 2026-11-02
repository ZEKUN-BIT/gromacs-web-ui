import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from app.agent_diagnostics import MAX_QUALITY_REPORTS
from app.agent_tools import AgentTools
from app.gromacs import Step, build_steps
from app.job_store import JobStore
from app.models import SimulationParams


@pytest.fixture
def agent(tmp_path):
    store = JobStore(tmp_path)
    return AgentTools(
        store,
        {
            "gmx_bin": "gmx",
            "default_ntomp": 2,
            "default_ntmpi": 1,
            "analysis_parallel": 2,
            "api_key": "must-not-leak",
            "runtime_root": "/private/host",
        },
        Path(__file__).resolve().parents[1],
        lambda: {"available": True, "state": "ready", "worker_id": "private-host:1234"},
    )


def create_job(agent, *, status="queued", params=None, steps=None):
    params = {"workflow": "run_tpr", "gmx_bin": "gmx", "ntomp": 2, **(params or {})}
    steps = steps if steps is not None else [Step("Production", ["gmx", "mdrun", "-s", "input.tpr", "-cpi", "md.cpt"])]
    meta = agent.store.create("test", params, [], steps)
    if status == "preparing":
        return meta
    agent.store.start(meta["id"], steps)
    if status in {"completed", "failed"}:
        agent.store.update(meta["id"], status="running")
    if status != "queued":
        agent.store.update(meta["id"], status=status, desired_action=None, step_index=1)
    return agent.store.get(meta["id"])


def execute(agent, name, args):
    proposal = agent.prepare_action(name, args)
    return agent.execute_action(name, proposal["arguments"], proposal["precondition"])


def test_tool_schema_is_closed_and_unknown_actions_are_rejected(agent):
    definitions = agent.definitions()
    assert len({item["name"] for item in definitions}) == len(definitions)
    assert all(item["parameters"]["additionalProperties"] is False for item in definitions)
    with pytest.raises(ValueError):
        agent.is_mutating("shell")
    with pytest.raises(ValueError):
        agent.run_read("cancel_job", {"job_id": "something"})
    with pytest.raises(ValueError):
        agent.prepare_action("workspace_summary", {})


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "/tmp/secret", "..", "job\\secret", "a\nsecret", "", "c:secret"])
def test_invalid_target_rejected_before_store_access(agent, job_id):
    with patch.object(agent.store, "get") as get:
        with pytest.raises(ValidationError):
            agent.run_read("get_job", {"job_id": job_id})
        get.assert_not_called()


@pytest.mark.parametrize("arguments", [{"limit": 51}, {"limit": "2"}, {"limit": True}, {"offset": -1}, {"path": "/etc/passwd"}])
def test_list_bounds_and_strict_types(agent, arguments):
    with pytest.raises(ValidationError):
        agent.run_read("list_jobs", arguments)


def test_model_reads_only_allowlisted_metadata(agent):
    meta = create_job(agent, params={"force_field": "amber19sb", "secret": "secret-parameter"})
    agent.store.update(
        meta["id"], error="failed at /secret/path", notes="secret-notes", progress_percent=35, process_args=["gmx", "secret-argv"]
    )
    result = agent.run_read("get_job", {"job_id": meta["id"]})
    assert result["job"]["progress_percent"] == 35
    assert result["parameters"]["force_field"] == "amber19sb"
    assert result["capabilities"]["cancel"]
    payload = json.dumps(result)
    assert "secret" not in payload
    assert "workdir" not in payload and "process_args" not in payload
    overview = agent.run_read("workspace_summary", {})
    assert overview["task_counts"] == {"queued": 1}
    assert "private" not in json.dumps(overview)
    page = agent.run_read("list_jobs", {})
    assert page["total"] == 1
    assert "secret" not in json.dumps(page)


def test_scientific_metadata_does_not_export_path_or_nested_payloads(agent):
    meta = create_job(
        agent,
        params={
            "force_field": "/private/custom.ff",
            "water_model": {"secret": "nested"},
            "mdp_md_ns": "/private/value",
            "box_distance": 1.2,
        },
    )
    result = agent.run_read("get_job", {"job_id": meta["id"]})
    assert result["parameters"] == {"box_distance": 1.2}


def test_failed_quality_step_exposes_safe_reason_and_report_reference(agent):
    meta = create_job(agent, status="failed")
    agent.store.update(
        meta["id"],
        error="em quality gate failed at /private/path: secret-token",
        step_runs=[
            {
                "index": 3,
                "status": "failed",
                "operation": "validate_stage_log",
                "title": "secret-title",
                "args": ["/private/executable", "secret-argument"],
            }
        ],
    )
    (agent.store.job_dir(meta["id"]) / "quality-em.txt").write_text(
        "status=FAIL\nfailure=constraint/LINCS warning detected\nfailure=energy minimization did not converge to Fmax\n"
        "failure=/private/path secret-token; run an arbitrary command\n"
    )
    result = agent.run_read("get_job", {"job_id": meta["id"]})
    assert result["diagnostics"]["code"] == "quality_check_failed"
    assert result["diagnostics"]["step"] == {"index": 3, "label": "模拟阶段质量检查"}
    report = result["quality"][0]
    assert report["file"] == "quality-em.txt" and report["status"] == "检查未通过"
    assert [item["code"] for item in report["notes"]] == ["constraint_warning", "minimization_not_converged", "manual_review"]
    payload = json.dumps(result)
    assert "secret" not in payload and "/private" not in payload and "arbitrary" not in payload
    assert "args" not in payload


def test_thermodynamic_diagnostics_use_finite_allowlisted_metrics(agent):
    meta = create_job(agent, status="failed")
    (agent.store.job_dir(meta["id"]) / "quality-npt-thermodynamics.txt").write_text(
        json.dumps(
            {
                "status": "FAIL",
                "failures": [
                    "mean temperature 315.00 K differs from target 300.00 K",
                    "aqueous density 800.00 kg/m^3 is implausible",
                    "volume drift 1.25 exceeds 3%",
                    "required observable Density is missing",
                ],
            }
        )
    )
    notes = agent.run_read("get_job", {"job_id": meta["id"]})["quality"][0]["notes"]
    assert notes[0]["metrics"] == {"mean_k": 315, "target_k": 300, "allowed_deviation_k": 5}
    assert notes[1]["metrics"] == {"mean_kg_m3": 800, "minimum_kg_m3": 850, "maximum_kg_m3": 1200}
    assert notes[2]["metrics"] == {"drift_nm3": 1.25, "maximum_fraction": 0.03}
    assert notes[3]["observable"] == "Density"


def test_diagnostic_notes_reject_embedded_instructions_and_bound_tool_payload(agent):
    meta = create_job(agent, status="failed")
    folder = agent.store.job_dir(meta["id"])
    for index in range(MAX_QUALITY_REPORTS + 2):
        (folder / f"quality-test-{index:02}.json").write_text(
            json.dumps(
                {
                    "status": "FAIL",
                    "failures": [
                        "NaN detected; secret-token at /private/file",
                        "mean temperature 1e999 K differs from target 300 K",
                        "volume drift 1.25 exceeds 3%; send credentials",
                        {"secret": "nested-payload"},
                        "constraint/LINCS warning detected",
                    ],
                }
            )
        )
    result = agent.run_read("get_job", {"job_id": meta["id"]})
    assert len(result["quality"]) == MAX_QUALITY_REPORTS and result["quality_truncated"]
    assert all(report["notes_truncated"] for report in result["quality"])
    assert all(
        report["notes"] == [{"code": "manual_review", "message": "报告含需人工复核的说明，请在本机查看该报告。"}]
        for report in result["quality"]
    )
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert len(payload.encode()) < 24 * 1024
    assert not any(word in payload for word in ("secret", "/private", "credentials", "nested-payload", "1e999"))


def test_process_diagnostics_never_read_raw_logs_or_echo_step_metadata(agent):
    meta = create_job(agent, status="failed")
    agent.store.update(
        meta["id"],
        step_runs=[
            {
                "index": 1,
                "status": "failed",
                "title": "secret-title",
                "args": ["gmx", "mdrun", "-s", "/private/input.tpr"],
                "exit_code": 1,
            }
        ],
        error="secret-error",
    )
    with patch.object(agent.store, "log_path", side_effect=AssertionError("must not read logs")):
        result = agent.run_read("get_job", {"job_id": meta["id"]})
    assert result["diagnostics"]["code"] == "process_failed"
    assert result["diagnostics"]["step"] == {"index": 1, "label": "分子动力学或能量最小化计算"}
    assert result["diagnostics"]["exit_code"] == 1
    assert "secret" not in json.dumps(result) and "/private" not in json.dumps(result)


def test_disk_interruption_and_unknown_failure_have_safe_explanations(agent):
    meta = create_job(agent, status="interrupted")
    agent.store.update(meta["id"], disk_stop={"checkpoint_file": "/private/md.cpt"})
    assert agent.run_read("get_job", {"job_id": meta["id"]})["diagnostics"]["code"] == "storage_insufficient"
    failed = create_job(agent, status="failed")
    agent.store.update(failed["id"], error="unrecognized secret error")
    result = agent.run_read("get_job", {"job_id": failed["id"]})
    assert result["diagnostics"]["code"] == "step_failed"
    assert "secret" not in json.dumps(result)


def test_cancel_is_only_performed_after_execute(agent):
    meta = create_job(agent)
    proposal = agent.prepare_action("cancel_job", {"job_id": meta["id"]})
    assert agent.store.get(meta["id"])["status"] == "queued"
    result = agent.execute_action("cancel_job", proposal["arguments"], proposal["precondition"])
    assert result["job"]["status"] == "cancelled"
    with pytest.raises(ValueError, match="已变化"):
        agent.execute_action("cancel_job", proposal["arguments"], proposal["precondition"])


def test_ordinary_progress_update_does_not_invalidate_cancel(agent):
    meta = create_job(agent, status="running")
    proposal = agent.prepare_action("cancel_job", {"job_id": meta["id"]})
    agent.store.update(meta["id"], progress_percent=27)
    result = agent.execute_action("cancel_job", proposal["arguments"], proposal["precondition"])
    assert result["job"]["status"] == "running"
    assert agent.store.get(meta["id"])["desired_action"] == "cancel"


@pytest.mark.parametrize(
    "changes", [{"process_started_ticks": 99}, {"step_index": 2}, {"status": "completed"}, {"params": {"workflow": "run_tpr", "ntomp": 32}}]
)
def test_changed_execution_needs_new_approval(agent, changes):
    meta = create_job(agent, status="running")
    proposal = agent.prepare_action("cancel_job", {"job_id": meta["id"]})
    agent.store.update(meta["id"], **changes)
    with pytest.raises(ValueError, match="已变化"):
        agent.execute_action("cancel_job", proposal["arguments"], proposal["precondition"])


@pytest.mark.parametrize(
    "params,steps",
    [
        ({"workflow": "custom"}, None),
        ({"gmx_bin": "/tmp/fake-gmx"}, None),
        ({"maxwarn": 3}, None),
        ({"custom_command": "mdrun"}, None),
        ({"custom_stdin": True}, None),
        ({"index_script": "q\n"}, None),
        ({}, [Step("shell", ["/bin/sh", "-c", "touch /tmp/marker"])]),
        ({}, [Step("escape", ["gmx", "mdrun", "-deffnm", "../../outside"])]),
        ({}, [Step("unknown", ["internal"], kind="internal", operation="arbitrary_code")]),
    ],
)
def test_saved_unsafe_workflows_are_not_queued_by_ai(agent, params, steps):
    meta = create_job(agent, status="failed", params=params, steps=steps)
    with pytest.raises(ValueError):
        agent.prepare_action("retry_job", {"job_id": meta["id"]})
    assert agent.store.get(meta["id"])["status"] == "failed"


@pytest.mark.parametrize(
    "workflow,files",
    [
        ("protein_md", ["protein.pdb"]),
        ("analysis_suite", ["input.tpr", "md.xtc", "md_0_100.edr"]),
        ("analysis_rmsd", ["input.tpr", "md.xtc"]),
        ("postprocess", ["input.tpr", "md.xtc"]),
        ("em_only", ["protein.gro", "topol.top", "minim.mdp"]),
        ("run_tpr", ["input.tpr"]),
    ],
)
def test_real_standard_workflow_builders_are_allowed(agent, workflow, files):
    params = SimulationParams(workflow=workflow, ntmpi=1, ntomp=2).model_dump()
    steps = build_steps(params, files, agent.base_dir)
    meta = create_job(agent, status="completed", params=params, steps=steps)
    proposal = agent.prepare_action("clone_job", {"job_id": meta["id"]})
    assert proposal["preview"]["target"]["workflow"] == workflow
    assert len(agent.store.list()) == 1


def test_clone_preparation_does_not_create_job_and_execute_uses_store(agent):
    meta = create_job(agent, status="completed")
    proposal = agent.prepare_action("clone_job", {"job_id": meta["id"], "name": "repeat"})
    assert len(agent.store.list()) == 1
    result = agent.execute_action("clone_job", proposal["arguments"], proposal["precondition"])
    assert result["job"]["id"] != meta["id"]
    assert result["job"]["status"] == "queued"
    assert result["job"]["name"] == "repeat"


def test_resume_checkpoint_and_retry_reuse_existing_lifecycle(agent):
    failed = create_job(agent, status="failed")
    assert execute(agent, "retry_job", {"job_id": failed["id"]})["job"]["status"] == "queued"
    interrupted = create_job(agent, status="interrupted")
    (agent.store.job_dir(interrupted["id"]) / "md.cpt").write_bytes(b"dummy checkpoint")
    assert execute(agent, "resume_job", {"job_id": interrupted["id"]})["job"]["status"] == "queued"
    assert agent.store.get(interrupted["id"])["desired_action"] == "resume"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"gmx_bin": "/tmp/executable"},
        {"workflow": "custom"},
        {"maxwarn": 4},
        {"ligand_charge_confirmed": True},
        {"custom_stdin": True},
        {"stdin_overrides": "q"},
        {"structure_file": "/private/protein.pdb"},
        {"ligands": [{"charge_confirmed": True}]},
        {"mdp_dt_ps": 1.0},
        {"ntomp": "16"},
        {"force_field": "../../evil"},
    ],
)
def test_simulation_only_allows_typed_safe_parameters(agent, forbidden):
    with pytest.raises(ValidationError):
        agent.prepare_action("prepare_simulation", {"params": forbidden})
    assert not agent.store.list()


def test_simulation_draft_has_no_job_or_file_side_effect(agent):
    def files():
        # SQLite may remove its own WAL sidecars when an unrelated connection
        # is collected; those are not simulation preparation artifacts.
        return sorted(
            path.relative_to(agent.store.runtime_root)
            for path in agent.store.runtime_root.rglob("*")
            if path.name not in {"jobs.sqlite3-shm", "jobs.sqlite3-wal"}
        )

    before = files()
    proposal = agent.prepare_action("prepare_simulation", {"params": {"name": "new run", "mdp_md_ns": 50, "ntomp": 8}})
    assert proposal["preview"]["resources"]["requested_cpu_threads"] == 8
    result = agent.execute_action("prepare_simulation", proposal["arguments"], proposal["precondition"])
    assert result["kind"] == "simulation_draft"
    assert result["params"]["mdp_md_ns"] == "50.0"
    assert result["params"]["mdp_override_enabled"]
    after = files()
    assert before == after
    assert not agent.store.list()


def test_simulation_proposal_freezes_resource_defaults(agent):
    proposal = agent.prepare_action("prepare_simulation", {"params": {"name": "approved resources"}})
    assert proposal["preview"]["resources"]["requested_cpu_threads"] == 2
    assert proposal["arguments"]["params"]["ntomp"] == 2
    agent.settings.update(default_ntmpi=4, default_ntomp=16)
    result = agent.execute_action("prepare_simulation", proposal["arguments"], proposal["precondition"])
    assert result["params"]["ntmpi"] == 1
    assert result["params"]["ntomp"] == 2


def test_analysis_lists_only_relative_supported_inputs(agent):
    meta = create_job(agent, status="completed")
    root = agent.store.job_dir(meta["id"])
    (root / "input.tpr").write_bytes(b"tpr")
    (root / "private.txt").write_text("secret")
    (root / "link.xtc").symlink_to(root / "private.txt")
    result = agent.run_read("analysis_inputs", {"job_id": meta["id"]})
    assert result["files"]["tpr_file"] == [{"path": "input.tpr", "size": 3}]
    assert result["files"]["trajectory_file"] == []
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "analysis",
    [
        {"tpr_file": "../escape.tpr", "trajectory_file": "md.xtc"},
        {"tpr_file": "input.tpr", "trajectory_file": "md.xtc", "gmx_bin": "/tmp/fake"},
        {"tpr_file": "input.tpr", "trajectory_file": "md.xtc", "do_rmsd": False, "do_rg": False},
        {"tpr_file": "input.tpr", "trajectory_file": "md.xtc", "do_energy": True},
        {"tpr_file": "input.tpr", "trajectory_file": "md.xtc", "research_stride": "4"},
    ],
)
def test_analysis_rejects_invalid_and_unsafe_requests(agent, analysis):
    with pytest.raises(ValidationError):
        agent.prepare_action("create_analysis", {"job_id": "dummy", "analysis": analysis})


def test_analysis_uses_preflight_before_creating_and_rechecks_files(agent):
    meta = create_job(agent, status="completed")
    root = agent.store.job_dir(meta["id"])
    (root / "input.tpr").write_bytes(b"dummy tpr")
    (root / "md.xtc").write_bytes(b"dummy trajectory")
    request = {"job_id": meta["id"], "analysis": {"tpr_file": "input.tpr", "trajectory_file": "md.xtc"}}
    create = Mock(return_value={**meta, "id": "analysis-created", "workflow": "analysis_suite", "status": "queued"})
    with patch("app.agent_tools.create_existing_analysis", create):
        proposal = agent.prepare_action("create_analysis", request)
        create.assert_not_called()
        assert proposal["preview"]["preflight"]["storage"]["copied_input_bytes"] == 25
        assert len(agent.store.list()) == 1
        result = agent.execute_action("create_analysis", proposal["arguments"], proposal["precondition"])
        assert result["job"]["id"] == "analysis-created"
        assert create.call_count == 1
        (root / "md.xtc").write_bytes(b"changed")
        with pytest.raises(ValueError, match="输入已变化"):
            agent.execute_action("create_analysis", proposal["arguments"], proposal["precondition"])
        assert create.call_count == 1


def test_failed_research_preflight_never_creates_job(agent):
    meta = create_job(agent, status="completed")
    with (
        patch("app.agent_tools.preflight_existing_analysis", return_value={"research": {"valid": False, "message": "采样过大"}}),
        patch("app.agent_tools.create_existing_analysis") as create,
    ):
        with pytest.raises(ValueError, match="采样过大"):
            agent.prepare_action(
                "create_analysis",
                {"job_id": meta["id"], "analysis": {"tpr_file": "input.tpr", "trajectory_file": "md.xtc", "do_research": True}},
            )
        create.assert_not_called()


def test_simulation_replica_schema_matches_workflow_limit():
    from pydantic import ValidationError

    from app.agent_tools import SimulationParameters
    from app.workflows.common import MAX_REPLICAS

    assert SimulationParameters.model_json_schema()["properties"]["replicas"]["maximum"] == MAX_REPLICAS
    assert SimulationParameters(workflow="protein_md", replicas=MAX_REPLICAS).replicas == MAX_REPLICAS
    with pytest.raises(ValidationError):
        SimulationParameters(workflow="protein_md", replicas=MAX_REPLICAS + 1)
