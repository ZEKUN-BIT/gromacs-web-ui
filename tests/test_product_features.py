from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.demo import create_example
from app.job_store import JobStore
from app.result_bundle import build_result_bundle, quality_summary, read_regular


@pytest.fixture
def product_api(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    return TestClient(main.app, base_url="http://127.0.0.1:8000"), store


def completed_job(store, name="研究任务", **params):
    meta = store.create(name, {"workflow": "protein_md", **params}, [], [])
    store.start(meta["id"], [])
    store.update(meta["id"], status="running")
    return store.update(meta["id"], status="completed", desired_action=None)


def test_template_versions_strip_machine_paths_files_and_confirmations(product_api):
    client, store = product_api
    request = {
        "name": "实验室协议",
        "source": "lab-v1",
        "applicability": "可溶性蛋白",
        "params": {
            "workflow": "protein_ligand_md",
            "gmx_bin": "/private/gmx",
            "protein_file": "secret.pdb",
            "project": "private",
            "ligand_charge_confirmed": True,
            "ligands": [{"key": "lig", "charge": -1, "charge_confirmed": True, "structure_file": "private.sdf"}],
        },
    }
    first = client.post("/api/templates", json=request)
    assert first.status_code == 200, first.text
    second = client.post("/api/templates", json=request).json()
    assert second["revision"] == 2
    params = first.json()["params"]
    assert not {"gmx_bin", "protein_file", "project", "ligand_charge_confirmed"}.intersection(params)
    assert params["ligands"][0]["charge"] == -1
    assert "charge_confirmed" not in params["ligands"][0]
    assert "structure_file" not in params["ligands"][0]
    assert len(client.get("/api/product").json()["templates"]) == 2
    # Export format can be reimported on another installation.
    payload = {key: value for key, value in second.items() if key not in {"id", "revision", "created_at"}}
    assert client.post("/api/templates", json=payload).status_code == 200
    assert client.delete("/api/templates/" + first.json()["id"]).status_code == 200
    assert client.get("/api/product").json()["templates"][0]["revision"] == 3


def test_template_validation_size_and_cross_origin_are_rejected(product_api):
    client, _ = product_api
    assert client.post("/api/templates", json={"name": " ", "params": {}}).status_code == 422
    assert client.post("/api/templates", json={"name": "x", "params": {"unexpected": "evil"}}).status_code == 422
    assert client.post("/api/templates", json={"name": "x", "schema_version": 2, "params": {}}).status_code == 422
    assert client.post("/api/templates", content=b"x" * (256 * 1024 + 1), headers={"Content-Type": "application/json"}).status_code == 413
    request = {"name": "x", "params": {}}
    assert client.post("/api/templates", json=request, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/templates", json=request, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_deleted_template_versions_are_never_reused(product_api):
    client, _ = product_api
    request = {"name": "共享协议", "params": {}}
    first = client.post("/api/templates", json=request).json()
    assert client.delete("/api/templates/" + first["id"]).status_code == 200
    second = client.post("/api/templates", json=request).json()
    assert second["revision"] == 2


def test_nonfinite_template_numbers_are_rejected(product_api):
    client, _ = product_api
    response = client.post(
        "/api/templates",
        content='{"name":"bad","params":{"box_distance":1e999}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_project_metadata_does_not_modify_parameters_or_execution(product_api):
    client, store = product_api
    first = completed_job(store, project="项目 A")
    second = completed_job(store, name="另一任务", project="项目 B")
    payload = {"project": "项目 B", "tags": ["WT", " WT "], "notes": "比较条件", "pinned": True}
    response = client.patch(f"/api/jobs/{first['id']}/organization", json=payload)
    assert response.status_code == 200
    changed = store.get(first["id"])
    assert changed["params"] == first["params"]
    assert changed["status"] == "completed"
    assert changed["tags"] == ["WT"]
    rows = client.get("/api/jobs", params={"project": "项目 B"}).json()["jobs"]
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert client.get("/api/jobs", params={"project": "项目 A"}).json()["total"] == 0
    assert client.get("/api/jobs", params={"search": "WT"}).json()["total"] == 1
    assert client.patch(f"/api/jobs/{first['id']}/organization", json={"status": "failed"}).status_code == 422
    assert client.get("/api/jobs", params={"search": "' OR 1=1 --"}).json()["total"] == 0


def test_result_package_contains_provenance_and_escapes_html(product_api):
    client, store = product_api
    job = completed_job(store, name='<img src=x onerror="alert(1)">')
    root = store.job_dir(job["id"])
    (root / "quality-npt.json").write_text(json.dumps({"status": "failed", "failures": ["<script>bad</script>"]}))
    (root / "md.mdp").write_text("dt = 0.002\n")
    (root / "rmsd.xvg").write_text("0 0\n1 0.1\n")
    (root / "large.xtc").write_text("trajectory")
    response = client.get(f"/api/jobs/{job['id']}/result-bundle")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert {"index.html", "report.css", "summary.json", "README.txt", "files/md.mdp", "files/rmsd.xvg"}.issubset(bundle.namelist())
        assert "files/large.xtc" not in bundle.namelist()
        html = bundle.read("index.html").decode()
        assert "<script>bad" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;bad" in html
        assert "不代表所有质量检查通过" in html
        assert '<link rel="stylesheet" href="report.css">' in html
        assert "<style>" not in html
    summary = client.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["quality"][0]["status"] == "检查未通过"
    assert summary["quality_is_scientific_validation"] is False


def test_bundle_refuses_active_jobs_and_skips_links_and_large_files(product_api, tmp_path, monkeypatch):
    client, store = product_api
    job = store.create("active", {}, [], [])
    assert client.get(f"/api/jobs/{job['id']}/result-bundle").status_code == 409
    store.update(job["id"], status="failed")
    root = store.job_dir(job["id"])
    outside = tmp_path / "private.xvg"
    outside.write_text("secret")
    (root / "link.xvg").symlink_to(outside)
    (root / "huge.xvg").write_bytes(b"x" * 100)
    monkeypatch.setattr("app.result_bundle.MAX_FILE_BYTES", 10)
    with build_result_bundle(store, job["id"]) as archive, zipfile.ZipFile(archive) as bundle:
        assert "files/link.xvg" not in bundle.namelist()
        assert "files/huge.xvg" not in bundle.namelist()
        assert len(json.loads(bundle.read("summary.json"))["skipped_files"]) == 2


def test_bundle_refuses_terminal_job_with_verified_live_process(product_api, monkeypatch):
    client, store = product_api
    job = completed_job(store)
    monkeypatch.setattr("app.result_bundle.live_process_identities", lambda meta, root: [{"process_pid": 123}])
    response = client.get(f"/api/jobs/{job['id']}/result-bundle")
    assert response.status_code == 409
    assert "活跃计算进程" in response.text


def test_bundle_refuses_pending_action_and_enforces_total_size(product_api, monkeypatch):
    _, store = product_api
    job = completed_job(store)
    store.update(job["id"], desired_action="cancel")
    with pytest.raises(ValueError, match="停止或完成"):
        build_result_bundle(store, job["id"])
    store.update(job["id"], desired_action=None)
    root = store.job_dir(job["id"])
    for name in ("a.xvg", "b.xvg", "c.xvg"):
        (root / name).write_bytes(b"123456")
    monkeypatch.setattr("app.result_bundle.MAX_BUNDLE_BYTES", 10)
    with build_result_bundle(store, job["id"]) as archive, zipfile.ZipFile(archive) as bundle:
        summary = json.loads(bundle.read("summary.json"))
        assert sum(item["size"] for item in summary["files"]) <= 10
        assert len(summary["skipped_files"]) == 2


def test_bounded_reader_refuses_a_symlinked_parent_and_fifo(tmp_path):
    import os

    root, outside = tmp_path / "job", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.xvg").write_text("private")
    (root / "replaced-directory").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_regular(root / "replaced-directory" / "private.xvg", 100, root=root)
    os.mkfifo(root / "blocked.xvg")
    with pytest.raises(ValueError, match="普通文件"):
        read_regular(root / "blocked.xvg", 100, root=root)


def test_quality_summary_understands_existing_text_and_json_gate_formats(tmp_path):
    (tmp_path / "quality-nvt.txt").write_text("stage=nvt\nstatus=FAIL\nfailure=constraint/LINCS warning detected\n")
    (tmp_path / "quality-npt-thermodynamics.txt").write_text(json.dumps({"status": "PASS", "failures": []}))
    (tmp_path / "quality-bad.json").write_text("{broken")
    summaries = {item["file"]: item for item in quality_summary(tmp_path)}
    assert summaries["quality-nvt.txt"]["status"] == "检查未通过"
    assert summaries["quality-nvt.txt"]["notes"] == ["constraint/LINCS warning detected"]
    assert summaries["quality-npt-thermodynamics.txt"]["status"] == "检查通过"
    assert "quality-bad.json" not in summaries


def test_example_has_independent_inputs_and_single_cpu_settings(product_api, monkeypatch):
    _, store = product_api
    monkeypatch.setattr("app.demo.write_experiment_manifest", lambda meta: {"input_files": []})
    job = create_example(store, {"gmx_bin": "/bin/true"})
    assert job["is_example"] and job["status"] == "queued"
    args = job["commands"][1]["args"]
    assert args[args.index("-ntomp") + 1] == "1"
    assert args[args.index("-nb") + 1] == "cpu"
    assert "initial.gro" in job["uploaded_files"]
    assert "initial.gro" not in job["commands"][1]["outputs"]


def test_local_usage_distinguishes_dry_runs_and_tutorials(product_api):
    client, store = product_api
    completed_job(store)
    dry = completed_job(store)
    store.update(dry["id"], dry_run=True)
    example = completed_job(store)
    store.update(example["id"], is_example=True)
    assert client.get("/api/support/usage").json()["statuses"] == {"completed": 1}


def test_support_preview_excludes_host_paths_and_data(product_api, monkeypatch):
    client, _ = product_api
    monkeypatch.setattr(
        "app.product_api.environment_diagnostics",
        lambda *args: {
            "gromacs": {"available": True, "version": "GROMACS version: 2026.3\n/home/alice/private/bin/gmx"},
            "host": {"hostname": "private-host", "cpu_count": 8},
            "gpu": {"runtime_verified": True},
        },
    )
    response = client.get("/api/support/diagnostics")
    assert response.status_code == 200
    assert "/home/alice" not in response.text and "private-host" not in response.text
    assert response.json()["components"]["gromacs"]["version"] == "2026.3"
    assert client.get("/api/help/document", params={"name": "../settings"}).status_code == 404
