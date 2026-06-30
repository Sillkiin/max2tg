"""Tests for the single-instance lock (singleton.py) and main()'s use of it.

The lock is what stops two bridges from double-forwarding every message, so the
core guarantee — a second acquire is refused while the first is held — is the
important behaviour to lock down.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import singleton


class SingletonLockTests(unittest.TestCase):
    def tearDown(self):
        singleton.release_single_instance()

    def test_second_acquire_is_blocked_until_release(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "max2tg.lock"
            self.assertTrue(singleton.acquire_single_instance(lock))    # 1st wins
            self.assertFalse(singleton.acquire_single_instance(lock))   # 2nd blocked
            singleton.release_single_instance()
            self.assertTrue(singleton.acquire_single_instance(lock))    # freed -> ok
            singleton.release_single_instance()  # free before tempdir cleanup (Windows)

    def test_fail_open_when_lock_file_cannot_open(self):
        # If the lock file itself can't be opened we can't tell -> run anyway.
        with patch("singleton.open", create=True, side_effect=OSError("denied")):
            self.assertTrue(singleton.acquire_single_instance(Path("nope.lock")))


class MainSingleInstanceTests(unittest.TestCase):
    def test_main_exits_when_another_instance_holds_lock(self):
        with patch("main._configure"), \
                patch("main.acquire_single_instance", return_value=False), \
                patch("main.apply_dotenv") as dotenv, \
                patch("main.MaxToTelegramBridge") as bridge, \
                patch("builtins.print"):
            main.main()
        bridge.assert_not_called()   # never built the bridge
        dotenv.assert_not_called()   # returned before doing any work


if __name__ == "__main__":
    unittest.main()
