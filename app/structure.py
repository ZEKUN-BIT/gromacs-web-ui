"""PDB/GRO structure inspection, cleaning, complex assembly, and index generation."""

from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import Iterable

from .files import ensure_under_root
from .workflow_parameters import bool_param

PDB_WATER_RESIDUES = {"HOH", "WAT", "SOL", "H2O", "TIP3", "TIP3P", "TIP4P", "OPC", "OPC3"}


PDB_ION_RESIDUES = {
    "AG",
    "AL",
    "BA",
    "BR",
    "CA",
    "CAL",
    "CD",
    "CL",
    "CLA",
    "CO",
    "CS",
    "CU",
    "F",
    "FE",
    "FE2",
    "FE3",
    "HG",
    "IOD",
    "K",
    "LI",
    "MG",
    "MG2",
    "MN",
    "NA",
    "NI",
    "PB",
    "POT",
    "RB",
    "SOD",
    "SR",
    "ZN",
}


def pdb_residue_name(line: str) -> str:
    return line[17:20].strip().upper()


def pdb_atom_serial(line: str) -> int | None:
    try:
        return int(line[6:11])
    except ValueError:
        return None


def is_pdb_atom_record(line: str) -> bool:
    return line[:6].strip().upper() in {"ATOM", "HETATM"}


def is_pdb_water(line: str) -> bool:
    return pdb_residue_name(line) in PDB_WATER_RESIDUES


def is_pdb_ion(line: str) -> bool:
    return pdb_residue_name(line) in PDB_ION_RESIDUES


def is_pdb_ligand(line: str, ligand_residue: str = "") -> bool:
    residue = pdb_residue_name(line)
    requested = ligand_residue.strip().upper()
    if requested:
        return is_pdb_atom_record(line) and residue == requested
    return line[:6].strip().upper() == "HETATM" and not is_pdb_water(line) and not is_pdb_ion(line)


def conect_serials(line: str) -> list[int]:
    serials: list[int] = []
    for token in line.split()[1:]:
        try:
            serials.append(int(token))
        except ValueError:
            continue
    return serials


def write_pdb_subset(path: Path, lines: list[str]) -> None:
    output = [line for line in lines if line[:6].strip().upper() != "MASTER"]
    if not output or output[-1].strip().upper() != "END":
        output.append("END")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def format_removed_records(records: dict[str, int]) -> list[str]:
    if not records:
        return []
    return ["removed_residues:", *(f"  {name}: {count}" for name, count in sorted(records.items()))]


def clean_pdb_for_pdb2gmx(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_pdb"]), workdir)
    target = ensure_under_root(workdir / str(data["output_pdb"]), workdir)
    remove_waters = bool_param(data, "remove_waters", True)
    remove_ions = bool_param(data, "remove_ions", True)
    remove_ligands = bool_param(data, "remove_ligands", True)
    ligand_residue = str(data.get("ligand_residue") or "")
    kept: list[str] = []
    removed_waters = 0
    removed_ions = 0
    removed_ligands = 0
    removed_records: dict[str, int] = {}
    removed_serials: set[int] = set()

    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"}:
            residue = pdb_residue_name(line)
            remove = False
            if remove_waters and residue in PDB_WATER_RESIDUES:
                removed_waters += 1
                remove = True
            elif remove_ions and residue in PDB_ION_RESIDUES:
                removed_ions += 1
                remove = True
            elif remove_ligands and is_pdb_ligand(line, ligand_residue):
                removed_ligands += 1
                remove = True
            if remove:
                serial = pdb_atom_serial(line)
                if serial is not None:
                    removed_serials.add(serial)
                removed_records[residue] = removed_records.get(residue, 0) + 1
                continue
        if record == "CONECT" and any(serial in removed_serials for serial in conect_serials(line)):
            continue
        kept.append(line)

    if not kept or not any(line[:6].strip().upper() in {"ATOM", "HETATM"} for line in kept):
        raise ValueError(f"{source.name} has no atoms left after removing waters, ions, and ligands")
    write_pdb_subset(target, kept)

    report_lines = [
        f"source: {source.name}",
        f"output: {target.name}",
        f"removed_water_atoms: {removed_waters}",
        f"removed_ion_atoms: {removed_ions}",
        f"removed_ligand_atoms: {removed_ligands}",
    ]
    report_lines.extend(format_removed_records(removed_records))
    (workdir / "pdb2gmx-clean-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def split_complex_pdb(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_pdb"]), workdir)
    protein_target = ensure_under_root(workdir / str(data["protein_output_pdb"]), workdir)
    remove_waters = bool_param(data, "remove_waters", True)
    remove_ions = bool_param(data, "remove_ions", True)
    multi_outputs = data.get("ligand_outputs") or []

    if multi_outputs:
        ligand_targets = {
            str(entry.get("residue") or "").strip().upper(): ensure_under_root(workdir / str(entry["output_pdb"]), workdir)
            for entry in multi_outputs
        }
        if any(not residue for residue in ligand_targets):
            raise ValueError("Every ligand in a multi-ligand split needs a PDB residue name.")
        ligand_lines: dict[str, list[str]] = {residue: [] for residue in ligand_targets}
        ligand_serials: dict[str, set[int]] = {residue: set() for residue in ligand_targets}
        ligand_residue = ""
    else:
        ligand_targets = {}
        ligand_lines = {}
        ligand_serials = {}
        ligand_residue = str(data.get("ligand_residue") or "")

    protein_lines: list[str] = []
    protein_serials: set[int] = set()
    skipped_waters = 0
    skipped_ions = 0
    ligand_atoms = 0
    ligand_records: dict[str, int] = {}
    unknown_hetero: set[str] = set()
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()

    for line in lines:
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"}:
            serial = pdb_atom_serial(line)
            residue = pdb_residue_name(line)
            if remove_waters and is_pdb_water(line):
                skipped_waters += 1
                continue
            if remove_ions and is_pdb_ion(line):
                skipped_ions += 1
                continue
            if multi_outputs:
                if record == "HETATM" and residue in ligand_targets:
                    ligand_lines[residue].append(line)
                    ligand_atoms += 1
                    ligand_records[residue] = ligand_records.get(residue, 0) + 1
                    if serial is not None:
                        ligand_serials[residue].add(serial)
                    continue
                if record == "HETATM":
                    unknown_hetero.add(residue)
                    continue
            elif is_pdb_ligand(line, ligand_residue):
                bucket = ligand_lines.setdefault("_single", [])
                bucket.append(line)
                ligand_atoms += 1
                ligand_records[residue] = ligand_records.get(residue, 0) + 1
                if serial is not None:
                    ligand_serials.setdefault("_single", set()).add(serial)
                continue
            protein_lines.append(line)
            if serial is not None:
                protein_serials.add(serial)
            continue
        if record not in {"CONECT", "MASTER"}:
            protein_lines.append(line)

    for line in lines:
        if line[:6].strip().upper() != "CONECT":
            continue
        serials = conect_serials(line)
        if not serials:
            continue
        if multi_outputs:
            for residue, serial_set in ligand_serials.items():
                if all(serial in serial_set for serial in serials):
                    ligand_lines[residue].append(line)
                    break
            else:
                if all(serial in protein_serials for serial in serials):
                    protein_lines.append(line)
            continue
        single_serials = ligand_serials.get("_single", set())
        if serials and all(serial in single_serials for serial in serials):
            ligand_lines.setdefault("_single", []).append(line)
        elif serials and all(serial in protein_serials for serial in serials):
            protein_lines.append(line)

    if not any(is_pdb_atom_record(line) for line in protein_lines):
        raise ValueError(f"{source.name} has no protein atoms after complex splitting")

    if multi_outputs:
        if unknown_hetero:
            listing = ", ".join(sorted(unknown_hetero))
            raise ValueError(
                f"Unmapped HETATM residues in {source.name}: {listing}. "
                "Add a ligand with a matching residue name for each one, or remove them from the PDB."
            )
        for residue, target in ligand_targets.items():
            if not any(is_pdb_atom_record(line) for line in ligand_lines[residue]):
                raise ValueError(f"No ligand atoms found for residue {residue} in {source.name}.")
        for residue, target in ligand_targets.items():
            write_pdb_subset(target, ligand_lines[residue])
    else:
        single_lines = ligand_lines.get("_single", [])
        if not any(is_pdb_atom_record(line) for line in single_lines):
            residue_hint = f" for residue {ligand_residue.strip().upper()}" if ligand_residue.strip() else ""
            raise ValueError(
                f"No ligand atoms found{residue_hint} in {source.name}. Set the ligand residue name or upload ligand files separately."
            )
        ligand_target = ensure_under_root(workdir / str(data["ligand_output_pdb"]), workdir)
        write_pdb_subset(ligand_target, single_lines)

    write_pdb_subset(protein_target, protein_lines)
    report_lines = [
        f"source: {source.name}",
        f"protein_output: {protein_target.name}",
        f"skipped_water_atoms: {skipped_waters}",
        f"skipped_ion_atoms: {skipped_ions}",
        f"ligand_atoms: {ligand_atoms}",
    ]
    if multi_outputs:
        for residue in ligand_targets:
            report_lines.append(f"ligand_{residue}_output: {ligand_targets[residue].name}")
            report_lines.append(f"ligand_{residue}_atoms: {ligand_records.get(residue, 0)}")
    else:
        report_lines.append(f"ligand_output: {str(data['ligand_output_pdb'])}")
        if ligand_residue.strip():
            report_lines.append(f"ligand_residue_filter: {ligand_residue.strip().upper()}")
    report_lines.extend(format_removed_records(ligand_records))
    (workdir / "complex-pdb-split-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def read_gro(path: Path) -> tuple[str, list[str], str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 3:
        raise ValueError(f"Invalid .gro file: {path.name}")
    try:
        atom_count = int(lines[1].strip())
    except ValueError as exc:
        raise ValueError(f"Invalid atom count in {path.name}") from exc
    atom_lines = lines[2:-1]
    if len(atom_lines) != atom_count:
        raise ValueError(f"{path.name} declares {atom_count} atoms but contains {len(atom_lines)} atom lines")
    return lines[0], atom_lines, lines[-1]


def merge_gro(workdir: Path, data: dict) -> None:
    protein_path = ensure_under_root(workdir / str(data["protein_gro"]), workdir)
    ligand_names = [str(name) for name in data.get("ligand_gros") or []]
    if not ligand_names and data.get("ligand_gro"):
        ligand_names = [str(data["ligand_gro"])]
    if not ligand_names:
        raise ValueError("merge_gro requires at least one ligand coordinate file")
    output_path = ensure_under_root(workdir / str(data["output_gro"]), workdir)
    _, protein_atoms, box = read_gro(protein_path)
    if not protein_atoms:
        raise ValueError("Protein and ligand coordinate files must both contain atoms")
    protein_xyz = [tuple(float(line[start : start + 8]) for start in (20, 28, 36)) for line in protein_atoms]
    ligand_atom_blocks: list[list[str]] = []
    ligand_xyz: list[tuple[str, list[tuple[float, float, float]]]] = []
    for name in ligand_names:
        _, atoms, _ = read_gro(ensure_under_root(workdir / name, workdir))
        if not atoms:
            raise ValueError("Protein and ligand coordinate files must both contain atoms")
        ligand_atom_blocks.append(atoms)
        ligand_xyz.append((name, [tuple(float(line[start : start + 8]) for start in (20, 28, 36)) for line in atoms]))

    distances: list[tuple[str, str, float]] = []
    for name, xyz in ligand_xyz:
        min_distance = min(((px - lx) ** 2 + (py - ly) ** 2 + (pz - lz) ** 2) ** 0.5 for px, py, pz in protein_xyz for lx, ly, lz in xyz)
        distances.append(("protein", name, min_distance))
        if min_distance < 0.06:
            raise ValueError(f"Protein-ligand atom clash detected ({min_distance:.3f} nm) for {name}; check the ligand pose")
        if min_distance > 1.2:
            raise ValueError(f"Ligand {name} is {min_distance:.3f} nm from the protein; provide coordinates in the intended bound pose")
    for (left_name, left_xyz), (right_name, right_xyz) in itertools.combinations(ligand_xyz, 2):
        min_distance = min(((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5 for ax, ay, az in left_xyz for bx, by, bz in right_xyz)
        distances.append((left_name, right_name, min_distance))
        if min_distance < 0.06:
            raise ValueError(f"Ligand-ligand atom clash detected ({min_distance:.3f} nm) between {left_name} and {right_name}")

    merged_atoms = protein_atoms + [atom for block in ligand_atom_blocks for atom in block]
    merged = ["Complex generated by GROMACS Console", f"{len(merged_atoms):5d}", *merged_atoms, box]
    output_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    report_lines = [f"protein_atoms={len(protein_atoms)}"]
    report_lines.extend(f"ligand_atoms[{name}]={len(block)}" for (name, _), block in zip(ligand_xyz, ligand_atom_blocks))
    report_lines.extend(f"minimum_distance_nm[{left}|{right}]={distance:.4f}" for left, right, distance in distances)
    report_lines.append("status=PASS")
    (workdir / "complex-pose-check.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def write_index_group(handle, name: str, atom_ids: Iterable[int]) -> None:
    handle.write(f"[ {name} ]\n")
    row: list[str] = []
    for atom_id in atom_ids:
        row.append(str(atom_id))
        if len(row) == 15:
            handle.write(" ".join(row) + "\n")
            row = []
    if row:
        handle.write(" ".join(row) + "\n")


def create_complex_index(workdir: Path, data: dict) -> None:
    _, protein_atoms, _ = read_gro(ensure_under_root(workdir / str(data["protein_gro"]), workdir))
    ligand_entries = [dict(entry) for entry in data.get("ligand_gros") or []]
    if not ligand_entries and data.get("ligand_gro"):
        ligand_entries = [{"gro": str(data["ligand_gro"]), "group": "Ligand"}]
    if not ligand_entries:
        raise ValueError("create_complex_index requires at least one ligand coordinate file")
    _, system_atoms, _ = read_gro(ensure_under_root(workdir / str(data["system_gro"]), workdir))
    protein_end = len(protein_atoms)
    ligand_atoms = [
        (str(entry.get("group") or "Ligand"), read_gro(ensure_under_root(workdir / str(entry["gro"]), workdir))[1])
        for entry in ligand_entries
    ]
    solute_end = protein_end + sum(len(atoms) for _, atoms in ligand_atoms)
    if solute_end >= len(system_atoms):
        raise ValueError("Solvated complex contains no water/ion atoms after the solute")
    output = ensure_under_root(workdir / str(data["output_index"]), workdir)

    def atom_name(line: str) -> str:
        return line[10:15].strip().upper()

    def residue_name(line: str) -> str:
        return line[5:10].strip().upper()

    backbone_names = {"N", "CA", "C"}
    mainchain_names = backbone_names | {"O", "O1", "O2", "OT", "OXT", "OC1", "OC2"}
    mainchain_h_names = mainchain_names | {"H", "H1", "H2", "H3", "HN", "HT1", "HT2", "HT3"}
    backbone = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in backbone_names]
    c_alpha = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) == "CA"]
    mainchain = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in mainchain_names]
    mainchain_h = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in mainchain_h_names]
    sidechain = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) not in mainchain_h_names]
    water = [index for index, line in enumerate(system_atoms, 1) if residue_name(line) in PDB_WATER_RESIDUES]
    if not backbone or not c_alpha or not water:
        raise ValueError("Could not build standard Backbone/C-alpha/Water index groups from the solvated complex")
    with output.open("w", encoding="utf-8") as handle:
        write_index_group(handle, "System", range(1, len(system_atoms) + 1))
        write_index_group(handle, "Protein", range(1, protein_end + 1))
        cursor = protein_end
        for group, atoms in ligand_atoms:
            write_index_group(handle, group, range(cursor + 1, cursor + len(atoms) + 1))
            cursor += len(atoms)
        write_index_group(handle, "Protein_Lig", range(1, solute_end + 1))
        write_index_group(handle, "Water_and_Ions", range(solute_end + 1, len(system_atoms) + 1))
        write_index_group(handle, "Backbone", backbone)
        write_index_group(handle, "C-alpha", c_alpha)
        write_index_group(handle, "MainChain", mainchain)
        write_index_group(handle, "MainChain+H", mainchain_h)
        write_index_group(handle, "SideChain", sidechain)
        write_index_group(handle, "Water", water)


def structure_preflight(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source"]), workdir)
    output = ensure_under_root(workdir / str(data["output"]), workdir)
    if source.suffix.lower() != ".pdb":
        output.write_text(f"source={source.name}\nformat={source.suffix.lower()}\nstatus=MANUAL_REVIEW\n", encoding="utf-8")
        return
    atoms = 0
    models: set[str] = set()
    altlocs: set[str] = set()
    hetero: set[str] = set()
    chains: dict[str, list[int]] = {}
    zero_occupancy = 0
    failures = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        record = line[:6].strip().upper()
        if record == "MODEL":
            models.add(line[10:14].strip() or str(len(models) + 1))
        if record not in {"ATOM", "HETATM"}:
            continue
        atoms += 1
        if len(line) < 54:
            failures.append(f"{source.name}:{line_number}: truncated {record} coordinate record")
            continue
        try:
            coordinates = [float(line[start : start + 8]) for start in (30, 38, 46)]
        except ValueError:
            failures.append(f"{source.name}:{line_number}: invalid XYZ coordinate fields")
            continue
        if not all(math.isfinite(value) for value in coordinates):
            failures.append(f"{source.name}:{line_number}: non-finite XYZ coordinates")
            continue
        if not line[12:16].strip() or not line[17:20].strip():
            failures.append(f"{source.name}:{line_number}: missing atom or residue name")
            continue
        altloc = line[16:17].strip()
        if altloc:
            altlocs.add(altloc)
        try:
            occupancy = float(line[54:60].strip() or "1")
            if not math.isfinite(occupancy) or occupancy <= 0:
                zero_occupancy += 1
        except ValueError:
            zero_occupancy += 1
        residue = line[17:20].strip().upper()
        chain = line[21:22].strip() or "_"
        try:
            residue_number = int(line[22:26])
        except ValueError:
            failures.append(f"{source.name}:{line_number}: invalid residue number")
            continue
        chains.setdefault(chain, []).append(residue_number)
        if record == "HETATM" and residue not in PDB_WATER_RESIDUES and residue not in PDB_ION_RESIDUES:
            hetero.add(residue)
    if atoms == 0:
        failures.append(f"{source.name}: no PDB atoms found")
    gaps = []
    for chain, values in chains.items():
        unique = sorted(set(values))
        gaps.extend(f"{chain}:{left}-{right}" for left, right in zip(unique, unique[1:]) if right - left > 1)
    warnings = []
    if len(models) > 1:
        warnings.append("multiple MODEL records; confirm the intended conformer")
    if altlocs:
        warnings.append("alternate locations present; confirm occupancy/conformer selection")
    if gaps:
        warnings.append("residue-number gaps present; distinguish missing residues from chain numbering")
    if hetero:
        warnings.append("non-water HETATM residues present; confirm cofactors, metals, and ligands")
    if zero_occupancy:
        warnings.append("zero or invalid occupancy atoms present")
    rows = [
        f"source={source.name}",
        f"atoms={atoms}",
        f"models={max(1, len(models))}",
        f"altlocs={','.join(sorted(altlocs)) or 'none'}",
        f"hetero_residues={','.join(sorted(hetero)) or 'none'}",
        f"sequence_gaps={','.join(gaps) or 'none'}",
        f"zero_occupancy_atoms={zero_occupancy}",
    ]
    rows.extend(f"warning={item}" for item in warnings)
    rows.extend(f"failure={item}" for item in failures[:20])
    rows.extend(
        [
            "manual_checks=protonation,tautomers,termini,disulfides,missing_atoms,biological_assembly",
            f"status={'FAIL' if failures else 'REVIEW' if warnings else 'PASS'}",
        ]
    )
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if failures:
        raise ValueError("structure preflight failed: " + "; ".join(failures[:3]))
