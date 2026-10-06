"""Search the persisted history while preserving literal text and pagination."""

from app.job_store import JobStore


def test_search_finds_jobs_beyond_first_page_and_pages_only_matching_history(tmp_path):
    store = JobStore(tmp_path)
    oldest = store.create("historical needle", {"workflow": "custom"}, [], [])
    for index in range(55):
        store.create(f"ordinary job {index}", {"workflow": "custom"}, [], [])
    newer = store.create("NEW needle", {"workflow": "custom"}, [], [])
    assert oldest["id"] not in {job["id"] for job in store.list_page()["jobs"]}
    page = store.list_page(limit=1, search="needle")
    assert page["total"] == 2 and page["has_more"]
    assert page["jobs"][0]["id"] == newer["id"]
    second = store.list_page(limit=1, offset=1, search="needle")
    assert second["jobs"][0]["id"] == oldest["id"]
    assert not second["has_more"]
    assert store.list_page(search="not present")["total"] == 0


def test_search_wildcards_backslashes_and_sql_are_literal_text(tmp_path):
    store = JobStore(tmp_path)
    literal = store.create(r"percent%_\literal", {"workflow": "custom"}, [], [])
    store.create("percentAXliteral", {"workflow": "custom"}, [], [])
    store.create("normal", {"workflow": "custom"}, [], [])
    for search in ("%", "_", "\\", "%_" + "\\"):
        page = store.list_page(search=search)
        assert page["total"] == 1 and page["jobs"][0]["id"] == literal["id"]
    assert store.list_page(search="' OR 1=1 --")["total"] == 0


def test_search_aliases_match_exact_status_workflow_and_keep_original_name_text(tmp_path):
    store = JobStore(tmp_path)
    chinese = store.create("等待我的蛋白", {"workflow": "custom"}, [], [])
    queued = store.create("unrelated name", {"workflow": "protein_ligand_md"}, [], [])
    store.start(queued["id"], [])
    store.create("queued substring only", {"workflow": "queued-example"}, [], [])
    page = store.list_page(search="等待", search_aliases=("queued", "queued", "' OR 1=1 --"))
    assert page["total"] == 2
    assert {item["id"] for item in page["jobs"]} == {chinese["id"], queued["id"]}
    assert store.list_page(search="配体模拟", search_aliases=("protein_ligand_md",))["jobs"][0]["id"] == queued["id"]


def test_search_matches_directory_id_workflow_and_status_fields(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("unique experiment", {"workflow": "protein_ligand_md"}, [], [])
    for search in (job["id"], job["directory_name"], "protein_ligand_md", "preparing", "UNIQUE"):
        assert store.list_page(search=search)["jobs"][0]["id"] == job["id"]
    assert store.list_page(search=" ")["total"] == 1
