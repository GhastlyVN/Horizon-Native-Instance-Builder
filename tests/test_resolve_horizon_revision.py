import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.resolve_horizon_revision import (
    UPSTREAM_REPOSITORY,
    fetch_release,
    main,
    release_api,
)


def github_release(tag="nightly", commit="a" * 40):
    return {
        "tag_name": tag,
        "name": f"Nightly {tag}",
        "prerelease": True,
        "published_at": "2026-11-13T07:47:16Z",
        "target_commitish": commit,
        "html_url": f"https://github.com/horizon-rust/horizon/releases/tag/{tag}",
    }


class ResolveHorizonRevisionTests(unittest.TestCase):
    def test_defaults_to_upstream(self):
        self.assertEqual(
            release_api(UPSTREAM_REPOSITORY),
            "https://api.github.com/repos/horizon-rust/horizon/releases/tags/",
        )

    def test_custom_repository_is_used_and_recorded(self):
        response = io.BytesIO(json.dumps(github_release()).encode())
        with mock.patch("scripts.resolve_horizon_revision.open_url", return_value=response) as opened:
            release = fetch_release("nightly", "GhastlyVN/horizon")
        opened.assert_called_once_with(
            "https://api.github.com/repos/GhastlyVN/horizon/releases/tags/nightly"
        )
        self.assertEqual(release["repository"], "GhastlyVN/horizon")

    def test_elyby_nightly_tag_is_supported(self):
        response = io.BytesIO(json.dumps(github_release("elyby-nightly")).encode())
        with mock.patch("scripts.resolve_horizon_revision.open_url", return_value=response):
            release = fetch_release("elyby-nightly", "GhastlyVN/horizon")
        self.assertEqual(release["tag"], "elyby-nightly")

    def test_invalid_repository_is_rejected_before_network(self):
        with mock.patch("scripts.resolve_horizon_revision.open_url") as opened:
            with self.assertRaisesRegex(ValueError, "OWNER/REPO"):
                fetch_release("nightly", "horizon")
        opened.assert_not_called()

    def test_main_writes_repository_to_report(self):
        response = io.BytesIO(json.dumps(github_release()).encode())
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "commit.txt"
            report = Path(temp) / "nightly.json"
            argv = [
                "resolve", "--repository", "GhastlyVN/horizon", "--ref", "nightly",
                "--output", str(output), "--report", str(report),
            ]
            with mock.patch("scripts.resolve_horizon_revision.open_url", return_value=response):
                with mock.patch("sys.argv", argv):
                    main()
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["repository"], "GhastlyVN/horizon")
            self.assertEqual(output.read_text(encoding="ascii"), "a" * 40 + "\n")

    def test_workflow_exposes_the_optional_source_repository_input(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/build.yml"
        ).read_text(encoding="utf-8")
        block = workflow.split("horizon_source_repository:", 1)[1].split("include_native_mods:", 1)[0]
        self.assertIn("blank = upstream horizon-rust/horizon", block)
        self.assertIn('default: ""', block)
        self.assertIn('required: false', block)
        self.assertIn('--repository "$HORIZON_SOURCE_REPOSITORY"', workflow)


if __name__ == "__main__":
    unittest.main()
