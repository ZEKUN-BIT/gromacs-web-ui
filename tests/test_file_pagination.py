from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main
from app.files import relative_file_page


def test_search_and_pages_cover_files_beyond_the_legacy_limit(tmp_path):
    for number in range(2505):
        (tmp_path / f"result-{number:04}.xtc").write_bytes(b"frame")
    (tmp_path / "metadata.json").write_text("private")
    first = relative_file_page(tmp_path, search="", offset=0, limit=100)
    second = relative_file_page(tmp_path, search="", offset=100, limit=100)
    last = relative_file_page(tmp_path, search="RESULT-2504", offset=0, limit=100)
    assert first["total"] == 2505 and first["has_more"]
    assert not {item["path"] for item in first["files"]} & {item["path"] for item in second["files"]}
    assert last["files"] == [{"path": "result-2504.xtc", "size": 5, "modified": last["files"][0]["modified"]}]
    assert last["total"] == 1 and not last["has_more"]


def test_file_page_ignores_symlinks_and_special_files(tmp_path):
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    root = tmp_path / "job"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "secret-link").symlink_to(outside / "secret")
    os.mkfifo(root / "pipe")
    (root / "safe.txt").write_text("safe")
    result = relative_file_page(root, search="", offset=0, limit=100)
    assert [item["path"] for item in result["files"]] == ["safe.txt"]


def test_page_api_validates_options_and_refreshes_active_files(tmp_path, monkeypatch):
    meta = {"id": "paged", "workdir": str(tmp_path), "status": "running", "step_index": 0}
    monkeypatch.setattr(main, "STORE", SimpleNamespace(get=lambda _: meta))
    main.FILE_PAGE_CACHE.clear()
    clock = [10.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    (tmp_path / "one.xtc").write_text("one")
    client = TestClient(main.app)
    url = "/api/jobs/paged/files"
    for query in ("limit=0", "limit=501", "limit=100&offset=-1", "limit=100&search=" + "a" * 257):
        assert client.get(url + "?" + query).status_code == 422
    assert client.get(url + "?limit=1").json()["total"] == 1
    (tmp_path / "two.xtc").write_text("two")
    assert client.get(url + "?limit=1").json()["total"] == 1
    clock[0] += main.ACTIVE_FILE_CACHE_SECONDS
    page = client.get(url + "?limit=1&offset=1").json()
    assert page["total"] == 2 and page["files"][0]["path"] == "two.xtc"
    assert client.get(url).json()["truncated"] is False
