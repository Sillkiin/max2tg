"""Characterization tests for the small bridge modules: fileperms, state,
maxactions, main, and maxmsg. No network, no live bridge — every external
dependency (subprocess, the MAX client, config/bridge in main) is mocked.
"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import fileperms
import main
import maxactions
import maxmsg
import state


class FilePermsTests(unittest.TestCase):
    def test_windows_builds_icacls_with_domain_qualified_principal(self):
        env = {"USERNAME": "alice", "USERDOMAIN": "CORP"}
        with patch.object(fileperms.os, "name", "nt"), \
                patch.dict(fileperms.os.environ, env, clear=True), \
                patch("fileperms.subprocess.run") as run:
            fileperms.restrict_to_owner("C:/secret.json")
        run.assert_called_once()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "icacls")
        self.assertEqual(cmd[1], "C:/secret.json")
        self.assertIn("/inheritance:r", cmd)
        # Principal is DOMAIN\USER, not a bare username.
        self.assertIn("CORP\\alice:(R,W,D)", cmd)
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_windows_falls_back_to_computername_when_no_userdomain(self):
        env = {"USERNAME": "bob", "COMPUTERNAME": "BOXPC"}
        with patch.object(fileperms.os, "name", "nt"), \
                patch.dict(fileperms.os.environ, env, clear=True), \
                patch("fileperms.subprocess.run") as run:
            fileperms.restrict_to_owner("x.json")
        self.assertIn("BOXPC\\bob:(R,W,D)", run.call_args.args[0])

    def test_windows_no_username_returns_without_running_icacls(self):
        with patch.object(fileperms.os, "name", "nt"), \
                patch.dict(fileperms.os.environ, {}, clear=True), \
                patch("fileperms.subprocess.run") as run:
            fileperms.restrict_to_owner("x.json")
        run.assert_not_called()

    def test_posix_uses_chmod_600(self):
        with patch.object(fileperms.os, "name", "posix"), \
                patch("fileperms.os.chmod") as chmod:
            fileperms.restrict_to_owner("/etc/secret")
        chmod.assert_called_once_with("/etc/secret", 0o600)

    def test_oserror_is_swallowed_not_raised(self):
        with patch.object(fileperms.os, "name", "posix"), \
                patch("fileperms.os.chmod", side_effect=OSError("denied")):
            # Must not raise — security hardening must never crash the bridge.
            fileperms.restrict_to_owner("/etc/secret")


class StateRoundTripTests(unittest.TestCase):
    def _state(self, name="state.json"):
        path = Path(self._dir.name) / name
        return state.BridgeState(path=path)

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def test_save_topic_then_reload_round_trips(self):
        st = self._state()
        st.save_topic(123, thread_id=7, title="Chat", chat_type="dialog",
                      sender="ivan")
        # A fresh instance reads what was persisted to disk.
        reloaded = state.BridgeState(path=st.path)
        topic = reloaded.get_topic(123)
        self.assertIsNotNone(topic)
        self.assertEqual(topic["telegram_thread_id"], 7)
        self.assertEqual(topic["title"], "Chat")
        self.assertEqual(topic["last_sender"], "ivan")
        self.assertIn("created_at", topic)

    def test_tg_sent_persists_across_instances(self):
        st = self._state()
        st.set_tg_sent({700: {"chat_id": 555, "message_id": "m1"}})
        reloaded = state.BridgeState(path=st.path)  # simulate a restart
        self.assertEqual(reloaded.get_tg_sent(),
                         {"700": {"chat_id": 555, "message_id": "m1"}})

    def test_get_topic_coerces_id_to_str_and_returns_none_when_absent(self):
        st = self._state()
        st.save_topic("123", thread_id=1, title="t", chat_type="dialog")
        # int and str ids address the same entry.
        self.assertIsNotNone(st.get_topic(123))
        self.assertIsNone(st.get_topic(999))

    def test_find_by_thread_matches_and_returns_none_otherwise(self):
        st = self._state()
        st.save_topic(5, thread_id=42, title="t", chat_type="dialog")
        self.assertEqual(st.find_by_thread(42)["max_chat_id"], 5)
        self.assertIsNone(st.find_by_thread(404))  # no match -> None

    def test_load_warns_and_keeps_default_on_corrupt_json(self):
        path = Path(self._dir.name) / "state.json"
        path.write_text("{ this is not json", encoding="utf-8")
        with self.assertLogs(state._logger, level="WARNING"):
            st = state.BridgeState(path=path)
        # Falls back to the empty default rather than crashing.
        self.assertEqual(st.get_topic(1), None)
        self.assertIsNone(st.find_by_thread(1))

    def test_mark_seeded_message_no_op_when_topic_absent(self):
        st = self._state()
        # No topic for chat 7 -> early return, nothing persisted, no crash.
        st.mark_seeded_message(7, max_message_id="m1", telegram_message_id=99)
        self.assertIsNone(st.get_topic(7))

    def test_mark_seeded_message_records_ids_when_topic_exists(self):
        st = self._state()
        st.save_topic(7, thread_id=1, title="t", chat_type="dialog")
        st.mark_seeded_message(7, max_message_id=555, telegram_message_id=88)
        topic = st.get_topic(7)
        self.assertEqual(topic["last_seeded_max_message_id"], "555")
        self.assertEqual(topic["last_seeded_telegram_message_id"], 88)

    def test_delete_topic_returns_true_then_false(self):
        st = self._state()
        st.save_topic(9, thread_id=1, title="t", chat_type="dialog")
        self.assertTrue(st.delete_topic(9))
        self.assertFalse(st.delete_topic(9))


class StateSaveFallbackTests(unittest.TestCase):
    """The save() error-handling branches (mkdir failure, atomic-rename
    fallback, in-place write failure) are mocked because they need an OS
    condition we can't reproduce on a normal temp dir."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "state.json"
        self.st = state.BridgeState(path=self.path)

    def test_save_swallows_mkdir_oserror(self):
        with patch.object(Path, "mkdir", side_effect=OSError("ro fs")):
            # Even if mkdir fails, save() proceeds to write the file.
            self.st.save()
        self.assertTrue(self.path.exists())

    def test_save_falls_back_to_in_place_write_when_replace_fails(self):
        with patch.object(Path, "replace", side_effect=OSError("EXDEV")), \
                self.assertLogs(state._logger, level="WARNING"):
            self.st._data = {"topics": {"1": {"telegram_thread_id": 3}}}
            self.st.save()
        # The in-place path wrote the real file despite the rename failing.
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["topics"]["1"]["telegram_thread_id"], 3)

    def test_save_logs_error_when_in_place_write_also_fails(self):
        with patch.object(Path, "replace", side_effect=OSError("EXDEV")), \
                patch.object(Path, "write_text", side_effect=OSError("nope")), \
                self.assertLogs(state._logger, level="ERROR"):
            # Both the atomic rename and the in-place write fail: logged, not raised.
            self.st.save()


class MaxActionsHelperTests(unittest.TestCase):
    def test_normalize_phone_maps_russian_8_prefix_to_plus7(self):
        self.assertEqual(maxactions._normalize_phone("89991234567"), "+79991234567")

    def test_normalize_phone_keeps_plus_e164(self):
        self.assertEqual(maxactions._normalize_phone("+1 (650) 555-1234"),
                         "+16505551234")

    def test_normalize_phone_rejects_too_short_and_too_long(self):
        self.assertIsNone(maxactions._normalize_phone("12345"))      # < 7 digits
        self.assertIsNone(maxactions._normalize_phone("1" * 16))     # > 15 digits

    def test_looks_like_phone_true_for_formatted_and_long_numbers(self):
        self.assertTrue(maxactions._looks_like_phone("+79991234567"))
        self.assertTrue(maxactions._looks_like_phone("8 (999) 123-45-67"))

    def test_looks_like_phone_false_for_bare_numeric_id(self):
        self.assertFalse(maxactions._looks_like_phone("21243808"))

    def test_norm_link_group_invite_hash(self):
        self.assertEqual(maxactions._norm_link("https://max.ru/join/AbCdEf"),
                         "join/AbCdEf")

    def test_norm_link_query_join_is_not_misread_as_invite(self):
        # 'join/' only counts as a path segment, not inside a query string.
        self.assertEqual(maxactions._norm_link("max.ru/news?ref=join/x"),
                         "https://max.ru/news")

    def test_norm_link_bare_username(self):
        self.assertEqual(maxactions._norm_link("@some_user"),
                         "https://max.ru/some_user")

    def test_norm_link_returns_none_for_garbage(self):
        self.assertIsNone(maxactions._norm_link("!! not a link !!"))


class MaxActionsJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_rejects_input_that_is_not_a_link(self):
        client = Mock()
        client.invoke_method = AsyncMock()
        result = await maxactions.join(client, "garbage that is not a link")
        self.assertIsInstance(result, maxactions.CommandResult)
        self.assertIn("Не похоже на ссылку", result.text)
        client.invoke_method.assert_not_awaited()

    async def test_join_success_sends_opcode_57_then_subscribes_75(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"chat": {"id": 555, "title": "News"}}},  # opcode 57 join
            {"payload": {"ok": True}},                            # opcode 75 subscribe
        ])
        result = await maxactions.join(client, "https://max.ru/join/AbCdEf")
        self.assertEqual(client.invoke_method.await_count, 2)
        join_call = client.invoke_method.await_args_list[0]
        self.assertEqual(join_call.kwargs["opcode"], 57)
        self.assertEqual(join_call.kwargs["payload"], {"link": "join/AbCdEf"})
        sub_call = client.invoke_method.await_args_list[1]
        self.assertEqual(sub_call.kwargs["opcode"], 75)
        self.assertEqual(sub_call.kwargs["payload"],
                         {"chatId": 555, "subscribe": True})
        self.assertIn("News", result.text)

    async def test_join_reports_max_error_payload(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"error": "already_member"}})
        result = await maxactions.join(client, "@channel_name")
        self.assertIn("MAX не дал вступить", result.text)
        self.assertIn("already_member", result.text)
        # Only the join call happened; no subscribe.
        self.assertEqual(client.invoke_method.await_count, 1)

    async def test_join_swallows_subscribe_failure(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"chat": {"id": 1, "title": "C"}}},  # join ok
            Exception("subscribe boom"),                     # subscribe fails
        ])
        # Subscribe failure is logged but the join is still reported as success.
        result = await maxactions.join(client, "@chan")
        self.assertIn("вступили", result.text)

    async def test_join_handles_invoke_exception(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=RuntimeError("socket dead"))
        result = await maxactions.join(client, "@chan")
        self.assertIn("Не удалось вступить", result.text)


class MaxActionsStartDmTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_body_rejected_without_lookup(self):
        client = Mock()
        client.invoke_method = AsyncMock()
        result = await maxactions.start_dm(client, "+79991234567", "   ")
        self.assertIn("Пустое сообщение", result.text)
        client.invoke_method.assert_not_awaited()

    async def test_too_long_body_rejected(self):
        client = Mock()
        client.invoke_method = AsyncMock()
        result = await maxactions.start_dm(client, "21243808", "x" * 4001)
        self.assertIn("Слишком длинное", result.text)
        client.invoke_method.assert_not_awaited()

    async def test_unresolvable_recipient_reports_help(self):
        client = Mock()
        client.invoke_method = AsyncMock()
        result = await maxactions.start_dm(client, "not-a-phone-or-id", "hi")
        self.assertIn("Кому писать", result.text)

    async def test_numeric_id_sends_opcode_64_with_top_level_user_id(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {"ok": True}})
        with patch("maxactions.randint", return_value=1800000000000):
            result = await maxactions.start_dm(client, "21243808", "привет")
        call = client.invoke_method.await_args
        self.assertEqual(call.kwargs["opcode"], 64)
        payload = call.kwargs["payload"]
        self.assertEqual(payload["userId"], 21243808)  # top-level userId, not chatId
        self.assertEqual(payload["message"]["text"], "привет")
        self.assertEqual(payload["message"]["cid"], 1800000000000)
        self.assertTrue(payload["notify"])
        self.assertIn("Отправлено", result.text)

    async def test_phone_recipient_is_resolved_via_opcode_46(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"contact": {"id": 4242}}},  # opcode 46 phone lookup
            {"payload": {"ok": True}},               # opcode 64 send
        ])
        await maxactions.start_dm(client, "+79991234567", "hi")
        lookup = client.invoke_method.await_args_list[0]
        self.assertEqual(lookup.kwargs["opcode"], 46)
        self.assertEqual(lookup.kwargs["payload"], {"phone": "+79991234567"})
        send = client.invoke_method.await_args_list[1]
        self.assertEqual(send.kwargs["payload"]["userId"], 4242)

    async def test_phone_lookup_failure_reports_help(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=RuntimeError("lookup boom"))
        result = await maxactions.start_dm(client, "+79991234567", "hi")
        self.assertIn("Кому писать", result.text)

    async def test_start_dm_reports_max_error_payload(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"error": "blocked"}})
        result = await maxactions.start_dm(client, "21243808", "hi")
        self.assertIn("MAX не принял", result.text)
        self.assertIn("blocked", result.text)

    async def test_start_dm_handles_send_exception(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=RuntimeError("send boom"))
        result = await maxactions.start_dm(client, "21243808", "hi")
        self.assertIn("Не удалось отправить", result.text)


class RedactSecretsFilterTests(unittest.TestCase):
    def _record(self, msg, *args):
        return logging.LogRecord("n", logging.INFO, __file__, 1, msg, args, None)

    def test_bot_token_is_redacted(self):
        flt = main._RedactSecretsFilter()
        token = "bot1234567890:AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        record = self._record("calling %s now", token)
        self.assertTrue(flt.filter(record))
        self.assertNotIn(token, record.getMessage())
        self.assertIn("bot<redacted>", record.getMessage())

    def test_url_secret_query_param_is_redacted(self):
        flt = main._RedactSecretsFilter()
        record = self._record("GET https://cdn.max/file?token=SUPERSECRETVALUE&x=1")
        self.assertTrue(flt.filter(record))
        out = record.getMessage()
        self.assertNotIn("SUPERSECRETVALUE", out)
        self.assertIn("<redacted>", out)

    def test_max_token_in_json_is_redacted(self):
        flt = main._RedactSecretsFilter()
        record = self._record('packet {"token": "opaqueLoginTokenXYZ"}')
        self.assertTrue(flt.filter(record))
        self.assertNotIn("opaqueLoginTokenXYZ", record.getMessage())

    def test_clean_message_passes_through_unchanged(self):
        flt = main._RedactSecretsFilter()
        record = self._record("nothing secret here")
        self.assertTrue(flt.filter(record))
        self.assertEqual(record.getMessage(), "nothing secret here")

    def test_filter_returns_true_even_if_get_message_raises(self):
        flt = main._RedactSecretsFilter()
        broken = Mock(spec=logging.LogRecord)
        broken.getMessage = Mock(side_effect=ValueError("bad args"))
        self.assertTrue(flt.filter(broken))


class MainEntryPointTests(unittest.TestCase):
    def test_no_config_without_tty_exits_loudly(self):
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = False
        with patch("main._configure"), \
                patch("main.acquire_single_instance", return_value=True), \
                patch("main.apply_dotenv"), \
                patch("main.load_config", return_value=None), \
                patch.object(main.sys, "stdin", fake_stdin), \
                patch("main.run_setup") as wizard, \
                patch("main.MaxToTelegramBridge") as bridge:
            with self.assertRaises(SystemExit) as ctx:
                main.main()
        self.assertEqual(ctx.exception.code, 1)
        wizard.assert_not_called()   # never prompts on a headless server
        bridge.assert_not_called()   # never builds/runs the bridge

    def test_no_config_with_tty_runs_setup_wizard_then_bridge(self):
        fake_stdin = Mock()
        fake_stdin.isatty.return_value = True
        wizard_cfg = {"telegram": {}}
        bridge_inst = Mock()
        # run_forever is a plain Mock (not AsyncMock): asyncio.run is patched, so
        # its return value is never awaited and no orphan coroutine is created.
        bridge_inst.run_forever = Mock()
        with patch("main._configure"), \
                patch("main.acquire_single_instance", return_value=True), \
                patch("main.apply_dotenv"), \
                patch("main.load_config", return_value=None), \
                patch.object(main.sys, "stdin", fake_stdin), \
                patch("main.run_setup", return_value=wizard_cfg) as wizard, \
                patch("main.MaxToTelegramBridge", return_value=bridge_inst) as bridge, \
                patch("main.asyncio.run") as run:
            main.main()
        wizard.assert_called_once()
        bridge.assert_called_once_with(wizard_cfg)
        run.assert_called_once()  # run_forever scheduled (but not actually awaited)

    def test_existing_config_builds_bridge_without_wizard(self):
        cfg = {"telegram": {"bot_token": "x"}}
        bridge_inst = Mock()
        bridge_inst.run_forever = Mock()
        with patch("main._configure"), \
                patch("main.acquire_single_instance", return_value=True), \
                patch("main.apply_dotenv"), \
                patch("main.load_config", return_value=cfg), \
                patch("main.run_setup") as wizard, \
                patch("main.MaxToTelegramBridge", return_value=bridge_inst) as bridge, \
                patch("main.asyncio.run") as run:
            main.main()
        wizard.assert_not_called()
        bridge.assert_called_once_with(cfg)
        run.assert_called_once()

    def test_keyboard_interrupt_during_run_is_handled(self):
        cfg = {"telegram": {}}
        bridge_inst = Mock()
        bridge_inst.run_forever = Mock()
        with patch("main._configure"), \
                patch("main.acquire_single_instance", return_value=True), \
                patch("main.apply_dotenv"), \
                patch("main.load_config", return_value=cfg), \
                patch("main.MaxToTelegramBridge", return_value=bridge_inst), \
                patch("main.asyncio.run", side_effect=KeyboardInterrupt):
            # KeyboardInterrupt is caught and turned into a clean stop, not raised.
            main.main()


class MaxMsgAsIntIdTests(unittest.TestCase):
    def test_numeric_string_is_converted_to_int(self):
        self.assertEqual(maxmsg._as_int_id("12345"), 12345)

    def test_huge_numeric_string_round_trips_exactly(self):
        big = "9" * 40  # well beyond 64-bit; Python ints are arbitrary precision
        self.assertEqual(maxmsg._as_int_id(big), int(big))

    def test_non_numeric_value_passes_through_untouched(self):
        self.assertEqual(maxmsg._as_int_id("abc"), "abc")

    def test_none_passes_through_untouched(self):
        self.assertIsNone(maxmsg._as_int_id(None))


if __name__ == "__main__":
    unittest.main()
