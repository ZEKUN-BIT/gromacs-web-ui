import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import app.main as main
from app.execution import WorkerEngine
from app.gromacs import Step
from app.job_store import JobStore


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def first_sse_log_event(job_id: str, log_tail: int = 0) -> dict:
    response = await main.job_events(request=FakeRequest(), job_id=job_id, log_tail=log_tail)
    body = ""
    async for chunk in response.body_iterator:
        body += chunk
        if "event: log" in body:
            break
    for block in body.split("\n\n"):
        if "event: log" not in block:
            continue
        data_line = next(line for line in block.splitlines() if line.startswith("data: "))
        return json.loads(data_line[len("data: ") :])
    raise AssertionError("no log event received")


class MemoryUpload:
    filename = "input.tpr"

    def __init__(self) -> None:
        self.sent = False

    async def read(self, _size: int) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return b"fake tpr"


class JobApiEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_stream_uses_requested_history_page_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            for index in range(55):
                store.create(f"historical-{index:02}", {"workflow": "custom"}, [], [])
            with patch.object(main, "STORE", store):
                response = await main.job_events(request=FakeRequest(), jobs_offset=50)
                iterator = response.body_iterator
                try:
                    event = await anext(iterator)
                finally:
                    await iterator.aclose()
                page = json.loads(event.split("data: ", 1)[1].strip())
                self.assertEqual(page["offset"], 50)
                self.assertEqual(page["total"], 55)
                self.assertEqual(len(page["jobs"]), 5)
                response = await main.job_events(request=FakeRequest(), jobs_search="historical-03")
                iterator = response.body_iterator
                try:
                    event = await anext(iterator)
                finally:
                    await iterator.aclose()
                page = json.loads(event.split("data: ", 1)[1].strip())
                self.assertEqual(page["total"], 1)
                self.assertEqual(page["jobs"][0]["name"], "historical-03")

    async def test_event_stream_log_tail_starts_near_end(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            step = Step("noop", ["/bin/true"])
            meta = store.create("tail", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            store.log_path(meta["id"]).write_text("H" * 200_000 + "T" * 5_000, encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(patch.object(main, "STORE", store))
                first_log = await first_sse_log_event(meta["id"], log_tail=2000)
            self.assertIn("text", first_log)
            self.assertTrue(first_log["text"].startswith("T"), f"expected tail content, got {first_log['text'][:20]!r}")
            self.assertEqual(first_log["start"], 205_000 - 2000)

    async def test_event_stream_without_log_tail_starts_at_beginning(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            step = Step("noop", ["/bin/true"])
            meta = store.create("tail2", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            store.log_path(meta["id"]).write_text("H" * 5_000, encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(patch.object(main, "STORE", store))
                first_log = await first_sse_log_event(meta["id"])
            self.assertTrue(first_log["text"].startswith("H"))
            self.assertEqual(first_log["start"], 0)

    async def test_create_job_api_upload_and_fake_gromacs_worker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_gmx = root / "gmx"
            fake_gmx.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "print('FAKE_GROMACS', ' '.join(sys.argv[1:]), flush=True)\n"
                "if 'mdrun' in sys.argv:\n"
                " print('step 5 time 0.01', flush=True)\n"
                " pathlib.Path('fake-gromacs-output.txt').write_text('ok', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_gmx.chmod(0o755)
            store = JobStore(root)
            settings = {
                "runtime_root": str(root),
                "gmx_bin": str(fake_gmx),
                "max_parallel": 1,
                "default_ntmpi": 1,
                "default_ntomp": 1,
            }
            payload = {"name": "API fake GROMACS", "workflow": "run_tpr", "tpr_file": "input.tpr"}

            with ExitStack() as stack:
                stack.enter_context(patch.object(main, "RUNTIME_ROOT", root))
                stack.enter_context(patch.object(main, "STORE", store))
                stack.enter_context(patch.object(main, "current_settings", return_value=settings))
                submitted = await main.create_job(
                    params=json.dumps(payload),
                    files=[MemoryUpload()],
                )

            self.assertEqual(submitted["status"], "queued")
            self.assertEqual(submitted["uploaded_files"], ["input.tpr"])
            self.assertEqual(submitted["commands"][0]["args"][0], str(fake_gmx))
            self.assertTrue(WorkerEngine(store, "worker:e2e").run_once())
            completed = store.get(submitted["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["progress_percent"], 100.0)
            self.assertEqual(completed["simulation_progress"]["step"], 5)
            self.assertEqual(completed["step_runs"][0]["status"], "completed")
            self.assertIn("duration_seconds", completed["step_runs"][0])
            output = Path(completed["workdir"]) / "fake-gromacs-output.txt"
            self.assertEqual(output.read_text(encoding="utf-8"), "ok")
            self.assertIn("FAKE_GROMACS mdrun", store.log_path(submitted["id"]).read_text(encoding="utf-8"))
            manifest = json.loads((Path(completed["workdir"]) / "experiment-manifest.json").read_text(encoding="utf-8"))
            input_tpr = next(item for item in manifest["input_files"] if item["path"] == "input.tpr")
            self.assertEqual(len(input_tpr["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
