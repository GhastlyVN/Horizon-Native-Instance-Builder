import io
import json
import os
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.extract_native_mods import extract_bundle


class NativeModExtractionTests(unittest.TestCase):
    def _bundle(self, path, root=""):
        prefix = root + "/" if root else ""
        manifest = {"id": "sample_mod", "horizon": "test"}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(prefix + "horizon-native.json", json.dumps(manifest))
            archive.writestr(
                prefix + "mixins/sample_mod/horizon-mixin.toml",
                '[mod]\nid = "sample_mod"\nversion = "1.0.0"\n',
            )
            archive.writestr(prefix + "mixins/sample_mod/src/lib.rs", "pub fn hook() {}\n")

    def test_extracts_root_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._bundle(archive)
            result = extract_bundle(archive, root / "out")
            self.assertTrue((result / "horizon-native.json").is_file())
            self.assertTrue((result / "mixins/sample_mod/horizon-mixin.toml").is_file())

    def test_strips_single_bundle_directory_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._bundle(archive, "sample")
            result = extract_bundle(archive, root / "out")
            self.assertTrue((result / "horizon-native.json").is_file())

    def test_rejects_mismatched_manifest_id_and_mixin_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("horizon-native.json", '{"id":"sample_mod"}')
                bundle.writestr("mixins/other/horizon-mixin.toml", '[mod]')
            with self.assertRaisesRegex(ValueError, "expected mixins/sample_mod"):
                extract_bundle(archive, root / "out")

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "no")
                bundle.writestr("horizon-native.json", '{"id":"sample_mod"}')
                bundle.writestr("mixins/sample_mod/horizon-mixin.toml", '[mod]')
            with self.assertRaisesRegex(ValueError, "Unsafe ZIP path"):
                extract_bundle(archive, root / "out")


    def test_rejects_unsafe_archive_stem_before_creating_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "...hnmod"
            self._bundle(archive)
            sentinel = root / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            destination = root / "out"

            with self.assertRaisesRegex(ValueError, "unsafe .hnmod filename"):
                extract_bundle(archive, destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertTrue(archive.is_file())
            self.assertFalse(destination.exists())

    def test_failed_replacement_preserves_existing_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._bundle(archive)
            destination = root / "out"
            target = destination / "sample"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("old bundle", encoding="utf-8")

            original_open = zipfile.ZipFile.open

            def fail_payload(instance, name, *args, **kwargs):
                filename = getattr(name, "filename", name)
                if filename == "mixins/sample_mod/src/lib.rs":
                    raise OSError("simulated member read failure")
                return original_open(instance, name, *args, **kwargs)

            with patch.object(zipfile.ZipFile, "open", fail_payload):
                with self.assertRaisesRegex(OSError, "simulated member read failure"):
                    extract_bundle(archive, destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old bundle")
            self.assertFalse((target / "horizon-native.json").exists())
            self.assertEqual([path.name for path in destination.iterdir()], ["sample"])

    def test_failed_atomic_promotion_restores_previous_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._bundle(archive)
            destination = root / "out"
            target = destination / "sample"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("old bundle", encoding="utf-8")

            original_replace = os.replace

            def fail_stage_promotion(source, dest):
                source_path = pathlib.Path(source)
                destination_path = pathlib.Path(dest)
                if source_path.name.startswith(".sample.stage-") and destination_path == target:
                    raise OSError("simulated atomic promotion failure")
                return original_replace(source, dest)

            with patch("scripts.extract_native_mods.os.replace", fail_stage_promotion):
                with self.assertRaisesRegex(OSError, "simulated atomic promotion failure"):
                    extract_bundle(archive, destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old bundle")
            self.assertEqual([path.name for path in destination.iterdir()], ["sample"])

    def test_rejects_actual_expansion_beyond_declared_size_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._bundle(archive)
            destination = root / "out"
            target = destination / "sample"
            target.mkdir(parents=True)
            sentinel = target / "keep.txt"
            sentinel.write_text("old bundle", encoding="utf-8")

            original_infolist = zipfile.ZipFile.infolist
            original_open = zipfile.ZipFile.open

            def undersized_infolist(instance):
                entries = original_infolist(instance)
                for info in entries:
                    if info.filename == "mixins/sample_mod/src/lib.rs":
                        info.file_size = 1
                return entries

            def expanded_open(instance, name, *args, **kwargs):
                filename = getattr(name, "filename", name)
                if filename == "mixins/sample_mod/src/lib.rs":
                    return io.BytesIO(b"payload larger than declared")
                return original_open(instance, name, *args, **kwargs)

            with (
                patch.object(zipfile.ZipFile, "infolist", undersized_infolist),
                patch.object(zipfile.ZipFile, "open", expanded_open),
            ):
                with self.assertRaisesRegex(ValueError, "expands beyond its declared size"):
                    extract_bundle(archive, destination)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old bundle")
            self.assertEqual([path.name for path in destination.iterdir()], ["sample"])


if __name__ == "__main__":
    unittest.main()
