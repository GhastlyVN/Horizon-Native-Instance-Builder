import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare import validate_zip


@unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO test")
class PrepareZipInputTests(unittest.TestCase):
    def test_a_replaced_fifo_is_refused_without_hanging(self):
        with tempfile.TemporaryDirectory() as dirname:
            fifo = pathlib.Path(dirname) / "input.zip"
            os.mkfifo(fifo)
            with patch.object(pathlib.Path, "is_file", return_value=True):
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    validate_zip(fifo)


if __name__ == "__main__":
    unittest.main()
