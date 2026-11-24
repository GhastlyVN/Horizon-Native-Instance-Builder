import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import scan_horizon_versions
from scripts.scan_horizon_versions import build_catalog


def release(tag, commit, date="2026-11-13T07:47:16Z", assets=True):
    return {
        "tag_name": tag,
        "name": f"Nightly {tag}",
        "prerelease": True,
        "published_at": date,
        "target_commitish": commit,
        "html_url": f"https://github.com/horizon-rust/horizon/releases/tag/{tag}",
        "assets": ([{
            "name": "horizon-linux-x86_64.tar.gz",
            "size": 123,
            "browser_download_url": "https://github.com/horizon-rust/horizon/releases/download/nightly/horizon-linux-x86_64.tar.gz",
        }] if assets else []),
    }


class HorizonVersionCatalogTests(unittest.TestCase):
    def test_catalog_includes_only_installable_nightly_releases(self):
        sha = "a" * 40
        items = [
            release("nightly", sha),
            release("nightly-2026-11-08", "b" * 40, "2026-11-08T10:00:00Z"),
            {**release("launcher@0.2.66", "c" * 40), "prerelease": False},
            release("nightly-2026-11-01", "d" * 40, assets=False),
        ]
        result = build_catalog(items, 20)
        self.assertEqual([x["tag"] for x in result["versions"]], ["nightly", "nightly-2026-11-08"])
        self.assertEqual(result["versions"][0]["install_tag"], "nightly-2026-11-13-aaaaaaa")

    def test_skips_releases_with_non_string_tag(self):
        malformed = release("nightly", "b" * 40)
        malformed["tag_name"] = None
        result = build_catalog([malformed, release("nightly", "a" * 40)], 20)
        self.assertEqual([x["tag"] for x in result["versions"]], ["nightly"])

    def test_refuses_release_without_full_source_commit(self):
        with self.assertRaisesRegex(ValueError, "immutable source commit"):
            build_catalog([release("nightly", "not-a-sha")], 20)

    def test_refuses_asset_urls_outside_github_release_downloads(self):
        for url in (
            "http://github.com/horizon-rust/horizon/releases/download/nightly/horizon-linux-x86_64.tar.gz",
            "https://evil.example/horizon-linux-x86_64.tar.gz",
            "https://github.com/horizon-rust/horizon/releases/download/nightly/wrong.tar.gz",
        ):
            item = release("nightly", "a" * 40)
            item["assets"][0]["browser_download_url"] = url
            with self.assertRaisesRegex(ValueError, "unsafe download URL"):
                build_catalog([item], 20)

    def test_sorts_newest_first_and_honors_limit(self):
        items = [
            release("nightly-2026-11-08", "b" * 40, "2026-11-08T10:00:00Z"),
            release("nightly", "a" * 40, "2026-11-13T10:00:00Z"),
        ]
        result = build_catalog(items, 1)
        self.assertEqual(len(result["versions"]), 1)
        self.assertEqual(result["versions"][0]["tag"], "nightly")

    def test_refuses_unapproved_asset_url(self):
        bad = release("nightly", "a" * 40)
        bad["assets"][0]["browser_download_url"] = "https://evil.example/horizon-linux-x86_64.tar.gz"
        with self.assertRaisesRegex(ValueError, "unapproved asset download URL"):
            build_catalog([bad], 20)

    def test_refuses_unapproved_release_url(self):
        bad = release("nightly", "a" * 40)
        bad["html_url"] = "https://evil.example/nightly"
        with self.assertRaisesRegex(ValueError, "unapproved release URL"):
            build_catalog([bad], 20)

    def test_catalog_output_refuses_links_and_directories(self):
        releases = [release("nightly", "a" * 40)]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            output = root / "catalog.json"
            output.symlink_to(victim)
            with patch.object(scan_horizon_versions, "fetch_releases", return_value=releases), patch(
                "sys.argv", ["scan", "--output", str(output)]
            ):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    scan_horizon_versions.main()
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

            output.unlink()
            output.mkdir()
            with patch.object(scan_horizon_versions, "fetch_releases", return_value=releases), patch(
                "sys.argv", ["scan", "--output", str(output)]
            ):
                with self.assertRaisesRegex(ValueError, "non-regular"):
                    scan_horizon_versions.main()

    def test_catalog_publish_replaces_a_raced_symlink_without_touching_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "catalog.json"
            victim = root / "victim.json"
            victim.write_text("keep", encoding="utf-8")
            original_replace = scan_horizon_versions.os.replace

            def insert_link_then_replace(source, destination):
                Path(destination).symlink_to(victim)
                return original_replace(source, destination)

            with patch.object(
                scan_horizon_versions.os, "replace", side_effect=insert_link_then_replace
            ):
                scan_horizon_versions.write_catalog_atomically(output, {"schema": 1})

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"schema": 1})
            self.assertFalse(output.is_symlink())
            self.assertEqual(list(root.glob(".catalog.json.*.tmp")), [])

    def test_catalog_replace_failure_preserves_previous_file_and_cleans_temp(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "catalog.json"
            output.write_text("previous\n", encoding="utf-8")

            with patch(
                "scripts.scan_horizon_versions.os.replace",
                side_effect=OSError("replace denied"),
            ):
                with self.assertRaisesRegex(OSError, "replace denied"):
                    scan_horizon_versions.write_catalog_atomically(output, {"schema": 1})

            self.assertEqual(output.read_text(encoding="utf-8"), "previous\n")
            self.assertEqual(list(root.glob(".catalog.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
