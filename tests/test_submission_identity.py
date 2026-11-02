from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app.job_store import JobStore
from app.submissions import prepare_submission, submission_status


class MemoryUpload:
    filename = "input.tpr"

    def __init__(self, content=b"input"):
        self.content = content

    async def read(self, _size):
        content, self.content = self.content, b""
        return content


class SubmissionIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = JobStore(self.root)
        self.context = ExitStack()
        self.context.enter_context(patch.object(main, "STORE", self.store))
        self.context.enter_context(patch.object(main, "RUNTIME_ROOT", self.root))
        self.context.enter_context(patch.object(main, "current_settings", return_value={"gmx_bin": "gmx"}))
        self.request_id = "a" * 32
        self.params = {"name": "idempotent", "workflow": "run_tpr", "tpr_file": "input.tpr", "dry_run": True}

    def tearDown(self):
        self.context.close()
        self.directory.cleanup()

    async def submit(self, content=b"input"):
        return await main.create_job(json.dumps(self.params), [MemoryUpload(content)], self.request_id)

    async def test_retry_returns_same_job_after_store_reopened(self):
        first = await self.submit()
        with patch.object(main, "STORE", JobStore(self.root)):
            second = await self.submit()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(main.resolve_submission(self.request_id)["job"]["id"], first["id"])

    async def test_same_identity_rejects_different_file_contents(self):
        await self.submit()
        with self.assertRaises(HTTPException) as error:
            await self.submit(b"other")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(len(self.store.list()), 1)

    async def test_cancel_before_delayed_multipart_request_prevents_creation(self):
        self.assertEqual(main.resolve_submission(self.request_id), {"state": "cancelled"})
        with self.assertRaises(HTTPException) as error:
            await self.submit()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.list(), [])
        self.assertFalse((self.root / "incoming").exists())

    async def test_deleted_job_does_not_reenable_replay(self):
        first = await self.submit()
        self.store.cancel(first["id"])
        self.store.delete(first["id"])
        self.assertEqual(main.get_submission(self.request_id)["state"], "deleted")
        with self.assertRaises(HTTPException) as error:
            await self.submit()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.list(), [])

    async def test_concurrent_retries_create_one_job(self):
        import asyncio

        results = await asyncio.gather(self.submit(), self.submit(), return_exceptions=True)
        self.assertEqual(len(self.store.list()), 1)
        successful = [value for value in results if isinstance(value, dict)]
        self.assertTrue(successful)
        self.assertEqual(main.resolve_submission(self.request_id)["job"]["id"], successful[0]["id"])
        for value in results:
            if isinstance(value, Exception):
                self.assertIsInstance(value, HTTPException)
                self.assertEqual(value.status_code, 409)

    def test_cancel_racing_atomic_job_creation_cannot_leave_orphan(self):
        prepare_submission(self.store, self.request_id, {}, {}, {})
        barrier = threading.Barrier(2)

        def create():
            barrier.wait()
            try:
                return self.store.create("racing", {}, [], [], submission_id=self.request_id)
            except RuntimeError:
                return None

        def cancel():
            barrier.wait()
            return submission_status(self.store, self.request_id, cancel=True)

        with ThreadPoolExecutor(2) as executor:
            creation, cancellation = executor.submit(create), executor.submit(cancel)
            job, result = creation.result(), cancellation.result()
        if job:
            self.assertEqual(result["state"], "submitted")
            self.assertEqual(result["job"]["id"], job["id"])
        else:
            self.assertEqual(result["state"], "cancelled")
            self.assertEqual(self.store.list(), [])
            self.assertEqual(list(self.store.jobs_root.iterdir()), [])

    async def test_invalid_identifier_rejected_before_staging(self):
        self.request_id = "../unsafe"
        with self.assertRaises(HTTPException) as error:
            await self.submit()
        self.assertEqual(error.exception.status_code, 422)
        self.assertFalse((self.root / "incoming").exists())

    def test_resolve_endpoint_rejects_cross_origin_before_mutation(self):
        client = TestClient(main.app, base_url="http://127.0.0.1:8000")
        path = f"/api/submissions/{self.request_id}"
        self.assertEqual(client.get(path).json(), {"state": "missing"})
        response = client.post(path + "/resolve", headers={"origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get(path).json(), {"state": "missing"})
        self.assertEqual(client.post(path + "/resolve").json(), {"state": "cancelled"})
        self.assertEqual(client.post(path + "/resolve").json(), {"state": "cancelled"})
        self.assertEqual(client.get("/api/submissions/not-valid").status_code, 422)

    def test_legacy_multipart_without_submission_identity_remains_supported(self):
        client = TestClient(main.app, base_url="http://127.0.0.1:8000")
        response = client.post(
            "/api/jobs", data={"params": json.dumps(self.params)}, files={"files": ("input.tpr", b"input", "application/octet-stream")}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(len(self.store.list()), 1)
