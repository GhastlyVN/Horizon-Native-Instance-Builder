import unittest

from scripts import download_minecraft


class MojangPathPortabilityTests(unittest.TestCase):
    def test_rejects_trailing_dot_and_space_components(self):
        for path in ("a./file.jar", "a /file.jar", "dir./file.jar"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Non-portable"):
                download_minecraft._safe_relative_path(path)

    def test_rejects_more_reserved_names(self):
        for path in ("nul/file.jar", "aux/file.jar", "com1/file.jar"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Non-portable"):
                download_minecraft._safe_relative_path(path)

    def test_rejects_control_characters(self):
        with self.assertRaisesRegex(ValueError, "Non-portable"):
            download_minecraft._safe_relative_path("a\x01b.jar")

    def test_accepts_a_plain_relative_path(self):
        self.assertEqual(download_minecraft._safe_relative_path("a/b.jar").as_posix(), "a/b.jar")


if __name__ == "__main__":
    unittest.main()
