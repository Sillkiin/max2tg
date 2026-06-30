"""Tests for the writable-runtime-files path resolver (paths.py)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paths


class DataDirTests(unittest.TestCase):
    def test_defaults_to_app_dir_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX2TG_DATA_DIR", None)
            self.assertEqual(paths.data_dir(), paths._APP_DIR)

    def test_uses_and_creates_env_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "max2tg-data"
            with patch.dict(os.environ, {"MAX2TG_DATA_DIR": str(target)}):
                self.assertEqual(paths.data_dir(), target)
                self.assertTrue(target.is_dir())  # created on demand

    def test_falls_back_to_app_dir_when_env_dir_unusable(self):
        # mkdir under an existing *file* raises OSError -> fall back to app dir.
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "afile"
            blocker.write_text("x", encoding="utf-8")
            with patch.dict(os.environ, {"MAX2TG_DATA_DIR": str(blocker / "sub")}):
                self.assertEqual(paths.data_dir(), paths._APP_DIR)

    def test_data_path_joins_filename_under_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"MAX2TG_DATA_DIR": d}):
                self.assertEqual(paths.data_path("state.json"), Path(d) / "state.json")


if __name__ == "__main__":
    unittest.main()
