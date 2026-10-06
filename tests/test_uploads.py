import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.files import _positive_env_int, read_log_chunk, relative_files, stage_uploads


class FakeUpload:
    def __init__(self, name: str, content: bytes) -> None:
        self.filename = name
        self._content = content
        self._read = False

    async def read(self, _size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._content


class InterruptedUpload:
    filename = "partial.xtc"

    def __init__(self, error: OSError) -> None:
        self.calls = 0
        self.error = error

    async def read(self, _size: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise self.error


def upload(name: str, content: bytes) -> FakeUpload:
    return FakeUpload(name, content)


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_names_are_staged_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            names, _ = await stage_uploads(
                [upload("input.pdb", b"first"), upload("input.pdb", b"second")],
                Path(folder),
                max_files=2,
                max_file_bytes=10,
                max_total_bytes=20,
            )

            self.assertEqual(names, ["input.pdb", "input-2.pdb"])
            self.assertEqual((Path(folder) / "input.pdb").read_bytes(), b"first")
            self.assertEqual((Path(folder) / "input-2.pdb").read_bytes(), b"second")

    async def test_single_file_limit_returns_413(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(HTTPException) as raised:
                await stage_uploads(
                    [upload("large.xtc", b"12345")],
                    Path(folder),
                    max_files=1,
                    max_file_bytes=4,
                    max_total_bytes=10,
                )

            self.assertEqual(raised.exception.status_code, 413)

    async def test_total_limit_returns_413(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(HTTPException) as raised:
                await stage_uploads(
                    [upload("one.dat", b"123"), upload("two.dat", b"456")],
                    Path(folder),
                    max_files=2,
                    max_file_bytes=4,
                    max_total_bytes=5,
                )

            self.assertEqual(raised.exception.status_code, 413)

    async def test_file_count_limit_returns_413_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(HTTPException) as raised:
                await stage_uploads(
                    [upload("one.dat", b"1"), upload("two.dat", b"2")],
                    Path(folder),
                    max_files=1,
                )

            self.assertEqual(raised.exception.status_code, 413)
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_interrupted_upload_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ConnectionError, "upload interrupted"):
                await stage_uploads([InterruptedUpload(ConnectionError("upload interrupted"))], Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_disk_full_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            error = OSError(errno.ENOSPC, "No space left on device")
            with self.assertRaises(OSError) as raised:
                await stage_uploads([InterruptedUpload(error)], Path(folder))
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(list(Path(folder).iterdir()), [])


class EnvironmentTests(unittest.TestCase):
    def test_invalid_positive_integer_uses_default(self) -> None:
        with patch.dict(os.environ, {"UPLOAD_TEST_LIMIT": "not-a-number"}):
            self.assertEqual(_positive_env_int("UPLOAD_TEST_LIMIT", 7), 7)


class BoundedFileReadTests(unittest.TestCase):
    def test_log_chunk_uses_offsets_without_splitting_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.log"
            path.write_text("开始\nfinished\n", encoding="utf-8")
            first = read_log_chunk(path, 0, 2)
            second = read_log_chunk(path, first["offset"], 1024)
            self.assertEqual(first["text"] + second["text"], "开始\nfinished\n")
            self.assertEqual(second["offset"], path.stat().st_size)

    def test_relative_file_listing_stops_at_limit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(5):
                (root / f"{index}.dat").write_text(str(index), encoding="utf-8")
            self.assertEqual(len(relative_files(root, max_files=3)), 3)


if __name__ == "__main__":
    unittest.main()
