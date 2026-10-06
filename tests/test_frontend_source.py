import unittest
from pathlib import Path


class FrontendSourceTests(unittest.TestCase):
    def test_submit_readiness_preserves_button_children(self) -> None:
        source = (Path(__file__).parents[1] / "app/static/js/form.js").read_text(encoding="utf-8")
        start = source.index("export function setSubmitReady")
        end = source.index("\n}\n", start)
        function_source = source[start:end]

        self.assertIn('querySelector(".button-label")', function_source)
        self.assertNotIn("els.submitJob.textContent", function_source)


if __name__ == "__main__":
    unittest.main()
