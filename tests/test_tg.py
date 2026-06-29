"""Unit tests for the Telegram client wrappers in tg.py.

tg.py is a thin layer over a single HTTP chokepoint (`_call` -> requests.post),
so these tests fake that layer and assert each wrapper builds the correct
Telegram method + params and parses the response. No real network is touched.
"""
import unittest
from unittest.mock import MagicMock, patch

import tg


def _resp(result, ok=True):
    """A fake requests.Response whose .json() mimics the Telegram envelope."""
    r = MagicMock()
    r.json.return_value = {"ok": ok, "result": result}
    return r


class TgLowLevelTests(unittest.TestCase):
    def test_call_posts_json_and_returns_result(self):
        with patch("tg.requests") as req:
            req.post.return_value = _resp({"message_id": 5})
            out = tg._call("T", "sendMessage", chat_id=1, text="hi")
        self.assertEqual(out, {"message_id": 5})
        self.assertIn("/botT/sendMessage", req.post.call_args.args[0])
        self.assertEqual(req.post.call_args.kwargs["json"],
                         {"chat_id": 1, "text": "hi"})

    def test_call_raises_when_not_ok(self):
        with patch("tg.requests") as req:
            req.post.return_value = _resp(None, ok=False)
            with self.assertRaises(RuntimeError):
                tg._call("T", "sendMessage")

    def test_call_upload_sends_files_and_returns_result(self):
        with patch("tg.requests") as req:
            req.post.return_value = _resp({"message_id": 9})
            out = tg._call_upload("T", "sendPhoto",
                                  {"photo": ("f", b"x")}, chat_id=1)
        self.assertEqual(out, {"message_id": 9})
        self.assertIn("files", req.post.call_args.kwargs)

    def test_call_upload_raises_when_not_ok(self):
        with patch("tg.requests") as req:
            req.post.return_value = _resp(None, ok=False)
            with self.assertRaises(RuntimeError):
                tg._call_upload("T", "sendPhoto", {"photo": ("f", b"x")})


class TgWrapperTests(unittest.TestCase):
    def test_check_token_calls_get_me(self):
        with patch("tg._call", return_value={"id": 1}) as c:
            tg.check_token("T")
        self.assertEqual(c.call_args.args[1], "getMe")

    def test_set_my_commands(self):
        cmds = [{"command": "join", "description": "x"}]
        with patch("tg._call") as c:
            tg.set_my_commands("T", cmds)
        self.assertEqual(c.call_args.args[1], "setMyCommands")
        self.assertEqual(c.call_args.kwargs["commands"], cmds)

    def test_get_updates_offset_allowed_updates_and_timeout(self):
        with patch("tg._call", return_value=[{"update_id": 1}]) as c:
            out = tg.get_updates("T", offset=10, timeout=25)
        self.assertEqual(out, [{"update_id": 1}])
        self.assertEqual(c.call_args.args[1], "getUpdates")
        self.assertEqual(c.call_args.kwargs["_timeout"], (5, 40))  # read > poll
        self.assertEqual(c.call_args.kwargs["offset"], 10)
        self.assertIn("message_reaction", c.call_args.kwargs["allowed_updates"])

    def test_get_updates_omits_offset_when_none(self):
        with patch("tg._call", return_value=[]) as c:
            tg.get_updates("T")
        self.assertNotIn("offset", c.call_args.kwargs)

    def test_get_file(self):
        with patch("tg._call", return_value={"file_path": "a/b"}) as c:
            tg.get_file("T", "FID")
        self.assertEqual(c.call_args.args[1], "getFile")
        self.assertEqual(c.call_args.kwargs["file_id"], "FID")

    def test_create_forum_topic_returns_thread_id(self):
        with patch("tg._call", return_value={"message_thread_id": 12}):
            self.assertEqual(tg.create_forum_topic("T", -100, "Name"), 12)

    def test_edit_forum_topic(self):
        with patch("tg._call") as c:
            tg.edit_forum_topic("T", -100, 12, "New")
        self.assertEqual(c.call_args.args[1], "editForumTopic")
        self.assertEqual(c.call_args.kwargs["message_thread_id"], 12)
        self.assertEqual(c.call_args.kwargs["name"], "New")

    def test_send_message_returns_first_id_and_sets_params(self):
        with patch("tg._call", return_value={"message_id": 100}) as c:
            out = tg.send_message("T", 1, "hi", reply_to_message_id=55,
                                  message_thread_id=7)
        self.assertEqual(out, 100)
        self.assertEqual(c.call_count, 1)
        kw = c.call_args.kwargs
        self.assertTrue(kw["disable_web_page_preview"])
        self.assertEqual(kw["message_thread_id"], 7)
        self.assertEqual(kw["reply_to_message_id"], 55)

    def test_send_message_empty_returns_none_without_call(self):
        with patch("tg._call") as c:
            self.assertIsNone(tg.send_message("T", 1, ""))
        c.assert_not_called()

    def test_send_message_splits_long_text_reply_only_on_first(self):
        with patch("tg._call",
                   side_effect=[{"message_id": 100}, {"message_id": 101}]) as c:
            out = tg.send_message("T", 1, "a" * 5000, reply_to_message_id=55)
        self.assertEqual(out, 100)               # id of the FIRST chunk
        self.assertEqual(c.call_count, 2)        # 5000 > 4096 -> two chunks
        self.assertEqual(c.call_args_list[0].kwargs.get("reply_to_message_id"), 55)
        self.assertNotIn("reply_to_message_id", c.call_args_list[1].kwargs)

    def test_edit_message_text(self):
        with patch("tg._call") as c:
            self.assertTrue(tg.edit_message_text("T", 1, 2, "new"))
        self.assertEqual(c.call_args.args[1], "editMessageText")

    def test_edit_message_text_empty_returns_false_without_call(self):
        with patch("tg._call") as c:
            self.assertFalse(tg.edit_message_text("T", 1, 2, ""))
        c.assert_not_called()

    def test_edit_message_caption(self):
        with patch("tg._call") as c:
            self.assertTrue(tg.edit_message_caption("T", 1, 2, "cap"))
        self.assertEqual(c.call_args.args[1], "editMessageCaption")

    def test_delete_message(self):
        with patch("tg._call", return_value=True) as c:
            self.assertTrue(tg.delete_message("T", 1, 2))
        self.assertEqual(c.call_args.args[1], "deleteMessage")
        self.assertEqual(c.call_args.kwargs["message_id"], 2)

    def test_set_message_reaction_with_emoji(self):
        with patch("tg._call") as c:
            tg.set_message_reaction("T", 1, 2, "👍")
        self.assertEqual(c.call_args.kwargs["reaction"],
                         [{"type": "emoji", "emoji": "👍"}])

    def test_set_message_reaction_clear(self):
        with patch("tg._call") as c:
            tg.set_message_reaction("T", 1, 2, None)
        self.assertEqual(c.call_args.kwargs["reaction"], [])


class TgMediaTests(unittest.TestCase):
    def test_send_photo_url_path(self):
        with patch("tg._call", return_value={"message_id": 7}) as c:
            out = tg.send_photo("T", 1, "https://cdn/x.jpg", "cap",
                                message_thread_id=5)
        self.assertEqual(out, 7)
        self.assertEqual(c.call_args.args[1], "sendPhoto")
        self.assertEqual(c.call_args.kwargs["photo"], "https://cdn/x.jpg")
        self.assertEqual(c.call_args.kwargs["caption"], "cap")
        self.assertEqual(c.call_args.kwargs["message_thread_id"], 5)

    def test_send_voice_uses_send_voice_method(self):
        with patch("tg._call", return_value={"message_id": 8}) as c:
            tg.send_voice("T", 1, "https://cdn/v.ogg")
        self.assertEqual(c.call_args.args[1], "sendVoice")
        self.assertEqual(c.call_args.kwargs["voice"], "https://cdn/v.ogg")

    def test_send_media_falls_back_to_upload_when_url_send_fails(self):
        with patch("tg._call", side_effect=RuntimeError("blocked")), \
                patch("tg._download", return_value=b"bytes"), \
                patch("tg._call_upload", return_value={"message_id": 9}) as up:
            out = tg.send_audio("T", 1, "https://cdn/a.mp3", "cap")
        self.assertEqual(out, 9)
        up.assert_called_once()
        self.assertEqual(up.call_args.args[1], "sendAudio")

    def test_send_sticker_url_path(self):
        with patch("tg._call", return_value={"message_id": 3}) as c:
            self.assertEqual(tg.send_sticker("T", 1, "https://cdn/s.webp"), 3)
        self.assertEqual(c.call_args.args[1], "sendSticker")

    def test_send_sticker_falls_back_to_document(self):
        with patch("tg._call", side_effect=RuntimeError("nope")), \
                patch("tg.send_document", return_value=42) as doc:
            self.assertEqual(tg.send_sticker("T", 1, "https://cdn/s.webp"), 42)
        doc.assert_called_once()


class TgUrlGuardTests(unittest.TestCase):
    def test_allows_public_hostname(self):
        tg._assert_public_url("https://example.com/file.jpg")  # no raise

    def test_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            tg._assert_public_url("ftp://example.com/x")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(ValueError):
            tg._assert_public_url("http://127.0.0.1/x")

    def test_rejects_link_local_metadata_ip(self):
        with self.assertRaises(ValueError):
            tg._assert_public_url("http://169.254.169.254/latest/meta-data/")


if __name__ == "__main__":
    unittest.main()
