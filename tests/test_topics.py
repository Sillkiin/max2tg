import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bridge import MaxToTelegramBridge, _contact_display_name, _message_content
from config import normalize_config
from state import BridgeState, normalize_topic_title


class DotenvTests(unittest.TestCase):
    def test_loads_file_but_does_not_override_real_env(self):
        import os

        import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                'MAX2TG_TEST_A=fromfile\n# comment\nMAX2TG_TEST_B="quoted"\n',
                encoding="utf-8",
            )
            os.environ.pop("MAX2TG_TEST_A", None)
            os.environ["MAX2TG_TEST_B"] = "realenv"
            try:
                config.apply_dotenv(path)
                self.assertEqual(os.environ["MAX2TG_TEST_A"], "fromfile")
                self.assertEqual(os.environ["MAX2TG_TEST_B"], "realenv")
            finally:
                os.environ.pop("MAX2TG_TEST_A", None)
                os.environ.pop("MAX2TG_TEST_B", None)

    def test_handles_export_prefix_and_inline_comment(self):
        import os

        import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                'export MAX2TG_TA=hello\nMAX2TG_TB=100  # a default\n'
                'MAX2TG_TC="q v"\n', encoding="utf-8")
            for k in ("MAX2TG_TA", "MAX2TG_TB", "MAX2TG_TC"):
                os.environ.pop(k, None)
            try:
                config.apply_dotenv(path)
                self.assertEqual(os.environ["MAX2TG_TA"], "hello")
                self.assertEqual(os.environ["MAX2TG_TB"], "100")
                self.assertEqual(os.environ["MAX2TG_TC"], "q v")
            finally:
                for k in ("MAX2TG_TA", "MAX2TG_TB", "MAX2TG_TC"):
                    os.environ.pop(k, None)


class StateSaveTests(unittest.TestCase):
    def test_falls_back_when_atomic_replace_fails(self):
        import json

        # A single-file bind mount in Docker makes tmp.replace() raise
        # EBUSY/EXDEV; save() must still persist via a direct write.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = BridgeState(path)
            with patch("pathlib.Path.replace", side_effect=OSError("EBUSY")):
                state.save_topic(123, thread_id=7, title="X", chat_type="dialog")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["topics"]["123"]["telegram_thread_id"], 7)


class ContactNameTests(unittest.TestCase):
    def test_prefers_full_name_over_first_name_only(self):
        contact = {
            "names": [{"name": "Алина", "firstName": "Алина", "lastName": "Чернова"}],
        }
        self.assertEqual(_contact_display_name(contact), "Алина Чернова")

    def test_single_name_when_no_last_name(self):
        contact = {"names": [{"name": "Кирилл", "firstName": "Кирилл"}]}
        self.assertEqual(_contact_display_name(contact), "Кирилл")

    def test_falls_back_to_contact_level_fields(self):
        contact = {"firstName": "Инна", "lastName": "Кладова"}
        self.assertEqual(_contact_display_name(contact), "Инна Кладова")


class TopicBodyTests(unittest.TestCase):
    def test_group_keeps_sender_prefix(self):
        self.assertEqual(
            MaxToTelegramBridge._topic_body("Иван", "привет", []), "Иван:\nпривет")

    def test_channel_drops_redundant_sender_prefix(self):
        # A channel post (sender == "MAX") must NOT get the "MAX:" prefix that
        # just duplicates the channel name shown above the message.
        self.assertEqual(
            MaxToTelegramBridge._topic_body("MAX", "Афиша на выходные", [], is_channel=True),
            "Афиша на выходные")

    def test_channel_media_caption_has_no_prefix(self):
        self.assertEqual(
            MaxToTelegramBridge._topic_caption("MAX", "Фото", is_channel=True), "Фото")
        self.assertEqual(
            MaxToTelegramBridge._topic_caption("Иван", "Фото", is_channel=False),
            "Иван:\nФото")


class ForwardTests(unittest.TestCase):
    def test_forwarded_text_unwrapped_from_link(self):
        text, _parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "chatName": "ПРОПЕЛЛЕР",
            "message": {"id": "2", "text": "Текст исходного", "attaches": []}}})
        self.assertIn("Переслано", text)
        self.assertIn("ПРОПЕЛЛЕР", text)
        self.assertIn("Текст исходного", text)

    def test_forwarded_media_unwrapped(self):
        _text, parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "message": {"id": "2", "text": "", "attaches": [
                {"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/x"}]}}})
        self.assertTrue(any(p.kind == "photo" for p in parsed))

    def test_reply_with_own_text_gets_quote_header(self):
        # A reply keeps its own text and gets a compact quote of the original.
        text, _parsed = _message_content({"text": "мой ответ", "attaches": [], "link": {
            "type": "REPLY", "message": {"text": "оригинал"}}})
        self.assertIn("мой ответ", text)
        self.assertIn("оригинал", text)
        self.assertIn("ответ на", text.lower())

    def test_forwarded_file_unwrapped_to_file_resolve(self):
        # A forwarded document with no direct url -> file_resolve (resolved later
        # via the forward's own chat/message ids, the same path video uses).
        _text, parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "message": {"id": "2", "text": "", "attaches": [
                {"_type": "FILE", "fileId": 555, "name": "doc.pdf", "size": 10}]}}})
        self.assertTrue(
            any(p.kind == "file_resolve" and p.file_id == 555 for p in parsed))

    def test_nested_forward_unwrapped_to_innermost(self):
        # A forward whose inner message is itself a forward carrying a photo:
        # descend to the innermost content instead of rendering empty.
        inner = {"id": "3", "text": "", "attaches": [], "link": {
            "type": "FORWARD", "message": {"id": "4", "text": "глубокий текст",
                "attaches": [{"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/y"}]}}}
        text, parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "chatName": "Канал", "message": inner}})
        self.assertIn("Переслано", text)
        self.assertIn("глубокий текст", text)
        self.assertTrue(any(p.kind == "photo" for p in parsed))

    def test_normal_message_passthrough(self):
        text, parsed = _message_content({"text": "привет", "attaches": []})
        self.assertEqual(text, "привет")
        self.assertEqual(parsed, [])


class SmartActionTests(unittest.TestCase):
    def test_max_link_becomes_join(self):
        self.assertEqual(
            MaxToTelegramBridge._smart_action("https://max.ru/join/AbC-d_e"),
            "/join https://max.ru/join/AbC-d_e")

    def test_link_extracted_from_surrounding_text(self):
        # A link pasted inside a sentence is still actioned (trailing comma trimmed).
        self.assertEqual(
            MaxToTelegramBridge._smart_action("вступи: max.ru/join/XyZ, спасибо"),
            "/join max.ru/join/XyZ")

    def test_username_becomes_join(self):
        self.assertEqual(
            MaxToTelegramBridge._smart_action("@cool_channel"), "/join @cool_channel")

    def test_phone_is_not_auto_actioned(self):
        # A bare phone needs text to DM, so it isn't auto-actioned.
        self.assertIsNone(MaxToTelegramBridge._smart_action("+7 999 123-45-67"))

    def test_plain_text_is_ignored(self):
        self.assertIsNone(MaxToTelegramBridge._smart_action("привет, как дела?"))

    def test_empty_is_ignored(self):
        self.assertIsNone(MaxToTelegramBridge._smart_action("   "))


class TopicStateTests(unittest.TestCase):
    def test_state_roundtrip_and_find_by_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = BridgeState(path)
            state.save_topic(
                148440672,
                thread_id=77,
                title="Людмила",
                chat_type="dialog",
                sender="Людмила",
            )

            loaded = BridgeState(path)
            self.assertEqual(loaded.get_topic(148440672)["telegram_thread_id"], 77)
            self.assertEqual(loaded.find_by_thread(77)["max_chat_id"], 148440672)

    def test_topic_title_is_normalized_and_limited(self):
        self.assertEqual(normalize_topic_title("  Людмила   Иванова  ", "fallback"), "Людмила Иванова")
        self.assertLessEqual(len(normalize_topic_title("x" * 200, "fallback")), 120)

    def test_delete_topic_forgets_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = BridgeState(Path(tmp) / "state.json")
            state.save_topic(555, thread_id=7, title="X", chat_type="dialog")
            self.assertTrue(state.delete_topic(555))
            self.assertIsNone(state.get_topic(555))
            self.assertFalse(state.delete_topic(555))  # already gone


class ConfigTests(unittest.TestCase):
    def test_optional_topic_config_defaults_to_fallback_chat(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
            "telegram_forum_chat_id": "-100456",
        })

        self.assertEqual(config["telegram_chat_id"], 123)
        self.assertEqual(config["telegram_forum_chat_id"], -100456)
        self.assertEqual(config["telegram_fallback_chat_id"], 123)
        self.assertTrue(config["telegram_topics_enabled"])
        self.assertFalse(config["telegram_preload_topics"])
        self.assertFalse(config["telegram_seed_last_messages"])
        self.assertEqual(config["telegram_preload_chat_count"], 100)

    def test_env_overrides_apply_over_config_json(self):
        import os

        import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                '{"telegram_bot_token": "token", "telegram_chat_id": "123",'
                ' "max_login_token": "max"}',
                encoding="utf-8",
            )
            os.environ["MAX2TG_TELEGRAM_CONFIRM_SENT"] = "false"
            with patch.object(config_module, "CONFIG_PATH", path):
                try:
                    loaded = config_module.load_config()
                finally:
                    os.environ.pop("MAX2TG_TELEGRAM_CONFIRM_SENT", None)

        # Tokens come from config.json, but the env var still wins.
        self.assertEqual(loaded["telegram_bot_token"], "token")
        self.assertFalse(loaded["telegram_confirm_sent"])

    def test_env_tokens_do_not_discard_config_json_optional_settings(self):
        # HIGH-fix: all 3 token env vars set must NOT wipe optional config.json
        # settings (topics, forum id, confirm_sent).
        import os

        import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "telegram_bot_token": "fromfile",
                "telegram_chat_id": 111,
                "max_login_token": "fromfile",
                "telegram_topics_enabled": True,
                "telegram_forum_chat_id": -100123,
                "telegram_confirm_sent": False,
            }), encoding="utf-8")
            keys = ("MAX2TG_TELEGRAM_BOT_TOKEN", "MAX2TG_TELEGRAM_CHAT_ID",
                    "MAX2TG_MAX_TOKEN")
            saved = {k: os.environ.get(k) for k in keys}
            os.environ.update({
                "MAX2TG_TELEGRAM_BOT_TOKEN": "fromenv",
                "MAX2TG_TELEGRAM_CHAT_ID": "222",
                "MAX2TG_MAX_TOKEN": "fromenv",
            })
            try:
                with patch.object(config_module, "CONFIG_PATH", path):
                    loaded = config_module.load_config()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        self.assertEqual(loaded["telegram_bot_token"], "fromenv")  # env wins per-key
        self.assertEqual(loaded["telegram_forum_chat_id"], -100123)  # survives
        self.assertTrue(loaded["telegram_topics_enabled"])
        self.assertFalse(loaded["telegram_confirm_sent"])

    def test_confirm_sent_defaults_to_true(self):
        config = normalize_config({
            "telegram_bot_token": "token",
            "telegram_chat_id": "123",
            "max_login_token": "max",
        })
        self.assertTrue(config["telegram_confirm_sent"])

    def test_corrupt_config_is_logged(self):
        import config as config_module
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            with patch.object(config_module, "CONFIG_PATH", path):
                with self.assertLogs("config", level="WARNING"):
                    self.assertEqual(config_module.load_partial(), {})


class BridgeTopicTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self):
        return MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        })

    async def test_creates_topic_for_new_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", return_value=42):
                chat_id, thread_id, in_topic = await bridge._telegram_target(
                    555, "Людмила", "dialog", "Людмила"
                )

            self.assertEqual(chat_id, -100222)
            self.assertEqual(thread_id, 42)
            self.assertTrue(in_topic)
            self.assertEqual(bridge._state.get_topic(555)["title"], "Людмила")

    async def test_concurrent_new_chat_creates_one_topic(self):
        # HIGH-fix: two concurrent packets from the same brand-new chat must
        # create exactly ONE Telegram topic, not duplicate it.
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            created = []

            async def slow_create(func, *args):
                # asyncio.to_thread passes the target callable first; yield so
                # the second coroutine reaches the lock while we're "creating"
                # (under the bug both would create a topic).
                await asyncio.sleep(0)
                created.append(args)
                return 100 + len(created)

            with patch("bridge.asyncio.to_thread", side_effect=slow_create):
                results = await asyncio.gather(
                    bridge._telegram_target(555, "X", "dialog", "X"),
                    bridge._telegram_target(555, "X", "dialog", "X"),
                )

            self.assertEqual(len(created), 1)  # exactly one topic created
            self.assertEqual(results[0][1], results[1][1])  # same thread id

    async def test_name_cache_is_bounded(self):
        bridge = self.make_bridge()
        with patch("bridge.NAME_CACHE_LIMIT", 3), \
                patch("bridge.resolve_users", new=AsyncMock(return_value={})):
            for i in range(6):
                await bridge._resolve_sender_name(object(), 1000 + i)
        self.assertLessEqual(len(bridge._name_cache), 3)

    async def test_on_packet_ignores_non_dict_frame(self):
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        # A valid-JSON but non-dict frame must be ignored cleanly, not raise
        # AttributeError out of the fire-and-forget handler task.
        await bridge._on_packet(client, [1, 2, 3])  # must not raise

    async def test_help_command_replies(self):
        bridge = self.make_bridge()
        sent = []
        with patch("bridge.tg.send_message", side_effect=lambda *a, **k: sent.append(a[2])):
            await bridge._handle_command(111, None, "/help")
        self.assertTrue(sent and "/join" in sent[0])

    async def test_bare_link_in_chat_triggers_join_without_command(self):
        # The simplification: a pasted link acts like /join — no command typed.
        import maxactions
        bridge = self.make_bridge()
        bridge._client = object()
        result = maxactions.CommandResult("✅ Вступил: Канал")
        update = {"message": {"chat": {"id": 111}, "text": "https://max.ru/join/AbCdEf"}}
        with patch("bridge.maxactions.join", new=AsyncMock(return_value=result)) as join, \
                patch("bridge.tg.send_message", return_value=1):
            await bridge._handle_update(update)
        join.assert_awaited_once()
        self.assertEqual(join.await_args.args[1], "https://max.ru/join/AbCdEf")

    async def test_stale_topic_dropped_on_thread_not_found(self):
        # A deleted Telegram thread -> bridge forgets the topic so it recreates,
        # instead of dropping that chat's messages forever.
        bridge = self.make_bridge()
        bridge._own_id = 999
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(555, thread_id=42, title="X", chat_type="dialog")
            bridge._client = object()
            packet = {"opcode": 128, "payload": {
                "chatId": 555,
                "message": {"id": 1, "sender": 7, "text": "привет"},
            }}
            err = RuntimeError("Telegram API sendMessage failed: {'description': "
                               "'Bad Request: message thread not found'}")
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="A")), \
                    patch("bridge.tg.send_message", side_effect=err):
                await bridge._on_packet(bridge._client, packet)
            self.assertIsNone(bridge._state.get_topic(555))

    async def test_seed_skips_own_message(self):
        # Your own message must NOT be seeded back as "Вы: …".
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        bridge._own_id = 999
        message = {"id": "m9", "sender": 999, "text": "моё"}
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="F", chat_type="dialog")
            with patch("bridge.tg.send_message") as send:
                seeded = await bridge._seed_last_message(object(), 100, 41, message)
        self.assertFalse(seeded)
        send.assert_not_called()

    async def test_duplicate_max_message_forwarded_once(self):
        # MAX replaying a message on reconnect must not double-post it.
        bridge = self.make_bridge()
        bridge._own_id = 999
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._client = object()
            packet = {"opcode": 128, "payload": {
                "chatId": 555, "message": {"id": 7, "sender": 1, "text": "hi"}}}
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="A")), \
                    patch.object(bridge, "_forward", new=AsyncMock()) as fwd:
                await bridge._on_packet(bridge._client, packet)
                await bridge._on_packet(bridge._client, packet)   # same id replayed
            fwd.assert_awaited_once()

    async def test_falls_back_when_topic_creation_fails(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", side_effect=RuntimeError("no rights")):
                chat_id, thread_id, in_topic = await bridge._telegram_target(
                    555, "Людмила", "dialog", "Людмила"
                )

            self.assertEqual(chat_id, 111)
            self.assertIsNone(thread_id)
            self.assertFalse(in_topic)

    async def test_text_inside_topic_routes_to_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(
                555,
                thread_id=42,
                title="Людмила",
                chat_type="dialog",
                sender="Людмила",
            )
            bridge._client = object()
            update = {
                "message": {
                    "chat": {"id": -100222},
                    "message_thread_id": 42,
                    "text": "Привет из Telegram",
                }
            }

            with patch("bridge.max_send", new=AsyncMock()) as max_send, \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._handle_update(update)

            max_send.assert_awaited_once_with(bridge._client, 555, "Привет из Telegram")

    async def test_media_inside_topic_uploads_file_to_max_chat(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(
                555,
                thread_id=42,
                title="Family",
                chat_type="dialog",
                sender="Family",
            )
            bridge._client = object()
            update = {
                "message": {
                    "chat": {"id": -100222},
                    "message_thread_id": 42,
                    "document": {"file_id": "tg-file-1", "file_name": "report.pdf"},
                }
            }

            with patch("bridge.tg.download_file_by_id", return_value=(b"pdf", "docs/report.pdf")), \
                    patch("bridge.mediamax.send_uploaded_media", new=AsyncMock()) as send_media, \
                    patch("bridge.tg.send_message", return_value=10):
                await bridge._handle_update(update)

            send_media.assert_awaited_once_with(
                bridge._client,
                555,
                b"pdf",
                "report.pdf",
                "application/octet-stream",
                kind="file",
                text="",
                reply_to_message_id=None,
            )

    def test_telegram_sticker_attachment_metadata(self):
        bridge = self.make_bridge()

        static = bridge._telegram_attachment({
            "sticker": {"file_id": "s1", "file_unique_id": "u1"}
        })
        animated = bridge._telegram_attachment({
            "sticker": {"file_id": "s2", "file_unique_id": "u2", "is_animated": True}
        })
        video = bridge._telegram_attachment({
            "sticker": {"file_id": "s3", "file_unique_id": "u3", "is_video": True}
        })

        self.assertEqual(static["filename"], "telegram-sticker-u1.webp")
        self.assertEqual(static["mime_type"], "image/webp")
        self.assertEqual(animated["filename"], "telegram-sticker-u2.tgs")
        self.assertEqual(animated["mime_type"], "application/x-tgsticker")
        self.assertEqual(video["filename"], "telegram-sticker-u3.webm")
        self.assertEqual(video["mime_type"], "video/webm")

    async def test_preload_topics_from_login_creates_missing_topics(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        bridge._config["telegram_preload_chat_count"] = 10
        login_response = {
            "payload": {
                "chats": [
                    {"id": 100, "type": "CHAT", "title": "Family"},
                    {"id": 200, "cid": 200, "type": "DIALOG"},
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.create_forum_topic", side_effect=[41, 42]), \
                    patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")):
                await bridge._preload_topics_from_login(object(), login_response)

            self.assertEqual(bridge._state.get_topic(100)["telegram_thread_id"], 41)
            self.assertEqual(bridge._state.get_topic(100)["title"], "Family")
            self.assertEqual(bridge._state.get_topic(200)["telegram_thread_id"], 42)
            self.assertEqual(bridge._state.get_topic(200)["title"], "Alice")

    async def test_preload_topics_skips_existing_topic(self):
        bridge = self.make_bridge()
        bridge._config["telegram_preload_topics"] = True
        login_response = {
            "payload": {"chats": [{"id": 100, "type": "CHAT", "title": "Family"}]}
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch("bridge.tg.create_forum_topic") as create_topic:
                await bridge._preload_topics_from_login(object(), login_response)

            create_topic.assert_not_called()

    async def test_seed_last_message_once(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        bridge._own_id = 999
        message = {"id": "m1", "sender": 123, "text": "Last text"}

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")), \
                    patch("bridge.tg.send_message", return_value=555) as send_message:
                first = await bridge._seed_last_message(object(), 100, 41, message)
                second = await bridge._seed_last_message(object(), 100, 41, message)

            self.assertTrue(first)
            self.assertFalse(second)
            send_message.assert_called_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m1"
            )

    async def test_seed_last_message_with_media_without_text(self):
        bridge = self.make_bridge()
        bridge._config["telegram_seed_last_messages"] = True
        message = {
            "id": "m2",
            "sender": 123,
            "text": "",
            "attaches": [{"_type": "STICKER", "url": "https://example.com/sticker.webp"}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(100, thread_id=41, title="Family", chat_type="chat")
            with patch.object(bridge, "_resolve_sender_name", new=AsyncMock(return_value="Alice")), \
                    patch.object(bridge, "_send_media_item", new=AsyncMock(return_value=True)) as send_media:
                seeded = await bridge._seed_last_message(object(), 100, 41, message)

            self.assertTrue(seeded)
            send_media.assert_awaited_once()
            self.assertEqual(
                bridge._state.get_topic(100)["last_seeded_max_message_id"], "m2"
            )


class RedactionTests(unittest.TestCase):
    def test_bot_token_and_url_secret_are_scrubbed(self):
        import logging

        import main
        rec = logging.LogRecord(
            "x", logging.WARNING, "f.py", 1,
            "poll error url: /bot123456789:AAEsecretTokenValue1234567/getUpdates"
            "?sig=ABCDEFsecret123&x=1",
            None, None)
        main._RedactSecretsFilter().filter(rec)
        out = rec.getMessage()
        self.assertNotIn("AAEsecretTokenValue1234567", out)
        self.assertNotIn("ABCDEFsecret123", out)
        self.assertIn("bot<redacted>", out)

    def test_max_login_token_is_scrubbed(self):
        import logging

        import main
        rec = logging.LogRecord(
            "x", logging.WARNING, "f.py", 1,
            "login payload {'token': 'maxSecretLoginToken1234567890', 'x': 0}",
            None, None)
        main._RedactSecretsFilter().filter(rec)
        out = rec.getMessage()
        self.assertNotIn("maxSecretLoginToken1234567890", out)
        self.assertIn("<redacted>", out)


class MaxClientPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fail_pending_unblocks_awaiters(self):
        import asyncio

        import max_client
        client = max_client.BrowserMaxClient()
        fut = asyncio.get_event_loop().create_future()
        client._pending = {1: fut}
        client._fail_pending()
        self.assertTrue(fut.done())
        with self.assertRaises(ConnectionError):
            fut.result()
        self.assertEqual(client._pending, {})

    async def test_recv_loop_skips_bad_frames(self):
        import max_client

        class FakeConn:
            def __init__(self, frames):
                self._frames = frames

            def __aiter__(self):
                return self._agen()

            async def _agen(self):
                for frame in self._frames:
                    yield frame

        client = max_client.BrowserMaxClient()
        dispatched = []

        async def callback(_c, packet):
            dispatched.append(packet)

        client._incoming_event_callback = callback
        client._connection = FakeConn([
            "not json at all",                       # unparseable -> skipped
            "[1, 2, 3]",                             # valid JSON, non-dict -> skipped
            "42",                                    # valid JSON scalar -> skipped
            '{"opcode": 128, "payload": {"x": 1}}',  # valid event (no seq)
        ])
        await client._recv_loop()
        await asyncio.sleep(0.01)  # let the dispatched task run
        # Only the dict event is dispatched; non-dict frames never reach a
        # callback that assumes a dict (would otherwise AttributeError).
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["opcode"], 128)


class TgHelperTests(unittest.TestCase):
    def test_send_message_empty_text_returns_none_without_api_call(self):
        import tg
        with patch("tg._call") as call:
            self.assertIsNone(tg.send_message("tok", 1, ""))
        call.assert_not_called()

    def test_get_updates_read_timeout_outlasts_poll_window(self):
        import tg
        captured = {}

        def fake_call(token, method, _timeout=tg.REQUEST_TIMEOUT, **params):
            captured["timeout"] = _timeout
            captured["poll"] = params.get("timeout")
            return []

        with patch("tg._call", side_effect=fake_call):
            tg.get_updates("tok", None, 25)
        self.assertGreater(captured["timeout"][1], captured["poll"])


class TgMutationTests(unittest.TestCase):
    def test_edit_message_text_empty_is_noop(self):
        import tg
        with patch("tg._call") as call:
            self.assertFalse(tg.edit_message_text("tok", 1, 2, ""))
        call.assert_not_called()

    def test_edit_message_text_calls_api(self):
        import tg
        with patch("tg._call") as call:
            self.assertTrue(tg.edit_message_text("tok", -100, 5, "новый"))
        self.assertEqual(call.call_args.args[1], "editMessageText")
        self.assertEqual(call.call_args.kwargs["message_id"], 5)
        self.assertEqual(call.call_args.kwargs["text"], "новый")

    def test_delete_message_calls_api(self):
        import tg
        with patch("tg._call", return_value=True) as call:
            self.assertTrue(tg.delete_message("tok", -100, 5))
        self.assertEqual(call.call_args.args[1], "deleteMessage")
        self.assertEqual(call.call_args.kwargs["message_id"], 5)

    def test_set_message_reaction_wraps_emoji(self):
        import tg
        with patch("tg._call") as call:
            tg.set_message_reaction("tok", -100, 5, "👍")
        self.assertEqual(call.call_args.kwargs["reaction"],
                         [{"type": "emoji", "emoji": "👍"}])

    def test_set_message_reaction_none_clears(self):
        import tg
        with patch("tg._call") as call:
            tg.set_message_reaction("tok", -100, 5, None)
        self.assertEqual(call.call_args.kwargs["reaction"], [])


class ForwardMapTests(unittest.TestCase):
    def make_bridge(self):
        return MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        })

    def test_remember_feeds_both_maps(self):
        bridge = self.make_bridge()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        # reverse (reply) map: TG id -> MAX message
        self.assertEqual(bridge._reply_map[500]["message_id"], "m1")
        # forward map: MAX message -> TG messages
        record = bridge._forward_map[(555, "m1")]
        self.assertEqual(record[0]["message_id"], 500)
        self.assertEqual(record[0]["role"], "text")

    def test_forward_map_accumulates_in_post_order(self):
        bridge = self.make_bridge()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        bridge._remember(501, 555, "m1", "Alice", -100222, 42, "caption")
        record = bridge._forward_map[(555, "m1")]
        self.assertEqual([e["message_id"] for e in record], [500, 501])

    def test_forward_map_is_bounded(self):
        bridge = self.make_bridge()
        with patch("bridge.FORWARD_MAP_LIMIT", 3):
            for i in range(6):
                bridge._remember(600 + i, 555, f"m{i}", "A", -100222, 42, "text")
        self.assertLessEqual(len(bridge._forward_map), 3)


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self):
        return MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        })

    async def test_mirror_edit_updates_text_body(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._state.save_topic(555, thread_id=42, title="Alice",
                                     chat_type="dialog")
            bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
            message = {"id": "m1", "sender": 7, "text": "исправлено",
                       "status": "EDITED"}
            with patch.object(bridge, "_message_sender_name",
                              new=AsyncMock(return_value="Alice")), \
                    patch("bridge.tg.edit_message_text") as edit:
                await bridge._mirror_edit(object(), 555, "m1", message)
            edit.assert_called_once()
            self.assertEqual(edit.call_args.args[2], 500)       # TG message id
            self.assertIn("исправлено", edit.call_args.args[3])  # new body
            self.assertIn("Alice:", edit.call_args.args[3])      # sender prefix

    async def test_mirror_edit_noop_when_not_forwarded(self):
        bridge = self.make_bridge()
        with patch("bridge.tg.edit_message_text") as edit:
            await bridge._mirror_edit(
                object(), 555, "ghost",
                {"text": "x", "status": "EDITED", "sender": 7})
        edit.assert_not_called()

    async def test_mirror_edit_swallows_not_modified(self):
        bridge = self.make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            bridge._state = BridgeState(Path(tmp) / "state.json")
            bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
            err = RuntimeError("Telegram API editMessageText failed: "
                               "{'description': 'Bad Request: message is not modified'}")
            with patch.object(bridge, "_message_sender_name",
                              new=AsyncMock(return_value="Alice")), \
                    patch("bridge.tg.edit_message_text", side_effect=err):
                # Must not raise: re-rendering identical content is a no-op.
                await bridge._mirror_edit(
                    object(), 555, "m1",
                    {"id": "m1", "sender": 7, "text": "x", "status": "EDITED"})

    async def test_mirror_delete_removes_all_and_forgets(self):
        bridge = self.make_bridge()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        bridge._remember(501, 555, "m1", "Alice", -100222, 42, "caption")
        deleted = []
        with patch("bridge.tg.delete_message",
                   side_effect=lambda _t, _c, mid: deleted.append(mid)):
            await bridge._mirror_delete(555, ["m1"])
        self.assertEqual(sorted(deleted), [500, 501])
        self.assertNotIn((555, "m1"), bridge._forward_map)

    async def test_mirror_reaction_targets_head_message(self):
        bridge = self.make_bridge()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        bridge._remember(501, 555, "m1", "Alice", -100222, 42, "caption")
        with patch("bridge.tg.set_message_reaction") as react:
            await bridge._mirror_reaction(555, "m1", "👍")
        react.assert_called_once()
        self.assertEqual(react.call_args.args[2], 500)  # head (first-posted) msg
        self.assertEqual(react.call_args.args[3], "👍")

    def test_top_reaction_picks_highest_then_none_when_empty(self):
        self.assertEqual(
            MaxToTelegramBridge._top_reaction(
                [{"reaction": "👍", "count": 1}, {"reaction": "😍", "count": 3}]),
            "😍")
        self.assertIsNone(MaxToTelegramBridge._top_reaction([]))

    async def test_on_packet_routes_delete(self):
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.delete_message", return_value=True) as delete:
            await bridge._on_packet(client, {"opcode": 142, "payload": {
                "chatId": 555, "messageIds": ["m1"]}})
        delete.assert_called_once()

    async def test_on_packet_routes_reaction(self):
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.set_message_reaction") as react:
            await bridge._on_packet(client, {"opcode": 156, "payload": {
                "chatId": 555, "messageId": "m1",
                "reactionInfo": {"counters": [{"reaction": "🔥", "count": 2}],
                                 "totalCount": 2}}})
        react.assert_called_once()
        self.assertEqual(react.call_args.args[3], "🔥")

    async def test_on_packet_edit_mirrors_and_does_not_reforward(self):
        # EDITED status on an already-seen message -> mirror as an edit, NOT a
        # second forward.
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        bridge._own_id = 999
        bridge._seen_messages[(555, "m1")] = None
        with patch.object(bridge, "_mirror_edit", new=AsyncMock()) as mirror, \
                patch.object(bridge, "_forward", new=AsyncMock()) as fwd:
            await bridge._on_packet(client, {"opcode": 128, "payload": {
                "chatId": 555,
                "message": {"id": "m1", "sender": 7, "text": "ред",
                            "status": "EDITED"}}})
        mirror.assert_awaited_once()
        fwd.assert_not_awaited()

    async def test_on_packet_edit_of_unseen_message_is_forwarded(self):
        # An EDITED message we never saw (edited before connect) must be
        # forwarded as new, not silently dropped.
        bridge = self.make_bridge()
        client = object()
        bridge._client = client
        bridge._own_id = 999
        with patch.object(bridge, "_mirror_edit", new=AsyncMock()) as mirror, \
                patch.object(bridge, "_forward", new=AsyncMock()) as fwd, \
                patch.object(bridge, "_resolve_sender_name",
                             new=AsyncMock(return_value="A")):
            await bridge._on_packet(client, {"opcode": 128, "payload": {
                "chatId": 555,
                "message": {"id": "m9", "sender": 7, "text": "x",
                            "status": "EDITED"}}})
        mirror.assert_not_awaited()
        fwd.assert_awaited_once()


class _FakeMaxClient:
    """Captures the last invoke_method opcode+payload for assertions."""
    def __init__(self, response=None):
        self.calls = []
        self._response = response or {"payload": {"message": {"id": "M1"}}}

    async def invoke_method(self, opcode, payload):
        self.calls.append((opcode, payload))
        return self._response


class MaxMsgTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_message_payload_shape(self):
        import maxmsg
        client = _FakeMaxClient()
        await maxmsg.edit_message(client, 555, 116766060130147953, "новый текст")
        opcode, payload = client.calls[0]
        self.assertEqual(opcode, 67)
        self.assertEqual(payload["messageId"], "116766060130147953")  # string
        self.assertEqual(payload["text"], "новый текст")
        self.assertEqual(payload["elements"], [])
        self.assertEqual(payload["attachments"], [])  # required empty arrays

    async def test_set_reaction_is_object_not_string(self):
        import maxmsg
        client = _FakeMaxClient()
        await maxmsg.set_reaction(client, 555, "116765779164748382", "👍")
        opcode, payload = client.calls[0]
        self.assertEqual(opcode, 178)
        self.assertEqual(payload["messageId"], 116765779164748382)  # int
        self.assertEqual(payload["reaction"],
                         {"reactionType": "EMOJI", "id": "👍"})

    async def test_remove_reaction_payload(self):
        import maxmsg
        client = _FakeMaxClient()
        await maxmsg.remove_reaction(client, 555, "116")
        opcode, payload = client.calls[0]
        self.assertEqual(opcode, 179)
        self.assertEqual(payload, {"chatId": 555, "messageId": 116})


class TgUpdatesTests(unittest.TestCase):
    def test_get_updates_requests_edits_and_reactions(self):
        import tg
        captured = {}

        def fake_call(token, method, _timeout=tg.REQUEST_TIMEOUT, **params):
            captured["allowed"] = params.get("allowed_updates")
            return []

        with patch("tg._call", side_effect=fake_call):
            tg.get_updates("tok", None, 25)
        self.assertIn("message", captured["allowed"])
        self.assertIn("edited_message", captured["allowed"])
        self.assertIn("message_reaction", captured["allowed"])


class ReverseMirrorTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self):
        return MaxToTelegramBridge({
            "telegram_bot_token": "token",
            "telegram_chat_id": 111,
            "telegram_fallback_chat_id": 111,
            "telegram_forum_chat_id": -100222,
            "telegram_topics_enabled": True,
            "max_login_token": "max",
        })

    async def test_edited_message_mirrors_to_max(self):
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember_tg_sent(700, 555, "m1")
        edited = {"chat": {"id": -100222}, "message_id": 700, "text": "fixed"}
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await bridge._handle_edited_message(edited)
        edit.assert_awaited_once_with(bridge._client, 555, "m1", "fixed")

    async def test_edit_of_unrelayed_message_ignored(self):
        bridge = self.make_bridge()
        bridge._client = object()
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await bridge._handle_edited_message(
                {"chat": {"id": -100222}, "message_id": 999, "text": "x"})
        edit.assert_not_awaited()

    async def test_edit_of_media_message_skipped(self):
        # Editing a media caption would strip the MAX attachment -> skip.
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember_tg_sent(700, 555, "m1")
        edited = {"chat": {"id": -100222}, "message_id": 700, "caption": "new",
                  "document": {"file_id": "d1", "file_name": "a.pdf"}}
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await bridge._handle_edited_message(edited)
        edit.assert_not_awaited()

    async def test_edit_outside_allowed_chat_ignored(self):
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember_tg_sent(700, 555, "m1")
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await bridge._handle_edited_message(
                {"chat": {"id": 99999}, "message_id": 700, "text": "x"})
        edit.assert_not_awaited()

    async def test_reaction_set_mirrors_to_max(self):
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        reaction = {"chat": {"id": -100222}, "message_id": 500,
                    "user": {"id": 12345},
                    "new_reaction": [{"type": "emoji", "emoji": "🔥"}]}
        with patch("bridge.maxmsg.set_reaction", new=AsyncMock()) as react:
            await bridge._handle_message_reaction(reaction)
        react.assert_awaited_once_with(bridge._client, 555, "m1", "🔥")

    async def test_reaction_removed_calls_remove(self):
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        reaction = {"chat": {"id": -100222}, "message_id": 500,
                    "user": {"id": 12345}, "new_reaction": []}
        with patch("bridge.maxmsg.remove_reaction", new=AsyncMock()) as rem:
            await bridge._handle_message_reaction(reaction)
        rem.assert_awaited_once_with(bridge._client, 555, "m1")

    async def test_bot_own_reaction_is_ignored(self):
        # The bot's own mirrored reaction must not loop back into MAX.
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._bot_id = 4242
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        reaction = {"chat": {"id": -100222}, "message_id": 500,
                    "user": {"id": 4242},
                    "new_reaction": [{"type": "emoji", "emoji": "🔥"}]}
        with patch("bridge.maxmsg.set_reaction", new=AsyncMock()) as react:
            await bridge._handle_message_reaction(reaction)
        react.assert_not_awaited()

    async def test_reaction_on_unmapped_message_ignored(self):
        bridge = self.make_bridge()
        bridge._client = object()
        reaction = {"chat": {"id": -100222}, "message_id": 777,
                    "user": {"id": 1},
                    "new_reaction": [{"type": "emoji", "emoji": "🔥"}]}
        with patch("bridge.maxmsg.set_reaction", new=AsyncMock()) as react:
            await bridge._handle_message_reaction(reaction)
        react.assert_not_awaited()

    async def test_custom_emoji_reaction_is_skipped_as_set(self):
        # A custom emoji has no MAX equivalent -> treated as "no emoji" (remove).
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        reaction = {"chat": {"id": -100222}, "message_id": 500, "user": {"id": 1},
                    "new_reaction": [{"type": "custom_emoji",
                                      "custom_emoji_id": "999"}]}
        with patch("bridge.maxmsg.set_reaction", new=AsyncMock()) as react, \
                patch("bridge.maxmsg.remove_reaction", new=AsyncMock()) as rem:
            await bridge._handle_message_reaction(reaction)
        react.assert_not_awaited()
        rem.assert_awaited_once()

    async def test_handle_update_routes_edit_and_reaction(self):
        bridge = self.make_bridge()
        with patch.object(bridge, "_handle_edited_message", new=AsyncMock()) as e, \
                patch.object(bridge, "_handle_message_reaction", new=AsyncMock()) as r:
            await bridge._handle_update({"edited_message": {"message_id": 1}})
            await bridge._handle_update({"message_reaction": {"message_id": 1}})
        e.assert_awaited_once()
        r.assert_awaited_once()

    async def test_relay_records_tg_sent_for_later_edit(self):
        # Relaying a user message into a topic records its TG id -> MAX id, so a
        # subsequent Telegram edit can be mirrored back.
        bridge = self.make_bridge()
        bridge._client = object()
        bridge._confirm_sent = False
        target = {"chat_id": 555, "message_id": None, "sender": "X",
                  "telegram_chat_id": -100222, "message_thread_id": 42}
        message = {"message_id": 700, "text": "привет"}
        with patch("bridge.max_send",
                   new=AsyncMock(return_value={"payload": {"message": {"id": "M99"}}})):
            await bridge._send_telegram_update_to_max(target, message)
        self.assertEqual(bridge._tg_sent_to_max[700],
                         {"chat_id": 555, "message_id": "M99"})


if __name__ == "__main__":
    unittest.main()
