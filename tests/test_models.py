import unittest

from app.main import PreviewRequest, app
from app.models import SimulationParams


class UnifiedRequestModelTests(unittest.TestCase):
    def test_preview_and_create_share_the_same_parameter_model(self) -> None:
        self.assertIs(PreviewRequest, SimulationParams)
        parsed = SimulationParams.model_validate_json('{"name":"demo","dry_run":true,"workflow":"postprocess","do_fit":false}')
        self.assertEqual(parsed.name, "demo")
        self.assertTrue(parsed.dry_run)
        self.assertFalse(parsed.do_fit)

    def test_create_job_multipart_has_only_params_and_files(self) -> None:
        schema = app.openapi()
        request_schema = schema["paths"]["/api/jobs"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        component = schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(set(component["properties"]), {"params", "files"})

    def test_cpu_pinning_mode_is_strictly_validated(self) -> None:
        self.assertEqual(SimulationParams.model_validate({"pin": "on"}).pin, "on")
        with self.assertRaises(ValueError):
            SimulationParams.model_validate({"pin": "invalid"})

    def test_analysis_center_defaults_match_available_protein_groups(self) -> None:
        for workflow in ("protein_md", "analysis_rmsd", "analysis_suite"):
            with self.subTest(workflow=workflow):
                self.assertEqual(SimulationParams(workflow=workflow).center_group, "Protein")
        for workflow in ("protein_ligand_md", "postprocess"):
            with self.subTest(workflow=workflow):
                self.assertEqual(SimulationParams(workflow=workflow).center_group, "Protein_Lig")
        custom = SimulationParams(workflow="analysis_suite", center_group="SelectedComplex")
        self.assertEqual(custom.center_group, "SelectedComplex")


if __name__ == "__main__":
    unittest.main()
