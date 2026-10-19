"""Bounded XVG parsing and peak-preserving chart samples."""

from __future__ import annotations

import copy
import math
import os
import re
import stat
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Iterator

MAX_XVG_BYTES = 512 * 1024**2
MAX_LINE_BYTES = 64 * 1024
MAX_COLUMNS = 13
MAX_CACHE_BYTES = 16 * 1024**2
MAX_CACHE_ENTRIES = 32
_CACHE: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
_CACHE_LOCK = Lock()

_TITLE = re.compile(r'^@\s+title\s+"([^"]+)"')
_AXIS = re.compile(r'^@\s+([xy])axis\s+label\s+"([^"]+)"')
_LEGEND = re.compile(r'^@\s+s(\d+)\s+legend\s+"([^"]+)"')
_TIME = re.compile(r"Time\s*\((ps|ns|us|ms|s)\)", re.IGNORECASE)
_NS_FACTORS = {"ps": 0.001, "ns": 1, "us": 1000, "ms": 1e6, "s": 1e9}


class XvgUnavailable(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _lines(stream: BinaryIO) -> Iterator[str]:
    consumed = 0
    while raw := stream.readline(MAX_LINE_BYTES + 1):
        consumed += len(raw)
        if consumed > MAX_XVG_BYTES:
            raise XvgUnavailable("file_too_large", "文件在读取过程中超过 512 MiB 展示上限；请下载完整 XVG。")
        if len(raw) > MAX_LINE_BYTES:
            raise XvgUnavailable("line_too_long", "文件包含超过 64 KiB 的单行，无法安全展示；请下载完整 XVG。")
        yield raw.decode("utf-8", errors="replace").strip()


def _row(line: str) -> list[float] | None:
    if not line or line[0] in "#@&":
        return None
    try:
        values = [float(value) for value in line.split()]
    except ValueError:
        return None
    if len(values) < 2 or not all(math.isfinite(value) for value in values):
        return None
    return values[:MAX_COLUMNS]


def _include(x: float, factor: float | None, begin: float | None, end: float | None) -> bool:
    if begin is None and end is None:
        return True
    time_ns = x * factor
    return (begin is None or time_ns >= begin) and (end is None or time_ns <= end)


def _summary(stream: BinaryIO, metadata: dict, *, factor=None, begin=None, end=None, read_headers=False) -> dict:
    count = 0
    width = MAX_COLUMNS
    means = [0.0] * (MAX_COLUMNS - 1)
    last = []
    for line in _lines(stream):
        if read_headers and line.startswith("@"):
            if match := _TITLE.match(line):
                metadata["title"] = match.group(1)[:160]
            elif match := _AXIS.match(line):
                metadata[f"{match.group(1)}_label"] = match.group(2)[:120]
            elif match := _LEGEND.match(line):
                index = int(match.group(1))
                if index < MAX_COLUMNS - 1:
                    metadata["legends"][index] = match.group(2)[:120]
        values = _row(line)
        if values is None or not _include(values[0], factor, begin, end):
            continue
        count += 1
        width = min(width, len(values))
        for index, value in enumerate(values[1:width]):
            delta = value - means[index]
            means[index] = means[index] + delta / count if math.isfinite(delta) else means[index] * (1 - 1 / count) + value / count
        last = values
    return {"count": count, "width": width, "means": means[: width - 1], "last": last[1:width]}


def _sample(stream: BinaryIO, summary: dict, max_points: int, *, factor=None, begin=None, end=None) -> list[list]:
    count, width = summary["count"], summary["width"]
    points = [[] for _ in range(width - 1)]
    # Each bucket contributes at most its minimum and maximum, plus both endpoints.
    bucket_count = (max_points - 2) // 2 if count > max_points else 0
    buckets = [[None] * bucket_count for _ in points]
    index = 0
    for line in _lines(stream):
        row = _row(line)
        if row is None or not _include(row[0], factor, begin, end):
            continue
        if count <= max_points or index in (0, count - 1):
            for series, value in enumerate(row[1:width]):
                points[series].append((index, [row[0], value]))
        elif bucket_count:
            bucket = min(bucket_count - 1, (index - 1) * bucket_count // (count - 2))
            for series, value in enumerate(row[1:width]):
                candidate = (index, [row[0], value])
                extrema = buckets[series][bucket]
                if extrema is None:
                    buckets[series][bucket] = [candidate, candidate]
                else:
                    if value < extrema[0][1][1]:
                        extrema[0] = candidate
                    if value > extrema[1][1][1]:
                        extrema[1] = candidate
        index += 1
    for series, extrema in enumerate(buckets):
        for pair in extrema:
            if pair is not None:
                points[series].extend(pair)
    return [[row for _, row in sorted(dict(samples).items())] for samples in points]


def load_xvg(path: Path, max_points: int, max_bytes: int, begin: float | None, end: float | None) -> dict:
    """Cache only bounded chart data; invalidate on replacement, append or rewrite."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise XvgUnavailable("not_regular", "仅支持普通 XVG 文件。")
    if info.st_size > max_bytes:
        raise XvgUnavailable("file_too_large", f"文件超过 {max_bytes / 1024**2:g} MiB 展示上限；请下载完整 XVG。")
    fingerprint = _fingerprint(info)
    key = (str(path), fingerprint, max_points, begin, end)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return copy.deepcopy(_CACHE[key][0])
    metadata = {"title": path.stem.replace("_", " ").replace("-", " "), "x_label": "X", "y_label": "Value", "legends": {}}
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
            raise XvgUnavailable("file_changed", "文件正在更新，请稍后刷新曲线。")
        full = _summary(stream, metadata, read_headers=True)
        time_match = _TIME.fullmatch(metadata["x_label"])
        time_unit = time_match.group(1).lower() if time_match else None
        factor = _NS_FACTORS.get(time_unit)
        summary = full
        if begin is not None or end is not None:
            if factor is None:
                raise XvgUnavailable("non_time_axis", "此文件横轴不是时间，无法应用时间区间筛选。")
            stream.seek(0)
            summary = _summary(stream, metadata, factor=factor, begin=begin, end=end)
        if summary["count"] < 2:
            raise XvgUnavailable("insufficient_points", "当前筛选范围内少于两个有效数据点。")
        if any(token in path.stem.lower() for token in ("protein-rg", "protein-sasa", "protein-ligand-hbonds")):
            summary["width"] = 2
        stream.seek(0)
        points = _sample(stream, summary, max_points, factor=factor, begin=begin, end=end)
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint or _fingerprint(path.stat()) != fingerprint:
            raise XvgUnavailable("file_changed", "文件正在更新，请稍后刷新曲线。")
    result = {
        **metadata,
        "points": points,
        "sample_count": summary["count"],
        "original_count": full["count"],
        "time_unit": time_unit,
        "means": summary["means"],
        "last": summary["last"],
    }
    # The weight includes Python lists, floats, dictionaries and cached strings.
    weight = 4096 + sum(4096 + 256 * len(series) for series in points)
    if weight <= MAX_CACHE_BYTES:
        with _CACHE_LOCK:
            # Discard previous versions/windows of this file before admitting another.
            for old_key in [item for item in _CACHE if item[0] == str(path) and item[1] != fingerprint]:
                del _CACHE[old_key]
            while _CACHE and (len(_CACHE) >= MAX_CACHE_ENTRIES or sum(item[1] for item in _CACHE.values()) + weight > MAX_CACHE_BYTES):
                _CACHE.popitem(last=False)
            _CACHE[key] = (copy.deepcopy(result), weight)
    return result
