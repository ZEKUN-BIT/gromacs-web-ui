import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import app.main as main
from app.execution import WorkerEngine
from app.gromacs import Step, _read_xvg, _series_stats, apply_mdp_overrides, build_steps, execute_internal_step, preview_protocol
from app.job_store import JobStore
from app.models import ProtocolPreviewRequest


class WorkflowSafetyTests(unittest.TestCase):
    def test_protocol_preview_uses_uploads_and_overrides_without_writing_templates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            params = {"workflow": "protein_md", "replicas": 3}
            uploaded = {"md.mdp": "dt = 0.001\nnsteps = 2000000\nref-t = 310 310\npcoupl = C-rescale\n"}
            production = preview_protocol(params, root, uploaded)[-1]
            self.assertEqual(production["duration_ns"], 2)
            self.assertEqual(production["temperature_k"], "310 310")
            self.assertEqual(production["source"], "uploaded")
            params.update(mdp_override_enabled=True, mdp_dt_ps="0.002", mdp_md_ns="50", mdp_temperature_k="320")
            production = preview_protocol(params, root, uploaded)[-1]
            self.assertEqual(production["duration_ns"], 50)
            self.assertEqual(production["temperature_k"], "320 320")
            self.assertEqual(list(root.iterdir()), [])

    def test_temperature_override_updates_initial_velocity_for_every_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("nvt.mdp", "npt.mdp", "md.mdp"):
                (root / name).write_text("ref-t = 300 300\ngen-temp = 300\n")
            apply_mdp_overrides(root, {"workflow": "protein_md", "mdp_override_enabled": True, "mdp_temperature_k": "310"})
            for name in ("nvt.mdp", "npt.mdp", "md.mdp"):
                text = (root / name).read_text()
                self.assertNotIn("300", text)
                self.assertEqual(len(text.splitlines()), 2)
                self.assertIn("310 310", text)

    def test_protocol_upload_names_and_content_are_bounded(self):
        from pydantic import ValidationError

        for uploaded in ({"../md.mdp": "x"}, {"md.mdp": "x" * 262145}):
            with self.assertRaises(ValidationError):
                ProtocolPreviewRequest(uploaded_mdp=uploaded)

    def test_api_rejects_critical_skip_and_exposes_protocol_without_structure_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            steps = [Step("quality", ["internal"], kind="internal", operation="validate_stage_log")]
            job = store.create("protected", {}, [], steps)
            store.start(job["id"], steps)
            store.claim_next("test")
            store.update(job["id"], status="failed", step_index=1)
            with patch.object(main, "STORE", store):
                with self.assertRaises(HTTPException) as failure:
                    main.skip_failed_step(job["id"])
                self.assertEqual(failure.exception.status_code, 409)
                detail = main.get_job(job["id"])
                self.assertFalse(detail["can_skip_failed_step"])
                self.assertFalse(detail["can_resume_checkpoint"])
                response = main.protocol_preview(
                    ProtocolPreviewRequest(
                        workflow="protein_md", mdp_override_enabled=True, mdp_dt_ps="0.002", mdp_md_ns="20", mdp_temperature_k="310"
                    )
                )
                self.assertEqual(response["stages"][-1]["duration_ns"], 20)

    def test_quality_gate_uses_uploaded_temperature_after_mdp_preparation(self):
        for workflow, source in (("protein_md", "structure_file"), ("protein_ligand_md", "protein_file")):
            for release in (True, False):
                with self.subTest(workflow=workflow, release=release), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    steps = build_steps({"workflow": workflow, source: "protein.pdb", "release_restraints": release}, ["protein.pdb"])
                    gate = next(step for step in steps if step.operation == "validate_thermodynamics")
                    (root / "npt.mdp").write_text("ref-t = 310 310 ; uploaded targets\ngen_temp = 310\n")
                    for step in steps:
                        if step.operation in {"prepare_npt_mdp", "prepare_unrestrained_npt_mdp"}:
                            execute_internal_step(root, step)
                    (root / gate.data["xvg"]).write_text(
                        '@ s0 legend "Temperature"\n@ s1 legend "Density"\n@ s2 legend "Volume"\n'
                        + "\n".join(f"{index} 310 1000 100" for index in range(30))
                    )
                    execute_internal_step(root, gate)
                    report = json.loads((root / gate.data["output"]).read_text())
                    self.assertEqual(report["status"], "PASS")
                    self.assertEqual(report["target_temperature"], 310)
                    self.assertEqual(report["target_source_mdp"], gate.data["source_mdp"])

    def test_quality_gate_rejects_missing_observables_and_ambiguous_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "npt.mdp").write_text("ref_t = 300 310\n")
            (root / "npt.xvg").write_text('@ s0 legend "Temperature"\n' + "\n".join(f"{i} 300" for i in range(30)))
            step = Step(
                "gate",
                ["internal"],
                kind="internal",
                operation="validate_thermodynamics",
                data={"source_mdp": "npt.mdp", "xvg": "npt.xvg", "output": "quality.json"},
            )
            with self.assertRaisesRegex(ValueError, "per coupling group"):
                execute_internal_step(root, step)
            (root / "npt.mdp").write_text("ref_t = 300\n")
            with self.assertRaisesRegex(ValueError, "Density.*missing"):
                execute_internal_step(root, step)

    def test_quality_and_sampling_reject_bad_numeric_rows_instead_of_dropping_them(self):
        headers = '@ s0 legend "Temperature"\n@ s1 legend "Density"\n@ s2 legend "Volume"\n'
        for bad_row in ("17 nan 1000 100", "17 300 inf 100", "17 invalid 1000 100", "17 300 1000", "17"):
            for operation in ("validate_thermodynamics", "summarize_replicas"):
                with self.subTest(row=bad_row, operation=operation), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    rows = [f"{index} 300 1000 100" for index in range(30)]
                    rows[17] = bad_row
                    (root / "bad.xvg").write_text(headers + "\n".join(rows))
                    data = {"xvg": "bad.xvg", "inputs": ["bad.xvg"], "output": "quality.json"}
                    if operation == "validate_thermodynamics":
                        (root / "quality.json").write_text('{"status":"PASS"}')
                    with self.assertRaisesRegex(ValueError, r"bad\.xvg:21:"):
                        execute_internal_step(root, Step("gate", ["internal"], kind="internal", operation=operation, data=data))
                    if operation == "validate_thermodynamics":
                        report = json.loads((root / "quality.json").read_text())
                        self.assertEqual(report["status"], "FAIL")
                        self.assertIn("bad.xvg:21:", report["failures"][0])

    def test_xvg_legends_keep_numeric_column_indices_and_accept_indented_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ordered.xvg"
            path.write_text(
                '  # comment\n  @ s2 legend "Volume"\n@ s0 legend "Temperature"\n@ s1 legend "Density"\n\n'
                + "\n".join(f"{index} 300 1000 100" for index in range(30))
            )
            legends, rows = _read_xvg(path)
            self.assertEqual(legends, ["Temperature", "Density", "Volume"])
            self.assertEqual(len(rows), 30)

    def test_blocked_sem_includes_all_remainder_samples(self):
        stats = _series_stats([0.0] * 50 + [10.0])
        self.assertAlmostEqual(stats["mean"], 10 / 51)
        self.assertAlmostEqual(stats["blocked_sem"], 1 / 6)
        self.assertEqual(stats["samples"], 51)

    def test_pdb_preflight_fails_invalid_coordinates_and_records_line(self):
        atom = f"ATOM  {1:5d} {'CA':^4s} {'ALA':3s} {'A'}{1:4d}    {0:8.3f}{0:8.3f}{0:8.3f}{1:6.2f}{20:6.2f}          {'C':>2s}"
        failures = (
            atom[:30] + f"{'nan':>8}" + atom[38:],
            atom[:38] + f"{'inf':>8}" + atom[46:],
            atom[:46] + f"{'broken':>8}" + atom[54:],
            atom[:40],
            atom[:12] + "    " + atom[16:],
            atom[:22] + "????" + atom[26:],
        )
        for record in failures:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "bad.pdb").write_text(record + "\nEND\n")
                step = Step(
                    "preflight",
                    ["internal"],
                    kind="internal",
                    operation="structure_preflight",
                    data={"source": "bad.pdb", "output": "preflight.txt"},
                )
                with self.assertRaisesRegex(ValueError, r"bad\.pdb:1:"):
                    execute_internal_step(root, step)
                self.assertIn("status=FAIL", (root / "preflight.txt").read_text())

    def test_each_replica_and_ligand_has_distinct_analysis_and_postprocess_outputs(self):
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "protein.gro",
            "replicas": 3,
            "auto_split_complex_pdb": False,
            "keep_fitted_trajectory": True,
            "ligands": [{"key": key, "gro_file": f"{key}.gro", "itp_file": f"{key}.itp"} for key in ("a", "b")],
        }
        steps = build_steps(params, ["protein.gro", "a.gro", "a.itp", "b.gro", "b.itp"])
        outputs = [output for step in steps if step.title.startswith("Replica ") for output in step.outputs]
        self.assertEqual(len(outputs), len(set(outputs)))
        for replica in range(1, 4):
            replica_steps = [step for step in steps if step.title.startswith(f"Replica {replica}: ")]
            self.assertTrue(any("Fit trajectory" in step.title for step in replica_steps))
            for key in ("a", "b"):
                hbond = next(step for step in replica_steps if f"protein-Ligand_{key} hydrogen bonds" in step.title)
                self.assertIn(f"md_r{replica:02d}.tpr", hbond.args)
            self.assertIn(f"start_r{replica:02d}.pdb", outputs)

    def test_critical_and_dependent_steps_cannot_be_skipped(self):
        critical = [
            Step("prepare", ["gmx", "grompp"]),
            Step("quality", ["internal"], kind="internal", operation="validate_thermodynamics"),
            Step("extract", ["gmx", "energy"], outputs=["quality.xvg"]),
        ]
        for step in critical:
            with self.subTest(step=step.title), tempfile.TemporaryDirectory() as folder:
                store = JobStore(Path(folder))
                steps = [
                    step,
                    Step("gate", ["internal"], kind="internal", operation="validate_thermodynamics", data={"xvg": "quality.xvg"}),
                ]
                job = store.create("protected", {}, [], steps)
                store.start(job["id"], steps)
                store.claim_next("test")
                store.update(job["id"], status="failed", step_index=1)
                self.assertFalse(store.get(job["id"])["can_skip_failed_step"])
                with self.assertRaises(RuntimeError):
                    store.skip_failed_step(job["id"])
                self.assertEqual(store.get(job["id"])["status"], "failed")

    def test_repeated_retry_preserves_position_and_optional_final_skip_is_recorded(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            steps = [
                Step("prepare", ["gmx", "grompp"]),
                Step("quality", ["internal"], kind="internal", operation="validate_stage_log"),
                Step("figures", ["internal"], kind="internal", operation="generate_analysis_figures"),
            ]
            job = store.create("recovery", {}, [], steps)
            store.start(job["id"], steps)
            for _ in range(2):
                store.claim_next("test")
                store.update(job["id"], status="failed", step_index=2)
                self.assertFalse(store.get(job["id"])["can_skip_failed_step"])
                retry = store.retry_failed_step(job["id"])
                self.assertEqual([step["title"] for step in retry["commands"]], ["quality", "figures"])
                self.assertEqual(retry["step_index"], 1)
            store.claim_next("test")
            store.update(job["id"], status="failed", step_index=3, error="plot failed")
            skipped = store.skip_failed_step(job["id"])
            self.assertEqual(skipped["commands"], [])
            self.assertEqual(skipped["skipped_steps"][0]["title"], "figures")
            self.assertTrue(WorkerEngine(store, "test").run_once())
            self.assertEqual(store.get(job["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
