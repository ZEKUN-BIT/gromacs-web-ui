"""Regenerate the bundled example with a real, isolated, single-threaded GROMACS run.

Run: .venv/bin/python scripts/generate_offline_example.py --gmx /path/to/gmx
The existing worker/runtime is never opened. The fixed-seed 1 ps argon example is
for learning the interface; it is not a protein protocol or sampling validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.demo import create_example  # noqa: E402
from app.execution import WorkerEngine  # noqa: E402
from app.job_store import JobStore  # noqa: E402
from app.result_bundle import build_result_bundle, render_summary  # noqa: E402


def generate(gmx: str, output: Path) -> dict:
    executable = shutil.which(gmx)
    if not executable:
        raise ValueError("GROMACS executable is unavailable")
    for key, value in {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "GMX_DISABLE_GPU_DETECTION": "1",
        "CUDA_VISIBLE_DEVICES": "",
    }.items():
        os.environ[key] = value
    with tempfile.TemporaryDirectory(prefix="gromacs-offline-example-") as directory:
        store = JobStore(Path(directory))
        job = create_example(store, {"gmx_bin": executable})
        if not WorkerEngine(store, "worker:offline-example").run_once():
            raise RuntimeError("Isolated tutorial was not claimed")
        job = store.get(job["id"])
        if job["status"] != "completed":
            raise RuntimeError(store.log_path(job["id"]).read_text(encoding="utf-8"))
        workdir = store.job_dir(job["id"])
        data_path = workdir / "example-energy.xvg"
        raw = data_path.read_text(encoding="utf-8")
        # Native comments contain host/user/path information, irrelevant to this
        # publicly shipped fixture. The XVG labels and numeric rows are retained.
        data_path.write_text("\n".join(line for line in raw.splitlines() if not line.startswith("#")) + "\n", encoding="utf-8")
        legends = {int(index): label for index, label in re.findall(r'@\s+s(\d+)\s+legend\s+"([^"]+)"', raw)}
        rows = [[float(value) for value in line.split()] for line in raw.splitlines() if line.strip() and line[0] not in "#@"]
        if len(rows) != 101 or abs(rows[-1][0] - 1.0) > 1e-6:
            raise RuntimeError("Tutorial did not produce the expected 101 energy frames through 1 ps")
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(2, 1, figsize=(8, 6), layout="constrained", sharex=True)
        for index, axis in enumerate(axes):
            label = legends[index]
            axis.plot([row[0] for row in rows], [row[index + 1] for row in rows], color=("#247d87", "#bd743c")[index], lw=1.8)
            axis.set_ylabel(f"{label} ({'K' if label == 'Temperature' else 'kJ/mol'})")
            axis.grid(alpha=0.16)
            axis.spines[["top", "right"]].set_visible(False)
        axes[-1].set_xlabel("Time (ps)")
        figure.suptitle("64-atom argon tutorial · 1 ps · fixed seed 20261019", fontsize=13)
        figure.savefig(workdir / "example-energy.png", dpi=150)
        plt.close(figure)

        manifest_path = workdir / "experiment-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = re.search(r"\b\d{4}(?:\.\d+)+\b", manifest["gromacs"].get("version") or "")
        public_params = {"workflow": "tutorial", "ntmpi": 1, "ntomp": 1, "gpu": False, "duration_ps": 1, "seed": 20261019}
        commands = job["commands"]
        for command in commands:
            if command["kind"] == "command":
                command["args"][0] = "gmx"
        public_manifest = {
            "schema_version": 1,
            "generated_at": manifest["generated_at"],
            "job": {"id": "argon-tutorial", "name": job["name"], "workflow": "tutorial"},
            "gromacs": {"available": True, "version": version.group() if version else "see local installation"},
            "parameters": public_params,
            "commands": commands,
            "input_files": manifest["input_files"],
            "scope": "真实 GROMACS 单线程 CPU 执行；仅验证教学示例运行与输出，不构成蛋白体系或采样充分性验证。",
        }
        manifest_path.write_text(json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # The processed native MDP records the temporary run directory in its
        # comments. Only the original, hash-recorded protocol is shipped.
        (workdir / "mdout.mdp").unlink(missing_ok=True)
        store.update(job["id"], params=public_params)
        output.mkdir(parents=True, exist_ok=True)
        with build_result_bundle(store, job["id"]) as archive, zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = output / member.filename
                if not target.resolve().is_relative_to(output.resolve()):
                    raise ValueError("Unexpected archive member path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(member))
        summary_path = output / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["job"]["id"] = "argon-tutorial"
        summary["limitations"].insert(0, public_manifest["scope"])
        summary["limitations"].append("每个图包含 101 个能量采样点，时间跨度为 0–1 ps；这里不据此判断平衡或采样收敛。")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "index.html").write_text(render_summary(summary), encoding="utf-8")
        return {
            "output": str(output),
            "gromacs_version": public_manifest["gromacs"]["version"],
            "energy_frames": len(rows),
            "end_ps": rows[-1][0],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gmx", default="gmx")
    parser.add_argument("--output", type=Path, default=ROOT / "app" / "static" / "examples")
    args = parser.parse_args()
    print(json.dumps(generate(args.gmx, args.output), ensure_ascii=False))
