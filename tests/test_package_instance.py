import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from scripts.package_instance import package


class PackageInstanceTests(unittest.TestCase):
    def test_package_contains_a_real_drop_in_instance_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance = root / "instance.zip"
            with zipfile.ZipFile(instance, "w") as archive:
                archive.writestr("saves/world/level.dat", b"world")
                archive.writestr("options.txt", "renderDistance:8")
            binary = root / "horizon-client.exe"
            binary.write_bytes(b"test executable")
            mods = root / "mods"
            mods.mkdir()
            (mods / "example.hnmod").write_bytes(b"native mod bundle")
            output = root / "bundle.zip"

            result = package(instance, binary, mods, "windows-x86_64",
                             "a" * 40, output)
            instance_id = "horizon-native-aaaaaaa"
            self.assertEqual(result["instance"]["id"], instance_id)
            self.assertEqual(result["instance"]["files"], 2)
            self.assertEqual(len(result["mods"]), 1)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read(f"instances/{instance_id}/saves/world/level.dat"),
                    b"world",
                )
                self.assertEqual(
                    archive.read(f"instances/{instance_id}/options.txt"),
                    b"renderDistance:8",
                )
                self.assertEqual(
                    archive.read("native-build/windows-x86_64/horizon-client.exe"),
                    binary.read_bytes(),
                )
                self.assertEqual(
                    archive.read("build-inputs/mods/example.hnmod"),
                    b"native mod bundle",
                )
                self.assertNotIn("instance/instance.zip", archive.namelist())
                self.assertIsNone(archive.testzip())

    def _inputs(self, root: Path):
        instance = root / "instance.zip"
        with zipfile.ZipFile(instance, "w") as archive:
            archive.writestr("saves/world/level.dat", b"world")
        binary = root / "horizon-client.exe"
        binary.write_bytes(b"test executable")
        mods = root / "mods"
        mods.mkdir()
        mod = mods / "example.hnmod"
        mod.write_bytes(b"native mod bundle")
        return instance, binary, mods, mod

    def test_package_output_cannot_overwrite_any_source_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, mod = self._inputs(root)
            for source in (instance, binary, mod):
                original = source.read_bytes()
                with self.subTest(source=source.name):
                    with self.assertRaisesRegex(ValueError, "must not overwrite an input"):
                        package(instance, binary, mods, "windows-x86_64",
                                "c" * 40, source)
                    self.assertEqual(source.read_bytes(), original)

    def test_package_output_rejects_hardlink_alias_to_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            alias = root / "bundle.zip"
            try:
                os.link(instance, alias)
            except OSError as error:
                self.skipTest(f"hard links are unavailable: {error}")
            original = instance.read_bytes()

            with self.assertRaisesRegex(ValueError, "must not overwrite an input"):
                package(instance, binary, mods, "windows-x86_64",
                        "d" * 40, alias)

            self.assertEqual(instance.read_bytes(), original)
            self.assertEqual(alias.read_bytes(), original)

    def test_package_output_rejects_symlink_alias_to_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            alias = root / "bundle.zip"
            try:
                alias.symlink_to(instance)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlinks are unavailable: {error}")
            original = instance.read_bytes()

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                package(instance, binary, mods, "windows-x86_64",
                        "e" * 40, alias)

            self.assertEqual(instance.read_bytes(), original)

    def test_package_write_failure_preserves_existing_output_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            output = root / "bundle.zip"
            previous = b"previous verified package"
            output.write_bytes(previous)

            with patch("scripts.package_instance.zipfile.ZipFile.testzip", return_value="broken"):
                with self.assertRaisesRegex(ValueError, "failed CRC verification"):
                    package(instance, binary, mods, "windows-x86_64",
                            "f" * 40, output)

            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(root.glob(".bundle.zip.*.tmp")), [])

    def test_rejects_zip_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance = root / "instance.zip"
            with zipfile.ZipFile(instance, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            binary = root / "horizon-client.exe"
            binary.write_bytes(b"test executable")
            mods = root / "mods"
            mods.mkdir()
            with self.assertRaisesRegex(ValueError, "Unsafe instance ZIP entry"):
                package(instance, binary, mods, "windows-x86_64",
                        "b" * 40, root / "bundle.zip")


if __name__ == "__main__":
    unittest.main()
