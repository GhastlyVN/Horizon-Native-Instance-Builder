import hashlib
import json
import pathlib
import unittest
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "registry/mods-index.json"
MODS = ROOT / "registry/mods"


class RegistryIndexIntegrityTests(unittest.TestCase):
    def test_selected_rows_match_checked_in_bundles(self):
        index = json.loads(INDEX.read_text(encoding="utf-8"))
        selected = index["selected"]
        self.assertTrue(selected, "the registry index has no selected mods")

        for row in selected:
            with self.subTest(mod=row.get("file")):
                path = MODS / row["file"]
                self.assertTrue(path.is_file(), f"missing {path.relative_to(ROOT)}")
                data = path.read_bytes()
                self.assertEqual(len(data), row["size"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
                self.assertEqual(row["game"], index["game"])
                url = urlparse(row["url"])
                self.assertEqual((url.scheme, url.netloc), ("https", "github.com"))
                self.assertEqual(pathlib.PurePosixPath(url.path).name, row["file"])


if __name__ == "__main__":
    unittest.main()
