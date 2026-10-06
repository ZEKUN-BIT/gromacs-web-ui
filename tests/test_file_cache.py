import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app.main as main


class FileListCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        main.FILE_LIST_CACHE.clear()

    def test_terminal_job_file_list_is_cached(self) -> None:
        store = Mock()
        store.get.return_value = {
            "id": "job-1",
            "status": "completed",
            "updated_at": "2026-08-12T00:00:00Z",
            "workdir": "/tmp/job-1",
        }
        files = [{"path": "result.xtc", "size": 10, "modified": "now"}]

        with patch.object(main, "STORE", store), patch.object(main, "relative_files", return_value=files) as scan:
            first = main.job_files("job-1")
            second = main.job_files("job-1")

        self.assertEqual(first, second)
        scan.assert_called_once()

    def test_running_job_file_list_is_cached_until_step_changes(self) -> None:
        store = Mock()
        store.get.return_value = {
            "id": "job-2",
            "status": "running",
            "updated_at": "2026-08-12T00:00:00Z",
            "workdir": "/tmp/job-2",
        }

        with patch.object(main, "STORE", store), patch.object(main, "relative_files", return_value=[]) as scan:
            main.job_files("job-2")
            main.job_files("job-2")
            scan.assert_called_once_with(Path("/tmp/job-2"), max_files=main.MAX_FILE_LIST_ITEMS + 1)
            store.get.return_value["step_index"] = 2
            main.job_files("job-2")
            self.assertEqual(scan.call_count, 2)


if __name__ == "__main__":
    unittest.main()
