"""Unit tests for the interactive first-run wizard in setup_wizard.py.

The wizard is glue around four side-effecting dependencies: Telegram HTTP
wrappers (tg.*), the MAX browser client (BrowserMaxClient), config persistence
(load_partial/save_config), and builtins.input/print. Every test fakes all of
those so nothing touches a real network, WebSocket, TTY, or config.json.

setup_wizard imports load_partial/save_config/BrowserMaxClient/MaxAuthError by
name, so those are patched on the setup_wizard module; tg.* stay on tg because
the module calls them through the `tg` namespace.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import setup_wizard
from max_client import MaxAuthError


class AskTests(unittest.TestCase):
    def test_ask_strips_and_returns_first_nonempty(self):
        with patch("builtins.input", return_value="  hello  "):
            self.assertEqual(setup_wizard._ask("p: "), "hello")

    def test_ask_loops_until_nonempty(self):
        # blank, all-whitespace, then a real answer -> only the last is kept
        with patch("builtins.input", side_effect=["", "   ", "tok"]) as inp:
            self.assertEqual(setup_wizard._ask("p: "), "tok")
        self.assertEqual(inp.call_count, 3)


class SetupTelegramTokenTests(unittest.TestCase):
    def test_returns_token_on_first_valid_check(self):
        with patch("builtins.print"), \
                patch("builtins.input", return_value="GOODTOKEN"), \
                patch("tg.check_token", return_value={"username": "mybot"}) as chk:
            out = setup_wizard._setup_telegram_token()
        self.assertEqual(out, "GOODTOKEN")
        chk.assert_called_once_with("GOODTOKEN")

    def test_retries_until_token_valid(self):
        # first token raises, second is accepted
        with patch("builtins.print"), \
                patch("builtins.input", side_effect=["BAD", "GOOD"]), \
                patch("tg.check_token",
                      side_effect=[RuntimeError("401"), {"username": "b"}]) as chk:
            out = setup_wizard._setup_telegram_token()
        self.assertEqual(out, "GOOD")
        self.assertEqual(chk.call_count, 2)


class SetupTelegramChatIdTests(unittest.TestCase):
    def test_returns_chat_id_from_first_message_update(self):
        update = {"update_id": 7, "message": {"chat": {"id": 555,
                                                       "first_name": "Tim"}}}
        with patch("builtins.print"), \
                patch("setup_wizard.time.monotonic", side_effect=[0.0, 1.0]), \
                patch("tg.get_updates", return_value=[update]) as gu:
            out = setup_wizard._setup_telegram_chat_id("TKN")
        self.assertEqual(out, 555)
        # first poll uses offset=None
        self.assertIsNone(gu.call_args.args[1])

    def test_advances_offset_and_skips_non_message_updates(self):
        no_msg = {"update_id": 10}
        with_msg = {"update_id": 11, "message": {"chat": {"id": 99}}}
        # monotonic: deadline calc, then two loop checks both < deadline
        with patch("builtins.print"), \
                patch("setup_wizard.time.monotonic", side_effect=[0.0, 1.0, 2.0]), \
                patch("tg.get_updates",
                      side_effect=[[no_msg], [with_msg]]) as gu:
            out = setup_wizard._setup_telegram_chat_id("TKN")
        self.assertEqual(out, 99)
        # second poll must carry offset = previous update_id + 1
        self.assertEqual(gu.call_args_list[1].args[1], 11)

    def test_polling_error_sleeps_and_retries(self):
        good = {"update_id": 3, "message": {"chat": {"id": 1, "username": "u"}}}
        with patch("builtins.print"), \
                patch("setup_wizard.time.monotonic", side_effect=[0.0, 1.0, 2.0]), \
                patch("setup_wizard.time.sleep") as slp, \
                patch("tg.get_updates",
                      side_effect=[ConnectionError("boom"), [good]]):
            out = setup_wizard._setup_telegram_chat_id("TKN")
        self.assertEqual(out, 1)
        slp.assert_called_once_with(3)

    def test_raises_system_exit_on_timeout(self):
        # deadline = 0 + 120; first loop check (1000) is past it -> immediate exit
        with patch("builtins.print"), \
                patch("setup_wizard.time.monotonic", side_effect=[0.0, 1000.0]), \
                patch("tg.get_updates") as gu:
            with self.assertRaises(SystemExit):
                setup_wizard._setup_telegram_chat_id("TKN")
        gu.assert_not_called()


def _client_returning(payload):
    """A fake BrowserMaxClient whose login_by_token yields `payload`."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.login_by_token = AsyncMock(return_value={"payload": payload})
    return client


class ValidateTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_token_and_disconnects(self):
        payload = {"profile": {"contact": {"names": [{"name": "Timur"}]}}}
        client = _client_returning(payload)
        with patch("builtins.print"), \
                patch("setup_wizard.BrowserMaxClient", return_value=client):
            out = await setup_wizard._validate_token("MAXTOK")
        self.assertEqual(out, "MAXTOK")
        client.connect.assert_awaited_once()
        client.login_by_token.assert_awaited_once_with("MAXTOK")
        client.disconnect.assert_awaited_once()

    async def test_error_payload_raises_and_still_disconnects(self):
        client = _client_returning({"error": "bad token"})
        with patch("builtins.print"), \
                patch("setup_wizard.BrowserMaxClient", return_value=client):
            with self.assertRaises(MaxAuthError):
                await setup_wizard._validate_token("MAXTOK")
        # finally-block disconnect must run even on failure
        client.disconnect.assert_awaited_once()

    async def test_falls_back_to_phone_when_no_contact_names(self):
        payload = {"profile": {"phone": "+70000000000"}}
        client = _client_returning(payload)
        with patch("builtins.print"), \
                patch("setup_wizard.BrowserMaxClient", return_value=client):
            out = await setup_wizard._validate_token("MAXTOK")
        self.assertEqual(out, "MAXTOK")


class SetupMaxLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_first_valid_token(self):
        with patch("builtins.print"), \
                patch("builtins.input", return_value="T1"), \
                patch("setup_wizard._validate_token",
                      new=AsyncMock(return_value="T1")) as vt:
            out = await setup_wizard._setup_max_login()
        self.assertEqual(out, "T1")
        vt.assert_awaited_once_with("T1")

    async def test_retries_after_validation_failure(self):
        with patch("builtins.print"), \
                patch("builtins.input", side_effect=["BAD", "GOOD"]), \
                patch("setup_wizard._validate_token",
                      new=AsyncMock(side_effect=[MaxAuthError("nope"), "GOOD"])) as vt:
            out = await setup_wizard._setup_max_login()
        self.assertEqual(out, "GOOD")
        self.assertEqual(vt.await_count, 2)


def _fake_asyncio_run(result):
    """Stand-in for asyncio.run that closes the coroutine (avoids a warning)
    and returns a canned MAX token, without ever entering an event loop."""
    def _run(coro):
        coro.close()
        return result
    return _run


class RunSetupTests(unittest.TestCase):
    def test_happy_path_full_run(self):
        # No saved Telegram creds -> steps 1+2 run, partial save, then MAX step.
        with patch("builtins.print"), \
                patch("setup_wizard.load_partial", return_value={}), \
                patch("setup_wizard._setup_telegram_token",
                      return_value="BOTTOK") as st, \
                patch("setup_wizard._setup_telegram_chat_id",
                      return_value=42) as sc, \
                patch("setup_wizard.asyncio.run",
                      side_effect=_fake_asyncio_run("MAXTOK")) as ar, \
                patch("setup_wizard.save_config") as save, \
                patch("tg.send_message") as send:
            config = setup_wizard.run_setup()

        self.assertEqual(config, {
            "telegram_bot_token": "BOTTOK",
            "telegram_chat_id": 42,
            "max_login_token": "MAXTOK",
        })
        st.assert_called_once()
        sc.assert_called_once_with("BOTTOK")
        ar.assert_called_once()
        # Telegram creds are saved BEFORE the final full save (resume safety).
        self.assertEqual(save.call_count, 2)
        self.assertEqual(save.call_args_list[0].args[0],
                         {"telegram_bot_token": "BOTTOK", "telegram_chat_id": 42})
        self.assertEqual(save.call_args_list[1].args[0], config)
        # Welcome message goes to the resolved bot token + chat id.
        self.assertEqual(send.call_args.args[0], "BOTTOK")
        self.assertEqual(send.call_args.args[1], 42)

    def test_resume_skips_telegram_steps_when_creds_present(self):
        existing = {"telegram_bot_token": "SAVEDTOK", "telegram_chat_id": 7}
        with patch("builtins.print"), \
                patch("setup_wizard.load_partial", return_value=existing), \
                patch("setup_wizard._setup_telegram_token") as st, \
                patch("setup_wizard._setup_telegram_chat_id") as sc, \
                patch("setup_wizard.asyncio.run",
                      side_effect=_fake_asyncio_run("MAXTOK")) as ar, \
                patch("setup_wizard.save_config") as save, \
                patch("tg.send_message"):
            config = setup_wizard.run_setup()

        # Steps 1 and 2 are skipped entirely on resume.
        st.assert_not_called()
        sc.assert_not_called()
        ar.assert_called_once()
        self.assertEqual(config["telegram_bot_token"], "SAVEDTOK")
        self.assertEqual(config["telegram_chat_id"], 7)
        self.assertEqual(config["max_login_token"], "MAXTOK")
        # Only the final full config save happens (no partial pre-save).
        save.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
