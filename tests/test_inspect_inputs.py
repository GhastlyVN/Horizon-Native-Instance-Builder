import hashlib
import io
import json
import os
import pathlib
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts import inspect_inputs
from scripts.prepare import MAX_CENTRAL_DIRECTORY_BYTES


class InspectInputsTests(unittest.TestCase):
    def _instance_zip(self, path: pathlib.Path) -> bytes:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("game/versions/26.3/26.3.json", "{}")
            archive.writestr("game/options.txt", "fullscreen:false")
        return path.read_bytes()

    def _native_mod_zip(self, path: pathlib.Path, source: bytes = b"fn main() {}") -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("bundle/horizon-native.json", json.dumps({"id": "test"}))
            archive.writestr("bundle/horizon-mixin.toml", "[mixin]\n")
            archive.writestr("bundle/src/lib.rs", source)

    def test_hash_stream_stops_after_limit_plus_one(self):
        class CountingStream(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.bytes_read = 0

            def read(self, size=-1):
                chunk = super().read(size)
                self.bytes_read += len(chunk)
                return chunk

        stream = CountingStream(b"x" * 20)
        with patch.object(inspect_inputs, "MAX_ARCHIVE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "inspection size limit"):
                inspect_inputs._hash_stream_bounded(stream)
        self.assertEqual(stream.bytes_read, 5)

    def test_windows_file_identity_uses_creation_time_semantics(self):
        from types import SimpleNamespace

        before = SimpleNamespace(
            st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5
        )
        after = SimpleNamespace(
            st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=99
        )
        with patch.object(inspect_inputs.sys, "platform", "win32"):
            self.assertEqual(
                inspect_inputs._file_identity(before),
                inspect_inputs._file_identity(after),
            )
        with patch.object(inspect_inputs.sys, "platform", "linux"):
            self.assertNotEqual(
                inspect_inputs._file_identity(before),
                inspect_inputs._file_identity(after),
            )

    def test_instance_inspection_uses_shared_preflight_before_crc_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../outside.txt", "unsafe")

            with patch.object(zipfile.ZipFile, "testzip") as crc_scan:
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP path"):
                    inspect_inputs.inspect_instance(archive)
                crc_scan.assert_not_called()

    def test_inspector_rejects_symlink_archive_input_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "instance.zip"
            alias = pathlib.Path(directory) / "instance-link.zip"
            self._instance_zip(archive)
            try:
                alias.symlink_to(archive)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlinks are unavailable: {error}")

            with patch.object(
                zipfile,
                "ZipFile",
                side_effect=AssertionError("inspector must reject links before ZIP parsing"),
            ):
                with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                    inspect_inputs.inspect_instance(alias)

    @unittest.skipIf(os.name == "nt", "open-file replacement semantics differ on Windows")
    def test_inspector_rejects_archive_path_replacement_after_open(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "instance.zip"
            replacement = pathlib.Path(directory) / "replacement.zip"
            self._instance_zip(archive)
            self._instance_zip(replacement)
            original_open = os.open
            replaced = False

            def replace_after_open(path, flags, *args, **kwargs):
                nonlocal replaced
                descriptor = original_open(path, flags, *args, **kwargs)
                if pathlib.Path(path) == archive and not replaced:
                    os.replace(replacement, archive)
                    replaced = True
                return descriptor

            with patch("scripts.prepare.os.open", side_effect=replace_after_open):
                with self.assertRaisesRegex(ValueError, "changed while it was being opened"):
                    inspect_inputs.inspect_instance(archive)
            self.assertTrue(replaced)

    def test_output_cannot_be_the_instance_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "instance.zip"
            original = self._instance_zip(archive)

            with self.assertRaisesRegex(ValueError, "must not overwrite an inspected input"):
                inspect_inputs.ensure_output_does_not_alias_inputs(archive, [archive])

            self.assertEqual(archive.read_bytes(), original)

    def test_output_cannot_be_a_hard_link_to_an_input(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "instance.zip"
            alias = pathlib.Path(directory) / "report.json"
            self._instance_zip(archive)
            try:
                os.link(archive, alias)
            except OSError as error:
                self.skipTest(f"hard links are unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "must not overwrite an inspected input"):
                inspect_inputs.ensure_output_does_not_alias_inputs(alias, [archive])

            with zipfile.ZipFile(archive) as source:
                self.assertEqual(source.namelist(), [
                    "game/versions/26.3/26.3.json",
                    "game/options.txt",
                ])

    def test_output_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "instance.zip"
            alias = pathlib.Path(directory) / "report.json"
            self._instance_zip(archive)
            try:
                alias.symlink_to(archive)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlinks are unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                inspect_inputs.ensure_output_does_not_alias_inputs(alias, [archive])

    def test_atomic_report_preserves_previous_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "report.json"
            output.write_text('{"previous":true}\n', encoding="utf-8")

            with patch("scripts.inspect_inputs.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    inspect_inputs.write_report_atomic(output, {"new": True})

            self.assertEqual(output.read_text(encoding="utf-8"), '{"previous":true}\n')
            self.assertEqual(list(pathlib.Path(directory).glob(".report.json.*.tmp")), [])

    def test_external_instance_archive_is_reported_without_relative_to_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "external.zip"
            self._instance_zip(archive)

            report = inspect_inputs.inspect_instance(archive)

            self.assertEqual(report["file"], str(archive.resolve()))
            self.assertEqual(report["entry_count"], 2)
            self.assertEqual(len(report["sha256"]), 64)

    def test_native_mod_source_file_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "large.hnmod"
            self._native_mod_zip(
                archive,
                b"x" * (inspect_inputs.MAX_SOURCE_FILE_BYTES + 1),
            )

            with self.assertRaisesRegex(ValueError, "Rust source file exceeds the inspection limit"):
                inspect_inputs.read_mod(archive)

    def test_native_mod_source_hash_covers_exact_archive_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "valid.hnmod"
            source = b'pub fn greet() { println!("hello"); }\n'
            self._native_mod_zip(archive, source)

            report = inspect_inputs.read_mod(archive)

            source_summary = report["source_files"]["bundle/src/lib.rs"]
            self.assertEqual(source_summary["bytes"], len(source))
            self.assertEqual(source_summary["sha256"], hashlib.sha256(source).hexdigest())

    def test_inspector_reads_and_hashes_the_same_snapshot_after_in_place_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = pathlib.Path(directory) / "instance.zip"
            original = self._instance_zip(source_path)
            original_mtime = source_path.stat().st_mtime_ns

            replacement_path = pathlib.Path(directory) / "replacement.zip"
            with zipfile.ZipFile(replacement_path, "w") as output:
                output.writestr("game/versions/26.3/26.3.json", "{}")
                output.writestr("game/options.txt", "fullscreen:true ")
            replacement = replacement_path.read_bytes()
            self.assertEqual(len(replacement), len(original))

            # Simulate Windows, where ctime is creation time and cannot detect
            # same-size in-place writes when the original mtime is restored.
            with patch.object(inspect_inputs.sys, "platform", "win32"):
                with inspect_inputs._validated_archive(source_path) as (
                    validated, digest, archive_bytes, _names
                ):
                    with source_path.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(replacement)
                        stream.truncate()
                    os.utime(source_path, ns=(original_mtime, original_mtime))

                    self.assertEqual(
                        validated.read("game/options.txt"),
                        b"fullscreen:false",
                    )
                    self.assertEqual(digest, hashlib.sha256(original).hexdigest())
                    self.assertEqual(archive_bytes, len(original))

    def test_archive_mutation_during_inspection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "changing.zip"
            self._instance_zip(archive)
            original_testzip = zipfile.ZipFile.testzip

            def mutate_after_crc_scan(instance):
                result = original_testzip(instance)
                with archive.open("ab") as stream:
                    stream.write(b"changed")
                return result

            with patch.object(zipfile.ZipFile, "testzip", mutate_after_crc_scan):
                with self.assertRaisesRegex(ValueError, "changed during inspection"):
                    inspect_inputs.inspect_instance(archive)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO test")
    def test_validated_archive_refuses_a_fifo_without_hanging(self):
        with tempfile.TemporaryDirectory() as dirname:
            fifo = pathlib.Path(dirname) / "input.zip"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "regular"):
                with inspect_inputs._validated_archive(fifo):
                    pass


    def test_validated_archive_refuses_symlink_before_opening_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "source.zip"
            alias = root / "linked.zip"
            self._instance_zip(archive)
            try:
                alias.symlink_to(archive)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation is unavailable: {error}")

            with patch(
                "scripts.inspect_inputs.os.open",
                side_effect=AssertionError("linked archive must be rejected before opening"),
            ):
                with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                    with inspect_inputs._validated_archive(alias):
                        pass

    @unittest.skipIf(os.name == "nt", "open-file replacement semantics differ on Windows")
    def test_validated_archive_rejects_symlink_swap_after_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            archive = root / "source.zip"
            moved = root / "source-original.zip"
            target = root / "target.zip"
            self._instance_zip(archive)
            self._instance_zip(target)
            original_open = os.open
            replaced = False

            def replace_after_open(path, flags, *args, **kwargs):
                nonlocal replaced
                descriptor = original_open(path, flags, *args, **kwargs)
                if pathlib.Path(path) == archive and not replaced:
                    os.replace(archive, moved)
                    archive.symlink_to(target)
                    replaced = True
                return descriptor

            with patch("scripts.inspect_inputs.os.open", side_effect=replace_after_open):
                with self.assertRaisesRegex(ValueError, "(?:ZIP archive|path) changed while it was being opened"):
                    with inspect_inputs._validated_archive(archive):
                        pass
            self.assertTrue(replaced)


    def test_inspector_rejects_oversized_central_directory_before_zipfile(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / "oversized-central-directory.zip"
            archive.write_bytes(
                struct.pack(
                    "<4s4H2LH",
                    b"PK\x05\x06",
                    0, 0, 0, 0,
                    MAX_CENTRAL_DIRECTORY_BYTES + 1,
                    0, 0,
                )
            )

            with patch.object(
                zipfile,
                "ZipFile",
                side_effect=AssertionError("inspector must preflight before ZIP parsing"),
            ):
                with self.assertRaisesRegex(ValueError, "central directory exceeds the size limit"):
                    inspect_inputs.inspect_instance(archive)


    def test_atomic_report_is_sorted_json_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "reports" / "inspection.json"
            inspect_inputs.write_report_atomic(
                output, {"z": 1, "a": {"d": 4, "b": 2}}
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                '{\n  "a": {\n    "b": 2,\n    "d": 4\n  },\n  "z": 1\n}\n',
            )



if __name__ == "__main__":
    unittest.main()
