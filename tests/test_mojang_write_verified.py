import pathlib
import tempfile
import unittest
from unittest.mock import patch

from scripts import download_minecraft


class FakeResponse:
    headers = {}

    def __init__(self, final_url, body=b""):
        self.final_url = final_url
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, _size):
        return self.body


class MojangWriteVerifiedTests(unittest.TestCase):
    def test_rejects_official_request_that_redirects_to_another_host(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            download_minecraft.urllib.request,
            "urlopen",
            return_value=FakeResponse("https://evil.example/client.jar"),
        ), patch.object(download_minecraft.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Could not download"):
                download_minecraft._write_verified(
                    pathlib.Path(folder),
                    "client.jar",
                    "https://launcher.mojang.com/client.jar",
                )
            self.assertEqual(sorted(p.name for p in pathlib.Path(folder).iterdir()), [])

    def test_rejects_non_official_request_url(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "non-official"):
                download_minecraft._write_verified(
                    pathlib.Path(folder), "client.jar", "https://evil.example/client.jar"
                )

    def test_wrong_hash_does_not_publish_a_partial_file(self):
        body = b"official-client-jar"
        with tempfile.TemporaryDirectory() as folder, patch.object(
            download_minecraft.urllib.request,
            "urlopen",
            return_value=FakeResponse("https://launcher.mojang.com/client.jar", body),
        ), patch.object(download_minecraft.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Could not download"):
                download_minecraft._write_verified(
                    pathlib.Path(folder),
                    "client.jar",
                    "https://launcher.mojang.com/client.jar",
                    sha1="0" * 40,
                )
            self.assertEqual(sorted(p.name for p in pathlib.Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
