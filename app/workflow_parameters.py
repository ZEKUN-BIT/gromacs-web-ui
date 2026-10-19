"""Shared workflow parameter coercion and file-name selection."""

from __future__ import annotations

import re
from typing import Iterable


def slugify(value: str, fallback: str = "job") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return value[:64] or fallback


def directory_slug(value: str, fallback: str = "job") -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-._")
    return value[:48] or fallback


def find_first(files: Iterable[str], suffixes: tuple[str, ...]) -> str | None:
    for name in files:
        if name.lower().endswith(suffixes):
            return name
    return None


def topology_file(files: Iterable[str]) -> str:
    return find_first(files, (".top",)) or "topol.top"


def maxwarn_args(maxwarn: int) -> list[str]:
    return ["-maxwarn", str(max(0, int(maxwarn)))] if int(maxwarn) > 0 else []


def bool_param(params: dict, key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def str_param(params: dict, key: str, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def output_name_param(params: dict, key: str, default: str, label: str) -> str:
    value = str_param(params, key, default)
    if value in {".", ".."} or ".." in value or "/" in value or "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{label} must be a file name only, without absolute paths, '..', '/', or '\\'.")
    return value


def int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def float_param(params: dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def file_param(params: dict, key: str, files: Iterable[str], suffixes: tuple[str, ...], fallback: str | None = None) -> str | None:
    requested = str_param(params, key, "")
    if requested:
        return requested
    found = find_first(files, suffixes)
    return found or fallback
