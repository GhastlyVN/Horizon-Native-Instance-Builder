import json
import os
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import patch

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
        seen = []

        def fake_urlopen(request, timeout=60):
            seen.append(request)
            return FakeResponse()

        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=True), patch(
            "scripts.github_api.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            open_url("https://api.github.com/repos/x/y", sleep=lambda _delay: None)
        request = seen[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertIsNone(request.headers.get("Authorization"))
        self.assertEqual(request.unredirected_hdrs.get("Authorization"), "Bearer secret")

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


if __name__ == "__main__":
    unittest.main()
