import struct
import tempfile
import unittest
from pathlib import Path

from scripts.verify_glibc_baseline import (
    evaluate,
    parse_version,
    required_versions,
    verify_binary,
)

SAMPLE_READELF = """
Version needs section '.gnu.version_r' contains 3 entries:
  0x0000: Version: 1  File: libc.so.6  Cnt: 3
  0x0010:   Name: GLIBC_2.2.5  Flags: none  Version: 6
  0x0020:   Name: GLIBC_2.34  Flags: none  Version: 7
  0x0030:   Name: GLIBC_2.35  Flags: none  Version: 8
  0x0040: Version: 1  File: libm.so.6  Cnt: 1
  0x0050:   Name: GLIBC_2.29  Flags: none  Version: 3
"""


class GlibcBaselineTests(unittest.TestCase):
    def test_parses_numeric_versions(self):
        self.assertEqual(parse_version("2.35"), (2, 35))
        self.assertEqual(parse_version("2.2.5"), (2, 2, 5))
        with self.assertRaises(ValueError):
            parse_version("2")
        with self.assertRaises(ValueError):
            parse_version("latest")

    def test_extracts_each_required_version_once(self):
        versions = required_versions(SAMPLE_READELF + SAMPLE_READELF)
        self.assertEqual(versions, [(2, 2, 5), (2, 29), (2, 34), (2, 35)])

    def test_accepts_artifact_at_the_baseline(self):
        report = evaluate(SAMPLE_READELF, "2.35")
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["baseline"], "GLIBC_2.35")
        self.assertEqual(report["required_max"], "GLIBC_2.35")

    def test_compares_versions_numerically_not_lexically(self):
        # GLIBC_2.9 is older than GLIBC_2.35 even though "9" > "3".
        report = evaluate("Name: GLIBC_2.9\n", "2.35")
        self.assertEqual(report["required_max"], "GLIBC_2.9")

    def test_rejects_artifact_newer_than_baseline(self):
        with self.assertRaisesRegex(ValueError, "GLIBC_2\\.43"):
            evaluate(SAMPLE_READELF + "Name: GLIBC_2.43\n", "2.35")

    def test_rejects_output_without_glibc_requirements(self):
        with self.assertRaisesRegex(ValueError, "does not declare"):
            evaluate("Version needs section '.gnu.version_r' is empty\n", "2.35")

    def test_verify_binary_reads_the_real_file(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "horizon-client"
            binary.write_bytes(b"\x7fELF" + b"\x00" * 60)
            report = verify_binary(binary, "2.35", readelf=_fake_readelf)
            self.assertEqual(report["binary"], "horizon-client")
            self.assertEqual(report["required_max"], "GLIBC_2.35")

    def test_verify_binary_rejects_symlinked_input(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "horizon-client"
            real = binary.with_name("real-client")
            binary.write_bytes(b"\x7fELF" + b"\x00" * 60)
            binary.rename(real)
            try:
                binary.symlink_to(real)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaisesRegex(ValueError, "refusing symlink"):
                verify_binary(binary, "2.35", readelf=_fake_readelf)

    def test_verify_binary_rejects_non_elf_input(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "horizon-client"
            binary.write_bytes(b"MZ" + struct.pack("<I", 0) + b"\x00" * 58)
            with self.assertRaisesRegex(ValueError, "not an ELF executable"):
                verify_binary(binary, "2.35", readelf=_fake_readelf)


def _fake_readelf(*args, **kwargs):
    class Completed:
        stdout = SAMPLE_READELF

    return Completed()


if __name__ == "__main__":
    unittest.main()
