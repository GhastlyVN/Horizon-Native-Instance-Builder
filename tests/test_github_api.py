import json
import os
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import resolve_horizon_revision as resolve
from scripts.github_api import build_headers, open_url, retry_delay, token


class FakeResponse:
    def __init__(self, payload: bytes = b"{}"):
        self.payload = payload

    def read(self, *_args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class GithubApiTests(unittest.TestCase):
    def test_token_prefers_github_token(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": " workflow ", "GH_TOKEN": "other"}, clear=True):
            self.assertEqual(token(), "workflow")
        with patch.dict(os.environ, {"GH_TOKEN": "other"}, clear=True):
            self.assertEqual(token(), "other")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(token())

    def test_authorization_only_sent_to_github_hosts(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True):
            api = build_headers("https://api.github.com/repos/x/y")
            release = build_headers("https://github.com/owner/repo/releases/download/t/a.hnmod")
            index = build_headers("https://horizon-rust.github.io/horizon-mods/index.json")
        self.assertEqual(api["Authorization"], "Bearer secret")
        self.assertEqual(release["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", index)

    def test_authorization_is_not_forwarded_on_redirect(self):
        captured = []

        def fake_urlopen(request, timeout=60):
            captured.append(request)
            return FakeResponse()

        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "scripts.github_api.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            open_url("https://api.github.com/repos/x/y", sleep=lambda _delay: None)

        request = captured[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            request, BytesIO(), 302, "Found", {}, "https://evil.example/leak"
        )
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_retries_rate_limited_responses(self):
        calls = []
        sleeps = []

        def fake_urlopen(request, timeout=60):
            calls.append(request.get_header("Authorization"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 403, "rate limit exceeded", {}, BytesIO(b"")
                )
            return FakeResponse(b'{"ok": true}')

        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "scripts.github_api.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            response = open_url("https://api.github.com/repos/x/y", sleep=sleeps.append)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "Bearer secret")
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(json.loads(response.read()), {"ok": True})

    def test_does_not_retry_permanent_errors(self):
        calls = []

        def fake_urlopen(request, timeout=60):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, BytesIO(b"")
            )

        with patch(
            "scripts.github_api.urllib.request.urlopen", side_effect=fake_urlopen
        ), self.assertRaises(urllib.error.HTTPError):
            open_url("https://api.github.com/repos/x/y", sleep=lambda _delay: None)
        self.assertEqual(len(calls), 1)

    def test_retry_delay_honours_retry_after(self):
        error = urllib.error.HTTPError(
            "https://api.github.com", 429, "slow down", {"Retry-After": "7"}, BytesIO(b"")
        )
        self.assertEqual(retry_delay(error, 1), 7.0)

    def test_retry_delay_honours_http_date_retry_after(self):
        future = urllib.error.HTTPError(
            "https://api.github.com", 429, "slow down",
            {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, BytesIO(b""),
        )
        past = urllib.error.HTTPError(
            "https://api.github.com", 429, "slow down",
            {"Retry-After": "Wed, 21 Oct 2009 07:28:00 GMT"}, BytesIO(b""),
        )
        self.assertEqual(retry_delay(future, 1), 60.0)
        self.assertEqual(retry_delay(past, 1), 0.0)

    def test_revision_outputs_refuse_links_and_directories(self):
        release = {"tag": "nightly", "name": "Nightly", "commit": "a" * 40}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            output = root / "commit"
            output.symlink_to(victim)
            report = root / "report.json"
            with patch.object(resolve, "fetch_release", return_value=release), patch(
                "sys.argv", ["resolve", "--output", str(output), "--report", str(report)]
            ):
                with self.assertRaisesRegex(ValueError, "linked or non-regular"):
                    resolve.main()
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertFalse(report.exists())

            output.unlink()
            output.write_text("old", encoding="utf-8")
            report.mkdir()
            with patch.object(resolve, "fetch_release", return_value=release), patch(
                "sys.argv", ["resolve", "--output", str(output), "--report", str(report)]
            ):
                with self.assertRaisesRegex(ValueError, "non-regular"):
                    resolve.main()
            self.assertEqual(output.read_text(encoding="utf-8"), "old")

    def test_revision_outputs_are_published_completely(self):
        release = {"tag": "nightly", "name": "Nightly", "commit": "a" * 40}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "nested" / "commit.txt"
            report = root / "nested" / "report.json"
            with patch.object(resolve, "fetch_release", return_value=release), patch(
                "sys.argv", ["resolve", "--output", str(output), "--report", str(report)]
            ):
                resolve.main()

            self.assertEqual(output.read_text(encoding="ascii"), "a" * 40 + "\n")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), release)
            self.assertEqual(list(output.parent.glob(".commit.txt.*.tmp")), [])
            self.assertEqual(list(output.parent.glob(".report.json.*.tmp")), [])

    def test_symlink_inserted_at_publish_is_replaced_not_written_through(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "commit.txt"
            victim = root / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            original_replace = os.replace

            def insert_link_then_replace(source, destination):
                Path(destination).symlink_to(victim)
                return original_replace(source, destination)

            with patch.object(resolve.os, "replace", side_effect=insert_link_then_replace):
                resolve.write_text_atomically(output, "safe\n", encoding="ascii")

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertEqual(output.read_text(encoding="ascii"), "safe\n")
            self.assertFalse(output.is_symlink())
            self.assertEqual(list(root.glob(".commit.txt.*.tmp")), [])

    def test_replace_failure_preserves_existing_output_and_cleans_temp(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "commit.txt"
            output.write_text("previous", encoding="ascii")

            with patch.object(resolve.os, "replace", side_effect=OSError("replace denied")):
                with self.assertRaisesRegex(OSError, "replace denied"):
                    resolve.write_text_atomically(output, "new\n", encoding="ascii")

            self.assertEqual(output.read_text(encoding="ascii"), "previous")
            self.assertEqual(list(root.glob(".commit.txt.*.tmp")), [])

    def test_commit_and_report_paths_must_not_alias(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "same.txt"
            with patch.object(resolve, "fetch_release") as fetch:
                with patch(
                    "sys.argv", ["resolve", "--output", str(output), "--report", str(output)]
                ):
                    with self.assertRaisesRegex(ValueError, "different files"):
                        resolve.main()
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
