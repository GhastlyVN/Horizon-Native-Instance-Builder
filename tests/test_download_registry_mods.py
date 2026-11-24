import hashlib
import io
import json
import struct
import unittest
from unittest.mock import patch
import zipfile
from unittest.mock import patch

from scripts.download_registry_mods import (
    APPROVED_DOWNLOAD_HOSTS, choose_versions, fetch_bytes, validate_bundle
)


class RegistryModSelectionTests(unittest.TestCase):
    def make_bundle(self, extra_entries=()):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("horizon-native.json", json.dumps({
                "id": "sample", "version": "1.2.0", "game": "26.3"
            }))
            archive.writestr("mixins/sample/horizon-mixin.toml", '[mod]\nid = "sample"\n')
            for name, content in extra_entries:
                archive.writestr(name, content)
        data = payload.getvalue()
        expected = {
            "id": "sample",
            "version": "1.2.0",
            "game": "26.3",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        return data, expected

    def make_index(self, published="2026-11-08"):
        return {
            "projects": [{
                "id": "sample",
                "kind": "native-mod",
                "versions": [{
                    "version": "1.2.0",
                    "published": published,
                    "game": "26.3",
                    "file": {
                        "name": "sample-1.2.0.hnmod",
                        "url": "https://github.com/example/sample/releases/download/v1.2.0/sample-1.2.0.hnmod",
                        "size": 100,
                        "sha256": "a" * 64,
                    },
                }],
            }],
            "revoked": [],
        }

    def test_selects_latest_version_when_health_report_matches(self):
        selected, skipped = choose_versions(
            self.make_index(),
            {"horizon": "1" * 40, "broken": {}},
            "2026-11-14T00:09:00Z",
            ("sample",),
            "26.3",
            "1" * 40,
        )
        self.assertEqual([item["version"] for item in selected], ["1.2.0"])
        self.assertEqual(skipped, [])

    def test_skips_version_marked_broken_on_latest_nightly(self):
        selected, skipped = choose_versions(
            self.make_index(),
            {"horizon": "1" * 40, "broken": {"sample": {
                "version": "1.2.0", "code": "E_TARGET_NOT_FOUND", "stage": "apply"
            }}},
            "2026-11-14T00:09:00Z",
            ("sample",),
            "26.3",
            "1" * 40,
        )
        self.assertEqual(selected, [])
        self.assertIn("E_TARGET_NOT_FOUND", skipped[0]["reason"])

    def test_refuses_health_report_for_another_horizon_commit(self):
        with self.assertRaisesRegex(ValueError, "not the selected Nightly"):
            choose_versions(
                self.make_index(),
                {"horizon": "2" * 40, "broken": {}},
                "2026-11-14T00:09:00Z",
                ("sample",),
                "26.3",
                "1" * 40,
            )

    def test_refuses_mod_published_after_health_report_date(self):
        with self.assertRaisesRegex(ValueError, "not demonstrably before"):
            choose_versions(
                self.make_index("2026-11-14"),
                {"horizon": "1" * 40, "broken": {}},
                "2026-11-14T00:09:00Z",
                ("sample",),
                "26.3",
                "1" * 40,
            )

    def test_refuses_malformed_revocations(self):
        index = self.make_index()
        index["revoked"] = {"sample": {"reason": "unsafe"}}
        with self.assertRaisesRegex(ValueError, "revocations must be a list"):
            choose_versions(
                index,
                {"horizon": "1" * 40, "broken": {}},
                "2026-11-14T00:09:00Z",
                ("sample",),
                "26.3",
                "1" * 40,
            )

    def test_skips_revoked_project(self):
        index = self.make_index()
        index["revoked"] = [{"id": "sample"}]
        selected, skipped = choose_versions(
            index,
            {"horizon": "1" * 40, "broken": {}},
            "2026-11-14T00:09:00Z",
            ("sample",),
            "26.3",
            "1" * 40,
        )
        self.assertEqual(selected, [])
        self.assertIn("revoked", skipped[0]["reason"])

    def test_refuses_malformed_broken_map(self):
        with self.assertRaisesRegex(ValueError, "broken map must be an object"):
            choose_versions(
                self.make_index(),
                {"horizon": "1" * 40, "broken": ["sample"]},
                "2026-11-14T00:09:00Z",
                ("sample",),
                "26.3",
                "1" * 40,
            )

    def test_bundle_rejects_windows_reserved_paths(self):
        data, expected = self.make_bundle((("CON.txt", b"reserved"),))
        with self.assertRaisesRegex(ValueError, "Unsafe ZIP path"):
            validate_bundle(data, expected)

    def test_bundle_rejects_oversized_member_before_decompression(self):
        data, expected = self.make_bundle((("large.bin", b"A" * 1024),))
        with patch("scripts.download_registry_mods.MAX_BUNDLE_MEMBER_BYTES", 512):
            with patch(
                "scripts.download_registry_mods.zipfile.ZipFile.open",
                side_effect=AssertionError("oversized member must be rejected before decompression"),
            ):
                with self.assertRaisesRegex(ValueError, "member exceeds the uncompressed size limit"):
                    validate_bundle(data, expected)

    def test_bundle_preflights_zip64_directory_before_zipfile_parsing(self):
        central_directory = b"C" * 32
        zip64_offset = len(central_directory)
        zip64_record = struct.pack(
            "<4sQ2H2I4Q",
            b"PK\x06\x06",
            44, 45, 45, 0, 0, 1, 1,
            len(central_directory),
            0,
        )
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, zip64_offset, 1)
        end_record = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0,
        )
        data = central_directory + zip64_record + locator + end_record
        expected = {
            "id": "sample",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

        with patch("scripts.prepare.MAX_CENTRAL_DIRECTORY_BYTES", 16):
            with patch(
                "scripts.download_registry_mods.zipfile.ZipFile",
                side_effect=AssertionError("ZIP64 directory must be bounded before parsing"),
            ):
                with self.assertRaisesRegex(ValueError, "central directory exceeds the size limit"):
                    validate_bundle(data, expected)

    def test_bundle_manifest_must_match_registry(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("horizon-native.json", json.dumps({
                "id": "sample", "version": "1.2.0", "game": "26.3"
            }))
            archive.writestr("mixins/sample/horizon-mixin.toml", '[mod]\nid = "sample"\n')
        data = payload.getvalue()
        expected = {
            "id": "sample",
            "version": "1.2.0",
            "game": "26.3",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        validate_bundle(data, expected)
        expected["version"] = "9.9.9"
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_bundle(data, expected)


if __name__ == "__main__":
    unittest.main()


class RegistryDownloadBoundaryTests(unittest.TestCase):
    def fake_response(self, final_url: str):
        class Response:
            headers = {}

            def geturl(self):
                return final_url

            def read(self, *_args):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return Response()

    def test_rejects_unapproved_redirect_host(self):
        with patch("scripts.download_registry_mods.open_url", return_value=self.fake_response("https://evil.example/file")):
            with self.assertRaisesRegex(ValueError, "unapproved download URL"):
                fetch_bytes("https://github.com/owner/repo/releases/download/v1/file")

    def test_rejects_credentials_and_non_default_port(self):
        for url in (
            "https://user@github.com/file",
            "https://github.com:8443/file",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_bytes(url)

    def test_accepts_asset_cdn_redirect(self):
        self.assertIn("objects.githubusercontent.com", APPROVED_DOWNLOAD_HOSTS)
        with patch("scripts.download_registry_mods.open_url", return_value=self.fake_response("https://objects.githubusercontent.com/file")):
            self.assertEqual(fetch_bytes("https://github.com/owner/repo/releases/download/v1/file"), b"{}")
