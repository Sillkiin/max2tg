"""Characterization tests for config.py: load/merge/env, dotenv parsing,
corrupt-config handling, and config.json permission restriction.

Run with PYTHONPATH=repo-root so `import config` resolves top-level.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class CoerceBoolTests(unittest.TestCase):
    def test_non_string_non_bool_value_is_truthiness(self):
        # Line 26: a value that is neither None, bool, nor str falls through
        # to bool(value).
        self.assertTrue(config._coerce_bool(1))
        self.assertTrue(config._coerce_bool([0]))
        self.assertFalse(config._coerce_bool(0))
        self.assertFalse(config._coerce_bool([]))


class ApplyDotenvEarlyReturnTests(unittest.TestCase):
    def test_missing_file_is_noop(self):
        # Lines 81-82: a non-existent path returns without touching os.environ.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.env"
            with patch.dict(os.environ, {}, clear=False):
                before = dict(os.environ)
                config.apply_dotenv(path)
                self.assertEqual(dict(os.environ), before)

    def test_unreadable_file_swallows_oserror(self):
        # Lines 83-86: read_text raising OSError is caught and returns quietly.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MAX2TG_X=y\n", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=OSError("EIO")):
                # Must not raise even though the file exists.
                config.apply_dotenv(path)

    def test_default_path_used_when_none(self):
        # Line 80: path defaults to DOTENV_PATH (which is absent under test),
        # so the call is a clean no-op.
        with patch.object(config, "DOTENV_PATH", Path(tempfile.gettempdir())
                          / "max2tg-nope.env"):
            config.apply_dotenv(None)  # must not raise


class ApplyDotenvParsingTests(unittest.TestCase):
    def _run(self, content, keys):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(content, encoding="utf-8")
            # Start from a known-clean slice of the environment for our keys.
            with patch.dict(os.environ, {}, clear=False):
                for k in keys:
                    os.environ.pop(k, None)
                config.apply_dotenv(path)
                return {k: os.environ.get(k) for k in keys}

    def test_strips_export_prefix(self):
        result = self._run("export MAX2TG_EXP=value\n", ["MAX2TG_EXP"])
        self.assertEqual(result["MAX2TG_EXP"], "value")

    def test_double_quoted_value_unwrapped(self):
        result = self._run('MAX2TG_Q="a b c"\n', ["MAX2TG_Q"])
        self.assertEqual(result["MAX2TG_Q"], "a b c")

    def test_single_quoted_value_unwrapped(self):
        result = self._run("MAX2TG_SQ='hello world'\n", ["MAX2TG_SQ"])
        self.assertEqual(result["MAX2TG_SQ"], "hello world")

    def test_unterminated_quote_drops_opening_quote(self):
        # end == -1 branch: value becomes everything after the opening quote.
        result = self._run('MAX2TG_UN="oops\n', ["MAX2TG_UN"])
        self.assertEqual(result["MAX2TG_UN"], "oops")

    def test_unquoted_inline_comment_dropped(self):
        result = self._run("MAX2TG_IC=token  # a note\n", ["MAX2TG_IC"])
        self.assertEqual(result["MAX2TG_IC"], "token")

    def test_unquoted_hash_without_leading_space_is_kept(self):
        # The " #" guard means a '#' with no preceding space stays in the value.
        result = self._run("MAX2TG_HASH=ab#cd\n", ["MAX2TG_HASH"])
        self.assertEqual(result["MAX2TG_HASH"], "ab#cd")

    def test_blank_comment_and_no_equals_lines_skipped(self):
        content = "\n# pure comment\nNO_EQUALS_HERE\nMAX2TG_OK=fine\n"
        result = self._run(content, ["MAX2TG_OK", "NO_EQUALS_HERE"])
        self.assertEqual(result["MAX2TG_OK"], "fine")
        self.assertIsNone(result["NO_EQUALS_HERE"])

    def test_setdefault_does_not_override_real_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MAX2TG_REAL=fromfile\n", encoding="utf-8")
            with patch.dict(os.environ, {"MAX2TG_REAL": "fromenv"}, clear=False):
                config.apply_dotenv(path)
                self.assertEqual(os.environ["MAX2TG_REAL"], "fromenv")


class LoadFromEnvTests(unittest.TestCase):
    REQUIRED_VARS = {
        "MAX2TG_TELEGRAM_BOT_TOKEN": "bot",
        "MAX2TG_TELEGRAM_CHAT_ID": "123",
        "MAX2TG_MAX_TOKEN": "maxtok",
    }

    def test_returns_none_when_required_missing(self):
        # Lines 139-141: not all REQUIRED_KEYS present -> None.
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.load_from_env())

    def test_returns_normalized_config_when_all_present(self):
        # Lines 139-142: builds + normalizes a full config from env vars.
        with patch.dict(os.environ, self.REQUIRED_VARS, clear=True):
            result = config.load_from_env()
        self.assertIsNotNone(result)
        self.assertEqual(result["telegram_bot_token"], "bot")
        self.assertEqual(result["telegram_chat_id"], 123)  # coerced to int
        self.assertEqual(result["max_login_token"], "maxtok")
        # normalize_config defaults applied.
        self.assertTrue(result["telegram_confirm_sent"])
        self.assertEqual(result["telegram_fallback_chat_id"], 123)

    def test_env_optional_override_carried_through(self):
        env = dict(self.REQUIRED_VARS)
        env["MAX2TG_TELEGRAM_CONFIRM_SENT"] = "false"
        env["MAX2TG_TELEGRAM_FORUM_CHAT_ID"] = "-100999"
        with patch.dict(os.environ, env, clear=True):
            result = config.load_from_env()
        self.assertFalse(result["telegram_confirm_sent"])
        self.assertEqual(result["telegram_forum_chat_id"], -100999)
        self.assertTrue(result["telegram_topics_enabled"])  # forum id present

    def test_empty_string_env_var_is_treated_as_unset(self):
        env = dict(self.REQUIRED_VARS)
        env["MAX2TG_MAX_TOKEN"] = ""  # present but empty -> ignored
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(config.load_from_env())


class LoadConfigMergeTests(unittest.TestCase):
    def _config_file(self, tmp, data):
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_returns_none_when_merged_missing_required(self):
        # Line 157: config.json missing a token and no env var -> None.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._config_file(tmp, {
                "telegram_bot_token": "tok",
                "telegram_chat_id": "123",
                # max_login_token deliberately absent
            })
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(config.load_config())

    def test_env_overrides_file_per_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._config_file(tmp, {
                "telegram_bot_token": "filetok",
                "telegram_chat_id": "111",
                "max_login_token": "filemax",
                "telegram_confirm_sent": True,
            })
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.dict(
                        os.environ,
                        {"MAX2TG_TELEGRAM_BOT_TOKEN": "envtok",
                         "MAX2TG_TELEGRAM_CONFIRM_SENT": "false"},
                        clear=True):
                result = config.load_config()
        self.assertEqual(result["telegram_bot_token"], "envtok")  # env wins
        self.assertEqual(result["telegram_chat_id"], 111)  # from file, coerced
        self.assertEqual(result["max_login_token"], "filemax")
        self.assertFalse(result["telegram_confirm_sent"])  # env override

    def test_env_only_when_config_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"  # never created
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.dict(
                        os.environ,
                        {"MAX2TG_TELEGRAM_BOT_TOKEN": "b",
                         "MAX2TG_TELEGRAM_CHAT_ID": "9",
                         "MAX2TG_MAX_TOKEN": "m"},
                        clear=True):
                result = config.load_config()
        self.assertEqual(result["telegram_bot_token"], "b")
        self.assertEqual(result["telegram_chat_id"], 9)


class LoadPartialTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        # Lines 163-164.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            with patch.object(config, "CONFIG_PATH", path):
                self.assertEqual(config.load_partial(), {})

    def test_corrupt_json_logs_warning_and_returns_empty(self):
        # Lines 167-171: JSONDecodeError caught, warning logged, {} returned.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ this is not json", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), \
                    self.assertLogs("config", level="WARNING") as cm:
                self.assertEqual(config.load_partial(), {})
        self.assertTrue(
            any("Could not read config.json" in m for m in cm.output))

    def test_valid_json_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"telegram_chat_id": "5"}', encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path):
                self.assertEqual(config.load_partial(), {"telegram_chat_id": "5"})


class RestrictPermissionsPosixTests(unittest.TestCase):
    def test_posix_chmod_called_with_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(type(path), "chmod") as chmod, \
                    patch.object(config.os, "name", "posix"):
                config._restrict_permissions()
            chmod.assert_called_once_with(0o600)

    def test_posix_chmod_oserror_logs_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(type(path), "chmod",
                                 side_effect=OSError("EPERM")), \
                    patch.object(config.os, "name", "posix"), \
                    self.assertLogs("config", level="WARNING") as cm:
                config._restrict_permissions()
        self.assertTrue(any("chmod" in m for m in cm.output))


class RestrictPermissionsWindowsTests(unittest.TestCase):
    def _run_with_env(self, env, getlogin=None):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return None

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            ctx = [
                patch.object(config, "CONFIG_PATH", path),
                patch.object(config.os, "name", "nt"),
                patch.dict(config.os.environ, env, clear=True),
                patch.object(config.subprocess, "run", side_effect=fake_run),
            ]
            if getlogin is not None:
                ctx.append(patch.object(config.os, "getlogin",
                                        side_effect=getlogin))
            for c in ctx:
                c.start()
            try:
                config._restrict_permissions()
            finally:
                for c in reversed(ctx):
                    c.stop()
        return captured

    def test_username_and_domain_form_qualified_principal(self):
        # Lines 183, 192-199: DOMAIN\USER principal passed to icacls.
        captured = self._run_with_env(
            {"USERNAME": "alice", "USERDOMAIN": "CORP"})
        args = captured["args"]
        self.assertEqual(args[0], "icacls")
        self.assertIn("/inheritance:r", args)
        self.assertIn("/grant:r", args)
        self.assertIn("CORP\\alice:(R,W)", args)

    def test_computername_used_when_no_userdomain(self):
        captured = self._run_with_env(
            {"USERNAME": "bob", "COMPUTERNAME": "PC01"})
        self.assertIn("PC01\\bob:(R,W)", captured["args"])

    def test_bare_username_when_no_domain_at_all(self):
        captured = self._run_with_env({"USERNAME": "carol"})
        self.assertIn("carol:(R,W)", captured["args"])

    def test_getlogin_fallback_when_username_env_empty(self):
        # Lines 183-188: empty USERNAME -> os.getlogin() supplies the user.
        captured = self._run_with_env(
            {"USERDOMAIN": "DOM"}, getlogin=lambda: "loginuser")
        self.assertIn("DOM\\loginuser:(R,W)", captured["args"])

    def test_getlogin_oserror_aborts_silently(self):
        # Lines 185-188: getlogin raising OSError -> return without icacls.
        def boom():
            raise OSError("no controlling terminal")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config.os, "name", "nt"), \
                    patch.dict(config.os.environ, {}, clear=True), \
                    patch.object(config.os, "getlogin", side_effect=boom), \
                    patch.object(config.subprocess, "run") as run:
                config._restrict_permissions()
            run.assert_not_called()

    def test_icacls_oserror_logs_warning(self):
        # Lines 200-201: subprocess.run raising OSError is logged, not raised.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config.os, "name", "nt"), \
                    patch.dict(config.os.environ,
                               {"USERNAME": "u", "USERDOMAIN": "D"},
                               clear=True), \
                    patch.object(config.subprocess, "run",
                                 side_effect=OSError("no icacls")), \
                    self.assertLogs("config", level="WARNING") as cm:
                config._restrict_permissions()
        self.assertTrue(
            any("restrict config.json permissions" in m for m in cm.output))


class SaveConfigTests(unittest.TestCase):
    def test_writes_json_and_restricts(self):
        # Lines 212-215: writes pretty JSON then calls _restrict_permissions.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            payload = {"telegram_chat_id": 123, "note": "Привет"}
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config, "_restrict_permissions") as restrict:
                config.save_config(payload)
            restrict.assert_called_once_with()
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written, payload)

    def test_non_ascii_preserved_unescaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config, "_restrict_permissions"):
                config.save_config({"name": "Людмила"})
            raw = path.read_text(encoding="utf-8")
        self.assertIn("Людмила", raw)  # ensure_ascii=False

    def test_posix_precreates_restricted_file(self):
        # Lines 205-211: on non-nt with no existing file, open(O_CREAT, 0o600)
        # is used to pre-create the token file before writing.
        opened = {}

        real_open = os.open

        def spy_open(p, flags, *rest):
            opened["path"] = str(p)
            opened["flags"] = flags
            opened["mode"] = rest[0] if rest else None
            return real_open(p, flags, *rest)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config.os, "name", "posix"), \
                    patch.object(config.os, "open", side_effect=spy_open), \
                    patch.object(config, "_restrict_permissions"):
                config.save_config({"k": "v"})
            self.assertEqual(opened["mode"], 0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"k": "v"})

    def test_posix_precreate_oserror_is_swallowed(self):
        # Lines 209-211: os.open raising OSError must not abort the save.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config, "CONFIG_PATH", path), \
                    patch.object(config.os, "name", "posix"), \
                    patch.object(config.os, "open",
                                 side_effect=OSError("EACCES")), \
                    patch.object(config, "_restrict_permissions"):
                config.save_config({"k": "v"})
            # Despite the failed pre-create, the write still lands.
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             {"k": "v"})


if __name__ == "__main__":
    unittest.main()
