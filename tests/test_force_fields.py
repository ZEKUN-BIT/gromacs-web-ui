import tempfile
import unittest
from pathlib import Path

from app.gromacs import discover_local_force_fields


class ForceFieldLayoutTests(unittest.TestCase):
    def test_discovers_force_field_in_forcefields_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base_dir = Path(folder)
            force_field = base_dir / "forcefields" / "example.ff"
            force_field.mkdir(parents=True)
            (force_field / "watermodels.dat").write_text("tip3p TIP3P three-site water\n", encoding="utf-8")

            fields = discover_local_force_fields(base_dir)

            self.assertEqual([field["name"] for field in fields], ["example"])
            self.assertEqual(fields[0]["water_models"][0]["name"], "tip3p")


if __name__ == "__main__":
    unittest.main()
