import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from MDAnalysis.lib.formats.libmdaxdr import TRRFile, XTCFile

from app import main, trajectory_preview
from app.job_store import JobStore


@pytest.fixture
def job(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "runtime")
    monkeypatch.setattr(main, "STORE", store)
    meta = store.create("saved frames", {}, [], [])
    folder = Path(meta["workdir"])
    replica = folder / "replica-1"
    replica.mkdir()
    (replica / "md.gro").write_text(
        "traj test\n3\n    1ALA     CA    1   0.000   0.000   0.000\n    2ALA      O    2   0.300   0.200   0.100\n"
        "    3SOL     OW    3   1.000   1.100   1.200\n   3.0 3.0 3.0\n"
    )
    return TestClient(main.app), f"/api/jobs/{meta['id']}/trajectory", replica


def write_traj(path, n=3, frames=13, missing=None):
    box = np.eye(3, dtype="f")
    with (XTCFile if path.suffix == ".xtc" else TRRFile)(str(path), "w") as out:
        for i in range(frames):
            xyz = np.arange(n * 3, dtype="f").reshape(n, 3) + i
            if path.suffix == ".xtc":
                out.write(xyz, box, i, i * 2.5)
            else:
                out.write(None if i == missing else xyz, xyz, None, box, i, i * 2.5, 0, n)


@pytest.mark.parametrize("ext", ["xtc", "trr"])
def test_random_seek_saves_all_frames_and_native_units(job, ext):
    client, url, root = job
    file = f"md.{ext}"
    write_traj(root / file)
    params = {"trajectory": f"replica-1/{file}", "topology": "replica-1/md.gro"}
    assert client.get(url + "/files").json() == {
        "trajectories": [params["trajectory"]],
        "topologies": [params["topology"]],
        "truncated": False,
    }
    preview = url + "/preview"
    meta = client.get(preview, params=params).json()
    assert (meta["frames"], meta["elements"], meta["source_atoms"]) == (13, ["C", "O"], 3)
    for index in (12, 0, 7, 1):
        batch = client.get(preview, params={**params, "start": index, "revision": meta["revision"]}).json()["frames"]
        assert [f["index"] for f in batch] == list(range(index, min(13, index + 8)))
        assert batch[0]["time_ps"] == index * 2.5
        assert batch[0]["xyz"] == ((np.arange(6) + index) * 10).tolist()
    assert client.get(preview, params={**params, "selection": "all"}).json()["elements"] == ["C", "O", "O"]
    assert client.get(preview, params={**params, "selection": "backbone"}).json()["elements"] == ["C"]
    for extra in ({"start": 0}, {"start": -1}, {"start": 13, "revision": meta["revision"]}):
        assert client.get(preview, params={**params, **extra}).status_code == 422
    assert sorted(p.name for p in root.iterdir()) == sorted([file, "md.gro"])
    os.utime(root / file, ns=(12345, 12345))
    assert client.get(preview, params={**params, "start": 0, "revision": meta["revision"]}).status_code == 409


def test_missing_coordinates_and_mismatch(job):
    client, url, root = job
    write_traj(root / "md.trr", missing=1)
    query = {"trajectory": "replica-1/md.trr", "topology": "replica-1/md.gro"}
    meta = client.get(url + "/preview", params=query).json()
    for index in (0, 1, 2):
        response = client.get(url + "/preview", params={**query, "revision": meta["revision"], "start": index})
        if index == 1:
            assert response.status_code == 422 and "未保存位置" in response.json()["detail"]
        else:
            assert response.json()["frames"][0]["index"] == index
    write_traj(root / "two.xtc", n=2)
    assert "原子" in client.get(url + "/preview", params={**query, "trajectory": "replica-1/two.xtc"}).json()["detail"]


def test_only_read_confined_regular_files(job):
    client, url, root = job
    assert client.get(url + "/files").json()["trajectories"] == []
    write_traj(root / "md.xtc", frames=1)
    (root / "linked.xtc").symlink_to(root / "md.xtc")
    (root.parent / "shortcut").symlink_to(root, target_is_directory=True)
    assert client.get(url + "/files").json()["trajectories"] == ["replica-1/md.xtc"]
    for filename in ("../md.xtc", "/tmp/md.xtc", "replica-1/linked.xtc", "shortcut/md.xtc", "bad\x00.xtc", "run.log", "missing.xtc"):
        res = client.get(url + "/preview", params={"trajectory": filename, "topology": "replica-1/md.gro"})
        assert res.status_code in (404, 422)


def test_pdb_and_atom_budget(job, monkeypatch):
    client, url, root = job
    (root / "model.pdb").write_text("ATOM      1  CA  ALA A   1       0.100   0.200   0.300  1.00  0.00           C\nEND\n")
    write_traj(root / "one.xtc", n=1, frames=1)
    params = {"trajectory": "replica-1/one.xtc", "topology": "replica-1/model.pdb"}
    assert client.get(url + "/preview", params=params).json()["frames"] == 1
    monkeypatch.setattr(trajectory_preview, "MAX_VISIBLE", 0)
    assert client.get(url + "/preview", params={**params, "selection": "all"}).status_code == 413


def test_recommend_production_before_heat_and_trr(job):
    client, url, root = job
    for name in ("00_heat.xtc", "06_production.xtc", "06_production.trr"):
        (root / name).touch()
    assert client.get(url + "/files").json()["trajectories"] == [
        "replica-1/06_production.xtc",
        "replica-1/06_production.trr",
        "replica-1/00_heat.xtc",
    ]


def test_auto_samples_fixed_nonwater_indices_without_skipping_frames(job, monkeypatch):
    client, url, root = job
    total = 15
    gro = root / "large.gro"

    def write_top(water_only=False):
        lines = [
            f"{i:5d}{'SOL' if water_only or i % 2 else 'PET':<5}{'OW' if water_only or i % 2 else 'C':>5}{i:5d}   0.000   0.000   0.000"
            for i in range(1, total + 1)
        ]
        gro.write_text("\n".join(["auto test", str(total), *lines, "3.0 3.0 3.0", ""]))

    write_top()
    write_traj(root / "large.xtc", n=total, frames=4)
    monkeypatch.setattr(trajectory_preview, "MAX_VISIBLE", 5)
    monkeypatch.setattr(trajectory_preview, "AUTO_SAMPLE", 3)
    params = {"trajectory": "replica-1/large.xtc", "topology": "replica-1/large.gro", "selection": "auto"}
    meta = client.get(url + "/preview", params=params).json()
    assert meta["frames"] == 4 and meta["elements"] == ["C"] * 3
    assert "固定子集" in meta["display_note"]
    for i in (3, 0, 2, 1):
        frame = client.get(url + "/preview", params={**params, "revision": meta["revision"], "start": i}).json()["frames"][0]
        assert (frame["index"], frame["time_ps"]) == (i, i * 2.5)
        np.testing.assert_allclose(frame["xyz"], ((np.arange(total * 3).reshape(total, 3)[[1, 7, 13]] + i) * 10).ravel())
    for selection in ("dry", "all"):
        assert client.get(url + "/preview", params={**params, "selection": selection}).status_code == 413
    write_top(water_only=True)
    meta = client.get(url + "/preview", params=params).json()
    assert meta["elements"] == ["O"] * 3 and "全水" in meta["display_note"]


@pytest.mark.parametrize("atoms", [9, 10, 31])
def test_xtc_index_matches_reader_and_rejects_truncation(tmp_path, atoms):
    from fastapi import HTTPException

    path = tmp_path / "sample.xtc"
    write_traj(path, n=atoms, frames=5)
    with path.open("rb") as source, XTCFile(str(path)) as native:
        size = path.stat().st_size
        offsets = trajectory_preview._xtc_offsets(source.fileno(), size, atoms)
        np.testing.assert_array_equal(offsets, native.offsets)
        assert offsets.dtype == np.int64
        with pytest.raises(HTTPException) as invalid:
            trajectory_preview._xtc_offsets(source.fileno(), size - 1, atoms)
        assert invalid.value.status_code == 422


def test_xtc_index_retains_offsets_beyond_4gib(monkeypatch):
    import struct

    # Headers only: no large fixture or multi-gigabyte reads needed to check seeking arithmetic.
    header = bytearray(92)
    atoms, payload, count = 500_000, 5_000_000, 870
    stride = len(header) + payload
    struct.pack_into(">ii", header, 0, 1995, atoms)
    struct.pack_into(">i", header, 52, atoms)
    struct.pack_into(">i", header, 88, payload)

    def read_header(_fd, size, offset):
        assert size == len(header) and offset % stride == 0
        return header

    monkeypatch.setattr(os, "pread", read_header)
    offsets = trajectory_preview._xtc_offsets(-1, stride * count, atoms)
    assert len(offsets) == count and offsets[-1] == (count - 1) * stride > 2**32


def test_molecular_protein_ligand_and_water_across_frames(job, monkeypatch):
    client, url, root = job
    atoms = []
    for r in range(3):
        atoms.extend(
            (r + 1, "ALA", name, (r * 3.8 + dx, dy, 0))
            for name, dx, dy in [("N", 0, 0), ("CA", 1.2, 0.4), ("CB", 1.4, 1.6), ("C", 2.5, 0), ("O", 3.1, -1)]
        )
    # Far PET first in topology; the next two PET residues are close enough to cross-bond if guessed globally.
    for r, y in [(4, 30), (5, 4), (6, 5.4)]:
        atoms.extend((r, "PET", name, (x, y, 0)) for name, x in [("C001", 0), ("C002", 1.5), ("O003", 2.8), ("H004", 3.6)])
    atoms.append((7, "SOL", "OW", (2, 3, 0)))
    xyz = np.array([coord for _, _, _, coord in atoms], dtype="f")
    gro = [f"{r:5d}{res:<5}{name:>5}{i:5d}" + "".join(f"{v / 10:8.3f}" for v in xyz[i]) for i, (r, res, name, _) in enumerate(atoms)]
    (root / "mixed.gro").write_text("\n".join(["mixed", str(len(atoms)), *gro, "5 5 5", ""]))
    with XTCFile(str(root / "mixed.xtc"), "w") as out:
        for i in range(4):
            out.write((xyz + i) / 10, np.eye(3, dtype="f") * 10, i, i * 2.0)
    params = {"trajectory": "replica-1/mixed.xtc", "topology": "replica-1/mixed.gro"}
    meta = client.get(url + "/preview", params=params).json()
    specs = meta["molecular"]["atoms"]
    assert [(s[0], s[1], s[4]) for s in specs[:12]] == [(n, "ALA", "c") for n in ("N", "CA", "C", "O")] * 3
    assert [(s[2], s[0]) for s in specs[12:]] == [(r, name) for r in (4, 5, 6) for name in ("C001", "C002", "O003")]
    assert meta["molecular"]["bonds"] == [[i + a, i + b] for i in (12, 15, 18) for a, b in ((0, 1), (1, 2))]
    for i in (3, 0, 2):
        batch = client.get(url + "/preview", params={**params, "start": i, "revision": meta["revision"]}).json()
        assert set(batch) == {"frames"}
        np.testing.assert_allclose(
            batch["frames"][0]["xyz"],
            xyz[[j for j, (_, res, n, _) in enumerate(atoms) if res != "SOL" and n not in {"CB", "H004"}]].ravel(),
            atol=0.011,
        )
    assert len(client.get(url + "/preview", params={**params, "selection": "all"}).json()["elements"]) == len(atoms)
    monkeypatch.setattr(trajectory_preview, "MAX_VISIBLE", 17)
    monkeypatch.setattr(trajectory_preview, "AUTO_SAMPLE", 16)
    os.utime(root / "mixed.gro", ns=(12345, 12345))
    meta = client.get(url + "/preview", params=params).json()
    assert "1/3 个 PET" in meta["display_note"]
    assert meta["molecular"]["atoms"] == specs[:12] + specs[15:18]


def test_molecular_hides_pure_water_and_prefers_explicit_bonds(job):
    client, url, root = job
    write_traj(root / "two.xtc", n=2, frames=1)
    params = {"trajectory": "replica-1/two.xtc", "topology": "replica-1/model.pdb"}
    path = root / "model.pdb"
    path.write_text(
        "HETATM    1  C1  PET A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  O2  PET A   1       8.000   0.000   0.000  1.00  0.00           O\nCONECT    1    2\nEND\n"
    )
    assert client.get(url + "/preview", params=params).json()["molecular"]["bonds"] == [[0, 1]]
    path.write_text(path.read_text().replace("PET", "HOH"))
    hidden = client.get(url + "/preview", params=params)
    assert hidden.status_code == 422 and "水已隐藏" in hidden.json()["detail"]
    assert client.get(url + "/preview", params={**params, "selection": "all"}).json()["elements"] == ["C", "O"]


@pytest.mark.parametrize("skew", [0, 0.3])
def test_periodic_cartoon_and_pet_are_whole_without_changing_raw_frames(job, skew):
    client, url, root = job
    box = np.array([[2, 0, 0], [skew, 2, 0], [0, 0, 2]], dtype="f")
    rows = [(r, "ALA", name) for r in (1, 2) for name in ("N", "CA", "C", "O")] + [(3, "PET", "C1"), (3, "PET", "O2")]
    lines = [f"{r:5d}{res:<5}{name:>5}{i + 1:5d}   0.000   0.000   0.000" for i, (r, res, name) in enumerate(rows)]
    (root / "pbc.gro").write_text("\n".join(["periodic test", "10", *lines, "2 2 2", ""]))
    # Both a peptide bond and a PET bond cross the periodic y face in the reference frame.
    base = np.array([13.9, 15.4, 16.9, 17.9, 18.2, 19.7, 21.2, 22.2, 19.5, 20.9])
    with XTCFile(str(root / "pbc.xtc"), "w") as out:
        for i in range(2):
            y = base + i * 5
            xyz = np.column_stack([np.full(10, 8), y, np.zeros(10)]) / 10
            frac = xyz @ np.linalg.inv(box)
            out.write(((frac % 1) @ box).astype("f"), box, i, i)
    params = {"trajectory": "replica-1/pbc.xtc", "topology": "replica-1/pbc.gro"}
    meta = client.get(url + "/preview", params=params).json()
    assert meta["molecular"]["bonds"] == [[8, 9]] and all(x[4] for x in meta["molecular"]["atoms"][:8])
    for index in (1, 0):
        batch = client.get(url + "/preview", params={**params, "start": index, "revision": meta["revision"]}).json()
        xyz = np.asarray(batch["frames"][0]["xyz"]).reshape(-1, 3)
        np.testing.assert_allclose(xyz[:, 1] - xyz[0, 1], base - base[0], atol=0.012)
        np.testing.assert_allclose(xyz[:, 0], xyz[0, 0], atol=0.012)
    raw = client.get(url + "/preview", params={**params, "selection": "all"}).json()
    batch = client.get(url + "/preview", params={**params, "selection": "all", "start": 0, "revision": raw["revision"]}).json()
    assert np.asarray(batch["frames"][0]["xyz"]).reshape(-1, 3)[8, 1] - np.asarray(batch["frames"][0]["xyz"]).reshape(-1, 3)[9, 1] > 18
