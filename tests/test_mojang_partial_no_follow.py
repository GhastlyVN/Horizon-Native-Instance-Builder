import os
import pathlib
import tempfile
import unittest

from scripts import download_minecraft


class MojangPartialFileTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are not available")
    def test_verified_write_does_not_follow_a_linked_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            victim = root / "victim.txt"
            victim.write_bytes(b"do not overwrite")
            (root / "client.jar.part").symlink_to(victim)

            with self.assertRaisesRegex(ValueError, "non-regular"):
                download_minecraft._write_verified(
                    root, "client.jar", "https://launcher.mojang.com/client.jar"
                )

            self.assertEqual(victim.read_bytes(), b"do not overwrite")
            self.assertFalse((root / "client.jar").exists())


if __name__ == "__main__":
    unittest.main()
