import hashlib
import json
import os
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts import prepare
from scripts.extract_native_mods import extract_bundle


class ZipSnapshotBindingTests(unittest.TestCase):
    def _write_bundle(self, path):
        manifest = {"id": "sample_mod", "horizon": "AAAA"}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("horizon-native.json", json.dumps(manifest))
            archive.writestr(
                "mixins/sample_mod/horizon-mixin.toml",
                '[mod]\nid = "sample_mod"\nversion = "1.0.0"\n',
            )
            archive.writestr("mixins/sample_mod/src/lib.rs", "pub fn hook() {}\n")

    def _mutate_after_validation(self, path):
        original = prepare.validate_zip_archive
        mutated = False

        def validate(archive):
            nonlocal mutated
            result = original(archive)
            if not mutated:
                mutated = True
                raw = path.read_bytes()
                index = raw.index(b"AAAA")
                raw = raw[:index] + b"BBBB" + raw[index + 4 :]
                with path.open("r+b") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                metadata = path.stat()
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            return result

        return validate

    def test_snapshot_metadata_describes_the_validated_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "instance.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("game/versions/1/1.json", "{}")
            raw = path.read_bytes()
            with prepare.open_zip_snapshot(path) as (archive, size, sha256):
                self.assertEqual(size, len(raw))
                self.assertEqual(sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(archive.read("game/versions/1/1.json"), b"{}")

    def test_native_extraction_uses_the_snapshot_after_in_place_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "sample.hnmod"
            self._write_bundle(archive)
            destination = root / "out"
            with patch.object(
                prepare, "validate_zip_archive", self._mutate_after_validation(archive)
            ):
                extract_bundle(archive, destination)
            written = (destination / "sample" / "horizon-native.json").read_text(
                encoding="utf-8"
            )
            self.assertEqual(json.loads(written)["horizon"], "AAAA")
            self.assertIn(b"BBBB", archive.read_bytes())
            self.assertTrue(
                (destination / "sample" / "mixins/sample_mod/src/lib.rs").is_file()
            )

    def test_prepare_manifest_records_the_snapshot_not_the_mutated_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            instance = root / "instance.zip"
            with zipfile.ZipFile(instance, "w") as archive:
                archive.writestr("game/versions/1/1.json", "{}")
            mods = root / "mods"
            mods.mkdir()
            self._write_bundle(mods / "sample.hnmod")
            report = root / "build-report"
            original = instance.read_bytes()
            with (
                patch.object(
                    prepare, "validate_zip_archive", self._mutate_after_validation(instance)
                ),
                patch.object(prepare, "REPORT", report),
                patch(
                    "sys.argv",
                    ["prepare.py", "--instance", str(instance), "--mods", str(mods)],
                ),
            ):
                prepare.main()
            manifest = json.loads(
                (report / "input-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["instance"]["bytes"], len(original))
            self.assertEqual(
                manifest["instance"]["sha256"], hashlib.sha256(original).hexdigest()
            )
            self.assertEqual(
                manifest["mods"][0]["bytes"], (mods / "sample.hnmod").stat().st_size
            )
            self.assertNotEqual(instance.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
