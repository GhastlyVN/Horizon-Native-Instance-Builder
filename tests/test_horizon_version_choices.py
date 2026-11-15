import json
import tempfile
import unittest
from pathlib import Path

from scripts.sync_horizon_version_choices import sync_choices


class HorizonVersionChoiceTests(unittest.TestCase):
    def test_build_workflow_dropdown_tracks_catalog_and_caps_at_25(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / "catalog.json"
            workflow = root / "build.yml"
            catalog.write_text(json.dumps({
                "versions": [{"tag": "nightly"}] + [
                    {"tag": f"nightly-2026-11-{day:02d}"} for day in range(13, 0, -1)
                ]
            }), encoding="utf-8")
            workflow.write_text(
                "      horizon_ref:\n"
                "        description: old\n"
                "        required: true\n"
                "        default: \"nightly\"\n"
                "        type: string\n"
                "      include_native_mods:\n",
                encoding="utf-8",
            )
            count = sync_choices(catalog, workflow)
            result = workflow.read_text(encoding="utf-8")
            self.assertEqual(count, 14)
            self.assertIn("type: choice", result)
            self.assertIn('- "nightly-2026-11-13"', result)
            self.assertIn("      include_native_mods:", result)

    def test_rejects_invalid_catalog_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog = root / "catalog.json"
            workflow = root / "build.yml"
            catalog.write_text(json.dumps({"versions": [{"tag": "launcher@0.2.66"}]}), encoding="utf-8")
            workflow.write_text("      horizon_ref:\n      include_native_mods:\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid Nightly tag"):
                sync_choices(catalog, workflow)


if __name__ == "__main__":
    unittest.main()
