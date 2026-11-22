import unittest

from scripts import download_minecraft


class MojangUrlBoundaryTests(unittest.TestCase):
    def test_rejects_url_credentials(self):
        for url in (
            "https://user@launcher.mojang.com/client.jar",
            "https://user:pass@launcher.mojang.com/client.jar",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "credentials"):
                download_minecraft._validate_url(url)

    def test_rejects_non_default_port(self):
        for url in (
            "https://launcher.mojang.com:8443/client.jar",
            "https://piston-data.mojang.com:8080/client.jar",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "non-standard port"):
                download_minecraft._validate_url(url)

    def test_accepts_plain_official_https(self):
        url = "https://launcher.mojang.com/client.jar"
        self.assertEqual(download_minecraft._validate_url(url), url)


if __name__ == "__main__":
    unittest.main()
