"""Characterization tests for additional bridge.py handlers.

Complements test_topics.py: targets uncovered branches of the MAX<->Telegram
mirror (event-frame logging, quote snippets, content unwrap prefixes, topic
title refresh, media item routing, seeding, /del, reverse edit) without ever
touching a real socket or the live run_forever loop.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge
from attaches import ParsedAttach
from bridge import (
    MaxToTelegramBridge,
    _log_event_frame,
    _message_content,
    _quote_snippet,
)
from state import BridgeState


def make_bridge():
    return MaxToTelegramBridge({
        "telegram_bot_token": "token",
        "telegram_chat_id": 111,
        "telegram_fallback_chat_id": 111,
        "telegram_forum_chat_id": -100222,
        "telegram_topics_enabled": True,
        "max_login_token": "max",
    })


class LogEventFrameTests(unittest.TestCase):
    def test_writes_opcode_and_payload_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.log"
            with patch("bridge.EVENT_DEBUG_LOG", log):
                _log_event_frame({"opcode": 156, "payload": {"chatId": 1}})
            line = log.read_text(encoding="utf-8").strip()
            self.assertIn('"opcode": 156', line)
            self.assertIn('"chatId": 1', line)

    def test_skips_when_over_size_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.log"
            log.write_text("x" * 100, encoding="utf-8")
            with patch("bridge.EVENT_DEBUG_LOG", log), \
                    patch("bridge.EVENT_DEBUG_LOG_MAX_BYTES", 10):
                _log_event_frame({"opcode": 1, "payload": {}})
            # Cap exceeded -> the frame must NOT be appended.
            self.assertEqual(log.read_text(encoding="utf-8"), "x" * 100)

    def test_os_error_is_swallowed(self):
        # A failing write must not raise out of the fire-and-forget handler.
        with patch("bridge.EVENT_DEBUG_LOG") as log:
            log.exists.return_value = False
            log.open.side_effect = OSError("disk full")
            _log_event_frame({"opcode": 1, "payload": {}})  # must not raise


class QuoteSnippetTests(unittest.TestCase):
    def test_uses_text_when_present(self):
        self.assertEqual(_quote_snippet({"text": "  привет   мир  "}), "привет мир")

    def test_falls_back_to_first_attach_text(self):
        # No text -> use the first parsed attach's description, first line only.
        snippet = _quote_snippet({"text": "", "attaches": [
            {"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/p"}]})
        self.assertEqual(snippet, "🖼 Фото")

    def test_truncates_long_text_with_ellipsis(self):
        snippet = _quote_snippet({"text": "a" * 300})
        self.assertEqual(len(snippet), bridge._QUOTE_SNIPPET_MAX)
        self.assertTrue(snippet.endswith("…"))

    def test_empty_with_no_attaches_is_empty(self):
        self.assertEqual(_quote_snippet({"text": "", "attaches": []}), "")


class MessageContentBranchTests(unittest.TestCase):
    def test_reply_quote_only_gets_reply_prefix(self):
        # A REPLY with no own body/attaches surfaces the inner text under "Ответ:".
        text, parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "REPLY", "message": {"text": "оригинал реплая", "attaches": []}}})
        self.assertTrue(text.startswith("↩️ Ответ:"))
        self.assertIn("оригинал реплая", text)
        self.assertEqual(parsed, [])

    def test_forward_prefix_without_chat_name(self):
        # A FORWARD with no chatName falls back to the bare "Переслано" prefix.
        text, _parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "message": {"text": "тело", "attaches": []}}})
        self.assertTrue(text.startswith("↪️ Переслано:"))
        self.assertNotIn("«", text)
        self.assertIn("тело", text)

    def test_unknown_link_type_surfaces_inner_without_prefix(self):
        # A link of an unrecognized type yields the inner text with no prefix.
        text, _parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "MENTION", "message": {"text": "внутренний", "attaches": []}}})
        self.assertEqual(text, "внутренний")

    def test_forward_empty_inner_renders_prefix_only(self):
        text, _parsed = _message_content({"text": "", "attaches": [], "link": {
            "type": "FORWARD", "chatName": "Канал",
            "message": {"text": "", "attaches": []}}})
        self.assertEqual(text, "↪️ Переслано из «Канал»")


class LookupMaxMessageTests(unittest.TestCase):
    def test_prefers_reply_map(self):
        b = make_bridge()
        b._remember(500, 555, "m1", "Alice", -100222, 42, "text")
        self.assertEqual(b._lookup_max_message(500), (555, "m1"))

    def test_falls_back_to_tg_sent_map(self):
        b = make_bridge()
        b._remember_tg_sent(700, 777, "M9")
        self.assertEqual(b._lookup_max_message(700), (777, "M9"))

    def test_unknown_returns_none_pair(self):
        b = make_bridge()
        self.assertEqual(b._lookup_max_message(123), (None, None))


class TelegramMediaNoteTests(unittest.TestCase):
    def test_document_uses_file_name(self):
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note(
                {"document": {"file_name": "a.pdf"}}),
            "[Telegram file: a.pdf]")

    def test_sticker_with_emoji(self):
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note({"sticker": {"emoji": "🔥"}}),
            "[Telegram sticker 🔥]")

    def test_voice_and_video_note_and_photo(self):
        # Telegram always ships a non-empty dict for these (a file_id at least).
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note({"voice": {"file_id": "v"}}),
            "[Telegram voice message]")
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note(
                {"video_note": {"file_id": "vn"}}),
            "[Telegram video note]")
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note({"photo": [{"file_id": "p"}]}),
            "[Telegram photo]")

    def test_audio_falls_back_to_title(self):
        self.assertEqual(
            MaxToTelegramBridge._telegram_media_note(
                {"audio": {"title": "Song"}}),
            "[Telegram audio: Song]")

    def test_plain_text_message_has_no_note(self):
        self.assertIsNone(MaxToTelegramBridge._telegram_media_note({"text": "hi"}))


class TelegramOutgoingTextTests(unittest.TestCase):
    def test_combines_caption_and_media_note(self):
        out = MaxToTelegramBridge._telegram_outgoing_text(
            {"caption": "подпись", "document": {"file_name": "f.pdf"}})
        self.assertIn("подпись", out)
        self.assertIn("[Telegram file: f.pdf]", out)

    def test_media_note_only_when_no_text(self):
        out = MaxToTelegramBridge._telegram_outgoing_text(
            {"voice": {"file_id": "v"}})
        self.assertEqual(out, "[Telegram voice message]")

    def test_plain_text_passthrough(self):
        self.assertEqual(
            MaxToTelegramBridge._telegram_outgoing_text({"text": "  hi  "}), "hi")


class RefreshTopicTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_renames_topic_for_numeric_current_title(self):
        b = make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(555, thread_id=42, title="MAX chat 555",
                                 chat_type="dialog")
            with patch("bridge.tg.edit_forum_topic") as edit:
                renamed = await b._refresh_topic_title(
                    555, 42, "Людмила", "dialog", "Людмила")
            self.assertTrue(renamed)
            edit.assert_called_once()
            self.assertEqual(b._state.get_topic(555)["title"], "Людмила")

    async def test_keeps_good_existing_title(self):
        b = make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(555, thread_id=42, title="Хороший",
                                 chat_type="dialog")
            with patch("bridge.tg.edit_forum_topic") as edit:
                renamed = await b._refresh_topic_title(
                    555, 42, "Другой", "dialog", "Другой")
            self.assertFalse(renamed)
            edit.assert_not_called()

    async def test_resync_mode_overwrites_good_title(self):
        b = make_bridge()
        b._resync_titles = True
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(555, thread_id=42, title="Старое имя",
                                 chat_type="dialog")
            with patch("bridge.tg.edit_forum_topic") as edit:
                renamed = await b._refresh_topic_title(
                    555, 42, "Новое имя", "dialog", "Новое имя")
            self.assertTrue(renamed)
            edit.assert_called_once()
            self.assertEqual(b._state.get_topic(555)["title"], "Новое имя")

    async def test_numeric_new_title_is_refused(self):
        b = make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            with patch("bridge.tg.edit_forum_topic") as edit:
                renamed = await b._refresh_topic_title(
                    555, 42, "12345", "dialog", "12345")
            self.assertFalse(renamed)
            edit.assert_not_called()

    async def test_rename_failure_returns_false(self):
        b = make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(555, thread_id=42, title="MAX chat 555",
                                 chat_type="dialog")
            with patch("bridge.tg.edit_forum_topic",
                       side_effect=RuntimeError("no rights")):
                renamed = await b._refresh_topic_title(
                    555, 42, "Имя", "dialog", "Имя")
            self.assertFalse(renamed)
            # State keeps the old title since the rename never landed.
            self.assertEqual(b._state.get_topic(555)["title"], "MAX chat 555")


class SendMediaItemTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, in_topic=True, is_channel=False):
        # (client, header, chat_id, max_message_id, sender, telegram_chat_id,
        #  thread_id, in_topic, is_channel)
        return (object(), "MAX | A (чат 555)", 555, "m1", "A", -100222, 42,
                in_topic, is_channel)

    async def test_photo_with_caption_role(self):
        # _MEDIA_SENDERS captures the tg.* refs at import, so patch the dict.
        # The senders are sync (run via asyncio.to_thread) -> MagicMock, not
        # AsyncMock.
        b = make_bridge()
        item = ParsedAttach("photo", "🖼 Фото", url="https://x/p.jpg")
        send = MagicMock(return_value=900)
        senders = dict(bridge._MEDIA_SENDERS)
        senders["photo"] = (send, True)
        with patch("bridge._MEDIA_SENDERS", senders):
            header_sent = await b._send_media_item(item, False, self._ctx())
        self.assertTrue(header_sent)
        send.assert_called_once()
        # First (header not yet sent) item caption gets the "A:" topic prefix.
        self.assertIn("A:", send.call_args.args[3])
        record = b._forward_map[(555, "m1")]
        self.assertEqual(record[0]["role"], "caption")

    async def test_sticker_has_no_caption_and_media_role(self):
        b = make_bridge()
        item = ParsedAttach("sticker", "🩷 Стикер", url="https://x/s.webp")
        send = MagicMock(return_value=901)
        senders = dict(bridge._MEDIA_SENDERS)
        senders["sticker"] = (send, False)
        with patch("bridge._MEDIA_SENDERS", senders):
            await b._send_media_item(item, True, self._ctx())
        send.assert_called_once()
        # A non-caption sender is called as (token, chat_id, url, thread kw).
        self.assertEqual(send.call_args.args[2], "https://x/s.webp")
        self.assertEqual(b._forward_map[(555, "m1")][0]["role"], "media")

    async def test_send_failure_falls_back_to_note(self):
        b = make_bridge()
        item = ParsedAttach("video", "🎞 Видео", url="https://x/v.mp4")
        send = MagicMock(side_effect=RuntimeError("boom"))
        senders = dict(bridge._MEDIA_SENDERS)
        senders["video"] = (send, True)
        with patch("bridge._MEDIA_SENDERS", senders), \
                patch("bridge.tg.send_message", return_value=902) as note:
            await b._send_media_item(item, True, self._ctx())
        note.assert_called_once()
        self.assertIn("не удалось переслать медиа", note.call_args.args[2])
        # Fallback note is recorded as a plain (non-editable) media role.
        self.assertEqual(b._forward_map[(555, "m1")][0]["role"], "media")

    async def test_subsequent_item_caption_has_no_prefix(self):
        # When the header was already sent, the caption is just the item text.
        b = make_bridge()
        item = ParsedAttach("photo", "🖼 Фото", url="https://x/p.jpg")
        send = MagicMock(return_value=903)
        senders = dict(bridge._MEDIA_SENDERS)
        senders["photo"] = (send, True)
        with patch("bridge._MEDIA_SENDERS", senders):
            await b._send_media_item(item, True, self._ctx())
        self.assertEqual(send.call_args.args[3], "🖼 Фото")


class SendResolvedItemTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, client=None):
        return (client or object(), "MAX | A (чат 555)", 555, "m1", "A",
                -100222, 42, True, False)

    async def test_file_resolve_resolves_and_sends_document(self):
        b = make_bridge()
        item = ParsedAttach("file_resolve", "📎 doc.pdf", filename="doc.pdf",
                            file_id=555)
        with patch("bridge.mediamax.resolve_file_url",
                   new=AsyncMock(return_value="https://cdn/doc.pdf")) as resolve, \
                patch("bridge.tg.send_document", return_value=910) as send:
            await b._send_resolved_item(item, True, self._ctx())
        resolve.assert_awaited_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], "https://cdn/doc.pdf")
        self.assertEqual(send.call_args.args[4], "doc.pdf")  # filename
        self.assertEqual(b._forward_map[(555, "m1")][0]["role"], "caption")

    async def test_video_resolve_sends_video(self):
        b = make_bridge()
        item = ParsedAttach("video_resolve", "🎞 Видео", video_id=88)
        with patch("bridge.mediamax.resolve_video_url",
                   new=AsyncMock(return_value="https://cdn/v.mp4")), \
                patch("bridge.tg.send_video", return_value=911) as send:
            await b._send_resolved_item(item, True, self._ctx())
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], "https://cdn/v.mp4")

    async def test_audio_resolve_ogg_sends_voice(self):
        b = make_bridge()
        item = ParsedAttach("audio_resolve", "🎤 Голосовое", file_id=7, token="t")
        with patch("bridge.mediamax.resolve_audio_url",
                   new=AsyncMock(return_value=("https://cdn/a.ogg", "audio/ogg"))), \
                patch("bridge.tg.send_voice", return_value=912) as voice, \
                patch("bridge.tg.send_audio") as audio:
            await b._send_resolved_item(item, True, self._ctx())
        voice.assert_called_once()
        audio.assert_not_called()

    async def test_audio_resolve_non_ogg_sends_audio(self):
        b = make_bridge()
        item = ParsedAttach("audio_resolve", "🎤 Голосовое", file_id=7)
        with patch("bridge.mediamax.resolve_audio_url",
                   new=AsyncMock(return_value=("https://cdn/a.m4a", "audio/mp4"))), \
                patch("bridge.tg.send_voice") as voice, \
                patch("bridge.tg.send_audio", return_value=913) as audio:
            await b._send_resolved_item(item, True, self._ctx())
        audio.assert_called_once()
        voice.assert_not_called()

    async def test_oversized_file_sends_note_not_upload(self):
        b = make_bridge()
        big = bridge.TELEGRAM_UPLOAD_LIMIT + 1
        item = ParsedAttach("file_resolve", "📎 big.bin", filename="big.bin",
                            file_id=9, size=big)
        with patch("bridge.mediamax.resolve_file_url") as resolve, \
                patch("bridge.tg.send_message", return_value=914) as note:
            await b._send_resolved_item(item, True, self._ctx())
        resolve.assert_not_called()  # never resolve an over-limit file
        note.assert_called_once()
        self.assertIn("слишком большой", note.call_args.args[2])
        self.assertEqual(b._forward_map[(555, "m1")][0]["role"], "media")

    async def test_resolve_failure_falls_back_to_note(self):
        b = make_bridge()
        item = ParsedAttach("video_resolve", "🎞 Видео", video_id=88)
        with patch("bridge.mediamax.resolve_video_url",
                   new=AsyncMock(side_effect=RuntimeError("gone"))), \
                patch("bridge.tg.send_message", return_value=915) as note:
            await b._send_resolved_item(item, True, self._ctx())
        note.assert_called_once()
        self.assertIn("открыть в MAX", note.call_args.args[2])
        self.assertEqual(b._forward_map[(555, "m1")][0]["role"], "media")


class SeedLastMessageResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_seeds_text_then_resolvable_item(self):
        # A last message with text + a file_resolve attach: a header text message
        # plus a resolved document, both recorded under the MAX message id.
        b = make_bridge()
        b._config["telegram_seed_last_messages"] = True
        b._own_id = 999
        message = {"id": "m5", "sender": 123, "text": "файл вам",
                   "attaches": [{"_type": "FILE", "fileId": 42, "name": "r.pdf"}]}
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(100, thread_id=41, title="F", chat_type="chat")
            with patch.object(b, "_resolve_sender_name",
                              new=AsyncMock(return_value="Alice")), \
                    patch("bridge.tg.send_message", return_value=600), \
                    patch.object(b, "_send_resolved_item",
                                 new=AsyncMock(return_value=True)) as resolved:
                seeded = await b._seed_last_message(object(), 100, 41, message)
        self.assertTrue(seeded)
        resolved.assert_awaited_once()

    async def test_seed_returns_false_for_empty_message(self):
        # No text, no notes, no media -> nothing to seed.
        b = make_bridge()
        b._config["telegram_seed_last_messages"] = True
        b._own_id = 999
        message = {"id": "m6", "sender": 123, "text": "", "attaches": []}
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(100, thread_id=41, title="F", chat_type="chat")
            with patch("bridge.tg.send_message") as send:
                seeded = await b._seed_last_message(object(), 100, 41, message)
        self.assertFalse(seeded)
        send.assert_not_called()

    async def test_seed_disabled_returns_false(self):
        b = make_bridge()  # telegram_seed_last_messages defaults off
        message = {"id": "m7", "sender": 1, "text": "hi"}
        with patch("bridge.tg.send_message") as send:
            seeded = await b._seed_last_message(object(), 100, 41, message)
        self.assertFalse(seeded)
        send.assert_not_called()


class HandleDelTests(unittest.IsolatedAsyncioTestCase):
    async def test_del_without_reply_explains(self):
        b = make_bridge()
        with patch("bridge.tg.send_message", return_value=1) as say:
            await b._handle_del({"chat": {"id": 111}})
        say.assert_called_once()
        self.assertIn("СВО", say.call_args.args[2])

    async def test_del_on_unrelayed_message_refuses(self):
        b = make_bridge()
        message = {"chat": {"id": 111},
                   "reply_to_message": {"message_id": 999}}
        with patch("bridge.tg.send_message", return_value=1) as say:
            await b._handle_del(message)
        say.assert_called_once()
        self.assertIn("ВАШИ", say.call_args.args[2])

    async def test_del_deletes_for_everyone_and_clears_map(self):
        b = make_bridge()
        b._client = object()
        b._remember_tg_sent(700, 555, "M9")
        message = {"chat": {"id": 111}, "message_id": 800,
                   "reply_to_message": {"message_id": 700}}
        with patch("bridge.maxmsg.delete_message", new=AsyncMock()) as dele, \
                patch("bridge.tg.delete_message", return_value=True):
            await b._handle_del(message)
        dele.assert_awaited_once_with(b._client, 555, ["M9"], for_me=False)
        self.assertNotIn(700, b._tg_sent_to_max)

    async def test_del_when_client_disconnected(self):
        b = make_bridge()
        b._client = None
        b._remember_tg_sent(700, 555, "M9")
        message = {"chat": {"id": 111},
                   "reply_to_message": {"message_id": 700}}
        with patch("bridge.tg.send_message", return_value=1) as say:
            await b._handle_del(message)
        self.assertIn("не подключён", say.call_args.args[2])


class HandleEditedMessageEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_with_empty_text_is_ignored(self):
        b = make_bridge()
        b._client = object()
        b._remember_tg_sent(700, 555, "m1")
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await b._handle_edited_message(
                {"chat": {"id": -100222}, "message_id": 700, "text": "   "})
        edit.assert_not_awaited()

    async def test_edit_when_client_none_is_ignored(self):
        b = make_bridge()
        b._client = None
        b._remember_tg_sent(700, 555, "m1")
        with patch("bridge.maxmsg.edit_message", new=AsyncMock()) as edit:
            await b._handle_edited_message(
                {"chat": {"id": -100222}, "message_id": 700, "text": "new"})
        edit.assert_not_awaited()

    async def test_edit_error_is_swallowed(self):
        b = make_bridge()
        b._client = object()
        b._remember_tg_sent(700, 555, "m1")
        with patch("bridge.maxmsg.edit_message",
                   new=AsyncMock(side_effect=RuntimeError("api down"))):
            # Must not raise out of the handler.
            await b._handle_edited_message(
                {"chat": {"id": -100222}, "message_id": 700, "text": "new"})


class MirrorEditCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_caption_role_uses_edit_message_caption(self):
        # An edit of a media message whose primary entry is a caption must call
        # editMessageCaption, not editMessageText.
        b = make_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            b._state = BridgeState(Path(tmp) / "state.json")
            b._state.save_topic(555, thread_id=42, title="A", chat_type="dialog")
            b._remember(500, 555, "m1", "A", -100222, 42, "caption")
            message = {"id": "m1", "sender": 7, "text": "новая подпись",
                       "status": "EDITED"}
            with patch.object(b, "_message_sender_name",
                              new=AsyncMock(return_value="A")), \
                    patch("bridge.tg.edit_message_caption") as cap, \
                    patch("bridge.tg.edit_message_text") as txt:
                await b._mirror_edit(object(), 555, "m1", message)
            cap.assert_called_once()
            txt.assert_not_called()
            self.assertIn("новая подпись", cap.call_args.args[3])

    async def test_media_only_record_has_nothing_to_mirror(self):
        # A forward recorded only as "media" (no editable text/caption) -> no-op.
        b = make_bridge()
        b._remember(500, 555, "m1", "A", -100222, 42, "media")
        with patch("bridge.tg.edit_message_text") as txt, \
                patch("bridge.tg.edit_message_caption") as cap:
            await b._mirror_edit(
                object(), 555, "m1",
                {"id": "m1", "sender": 7, "text": "x", "status": "EDITED"})
        txt.assert_not_called()
        cap.assert_not_called()


class HandleReactionEventShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_155_payload_uses_top_level_counters(self):
        # Opcode 155 carries counters flat (no reactionInfo nesting).
        b = make_bridge()
        b._client = object()
        b._remember(500, 555, "m1", "A", -100222, 42, "text")
        packet = {"opcode": 155, "payload": {
            "chatId": 555, "messageId": "m1",
            "counters": [{"reaction": "👍", "count": 4}]}}
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.set_message_reaction") as react:
            await b._handle_reaction_event(packet)
        react.assert_called_once()
        self.assertEqual(react.call_args.args[3], "👍")

    async def test_missing_message_id_is_ignored(self):
        b = make_bridge()
        packet = {"opcode": 156, "payload": {"chatId": 555}}
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.set_message_reaction") as react:
            await b._handle_reaction_event(packet)
        react.assert_not_called()

    async def test_non_dict_payload_is_ignored(self):
        b = make_bridge()
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.set_message_reaction") as react:
            await b._handle_reaction_event({"opcode": 156, "payload": None})
        react.assert_not_called()


class HandleDeleteEventShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_message_id_field_is_wrapped(self):
        # A delete frame using the singular messageId field must still mirror.
        b = make_bridge()
        b._remember(500, 555, "m1", "A", -100222, 42, "text")
        packet = {"opcode": 142, "payload": {"chatId": 555, "messageId": "m1"}}
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.delete_message", return_value=True) as dele:
            await b._handle_delete_event(packet)
        dele.assert_called_once()

    async def test_range_delete_without_ids_is_noop(self):
        # A 140 range-delete (no explicit ids) is captured but not mirrored.
        b = make_bridge()
        packet = {"opcode": 140, "payload": {"chatId": 555}}
        with patch("bridge._log_event_frame"), \
                patch("bridge.tg.delete_message") as dele:
            await b._handle_delete_event(packet)
        dele.assert_not_called()


if __name__ == "__main__":
    unittest.main()
