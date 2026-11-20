"""Read-only, random-access previews of saved XTC/TRR coordinates (Linux/WSL)."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import threading
from array import array
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import HTTPException

from .trajectory_imaging import cell

TRAJECTORIES = {".xtc", ".trr"}
TOPOLOGIES = {".tpr", ".gro", ".pdb"}
MAX_VISIBLE = 12_000
AUTO_SAMPLE = 4_000
TRAJECTORY_BYTES = {".xtc": 16 * 1024**3, ".trr": 4 * 1024**3}
MAX_SOURCE_ATOMS = 500_000
MAX_FRAMES = 1_000_000
MAX_CHOICES = 200
WATER = {"HOH", "H2O", "WAT", "SOL", "TIP3", "TIP3P", "TIP4", "TIP4P", "TIP5", "TIP5P", "SPC", "SPCE", "OPC", "OPC3"}
_cache = OrderedDict()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)


def choices(root: Path) -> dict:
    trajectories, topologies = [], []
    truncated = False
    if root.is_dir():
        for directory, folders, names, dir_fd in os.fwalk(root, follow_symlinks=False):
            folders.sort()
            for name in sorted(names):
                ext = Path(name).suffix.lower()
                if ext not in TRAJECTORIES | TOPOLOGIES:
                    continue
                try:
                    if not stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                        continue
                except OSError:
                    continue
                items = trajectories if ext in TRAJECTORIES else topologies
                if len(items) == MAX_CHOICES:
                    truncated = True
                else:
                    items.append((Path(directory) / name).relative_to(root).as_posix())

    def rank(name):
        path = Path(name)
        stem = path.stem.lower()
        production = stem == "md" or bool(re.search(r"(?:^|[._-])(?:prod|production)(?:$|[._-])", stem))
        return (0 if production else 1 if stem in {"traj_comp", "traj"} else 2, path.suffix.lower() != ".xtc", name)

    trajectories.sort(key=rank)
    return {"trajectories": trajectories, "topologies": topologies, "truncated": truncated}


@contextmanager
def _source(root, name, allowed, byte_limit):
    path = Path(name)
    if "\x00" in name or path.is_absolute() or ".." in path.parts or path.suffix.lower() not in allowed:
        raise HTTPException(422, "请选择任务目录中受支持的轨迹或拓扑文件。")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory = os.open(root, flags)
        try:
            for part in path.parts[:-1]:
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise HTTPException(404, "文件不存在或不是任务中的普通文件，请刷新文件列表。") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not info.st_size:
            raise HTTPException(422, "所选文件为空或不是普通文件。")
        if info.st_size > byte_limit:
            raise HTTPException(413, "文件超过动画预览上限（XTC 16 GiB、TRR 4 GiB、拓扑 64 MiB），请先截取较小的轨迹和配套拓扑。")
        yield fd
    finally:
        os.close(fd)


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _selection(ref_path, fmt, total, selection, reference=None, box=None):
    from MDAnalysis.guesser.default_guesser import DefaultGuesser
    from MDAnalysis.guesser.tables import SYMB2Z
    from MDAnalysis.topology import GROParser, PDBParser, TPRParser

    parser = {".gro": GROParser.GROParser, ".pdb": PDBParser.PDBParser, ".tpr": TPRParser.TPRParser}[fmt]
    try:
        with parser(ref_path) as source:
            top = source.parse()
    except NotImplementedError as exc:
        if fmt != ".tpr":
            raise
        raise HTTPException(422, "此 TPR 版本不受预览器支持。请选择同一阶段、同一原子顺序的 GRO 或 PDB，无需重新模拟。") from exc
    if top.n_atoms != total:
        raise HTTPException(422, f"拓扑有 {top.n_atoms} 个原子，轨迹有 {total} 个。请选择原子数量和顺序匹配的拓扑。")
    names = top.names.values
    values = getattr(top, "elements", None)
    guess = DefaultGuesser(None).guess_atom_element

    @lru_cache(maxsize=4096)
    def guessed(name):
        symbol = guess(name).capitalize()
        literal = name.strip().capitalize()
        return symbol if symbol in SYMB2Z else literal if literal in SYMB2Z else "X"

    def element_for(i):
        return str(values.values[i]).capitalize() if values is not None and values.values[i] else guessed(names[i])

    if selection == "molecular":
        from .trajectory_molecular import molecular_selection

        return molecular_selection(top, reference, box, WATER, element_for, MAX_VISIBLE, AUTO_SAMPLE)
    indices = np.arange(total)
    display_note = ""
    if selection != "all":
        water = np.isin(np.char.upper(top.resnames.values.astype(str)), list(WATER))
        mask = ~water[top.tt.atoms2residues(indices)]
        if selection == "backbone":
            mask &= np.isin(names, ["CA", "P"])
        if selection == "auto" and not mask.any():
            display_note = "全水体系，显示含水原子。"
        else:
            indices = indices[mask]
        if selection == "auto" and len(indices) > MAX_VISIBLE:
            # Sample across topology order, keeping identical atom indices in every frame.
            indices = indices[np.linspace(0, len(indices) - 1, min(AUTO_SAMPLE, MAX_VISIBLE), dtype=np.intp)]
            display_note += "大体系：按原子顺序等距选取固定子集，未抽帧。"
    if not len(indices):
        raise HTTPException(422, "所选范围没有原子，请改选「全部原子」或「隐藏水」。")
    if len(indices) > MAX_VISIBLE:
        raise HTTPException(413, f"所选范围有 {len(indices)} 个原子，预览上限为 {MAX_VISIBLE}。请改选「自动」或「仅 Cα / P 原子」。")
    return indices, [element_for(i) for i in indices], display_note, None, None


def _xtc_offsets(fd, size, atoms):
    """Index XDR frame headers only; cap memory before allocating (also for files >4 GiB)."""
    if atoms < 10:  # uncompressed floats after the 56-byte header
        count, remainder = divmod(size, 56 + atoms * 12)
        if not 0 < count <= MAX_FRAMES or remainder:
            raise HTTPException(422, "XTC 不完整或超过 100 万帧。")
        return np.arange(count, dtype=np.int64) * (56 + atoms * 12)
    # libmdaxdr reserves the first three int32 slots of its compressed-data buffer.
    max_payload = (int(atoms * 3 * 1.2) - 3) * 4
    offsets = array("q")
    pos = 0
    while pos < size:
        if len(offsets) >= MAX_FRAMES:
            raise HTTPException(422, "轨迹超过 100 万帧，请先截取所需时间段。")
        # Standard compressed XTC: 88 bytes, then the length of the padded XDR payload.
        header = os.pread(fd, 92, pos)
        if len(header) != 92 or struct.unpack_from(">ii", header) != (1995, atoms) or struct.unpack_from(">i", header, 52)[0] != atoms:
            raise HTTPException(422, "XTC 帧头不完整或格式不受支持。")
        length = struct.unpack_from(">i", header, 88)[0]
        end = pos + 92 + ((length + 3) & ~3)
        if not 0 < length <= max_payload or end > size:
            raise HTTPException(422, "XTC 坐标帧不完整。若轨迹仍在写入，请稍后重新加载。")
        offsets.append(pos)
        pos = end
    return np.frombuffer(offsets, dtype=np.int64)


def preview(root, trajectory, topology, selection, *, start=None, revision=None):
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "轨迹预览正在读取其他请求，请稍后重试。")
    try:
        with (
            _source(root, trajectory, TRAJECTORIES, TRAJECTORY_BYTES.get(Path(trajectory).suffix.lower(), 0)) as traj,
            _source(root, topology, TOPOLOGIES, 64 * 1024**2) as ref,
        ):
            return _preview(traj, ref, Path(trajectory).suffix.lower(), Path(topology).suffix.lower(), selection, start, revision)
    finally:
        _slots.release()


def _preview(traj, ref, fmt, ref_fmt, selection, start, revision):
    from MDAnalysis.lib.formats.libmdaxdr import TRRFile, XTCFile

    signatures = (_identity(traj), _identity(ref))
    key = hashlib.sha256(repr((signatures, fmt, ref_fmt, selection)).encode()).hexdigest()
    if revision is not None and key != revision:
        raise HTTPException(409, "轨迹或拓扑已更新，请重新加载动画。")
    try:
        with (XTCFile if fmt == ".xtc" else TRRFile)(f"/proc/self/fd/{traj}") as reader:
            with _lock:
                if key not in _cache:
                    if not 0 < reader.n_atoms <= MAX_SOURCE_ATOMS:
                        raise HTTPException(413, "轨迹原子数超出预览范围。请先提取较小体系。")
                    offsets = _xtc_offsets(traj, signatures[0][2], reader.n_atoms) if fmt == ".xtc" else reader.offsets
                    if not len(offsets) or len(offsets) > MAX_FRAMES:
                        raise HTTPException(422, "轨迹为空或超过 100 万帧，请先截取所需时间段。")
                    reference = box = None
                    if selection == "molecular":
                        reader.set_offsets(offsets)
                        reader.seek(0)
                        first = reader.read()
                        if getattr(first, "hasx", True) and np.isfinite(first.x).all():
                            reference = first.x.astype(float) * 10
                            box = cell(first.box)
                    indices, elements, display_note, molecular, images = _selection(
                        f"/proc/self/fd/{ref}", ref_fmt, reader.n_atoms, selection, reference, box
                    )
                    if len(_cache) >= 4:
                        _cache.popitem(last=False)
                    _cache[key] = offsets, indices, elements, display_note, molecular, images
                offsets, indices, elements, display_note, molecular, images = _cache[key]
                _cache.move_to_end(key)
            if start is None:
                result = {
                    "frames": len(offsets),
                    "elements": elements,
                    "revision": key,
                    "source_atoms": reader.n_atoms,
                    "display_note": display_note,
                    **({"molecular": molecular} if molecular is not None else {}),
                }
            else:
                if not 0 <= start < len(offsets):
                    raise HTTPException(422, "帧号超出轨迹范围。")
                reader.set_offsets(offsets)
                reader.seek(start)
                batch = []
                for index in range(start, min(len(offsets), start + min(8, 24000 // len(indices)))):
                    frame = reader.read()
                    if not getattr(frame, "hasx", True):
                        # Let earlier coordinate-bearing frames in this batch remain viewable.
                        if batch:
                            break
                        raise HTTPException(422, "此 TRR 帧未保存位置坐标。可选择其他帧或轨迹。")
                    xyz = frame.x[indices].astype(float) * 10  # native XDR nm -> viewer Å
                    if not np.isfinite(xyz).all() or not np.isfinite(frame.time):
                        if batch:
                            break
                        raise HTTPException(422, "此帧包含无效坐标或时间。")
                    if images is not None:
                        xyz = images.apply(xyz, cell(frame.box))
                    batch.append({"index": index, "time_ps": float(frame.time), "xyz": xyz.round(3).ravel().tolist()})
                result = {"frames": batch}
    except (ValueError, OSError, EOFError, IndexError, KeyError, TypeError, RuntimeError, NotImplementedError) as exc:
        raise HTTPException(422, "无法解码轨迹或拓扑。请检查文件是否完整；若 TPR 版本不兼容，可选择配套 GRO / PDB。") from exc
    if signatures != (_identity(traj), _identity(ref)):
        raise HTTPException(409, "读取期间文件已更新，请重新加载动画。")
    return result
