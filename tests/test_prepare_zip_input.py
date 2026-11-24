import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare import validate_zip


@unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO test")
class PrepareZipInputTests(unittest.TestCase):
    def test_a_replaced_fifo_is_refused_without_hanging(self):
        with tempfile.TemporaryDirectory() as dirname:
            fifo = pathlib.Path(dirname) / "input.zip"
            os.mkfifo(fifo)
            with patch.object(pathlib.Path, "is_file", return_value=True):
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    validate_zip(fifo)




class PrepareManifestTests(unittest.TestCase):
    def test_manifest_writer_refuses_linked_destination(self):
        from scripts import prepare

        with tempfile.TemporaryDirectory() as dirname:
            root = pathlib.Path(dirname)
            victim = root / "victim.json"
            victim.write_text("keep\n", encoding="utf-8")
            alias = root / "input-manifest.json"
            try:
                alias.symlink_to(victim)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with patch.object(prepare, "REPORT", root):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    prepare.write_input_manifest({})
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
