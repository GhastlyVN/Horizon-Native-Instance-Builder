import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_native_artifacts
from scripts.verify_native_artifacts import verify_artifacts

class NativeArtifactVerificationTests(unittest.TestCase):
    def _fixture(self, root: Path, commit: str = "a" * 40):
        inputs = root / "inputs"
        inputs.joinpath("build-report").mkdir(parents=True)
        inputs.joinpath("build-report", "horizon-commit.txt").write_text(
            commit + "\n", encoding="ascii"
        )
        mod_dir = inputs / "input" / "mods-latest"
        mod_dir.mkdir(parents=True)
        mod_bytes = b"test bundle"
        mod_dir.joinpath("sample-1.0.0.hnmod").write_bytes(mod_bytes)
        inputs.joinpath("build-report", "registry-mods.json").write_text(
            json.dumps({
                "schema": 1,
                "horizon_commit": commit,
                "selected": [{
                    "file": "sample-1.0.0.hnmod",
                    "size": len(mod_bytes),
                    "sha256": hashlib.sha256(mod_bytes).hexdigest(),
                }],
                "skipped": [],
            }),
            encoding="utf-8",
        )
        linux = root / "linux"
        windows = root / "windows"
        for base, target, client in (
            (linux, "linux-x86_64", "horizon-client"),
            (windows, "windows-x86_64", "horizon-client.exe"),
        ):
            report = base / "build-report" / target
            report.mkdir(parents=True)
            report.joinpath("build.txt").write_text(
                f"Horizon commit: {commit}\nTarget: {target}\nRunner: test\n"
                f"Optional native mods: built\nBuild mode: core+native-mods\n"
                f"Native version: native-{commit[:7]}-test\n",
                encoding="utf-8",
            )
            runtime = base / "native-runtime"
            runtime.mkdir()
            if target == "linux-x86_64":
                binary = bytearray(64)
                binary[:4] = b"\x7fELF"
                binary[4:7] = bytes((2, 1, 1))
                struct.pack_into("<H", binary, 18, 62)
            else:
                binary = bytearray(0x86)
                binary[:2] = b"MZ"
                struct.pack_into("<I", binary, 0x3C, 0x80)
                binary[0x80:0x84] = b"PE\x00\x00"
                struct.pack_into("<H", binary, 0x84, 0x8664)
            runtime.joinpath(client).write_bytes(binary)
        return inputs, linux, windows

    def test_verifies_both_platforms_and_emits_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            result = verify_artifacts(inputs, linux, windows)
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(result["horizon_commit"], "a" * 40)
            self.assertEqual(set(result["targets"]), {"linux-x86_64", "windows-x86_64"})
            for target in result["targets"].values():
                self.assertGreater(target["bytes"], 0)
                self.assertEqual(len(target["sha256"]), 64)

    def test_verifies_separately_published_core_client(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            for root, source, core_name in (
                (linux, "horizon-client", "horizon-client-core"),
                (windows, "horizon-client.exe", "horizon-client-core.exe"),
            ):
                runtime = root / "native-runtime"
                runtime.joinpath(core_name).write_bytes(runtime.joinpath(source).read_bytes())
            result = verify_artifacts(inputs, linux, windows)
            for target in result["targets"].values():
                self.assertIn("core_client", target)
                self.assertEqual(len(target["core_client"]["sha256"]), 64)

    def test_rejects_wrong_native_machine_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            binary = windows / "native-runtime" / "horizon-client.exe"
            data = bytearray(binary.read_bytes())
            struct.pack_into("<H", data, 0x84, 0x014C)  # 32-bit x86
            binary.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "expected PE AMD64"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_non_executable_payload_with_valid_size(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            binary = linux / "native-runtime" / "horizon-client"
            binary.write_bytes(b"not-an-ELF-binary".ljust(64, b"\x00"))
            with self.assertRaisesRegex(ValueError, "not an ELF executable"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_native_binary_changed_while_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            binary = linux / "native-runtime" / "horizon-client"
            original_fdopen = os.fdopen
            fdopen_calls = 0
            mutated = False

            class MutatingStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def fileno(self):
                    return self.stream.fileno()

                def seek(self, *args):
                    return self.stream.seek(*args)

                def read(self, size=-1):
                    nonlocal mutated
                    data = self.stream.read(size)
                    if not mutated:
                        with binary.open("ab") as changed:
                            changed.write(b"changed")
                        mutated = True
                    return data

            def patched_fdopen(descriptor, mode="r", *args, **kwargs):
                nonlocal fdopen_calls
                fdopen_calls += 1
                stream = original_fdopen(descriptor, mode, *args, **kwargs)
                # pinned commit, registry report, mod, Linux report, then Linux binary.
                if fdopen_calls == 5 and mode == "rb":
                    return MutatingStream(stream)
                return stream

            with patch("scripts.verify_native_artifacts.os.fdopen", side_effect=patched_fdopen):
                with self.assertRaisesRegex(ValueError, "changed during reading"):
                    verify_artifacts(inputs, linux, windows)
            self.assertTrue(mutated)


    def test_rejects_report_symlink_swap_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            report = linux / "build-report" / "linux-x86_64" / "build.txt"
            moved = report.with_name("build-original.txt")
            victim = Path(temp) / "outside-report.txt"
            victim.write_text("Target: linux-x86_64\\n", encoding="utf-8")
            real_open = os.open
            real_fdopen = os.fdopen
            fdopen_calls = 0
            swapped_report_read = False
            swapped = False

            class TrackReportRead:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def fileno(self):
                    return self.stream.fileno()

                def read(self, size=-1):
                    nonlocal swapped_report_read
                    swapped_report_read = True
                    return self.stream.read(size)

            def replace_before_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if Path(path) == report and not swapped:
                    report.replace(moved)
                    try:
                        report.symlink_to(victim)
                    except (OSError, NotImplementedError) as error:
                        self.skipTest(f"symlink creation unavailable: {error}")
                    swapped = True
                return real_open(path, flags, *args, **kwargs)

            def track_fdopen(descriptor, *args, **kwargs):
                nonlocal fdopen_calls
                fdopen_calls += 1
                stream = real_fdopen(descriptor, *args, **kwargs)
                # On Windows the reparse-point handle may be opened, but it must never be read.
                if fdopen_calls == 4:
                    return TrackReportRead(stream)
                return stream

            with patch("scripts.verify_native_artifacts.os.open", side_effect=replace_before_open):
                with patch("scripts.verify_native_artifacts.os.fdopen", side_effect=track_fdopen):
                    with self.assertRaisesRegex(ValueError, "changed while it was being opened|could not be opened safely|refusing symlink"):
                        verify_artifacts(inputs, linux, windows)

            self.assertTrue(swapped)
            self.assertFalse(swapped_report_read)
            self.assertEqual(victim.read_text(encoding="utf-8"), "Target: linux-x86_64\\n")


    def test_rejects_symlinked_native_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            binary = linux / "native-runtime" / "horizon-client"
            target = binary.with_name("real-client")
            binary.rename(target)
            try:
                binary.symlink_to(target)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "symlink or reparse-point"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_symlink_swapped_before_open(self):
        if not getattr(os, "O_NOFOLLOW", 0):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            binary = linux / "native-runtime" / "horizon-client"
            target = binary.with_name("real-client")
            binary.rename(target)
            try:
                binary.symlink_to(target)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            original_lstat = Path.lstat
            target_stat = original_lstat(target)

            def patched_lstat(candidate):
                if candidate == binary:
                    return target_stat
                return original_lstat(candidate)

            with patch.object(Path, "lstat", patched_lstat):
                with self.assertRaisesRegex(ValueError, "refusing symlink"):
                    verify_artifacts(inputs, linux, windows)

    def test_rejects_platform_built_from_different_horizon_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            report = windows / "build-report" / "windows-x86_64" / "build.txt"
            report.write_text(
                f"Horizon commit: {'b' * 40}\nTarget: windows-x86_64\n"
                "Optional native mods: built\nBuild mode: core+native-mods\n"
                f"Native version: native-{'b' * 7}-test\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Horizon commit mismatch"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_missing_native_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            (windows / "native-runtime" / "horizon-client.exe").unlink()
            with self.assertRaisesRegex(ValueError, "missing native client binary"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_non_immutable_commit_identifier(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp), "main")
            with self.assertRaisesRegex(ValueError, "full 40-character SHA-1"):
                verify_artifacts(inputs, linux, windows)
    def test_report_output_refuses_links_and_directories(self):
        result = {"horizon_commit": "a" * 40}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            output = root / "report.json"
            output.symlink_to(victim)
            with patch.object(verify_native_artifacts, "verify_artifacts", return_value=result), patch(
                "sys.argv",
                ["verify", "--inputs", "inputs", "--linux", "linux", "--windows", "windows", "--output", str(output)],
            ):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    verify_native_artifacts.main()
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

            output.unlink()
            output.mkdir()
            with patch.object(verify_native_artifacts, "verify_artifacts", return_value=result), patch(
                "sys.argv",
                ["verify", "--inputs", "inputs", "--linux", "linux", "--windows", "windows", "--output", str(output)],
            ):
                with self.assertRaisesRegex(ValueError, "non-regular"):
                    verify_native_artifacts.main()

    def test_rejects_symlinked_pinned_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            pinned = inputs / "build-report" / "horizon-commit.txt"
            real = inputs / "build-report" / "real-commit.txt"
            real.write_text(pinned.read_text(encoding="ascii"), encoding="ascii")
            pinned.unlink()
            try:
                pinned.symlink_to(real)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                verify_artifacts(inputs, linux, windows)

    def test_ignores_symlinked_mod_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            mod_dir = inputs / "input" / "mods-latest"
            alias = mod_dir / "linked.hnmod"
            try:
                alias.symlink_to(mod_dir / "sample-1.0.0.hnmod")
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            result = verify_artifacts(inputs, linux, windows)
            self.assertEqual(result["status"], "VERIFIED")


    def test_verifies_explicit_core_only_build(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            mod_dir = inputs / "input" / "mods-latest"
            for mod in mod_dir.glob("*.hnmod"):
                mod.unlink()
            report_path = inputs / "build-report" / "registry-mods.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["selected"] = []
            report_path.write_text(json.dumps(report), encoding="utf-8")
            for root, target in ((linux, "linux-x86_64"), (windows, "windows-x86_64")):
                path = root / "build-report" / target / "build.txt"
                text = path.read_text(encoding="utf-8")
                text = text.replace("Optional native mods: built", "Optional native mods: core-only")
                text = text.replace("Build mode: core+native-mods", "Build mode: core-only")
                path.write_text(text, encoding="utf-8")
            result = verify_artifacts(inputs, linux, windows)
            self.assertEqual(result["status"], "VERIFIED")

    def test_rejects_mod_changed_after_registry_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            mod = inputs / "input" / "mods-latest" / "sample-1.0.0.hnmod"
            raw = bytearray(mod.read_bytes())
            raw[0] ^= 1
            mod.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "no longer match the registry-verified digest"):
                verify_artifacts(inputs, linux, windows)

    def test_rejects_mod_absent_from_registry_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs, linux, windows = self._fixture(Path(temp))
            (inputs / "input" / "mods-latest" / "extra.hnmod").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "do not match the registry selection"):
                verify_artifacts(inputs, linux, windows)

    def test_write_report_atomically_refuses_linked_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "report.json"
            victim = root / "victim.json"
            victim.write_text("{}", encoding="utf-8")
            try:
                output.symlink_to(victim)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                verify_native_artifacts.write_report_atomically(output, "{}\n")
            self.assertEqual(victim.read_text(encoding="utf-8"), "{}")

    def test_write_report_atomically_replaces_existing_report(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            output.write_text("old", encoding="utf-8")

            verify_native_artifacts.write_report_atomically(output, "new\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "new\n")

if __name__ == "__main__":
    unittest.main()
