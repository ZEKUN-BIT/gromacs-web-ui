import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.job_store import JobStore


@pytest.fixture
def console(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    job = store.create("storage API", {"workflow": "protein_md"}, ["input.pdb"], [])
    root = store.job_dir(job["id"])
    for name in ("input.pdb", "nvt.trr", "md.xtc", "md.cpt"):
        (root / name).write_bytes(b"scientific data")
    store.start(job["id"], [])
    store.claim_next("worker")
    store.update(job["id"], status="completed")
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    main.FILE_LIST_CACHE.clear()
    with TestClient(main.app, base_url="http://127.0.0.1:8000") as client:
        yield client, store, job
    main.FILE_LIST_CACHE.clear()


def test_storage_api_requires_preview_and_invalidates_file_cache(console):
    client, store, job = console
    payload = {"job_ids": [job["id"]]}
    before = client.get(f"/api/jobs/{job['id']}/files").json()
    assert any(item["path"] == "nvt.trr" for item in before["files"])
    assert client.post("/api/storage/cleanup", json=payload).status_code == 409
    preview = client.post("/api/storage/preview", json=payload).json()
    assert preview["total_bytes"] == len(b"scientific data")
    assert (store.job_dir(job["id"]) / "nvt.trr").is_file()
    payload["preview_token"] = preview["preview_token"]
    response = client.post("/api/storage/cleanup", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["cleaned_jobs"] == [job["id"]]
    after = client.get(f"/api/jobs/{job['id']}/files").json()
    assert not any(item["path"] == "nvt.trr" for item in after["files"])
    for name in ("input.pdb", "md.xtc", "md.cpt"):
        assert (store.job_dir(job["id"]) / name).is_file()
    assert client.post("/api/storage/cleanup", json=payload).status_code == 409


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://untrusted.example"},
        {"origin": "http://127.0.0.1:9000"},
        {"origin": "http://attacker.local:8000"},
        {"origin": "http://[invalid"},
        {"sec-fetch-site": "cross-site"},
    ],
)
def test_storage_rejects_browser_requests_from_other_origins(console, headers):
    client, store, job = console
    payload = {"job_ids": [job["id"]], "mode": "delete_jobs"}
    assert client.post("/api/storage/preview", json=payload, headers=headers).status_code == 403
    preview = client.post("/api/storage/preview", json=payload).json()
    payload["preview_token"] = preview["preview_token"]
    assert client.post("/api/storage/cleanup", json=payload, headers=headers).status_code == 403
    assert store.get(job["id"])["status"] == "completed"
    response = client.post("/api/storage/cleanup", json=payload, headers={"origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_jobs"] == [job["id"]]


def test_storage_request_cannot_supply_filesystem_paths(console):
    client, _, _ = console
    assert client.post("/api/storage/preview", json={"job_ids": ["../../.."]}).status_code == 422
