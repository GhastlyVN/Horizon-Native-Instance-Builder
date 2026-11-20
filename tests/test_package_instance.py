import hashlib
import json
import os
import stat
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from scripts import package_instance
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
                manifest = json.loads(archive.read("build-manifest.json"))
                self.assertEqual(
                    manifest["native_client"]["sha256"],
                    hashlib.sha256(binary.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    manifest["mods"][0]["sha256"],
                    hashlib.sha256((mods / "example.hnmod").read_bytes()).hexdigest(),
                )
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

    @unittest.skipIf(os.name == "nt", "open-file rename semantics differ on Windows")
    def test_package_uses_open_instance_snapshot_after_source_path_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            original_archive = instance.read_bytes()
            replacement = root / "replacement.zip"
            with zipfile.ZipFile(replacement, "w") as archive:
                archive.writestr("saves/world/level.dat", b"replacement world")
            replacement_bytes = replacement.read_bytes()
            output = root / "bundle.zip"
            real_copy = package_instance._copy_zip_member_to_zip
            replaced = False

            def replace_path_before_copy(archive, arcname, source, info):
                nonlocal replaced
                if not replaced:
                    os.replace(replacement, instance)
                    replaced = True
                return real_copy(archive, arcname, source, info)

            with patch.object(
                package_instance, "_copy_zip_member_to_zip",
                side_effect=replace_path_before_copy,
            ):
                result = package(instance, binary, mods, "windows-x86_64",
                                 "1" * 40, output)

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read("instances/horizon-native-1111111/saves/world/level.dat"),
                    b"world",
                )
            self.assertEqual(
                result["instance"]["source_archive_sha256"],
                hashlib.sha256(original_archive).hexdigest(),
            )
            self.assertEqual(instance.read_bytes(), replacement_bytes)

    @unittest.skipIf(os.name == "nt", "open-file rename semantics differ on Windows")
    def test_package_hashes_the_same_open_binary_snapshot_it_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            original_binary = binary.read_bytes()
            replacement = root / "replacement.exe"
            replacement.write_bytes(b"replacement executable")
            replacement_bytes = replacement.read_bytes()
            output = root / "bundle.zip"
            real_copy = package_instance._copy_stream_to_zip
            replaced = False

            def replace_path_before_copy(archive, arcname, stream, metadata):
                nonlocal replaced
                if arcname == "native-build/windows-x86_64/horizon-client.exe" and not replaced:
                    os.replace(replacement, binary)
                    replaced = True
                return real_copy(archive, arcname, stream, metadata)

            with patch.object(
                package_instance, "_copy_stream_to_zip",
                side_effect=replace_path_before_copy,
            ):
                result = package(instance, binary, mods, "windows-x86_64",
                                 "2" * 40, output)

            with zipfile.ZipFile(output) as archive:
                packaged_binary = archive.read("native-build/windows-x86_64/horizon-client.exe")
            self.assertEqual(packaged_binary, original_binary)
            self.assertEqual(
                result["native_client"]["sha256"],
                hashlib.sha256(original_binary).hexdigest(),
            )
            self.assertEqual(binary.read_bytes(), replacement_bytes)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not portable to Windows")
    def test_atomic_replacement_preserves_existing_output_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, _ = self._inputs(root)
            output = root / "bundle.zip"
            output.write_bytes(b"previous package")
            output.chmod(0o640)

            package(instance, binary, mods, "windows-x86_64",
                    "3" * 40, output)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)

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

    def test_package_rejects_unicode_normalized_directory_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance = root / "instance.zip"
            with zipfile.ZipFile(instance, "w") as archive:
                archive.writestr("game/Caf\u00e9/", b"")
                archive.writestr("game/cafe\u0301/child.txt", b"payload")
            binary = root / "horizon-client.exe"
            binary.write_bytes(b"test executable")
            mods = root / "mods"
            mods.mkdir()
            output = root / "bundle.zip"

            with self.assertRaisesRegex(ValueError, "Case-insensitive ZIP directory prefix collision"):
                package(instance, binary, mods, "windows-x86_64",
                        "1" * 40, output)

            self.assertFalse(output.exists())

    def test_package_rejects_windows_superscript_reserved_device_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance = root / "instance.zip"
            with zipfile.ZipFile(instance, "w") as archive:
                archive.writestr("game/COM\u00b9.txt", b"reserved")
            binary = root / "horizon-client.exe"
            binary.write_bytes(b"test executable")
            mods = root / "mods"
            mods.mkdir()
            output = root / "bundle.zip"

            with self.assertRaisesRegex(ValueError, "Unsafe ZIP path"):
                package(instance, binary, mods, "windows-x86_64",
                        "2" * 40, output)

            self.assertFalse(output.exists())

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
            with self.assertRaisesRegex(ValueError, "Unsafe ZIP path"):
                package(instance, binary, mods, "windows-x86_64",
                        "b" * 40, root / "bundle.zip")


    def test_package_rejects_symlink_native_mod_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, mod = self._inputs(root)
            outside = root / "outside.hnmod"
            outside.write_bytes(b"must not be imported through a symlink")
            mod.unlink()
            try:
                mod.symlink_to(outside)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlinks are unavailable: {error}")

            output = root / "bundle.zip"
            with self.assertRaisesRegex(
                ValueError, "symlinks and special files are refused"
            ):
                package(instance, binary, mods, "windows-x86_64",
                        "9" * 40, output)

            self.assertFalse(output.exists())
            self.assertEqual(
                outside.read_bytes(), b"must not be imported through a symlink"
            )

    def test_package_rejects_symlink_swap_between_mod_scan_and_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, binary, mods, mod = self._inputs(root)
            outside = root / "outside.hnmod"
            outside.write_bytes(b"must not be imported after a path swap")
            output = root / "bundle.zip"
            real_open = os.open
            swapped = False

            def swap_before_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if Path(path) == mod and not swapped:
                    mod.unlink()
                    try:
                        mod.symlink_to(outside)
                    except (OSError, NotImplementedError) as error:
                        raise unittest.SkipTest(
                            f"symlinks are unavailable: {error}"
                        )
                    swapped = True
                return real_open(path, flags, *args, **kwargs)

            with patch.object(package_instance.os, "open", side_effect=swap_before_open):
                with self.assertRaisesRegex(
                    ValueError, "could not be opened without following a symlink|changed while it was being opened"
                ):
                    package(instance, binary, mods, "windows-x86_64",
                            "8" * 40, output)

            self.assertTrue(swapped)
            self.assertFalse(output.exists())
            self.assertEqual(
                outside.read_bytes(), b"must not be imported after a path swap"
            )



if __name__ == "__main__":
    unittest.main()
