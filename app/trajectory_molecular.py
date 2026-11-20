"""Fixed, residue-aware display topology for the trajectory viewer (coordinates in Å)."""

import numpy as np
from fastapi import HTTPException

from .trajectory_imaging import FragmentImages, nearby, short_vector

# Only infer common covalent bonds, never metal coordination or contacts between residues.
_RADII = {
    "B": 0.85,
    "C": 0.76,
    "N": 0.71,
    "O": 0.66,
    "F": 0.57,
    "Si": 1.11,
    "P": 1.07,
    "S": 1.05,
    "Cl": 1.02,
    "Br": 1.20,
    "I": 1.39,
    "Se": 1.20,
}


def _bonds(top, indices, ligand_groups, elems, xyz, box):

    ligand = {int(indices[i]): i for group in ligand_groups for i in group}
    pairs = []
    known = set()
    topology_bonds = getattr(top, "bonds", None)
    if topology_bonds is not None:
        for a, b in topology_bonds.values:
            if a in ligand and b in ligand:
                i, j = ligand[a], ligand[b]
                pairs.append([i, j])
                known.update((i, j))
    inferred = False
    if xyz is not None:
        for group in ligand_groups:
            if len(group) < 2 or not known.isdisjoint(group):
                continue
            local = [i for i in group if elems[i] in _RADII]
            if len(local) < 2:
                continue
            coords = xyz[indices[local]]
            # Cap candidates per atom even for coincident/malformed coordinates.
            dist, near = nearby(coords, coords, box, k=min(9, len(local)))
            for row, i in enumerate(local):
                for d, col in zip(dist[row], near[row], strict=True):
                    if col <= row or col >= len(local):
                        continue
                    j = local[col]
                    if 0.45 < d <= _RADII[elems[i]] + _RADII[elems[j]] + 0.40:
                        pairs.append([i, j])
                        inferred = True
    return pairs, inferred


def molecular_selection(top, xyz, box, water, element_for, limit, sample):
    """Return one index order shared by metadata, bonds and all subsequent coordinate batches."""
    from MDAnalysis.analysis.dssp import assign
    from MDAnalysis.core.selection import ProteinSelection

    names = top.names.values
    resnames = np.char.upper(top.resnames.values.astype(str))
    resids = top.resids.values
    chains = getattr(top, "chainIDs", None)
    segments = top.tt.residues2segments(np.arange(top.n_residues))
    nonwater = np.flatnonzero(~np.isin(resnames, list(water)))
    if not len(nonwater):
        raise HTTPException(422, "水已隐藏，没有可显示的原子。要查看水，请选择含水的原子球视图。")

    # A cartoon needs an ordered backbone, not random individual atoms.
    backbone = {}
    others = {}
    for r, atoms in zip(nonwater, top.tt.residues2atoms_2d(nonwater), strict=True):
        if resnames[r] in ProteinSelection.prot_res:
            by_name = dict(zip(names[atoms], atoms, strict=True))
            oxygen = next((key for key in ("O", "OT1", "OC1", "O1") if key in by_name), None)
            if oxygen and all(key in by_name for key in ("N", "CA", "C")):
                backbone[r] = [by_name[key] for key in ("N", "CA", "C", oxygen)]
                continue
        heavy = [i for i in atoms if element_for(i) not in {"H", "D"}]
        if heavy:
            others[r] = heavy

    nback = 4 * len(backbone)
    if nback > limit:
        raise HTTPException(413, "完整蛋白主链超过预览上限，请改选「仅 Cα / P 原子」或提取较小体系。")
    total = nback + sum(map(len, others.values()))
    if not total:
        raise HTTPException(422, "此视图没有可显示的重原子，请选择原子球视图。")
    keep = list(others)
    if total > limit:
        budget = min(limit - nback, max(sample - nback, min(sample // 4, sum(map(len, others.values())))))
        # Prefer whole PET residues at the protein interface; do not reselect on seek.
        closest = {}
        if backbone and xyz is not None and others:
            lengths = np.fromiter(map(len, others.values()), dtype=int)
            distances = nearby(xyz[np.asarray(list(backbone.values())).ravel()], xyz[np.concatenate(list(others.values()))], box)[0]
            closest = dict(zip(others, np.minimum.reduceat(distances, np.cumsum(lengths) - lengths), strict=True))
        keep = []
        for r in sorted(others, key=lambda r: (not resnames[r].startswith("PET"), closest.get(r, r))):
            if len(others[r]) <= budget:
                keep.append(r)
                budget -= len(others[r])
                if not budget:
                    break
        if not keep and not backbone:
            raise HTTPException(413, "没有完整残基能放入预览范围，请改选原子球视图。")

    # Split chains at topology or peptide discontinuities; never bridge adjacent protein copies.
    runs = []
    for r, atoms in backbone.items():
        prev = runs[-1][-1] if runs else None
        if (
            prev is None
            or r != prev + 1
            or segments[r] != segments[prev]
            or resids[r] != resids[prev] + 1
            or (chains is not None and chains.values[atoms[0]] != chains.values[backbone[prev][0]])
            or (xyz is not None and np.linalg.norm(short_vector(xyz[atoms[0]] - xyz[backbone[prev][2]], box)) > 2.1)
        ):
            runs.append([])
        runs[-1].append(r)

    indices = []
    specs = []
    ligand_groups = []
    note = []
    long_chain = False
    backbone_edges = []
    anchor = None
    longest = 1
    for chain, residues in enumerate(runs):
        first = len(indices)
        if len(residues) > longest:
            anchor, longest = first, len(residues)
        for offset in range(0, 4 * len(residues), 4):
            n = first + offset
            backbone_edges.extend([(n, n + 1), (n + 1, n + 2), (n + 2, n + 3)])
            if offset:
                backbone_edges.append((n - 2, n))
        # Fixed reference labels, moving coordinates. Avoid quadratic DSSP allocation on very long chains.
        ss = "c" * len(residues)
        if xyz is not None and 6 <= len(residues) <= 512:
            coords = xyz[np.asarray([backbone[r] for r in residues])]
            if box is not None:
                # Consecutive N,CA,C,O and following N are close within an intact peptide.
                flat = coords.reshape(-1, 3)
                coords = np.vstack([flat[0], flat[0] + np.cumsum(short_vector(np.diff(flat, axis=0), box), axis=0)]).reshape(coords.shape)
            with np.errstate(divide="ignore", invalid="ignore"):
                ss = "".join("chs"[label] for label in assign(coords).argmax(axis=1))
        long_chain |= len(residues) > 512
        for r, label in zip(residues, ss, strict=True):
            ribbon = len(residues) > 1
            if not ribbon:
                ligand_groups.append(range(len(indices), len(indices) + 4))
            for atom, i in zip(("N", "CA", "C", "O"), backbone[r], strict=True):
                indices.append(i)
                specs.append([atom, str(resnames[r]), int(resids[r]), chain, label if ribbon else ""])
    for r in sorted(keep):
        group = others[r]
        ligand_groups.append(range(len(indices), len(indices) + len(group)))
        for i in group:
            indices.append(i)
            specs.append([str(names[i]), str(resnames[r]), int(resids[r]), -1, ""])
    selected = np.asarray(indices, dtype=np.intp)
    elems = [element_for(i) for i in selected]
    pairs, inferred = _bonds(top, selected, ligand_groups, elems, xyz, box)
    images = FragmentImages.build(len(indices), [*backbone_edges, *pairs], anchor)
    if xyz is not None:
        images.apply(xyz[selected], box, reference=True)
    if len(keep) < len(others):
        pet = sum(resnames[r].startswith("PET") for r in others)
        kept = sum(resnames[r].startswith("PET") for r in keep)
        note.append(f"大体系：固定显示 {kept}/{pet} 个 PET 残基。" if pet else "大体系：按完整残基选取固定子集。")
    if long_chain:
        note.append("超长链仅显示主链走向。")
    if xyz is None:
        note.append("首帧坐标不可用，未推断二级结构或化学键。")
    if inferred:
        note.append("无拓扑键的残基按首帧距离推断连线。")
    return selected, elems, "".join(note), {"atoms": specs, "bonds": pairs}, images
