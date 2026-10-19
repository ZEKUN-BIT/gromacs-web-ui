"""Ligand chemistry mapping, parameterization artifacts, and topology integration."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from .files import ensure_under_root
from .structure import read_gro


def copy_to(workdir: Path, source: str, target: str) -> None:
    src = ensure_under_root(workdir / source, workdir)
    dst = ensure_under_root(workdir / target, workdir)
    if src.resolve() == dst.resolve():
        return
    shutil.copyfile(src, dst)


def stage_prepared_ligand(workdir: Path, data: dict) -> None:
    copy_to(workdir, str(data["ligand_gro"]), str(data.get("gro_output") or "ligand_GMX.gro"))
    copy_to(workdir, str(data["ligand_itp"]), str(data.get("itp_output") or "ligand_GMX.itp"))


def write_ligand_smiles(workdir: Path, data: dict) -> None:
    smiles = str(data.get("smiles", "")).strip()
    if not smiles or len(smiles) > 2000 or any(character in smiles for character in "\r\n\0"):
        raise ValueError("Ligand SMILES must be a single non-empty line no longer than 2000 characters")
    output = ensure_under_root(workdir / str(data["output"]), workdir)
    output.write_text(f"{smiles}\tligand\n", encoding="utf-8")


def pdb_pose_atoms(path: Path) -> tuple[list[dict], set[tuple[int, int]]]:
    atoms: list[dict] = []
    serial_to_index: dict[int, int] = {}
    conect_serials: list[tuple[int, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"} and len(line) >= 54:
            element = line[76:78].strip().upper() if len(line) >= 78 else ""
            if not element:
                element = re.sub(r"[^A-Za-z]", "", line[12:16]).upper()[:1]
            serial = int(line[6:11])
            serial_to_index[serial] = len(atoms)
            atoms.append({"element": element, "xyz": (float(line[30:38]), float(line[38:46]), float(line[46:54]))})
        elif record == "CONECT":
            fields = line[6:].split()
            if fields:
                source = int(fields[0])
                conect_serials.extend((source, int(target)) for target in fields[1:])
    heavy_indices = [index for index, atom in enumerate(atoms) if atom["element"] not in {"H", "D"}]
    old_to_heavy = {old: new for new, old in enumerate(heavy_indices)}
    heavy_atoms = [atoms[index] for index in heavy_indices]
    bonds: set[tuple[int, int]] = set()
    for left_serial, right_serial in conect_serials:
        left = serial_to_index.get(left_serial)
        right = serial_to_index.get(right_serial)
        if left in old_to_heavy and right in old_to_heavy:
            pair = tuple(sorted((old_to_heavy[left], old_to_heavy[right])))
            if pair[0] != pair[1]:
                bonds.add(pair)
    return heavy_atoms, bonds


def read_sdf_atoms(path: Path) -> tuple[list[dict], list[tuple[int, int, int]], dict[int, int]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 4 or "V3000" in lines[3]:
        raise ValueError("Ligand chemistry must normalize to an SDF V2000 record")
    try:
        atom_count = int(lines[3][0:3])
        bond_count = int(lines[3][3:6])
    except ValueError as exc:
        raise ValueError("Invalid SDF counts line") from exc
    if len(lines) < 4 + atom_count + bond_count:
        raise ValueError("Truncated SDF record")
    raw_atoms = []
    for line in lines[4 : 4 + atom_count]:
        raw_atoms.append({"element": line[31:34].strip().upper(), "xyz": (float(line[0:10]), float(line[10:20]), float(line[20:30]))})
    raw_bonds: list[tuple[int, int, int]] = []
    for line in lines[4 + atom_count : 4 + atom_count + bond_count]:
        raw_bonds.append((int(line[0:3]) - 1, int(line[3:6]) - 1, int(line[6:9])))
    formal_charges: dict[int, int] = {}
    for line in lines[4 + atom_count + bond_count :]:
        if line.startswith("M  CHG"):
            fields = line.split()
            for offset in range(int(fields[2])):
                formal_charges[int(fields[3 + 2 * offset]) - 1] = int(fields[4 + 2 * offset])
    return raw_atoms, raw_bonds, formal_charges


def read_sdf_heavy(path: Path) -> tuple[list[dict], list[tuple[int, int, int]], int]:
    raw_atoms, raw_bonds, formal_charges = read_sdf_atoms(path)
    heavy_old = [index for index, atom in enumerate(raw_atoms) if atom["element"] not in {"H", "D"}]
    old_to_heavy = {old: new for new, old in enumerate(heavy_old)}
    atoms = [{**raw_atoms[old], "charge": formal_charges.get(old, 0)} for old in heavy_old]
    bonds = [
        (old_to_heavy[left], old_to_heavy[right], order)
        for left, right, order in raw_bonds
        if left in old_to_heavy and right in old_to_heavy
    ]
    return atoms, bonds, sum(atom["charge"] for atom in atoms)


_COVALENT_RADII = {
    "B": 0.84,
    "C": 0.76,
    "N": 0.71,
    "O": 0.66,
    "F": 0.57,
    "P": 1.07,
    "S": 1.05,
    "CL": 1.02,
    "BR": 1.20,
    "I": 1.39,
}


def infer_pose_bonds(pose_atoms: list[dict]) -> set[tuple[int, int]]:
    """Infer an organic ligand's connectivity from PDB coordinates.

    PDB files produced by structure predictors commonly omit CONECT records.
    The inferred graph is used only to find an atom permutation; bond orders
    still come exclusively from the supplied SDF/MOL2 chemistry source.
    """
    bonds: set[tuple[int, int]] = set()
    for left, left_atom in enumerate(pose_atoms):
        left_radius = _COVALENT_RADII.get(left_atom["element"])
        if left_radius is None:
            raise ValueError(f"Cannot infer ligand connectivity for element {left_atom['element']}; provide a pose PDB with CONECT records")
        for right in range(left + 1, len(pose_atoms)):
            right_atom = pose_atoms[right]
            right_radius = _COVALENT_RADII.get(right_atom["element"])
            if right_radius is None:
                raise ValueError(
                    f"Cannot infer ligand connectivity for element {right_atom['element']}; provide a pose PDB with CONECT records"
                )
            distance = math.dist(left_atom["xyz"], right_atom["xyz"])
            if 0.4 < distance <= 1.25 * (left_radius + right_radius):
                bonds.add((left, right))
    return bonds


def map_pose_atoms(
    pose_atoms: list[dict], pose_bonds: set[tuple[int, int]], chemistry_atoms: list[dict], chemistry_bonds: list[tuple[int, int, int]]
) -> list[int]:
    if len(pose_atoms) != len(chemistry_atoms):
        raise ValueError(f"Ligand heavy-atom count mismatch: pose={len(pose_atoms)}, chemistry={len(chemistry_atoms)}")
    if not pose_bonds:
        pose_bonds = infer_pose_bonds(pose_atoms)
    if not pose_bonds and chemistry_bonds:
        raise ValueError("Could not infer ligand bonds from the pose PDB coordinates; provide a pose PDB with CONECT records")
    chemistry_edges = {tuple(sorted((left, right))) for left, right, _ in chemistry_bonds}
    pose_neighbors = [
        {right if left == index else left for left, right in pose_bonds if index in {left, right}} for index in range(len(pose_atoms))
    ]
    chemistry_neighbors = [
        {right if left == index else left for left, right in chemistry_edges if index in {left, right}}
        for index in range(len(chemistry_atoms))
    ]
    candidates = [
        [
            pose_index
            for pose_index, pose in enumerate(pose_atoms)
            if pose["element"] == atom["element"] and len(pose_neighbors[pose_index]) == len(chemistry_neighbors[index])
        ]
        for index, atom in enumerate(chemistry_atoms)
    ]
    order = sorted(range(len(chemistry_atoms)), key=lambda index: len(candidates[index]))
    mapping: dict[int, int] = {}
    used: set[int] = set()

    def search(position: int) -> bool:
        if position == len(order):
            return True
        chemistry_index = order[position]
        for pose_index in candidates[chemistry_index]:
            if pose_index in used:
                continue
            if any(
                (mapped_neighbor in pose_neighbors[pose_index]) != (neighbor in chemistry_neighbors[chemistry_index])
                for neighbor, mapped_neighbor in mapping.items()
            ):
                continue
            mapping[chemistry_index] = pose_index
            used.add(pose_index)
            if search(position + 1):
                return True
            used.remove(pose_index)
            del mapping[chemistry_index]
        return False

    if not search(0):
        raise ValueError("Could not map ligand chemistry graph onto the co-folded PDB pose")
    return [mapping[index] for index in range(len(chemistry_atoms))]


def prepare_ligand_chemistry(workdir: Path, data: dict) -> None:
    pose_path = ensure_under_root(workdir / str(data["pose_pdb"]), workdir)
    chemistry_path = ensure_under_root(workdir / str(data["chemistry_sdf"]), workdir)
    output_path = ensure_under_root(workdir / str(data["output_sdf"]), workdir)
    report_path = ensure_under_root(workdir / str(data["report"]), workdir)
    pose_atoms, pose_bonds = pdb_pose_atoms(pose_path)
    chemistry_atoms, chemistry_bonds, formal_charge = read_sdf_heavy(chemistry_path)
    expected_charge = int(data["expected_charge"])
    if formal_charge != expected_charge:
        raise ValueError(f"Ligand formal charge ({formal_charge}) does not match confirmed ACPYPE charge ({expected_charge})")
    mapping_source = "pdb-conect" if pose_bonds else "distance-inferred-graph"
    mapping = map_pose_atoms(pose_atoms, pose_bonds, chemistry_atoms, chemistry_bonds)
    rows = ["Mapped ligand chemistry onto co-folded pose", "  GROMACS Web UI", ""]
    rows.append(f"{len(chemistry_atoms):>3}{len(chemistry_bonds):>3}  0  0  0  0            999 V2000")
    for chemistry_index, atom in enumerate(chemistry_atoms):
        x, y, z = pose_atoms[mapping[chemistry_index]]["xyz"]
        rows.append(f"{x:10.4f}{y:10.4f}{z:10.4f} {atom['element']:<3} 0  0  0  0  0  0  0  0  0  0  0  0")
    rows.extend(f"{left + 1:>3}{right + 1:>3}{order:>3}  0  0  0  0" for left, right, order in chemistry_bonds)
    charged = [(index + 1, atom["charge"]) for index, atom in enumerate(chemistry_atoms) if atom["charge"]]
    for start in range(0, len(charged), 8):
        chunk = charged[start : start + 8]
        rows.append(f"M  CHG{len(chunk):>3}" + "".join(f"{index:>4}{charge:>4}" for index, charge in chunk))
    rows.extend(["M  END", "$$$$"])
    output_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    report_path.write_text(
        f"pose={pose_path.name}\nchemistry={chemistry_path.name}\nheavy_atoms={len(chemistry_atoms)}\n"
        f"mapping={'atom-order' if mapping == list(range(len(mapping))) else 'graph-isomorphism'}\nmapping_source={mapping_source}\nformal_charge={formal_charge}\n"
        f"expected_charge={expected_charge}\nheavy_atom_coordinates=preserved\nstatus=PASS\n",
        encoding="utf-8",
    )


def stage_acpype_ligand(workdir: Path, data: dict) -> None:
    gro_output = str(data.get("gro_output") or "ligand_GMX.gro")
    itp_output = str(data.get("itp_output") or "ligand_GMX.itp")
    acpype_dir_name = str(data.get("acpype_dir") or "")
    acpype_dir = ensure_under_root(workdir / acpype_dir_name, workdir) if acpype_dir_name else None
    if acpype_dir is not None and not acpype_dir.exists():
        source_stem = str(data.get("source_stem") or "")
        matches = sorted(workdir.glob("*.acpype"))
        if source_stem and (workdir / f"{source_stem}.acpype").exists():
            acpype_dir = ensure_under_root(workdir / f"{source_stem}.acpype", workdir)
        elif len(matches) == 1:
            acpype_dir = matches[0]
        else:
            raise FileNotFoundError(f"ACPYPE output folder not found: {acpype_dir_name or source_stem}.acpype")
    if acpype_dir is None:
        source_stem = str(data["source_stem"])
        acpype_dir = ensure_under_root(workdir / f"{source_stem}.acpype", workdir)
        if not acpype_dir.exists():
            matches = sorted(workdir.glob("*.acpype"))
            if len(matches) == 1:
                acpype_dir = matches[0]
            else:
                raise FileNotFoundError(f"ACPYPE output folder not found: {source_stem}.acpype")
    gro_files = sorted(acpype_dir.glob("*_GMX.gro"))
    itp_files = sorted(acpype_dir.glob("*_GMX.itp"))
    if not gro_files or not itp_files:
        raise FileNotFoundError("ACPYPE output must contain *_GMX.gro and *_GMX.itp")
    shutil.copyfile(gro_files[0], ensure_under_root(workdir / gro_output, workdir))
    shutil.copyfile(itp_files[0], ensure_under_root(workdir / itp_output, workdir))


def restore_ligand_pose_coordinates(workdir: Path, data: dict) -> None:
    pose_path = ensure_under_root(workdir / str(data["pose_sdf"]), workdir)
    parameter_path = ensure_under_root(workdir / str(data["parameter_sdf"]), workdir)
    gro_path = ensure_under_root(workdir / str(data["gro_file"]), workdir)
    report_path = ensure_under_root(workdir / str(data["report"]), workdir)
    pose_atoms, _, _ = read_sdf_atoms(pose_path)
    parameter_atoms, _, _ = read_sdf_atoms(parameter_path)
    pose_elements = [atom["element"] for atom in pose_atoms]
    parameter_elements = [atom["element"] for atom in parameter_atoms]
    if pose_elements != parameter_elements:
        raise ValueError("Ligand atom order changed between the bound pose and parameterization conformer")
    title, gro_atoms, box = read_gro(gro_path)
    if len(gro_atoms) != len(pose_atoms):
        raise ValueError(f"ACPYPE coordinate atom count ({len(gro_atoms)}) does not match the bound ligand pose ({len(pose_atoms)})")
    restored: list[str] = []
    for line, atom in zip(gro_atoms, pose_atoms):
        x, y, z = (coordinate / 10.0 for coordinate in atom["xyz"])
        restored.append(f"{line[:20]}{x:8.3f}{y:8.3f}{z:8.3f}{line[44:]}")
    gro_path.write_text("\n".join([title, str(len(restored)), *restored, box]) + "\n", encoding="utf-8")
    report_path.write_text(
        f"pose={pose_path.name}\nparameterization={parameter_path.name}\ngro={gro_path.name}\n"
        f"atoms={len(restored)}\natom_order=verified\ncoordinates=co-folded-pose\nstatus=PASS\n",
        encoding="utf-8",
    )


def isolate_acpype_output(workdir: Path, data: dict) -> None:
    expected = ensure_under_root(workdir / str(data["expected_dir"]), workdir)
    target = ensure_under_root(workdir / str(data["output_dir"]), workdir)
    if not expected.exists():
        raise FileNotFoundError(f"ACPYPE output folder not found: {expected.name}")
    if target.exists():
        shutil.rmtree(target)
    expected.rename(target)


def itp_sections(path: Path) -> list[tuple[str | None, list[str]]]:
    blocks: list[tuple[str | None, list[str]]] = []
    current_name: str | None = None
    current: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*\[\s*([^\]\[]+)\s*\]", line)
        if match:
            blocks.append((current_name, current))
            current_name = match.group(1).strip().lower()
            current = [line]
        else:
            current.append(line)
    blocks.append((current_name, current))
    return blocks


def itp_section_data_key(section: str, line: str) -> str | None:
    clean = line.split(";", 1)[0].split("#", 1)[0].strip()
    if not clean or clean.startswith("["):
        return None
    tokens = clean.split()
    if section == "atomtypes":
        return tokens[0]
    return " ".join(tokens[:2])


def merge_ligand_atomtypes(workdir: Path, data: dict) -> None:
    """Merge `[ atomtypes ]` (and pairtypes/nonbond_params) across ligand itps.

    Every ACPYPE `*_GMX.itp` carries its own `[ atomtypes ]` section.  When two
    ligand itps are `#include`d into the same `topol.top`, GROMACS aborts with
    "Atomtype X multiply defined" for every shared atom type.  This step unions
    those sections (deduplicated by type name) into the first ligand itp and
    strips them from the others.  Conflicting definitions for the same type
    name are a hard error instead of a silent override.
    """
    itp_names = [str(name) for name in data.get("itps") or []]
    if not itp_names:
        raise ValueError("merge_ligand_atomtypes requires at least one ligand itp")
    paths = [ensure_under_root(workdir / name, workdir) for name in itp_names]
    blocks_by_path = {path: itp_sections(path) for path in paths}
    merge_sections = ("atomtypes", "nonbond_params", "pairtypes")
    merged: dict[str, dict[str, str]] = {section: {} for section in merge_sections}
    found: dict[str, int] = {section: 0 for section in merge_sections}

    for path in paths:
        for name, lines in blocks_by_path[path]:
            if name not in merge_sections:
                continue
            for line in lines:
                key = itp_section_data_key(name, line)
                if key is None:
                    continue
                found[name] += 1
                existing = merged[name].get(key)
                if existing is not None and existing != line.strip():
                    raise ValueError(
                        f"Conflicting {name} definition for '{key}' between ligand itp files: '{existing}' vs '{line.strip()}'"
                    )
                merged[name][key] = line.strip()

    if not any(found.values()):
        (workdir / str(data.get("report") or "ligand-atomtypes-merge.txt")).write_text(
            "status=SKIP\nreason=no atomtypes/pairtypes/nonbond_params sections found in ligand itps\n", encoding="utf-8"
        )
        return

    keeper_index = next(
        (index for index, path in enumerate(paths) if any(name in merge_sections for name, _ in blocks_by_path[path])),
        0,
    )

    for index, path in enumerate(paths):
        keep = index == keeper_index
        output_lines: list[str] = []
        insert_at: int | None = None
        for name, lines in blocks_by_path[path]:
            if name in merge_sections:
                if keep and insert_at is None:
                    insert_at = len(output_lines)
                continue
            output_lines.extend(lines)
        if keep:
            combined = []
            for section in merge_sections:
                if not merged[section]:
                    continue
                combined.append(f"[ {section} ]")
                combined.append(f"; merged from {len(paths)} ligand itp file(s)")
                combined.extend(merged[section].values())
                combined.append("")
            if insert_at is None:
                raise ValueError(f"{path.name} lost its atomtypes insertion point while merging ligand itps")
            output_lines[insert_at:insert_at] = combined
        path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")

    report_lines = [f"itp_files={', '.join(itp_names)}"]
    for section in merge_sections:
        report_lines.append(f"merged_{section}={len(merged[section])}")
    report_lines.append("status=PASS")
    ensure_under_root(workdir / str(data.get("report") or "ligand-atomtypes-merge.txt"), workdir).write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )


def parse_moleculetype(itp_path: Path) -> str | None:
    lines = itp_path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_section = False
    for line in lines:
        clean = line.split(";", 1)[0].strip()
        if not clean:
            continue
        if clean.lower() == "[ moleculetype ]":
            in_section = True
            continue
        if in_section and clean.startswith("["):
            return None
        if in_section:
            return clean.split()[0]
    return None


def insert_ligand_includes(text: str, ligand_itp: str, posre_lig: str, define: str = "POSRES_LIG") -> str:
    include_lines = [f'#include "{ligand_itp}"']
    if posre_lig:
        include_lines.extend([f"#ifdef {define}", f'#include "{posre_lig}"', "#endif"])
    if ligand_itp in text:
        return text
    lines = text.splitlines()
    insert_at = None
    for index, line in enumerate(lines):
        if "forcefield.itp" in line:
            insert_at = index + 1
            break
    if insert_at is None:
        for index, line in enumerate(lines):
            if line.strip().lower() == "[ moleculetype ]":
                insert_at = index
                break
    if insert_at is None:
        insert_at = 0
    # Append after previously inserted ligand blocks so topol.top lists
    # ligands in input order.
    marker = "; Include ligand topology"
    last_block_start = None
    for index, line in enumerate(lines[insert_at:], insert_at):
        if line.strip() == marker:
            last_block_start = index
    if last_block_start is not None:
        index = last_block_start + 1
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped == "" or stripped.startswith(("#include", "#ifdef", "#endif")):
                index += 1
                continue
            break
        insert_at = index
    lines[insert_at:insert_at] = ["", marker, *include_lines, ""]
    return "\n".join(lines) + "\n"


def append_ligand_molecule(text: str, molecule_name: str, count: int) -> str:
    lines = text.splitlines()
    molecules_index = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "[ molecules ]":
            molecules_index = index
    if molecules_index is None:
        lines.extend(["", "[ molecules ]", "; Compound        #mols"])
        molecules_index = len(lines) - 2
    for line in lines[molecules_index + 1 :]:
        clean = line.split(";", 1)[0].strip()
        if clean and clean.split()[0] == molecule_name:
            return "\n".join(lines) + "\n"
    lines.append(f"{molecule_name:<20} {max(1, int(count))}")
    return "\n".join(lines) + "\n"


def patch_topology(workdir: Path, data: dict) -> None:
    topology = ensure_under_root(workdir / str(data["topology"]), workdir)
    ligand_entries = [dict(entry) for entry in data.get("ligands") or []]
    if not ligand_entries:
        ligand_entries = [
            {
                "itp": str(data["ligand_itp"]),
                "posre": str(data.get("posre_lig") or ""),
                "name": str(data.get("ligand_name") or "lig"),
                "count": int(data.get("ligand_count") or 1),
                "define": "POSRES_LIG",
            }
        ]
    if not ligand_entries:
        raise ValueError("patch_topology requires at least one ligand entry")
    text = topology.read_text(encoding="utf-8", errors="replace")
    included: dict[str, str] = {}
    for entry in ligand_entries:
        ligand_itp = str(entry["itp"])
        ligand_itp_path = ensure_under_root(workdir / ligand_itp, workdir)
        molecule_name = parse_moleculetype(ligand_itp_path) or str(entry.get("name") or "lig")
        if ligand_itp in included and included[ligand_itp] != molecule_name:
            raise ValueError(
                f"Ligand itp {ligand_itp} defines moleculetype {included[ligand_itp]} but is also referenced as {molecule_name}"
            )
        included[ligand_itp] = molecule_name
        text = insert_ligand_includes(text, ligand_itp, str(entry.get("posre") or ""), str(entry.get("define") or "POSRES_LIG"))
        text = append_ligand_molecule(text, molecule_name, int(entry.get("count") or 1))
    topology.write_text(text, encoding="utf-8")
