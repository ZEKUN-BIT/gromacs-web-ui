"""Self-contained 64-atom argon tutorial, independent of installed force fields."""

from __future__ import annotations

from .gromacs import Step
from .reproducibility import write_experiment_manifest
from .steps import internal_step


def example_inputs() -> dict[str, str]:
    coordinates = ["GROMACS Console tutorial: 64 argon atoms", "64"]
    for atom in range(64):
        x, y, z = ((atom // 16) * 0.55 + 0.1, ((atom // 4) % 4) * 0.55 + 0.1, (atom % 4) * 0.55 + 0.1)
        coordinates.append(f"{atom + 1:5d}{'AR':<5}{'AR':>5}{atom + 1:5d}{x:8.3f}{y:8.3f}{z:8.3f}")
    return {
        "initial.gro": "\n".join([*coordinates, "2.2 2.2 2.2", ""]),
        "example.top": "[ defaults ]\n1 2 yes 1.0 1.0\n[ atomtypes ]\nAr 39.948 0.0 A 0.34 0.997\n"
        "[ moleculetype ]\nArgon 1\n[ atoms ]\n1 Ar 1 AR AR 1 0.0 39.948\n[ system ]\nArgon tutorial\n[ molecules ]\nArgon 64\n",
        "example.mdp": "integrator = md\ndt = 0.001\nnsteps = 1000\nnstxout = 0\nnstvout = 0\nnstenergy = 10\nnstlog = 10\n"
        "nstxout-compressed = 10\ncutoff-scheme = Verlet\nnstlist = 10\nrlist = 0.8\ncoulombtype = Cut-off\nrcoulomb = 0.8\n"
        "vdwtype = Cut-off\nrvdw = 0.8\npbc = xyz\ntcoupl = no\npcoupl = no\nconstraints = none\n"
        "gen-vel = yes\ngen-temp = 90\ngen-seed = 20261019\ncomm-mode = Linear\nnstcomm = 10\nDispCorr = no\n",
    }


def create_example(store, settings: dict) -> dict:
    gmx = settings["gmx_bin"]
    steps = [
        Step(
            "Prepare tutorial TPR",
            [gmx, "grompp", "-f", "example.mdp", "-c", "initial.gro", "-p", "example.top", "-o", "example.tpr"],
            outputs=["example.tpr"],
        ),
        Step(
            "Run 1 ps argon tutorial",
            [gmx, "mdrun", "-s", "example.tpr", "-deffnm", "example", "-ntmpi", "1", "-ntomp", "1", "-nb", "cpu", "-pin", "off"],
            outputs=["example.gro", "example.edr", "example.xtc", "example.log"],
        ),
        Step(
            "Read tutorial energy",
            [gmx, "energy", "-f", "example.edr", "-o", "example-energy.xvg"],
            stdin_text="Temperature\nPotential\n0\n",
            outputs=["example-energy.xvg"],
        ),
        internal_step(
            "Check tutorial log",
            "validate_stage_log",
            {"stage": "tutorial", "log_file": "example.log", "output_report": "quality-tutorial.txt"},
            outputs=["quality-tutorial.txt"],
        ),
    ]
    inputs = example_inputs()
    meta = store.create(
        "首次使用 · 1 ps 氩体系示例",
        {"workflow": "tutorial", "gmx_bin": gmx, "ntmpi": 1, "ntomp": 1, "project": "教学示例"},
        list(inputs),
        steps,
    )
    try:
        with store.job_files_lock(meta["id"]):
            root = store.job_dir(meta["id"])
            for name, content in inputs.items():
                (root / name).write_text(content, encoding="utf-8")
            manifest = write_experiment_manifest(meta)
            store.update(
                meta["id"],
                is_example=True,
                experiment_manifest="experiment-manifest.json",
                input_hashes=manifest["input_files"],
                notes="用于练习提交、监控、查看图表和导出结果，不是蛋白体系的研究协议。",
            )
            store.start(meta["id"], steps)
            return store.get(meta["id"])
    except Exception:
        store.update(meta["id"], status="failed", error="教学示例准备失败，请检查环境和运行目录空间。")
        raise
