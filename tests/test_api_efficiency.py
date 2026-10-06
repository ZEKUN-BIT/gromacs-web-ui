import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import app.main as main


class EfficientApiTests(unittest.TestCase):
    def test_list_endpoint_delegates_bounded_pagination(self) -> None:
        store = Mock()
        store.list_page.return_value = {"jobs": [], "limit": 50, "offset": 0, "total": 0, "has_more": False}
        with patch.object(main, "STORE", store):
            payload = main.list_jobs(limit=50, offset=0)
        self.assertEqual(payload["jobs"], [])
        store.list_page.assert_called_once_with(limit=50, offset=0)

    def test_history_search_keeps_original_text_and_translates_chinese_status(self) -> None:
        store = Mock()
        store.list_page.return_value = {"jobs": [], "limit": 50, "offset": 50, "total": 0, "has_more": False}
        with patch.object(main, "STORE", store):
            payload = main.list_jobs(limit=50, offset=50, search=" 已完成 ")
        store.list_page.assert_called_once_with(limit=50, offset=50, search="已完成", search_aliases=("completed",))
        self.assertEqual(payload["search"], "已完成")

    def test_history_search_can_match_multiple_workflow_labels(self) -> None:
        store = Mock()
        store.list_page.return_value = {"jobs": []}
        with patch.object(main, "STORE", store):
            main.list_jobs(search="MD")
        store.list_page.assert_called_once_with(limit=50, offset=0, search="MD", search_aliases=("protein_md", "protein_ligand_md"))

    def test_log_endpoint_returns_incremental_offset_payload(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.log"
            path.write_text("first\nsecond\n", encoding="utf-8")
            store = Mock()
            store.get.return_value = {"id": "job-1"}
            store.log_path.return_value = path
            with patch.object(main, "STORE", store):
                first = main.job_log("job-1", offset=0, limit=6)
                second = main.job_log("job-1", offset=first["offset"], limit=64)
            self.assertEqual(first["text"] + second["text"], "first\nsecond\n")
            self.assertEqual(second["offset"], path.stat().st_size)

    def test_sse_payload_is_single_json_data_line(self) -> None:
        encoded = main._sse_event("log", {"text": "line 1\nline 2"})
        lines = encoded.splitlines()
        self.assertEqual(lines[0], "event: log")
        self.assertEqual(json.loads(lines[1].removeprefix("data: "))["text"], "line 1\nline 2")

    def test_plot_archive_download_contains_bounded_xvg_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rmsd.xvg").write_text("0 0\n1 1\n", encoding="utf-8")
            (root / "ignore.txt").write_text("not plot data", encoding="utf-8")
            store = Mock()
            store.get.return_value = {"id": "job-1", "directory_name": "job-1", "workdir": str(root)}
            with patch.object(main, "STORE", store):
                response = main.download_plot_data("job-1")
            self.assertEqual(response.media_type, "application/zip")
            with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                self.assertEqual(archive.namelist(), ["rmsd.xvg"])


if __name__ == "__main__":
    unittest.main()
