"""Local protocol library. Versions are immutable and contain no uploaded files."""

from __future__ import annotations

import json
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .files import FILE_REFERENCE_FIELDS
from .gromacs import utc_now
from .models import SimulationParams


class JobOrganization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(default="", max_length=80)
    tags: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=4000)
    pinned: bool = False

    @field_validator("project", "notes")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class ProtocolTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    applicability: str = Field(default="", max_length=500)
    source: str = Field(default="", max_length=500)
    params: SimulationParams
    schema_version: int = Field(default=1, ge=1, le=1)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请填写模板名称。")
        return value.strip()

    @field_validator("params")
    @classmethod
    def finite_parameters(cls, value: SimulationParams) -> SimulationParams:
        # JSON parsers can accept NaN/Infinity; SQLite and portable templates cannot.
        try:
            json.dumps(value.model_dump(), allow_nan=False)
        except ValueError as exc:
            raise ValueError("模板中的数值必须是有限数。") from exc
        return value


def template_payload(template: ProtocolTemplate) -> dict:
    payload = template.model_dump()
    params = payload["params"]
    # Files, local executable paths, project identities and charge confirmations
    # must be chosen again on the destination machine.
    for key in list(params):
        if (
            key in FILE_REFERENCE_FIELDS
            or key.endswith("_bin")
            or key in {"files", "name", "project", "tags", "notes", "ligand_charge_confirmed", "dry_run"}
        ):
            params.pop(key, None)
    for ligand in params.get("ligands") or []:
        for key in list(ligand):
            if key.endswith("_file") or key == "charge_confirmed":
                ligand.pop(key, None)
    return payload


def list_templates(store) -> list[dict]:
    with store._connect() as connection:
        rows = connection.execute("SELECT payload FROM protocol_templates ORDER BY created_at DESC, revision DESC LIMIT 200").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def save_template(store, template: ProtocolTemplate) -> dict:
    payload = template_payload(template)
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 128 * 1024:
        raise ValueError("模板超过 128 KiB，请缩短自定义参数。")
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = connection.execute(
            "SELECT max(coalesce((SELECT max(revision) FROM protocol_templates WHERE name=?),0), "
            "coalesce((SELECT last_revision FROM protocol_template_revisions WHERE name=?),0))+1",
            (payload["name"], payload["name"]),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO protocol_template_revisions(name,last_revision) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET last_revision=excluded.last_revision",
            (payload["name"], revision),
        )
        payload.update(id=uuid4().hex, revision=revision, created_at=utc_now())
        connection.execute(
            "INSERT INTO protocol_templates(id,name,revision,created_at,payload) VALUES(?,?,?,?,?)",
            (payload["id"], payload["name"], revision, payload["created_at"], json.dumps(payload, ensure_ascii=False)),
        )
        connection.commit()
    return payload


def project_catalog(store) -> list[dict]:
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT coalesce(json_extract(meta_json,'$.project'),'') AS name,count(*) AS count "
            "FROM jobs GROUP BY name ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]
