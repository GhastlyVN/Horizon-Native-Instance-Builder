import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_horizon_version.py"


class VersionMetadataLimitTests(unittest.TestCase):
    def _check(self, payload):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "lock.json"
            lock.write_text(json.dumps({"minecraft": {"version": "1.21.1"}}), encoding="utf-8")
            archive = root / "minecraft.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("versions/1.21.1/1.21.1.json", payload)
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--lock", str(lock),
                 "--minecraft-zip", str(archive), "--print-version"],
                capture_output=True, text=True, check=False,
            )

    def test_accepts_normal_version_metadata(self):
        result = self._check(json.dumps({"id": "1.21.1"}).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1.21.1")

    def test_rejects_oversized_version_metadata(self):
        result = self._check(json.dumps({"id": "1.21.1", "padding": "x" * (4 * 1024 * 1024)}).encode())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("safety limit", result.stderr)
