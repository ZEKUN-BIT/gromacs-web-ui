"""Durable upload identities, including cancellation before multipart parsing ends."""

from __future__ import annotations

import hashlib
import json
import re

from fastapi import HTTPException


def validate_submission_id(request_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise HTTPException(status_code=422, detail="Invalid submission identifier.")
    return request_id


def _schema(connection) -> None:
    # Deliberately no cascading foreign key: deleting a job must not make a
    # delayed retry eligible to create it again.
    connection.execute("""CREATE TABLE IF NOT EXISTS upload_submissions (
        request_id TEXT PRIMARY KEY,
        fingerprint TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','cancelled')),
        job_id TEXT
    )""")


def _result(store, row) -> dict:
    if row is None:
        return {"state": "missing"}
    if row["job_id"]:
        try:
            return {"state": "submitted", "job": store.get(row["job_id"])}
        except FileNotFoundError:
            return {"state": "deleted", "job_id": row["job_id"]}
    return {"state": row["state"]}


def submission_status(store, request_id: str, *, cancel: bool = False) -> dict:
    validate_submission_id(request_id)
    with store._connect() as connection:
        _schema(connection)
        connection.execute("BEGIN IMMEDIATE" if cancel else "BEGIN")
        try:
            row = connection.execute("SELECT * FROM upload_submissions WHERE request_id=?", (request_id,)).fetchone()
            if cancel and (row is None or not row["job_id"]):
                connection.execute(
                    "INSERT INTO upload_submissions(request_id,state) VALUES(?,'cancelled') "
                    "ON CONFLICT(request_id) DO UPDATE SET state='cancelled' WHERE job_id IS NULL",
                    (request_id,),
                )
                row = connection.execute("SELECT * FROM upload_submissions WHERE request_id=?", (request_id,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _result(store, row)


def prepare_submission(store, request_id: str, params: dict, hashes: dict, name_map: dict) -> dict:
    validate_submission_id(request_id)
    content = {
        "params": params,
        "files": {name: {key: item[key] for key in ("size", "sha256")} for name, item in hashes.items()},
        "names": name_map,
    }
    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with store._connect() as connection:
        _schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT * FROM upload_submissions WHERE request_id=?", (request_id,)).fetchone()
            if row and row["state"] == "cancelled":
                raise HTTPException(status_code=409, detail="本次上传已取消，请重新提交。")
            if row and row["fingerprint"] != fingerprint:
                raise HTTPException(status_code=409, detail="提交标识已用于不同的参数或文件，请核对原任务。")
            if row is None:
                connection.execute(
                    "INSERT INTO upload_submissions(request_id,fingerprint,state) VALUES(?,?,'pending')", (request_id, fingerprint)
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _result(store, row) if row else {"state": "pending"}
