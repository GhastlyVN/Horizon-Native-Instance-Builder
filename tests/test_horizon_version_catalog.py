import unittest

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

    def test_refuses_release_without_full_source_commit(self):
        with self.assertRaisesRegex(ValueError, "immutable source commit"):
            build_catalog([release("nightly", "not-a-sha")], 20)

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


if __name__ == "__main__":
    unittest.main()
