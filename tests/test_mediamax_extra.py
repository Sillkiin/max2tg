import unittest
from unittest.mock import AsyncMock, Mock, patch

import mediamax


class ResolveFileUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_url_and_uses_file_opcode(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"url": "https://cdn/file?expires=1"}})

        url = await mediamax.resolve_file_url(client, "fid", "cid", "mid")

        self.assertEqual(url, "https://cdn/file?expires=1")
        client.invoke_method.assert_awaited_once_with(
            opcode=mediamax.FILE_RESOLVE_OPCODE,
            payload={"fileId": "fid", "chatId": "cid", "messageId": "mid"})

    async def test_raises_when_no_url(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {"other": 1}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.resolve_file_url(client, 1, 2, 3)
        self.assertIn("file resolve returned no url", str(ctx.exception))

    async def test_raises_when_payload_missing(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={})

        with self.assertRaises(RuntimeError):
            await mediamax.resolve_file_url(client, 1, 2, 3)


class ResolveVideoUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_picks_highest_resolution_mp4(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "MP4_240": "https://cdn/240",
            "MP4_1080": "https://cdn/1080",
            "MP4_480": "https://cdn/480",
            "thumb": "https://cdn/thumb",
        }})

        url = await mediamax.resolve_video_url(client, "vid", "cid", "mid")

        self.assertEqual(url, "https://cdn/1080")
        client.invoke_method.assert_awaited_once_with(
            opcode=mediamax.VIDEO_RESOLVE_OPCODE,
            payload={"videoId": "vid", "chatId": "cid", "messageId": "mid"})

    async def test_ignores_non_integer_height_keys(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "MP4_HD": "https://cdn/bad",  # non-int suffix -> skipped
            "MP4_360": "https://cdn/360",
        }})

        url = await mediamax.resolve_video_url(client, 1, 2, 3)

        self.assertEqual(url, "https://cdn/360")

    async def test_ignores_non_string_values(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "MP4_720": {"nested": "notastring"},  # non-str value -> skipped
            "MP4_144": "https://cdn/144",
        }})

        url = await mediamax.resolve_video_url(client, 1, 2, 3)

        self.assertEqual(url, "https://cdn/144")

    async def test_raises_when_no_mp4_source(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "MP4_HD": "https://cdn/bad",  # only invalid suffixes
            "preview": "https://cdn/p",
        }})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.resolve_video_url(client, 1, 2, 3)
        self.assertIn("video resolve returned no MP4 source", str(ctx.exception))


class ResolveAudioUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_opus_returns_audio_ogg(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "opus": "https://cdn/voice.opus",
            "m4a": "https://cdn/voice.m4a",
            "mp3": "https://cdn/voice.mp3",
        }})

        url, mime = await mediamax.resolve_audio_url(client, "aid", "cid", "mid")

        self.assertEqual(url, "https://cdn/voice.opus")
        self.assertEqual(mime, "audio/ogg")
        client.invoke_method.assert_awaited_once_with(
            opcode=mediamax.AUDIO_RESOLVE_OPCODE,
            payload={"audioId": "aid", "chatId": "cid", "messageId": "mid"})

    async def test_falls_back_to_m4a(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "m4a": "https://cdn/voice.m4a",
            "mp3": "https://cdn/voice.mp3",
        }})

        url, mime = await mediamax.resolve_audio_url(client, 1, 2, 3)

        self.assertEqual(url, "https://cdn/voice.m4a")
        self.assertEqual(mime, "audio/mp4")

    async def test_falls_back_to_mp3(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "mp3": "https://cdn/voice.mp3",
        }})

        url, mime = await mediamax.resolve_audio_url(client, 1, 2, 3)

        self.assertEqual(url, "https://cdn/voice.mp3")
        self.assertEqual(mime, "audio/mpeg")

    async def test_includes_token_in_payload_when_provided(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "opus": "https://cdn/voice.opus"}})

        await mediamax.resolve_audio_url(client, "aid", "cid", "mid", token="TK")

        client.invoke_method.assert_awaited_once_with(
            opcode=mediamax.AUDIO_RESOLVE_OPCODE,
            payload={"audioId": "aid", "chatId": "cid",
                     "messageId": "mid", "token": "TK"})

    async def test_skips_non_string_and_empty_sources(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "opus": "",          # empty -> skipped
            "m4a": 12345,        # non-str -> skipped
            "mp3": "https://cdn/voice.mp3",
        }})

        url, mime = await mediamax.resolve_audio_url(client, 1, 2, 3)

        self.assertEqual(url, "https://cdn/voice.mp3")
        self.assertEqual(mime, "audio/mpeg")

    async def test_raises_when_no_source(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {"junk": 1}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.resolve_audio_url(client, 1, 2, 3)
        self.assertIn("audio resolve returned no source", str(ctx.exception))

    async def test_non_dict_response_raises(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value="unexpected")

        with self.assertRaises(RuntimeError):
            await mediamax.resolve_audio_url(client, 1, 2, 3)


class UploadBytesTests(unittest.TestCase):
    def test_raises_on_bad_status_with_x_reason_header(self):
        response = Mock(status_code=500,
                        headers={"X-Reason": "boom"}, text="ignored body")

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                mediamax._upload_bytes("https://up", b"data", "f.bin", "text/plain")
        self.assertIn("MAX upload failed: 500 boom", str(ctx.exception))

    def test_raises_on_bad_status_falls_back_to_body_text(self):
        response = Mock(status_code=403, headers={}, text="forbidden detail")

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                mediamax._upload_bytes("https://up", b"data", "f.bin", "text/plain")
        self.assertIn("403 forbidden detail", str(ctx.exception))

    def test_returns_empty_dict_when_body_not_json(self):
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(side_effect=ValueError("no json"))

        with patch("mediamax.requests.post", return_value=response):
            result = mediamax._upload_bytes("https://up", b"d", "f.bin", "text/plain")
        self.assertEqual(result, {})

    def test_returns_parsed_json_body(self):
        response = Mock(status_code=201, headers={}, text="")
        response.json = Mock(return_value={"ok": 1})

        with patch("mediamax.requests.post", return_value=response):
            result = mediamax._upload_bytes("https://up", b"d", "f.bin", "text/plain")
        self.assertEqual(result, {"ok": 1})

    def test_default_content_type_when_mime_blank(self):
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={})

        with patch("mediamax.requests.post", return_value=response) as post:
            mediamax._upload_bytes("https://up", b"abc", "f.bin", "")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["Content-Range"], "0-2/3")


class SlotTests(unittest.TestCase):
    def test_returns_first_info_entry_when_list(self):
        payload = {"info": [{"url": "a"}, {"url": "b"}]}
        self.assertEqual(mediamax._slot(payload), {"url": "a"})

    def test_returns_payload_when_no_info_list(self):
        payload = {"url": "top"}
        self.assertEqual(mediamax._slot(payload), payload)

    def test_returns_payload_when_info_empty_list(self):
        payload = {"info": [], "url": "top"}
        self.assertEqual(mediamax._slot(payload), payload)

    def test_returns_payload_when_info_first_not_dict(self):
        payload = {"info": ["notadict"], "url": "top"}
        self.assertEqual(mediamax._slot(payload), payload)


class UploadFileErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_when_slot_returns_no_info(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {"info": []}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.upload_file(client, b"x", "f.bin")
        self.assertIn("file upload slot returned no info", str(ctx.exception))

    async def test_raises_when_slot_incomplete_missing_url(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"fileId": 9}]}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.upload_file(client, b"x", "f.bin")
        self.assertIn("file upload slot is incomplete", str(ctx.exception))

    async def test_raises_when_slot_incomplete_missing_file_id(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"url": "https://up"}]}})

        with self.assertRaises(RuntimeError):
            await mediamax.upload_file(client, b"x", "f.bin")

    async def test_guesses_mime_from_filename_extension(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"fileId": 3, "url": "https://up"}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={})

        with patch("mediamax.requests.post", return_value=response) as post:
            file_id = await mediamax.upload_file(client, b"x", "doc.pdf")

        self.assertEqual(file_id, 3)
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"],
                         "application/pdf")


class InvokeSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_on_non_ready_error(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"error": "permission.denied"}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax._invoke_send(client, {"chatId": 1}, "FILE")
        self.assertIn("MAX rejected FILE message", str(ctx.exception))

    async def test_raises_on_error_code(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"error_code": 42}})

        with self.assertRaises(RuntimeError):
            await mediamax._invoke_send(client, {"chatId": 1}, "VIDEO")

    async def test_returns_response_on_success(self):
        client = Mock()
        ok = {"payload": {"messageId": "m99"}}
        client.invoke_method = AsyncMock(return_value=ok)

        result = await mediamax._invoke_send(client, {"chatId": 1}, "PHOTO")

        self.assertIs(result, ok)
        client.invoke_method.assert_awaited_once_with(
            opcode=mediamax.SEND_MESSAGE_OPCODE, payload={"chatId": 1})

    async def test_returns_response_when_payload_not_dict(self):
        client = Mock()
        # payload is not a dict -> error checks are skipped, response returned.
        ok = {"payload": "stringy"}
        client.invoke_method = AsyncMock(return_value=ok)

        result = await mediamax._invoke_send(client, {"chatId": 1}, "FILE")
        self.assertIs(result, ok)

    async def test_returns_response_when_response_not_dict(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value="not-a-dict")

        result = await mediamax._invoke_send(client, {"chatId": 1}, "FILE")
        self.assertEqual(result, "not-a-dict")

    async def test_retries_until_ready_then_returns(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"error": mediamax.ATTACHMENT_NOT_READY}},
            {"payload": {"error": mediamax.ATTACHMENT_NOT_READY}},
            {"payload": {"ok": True}},
        ])

        with patch("mediamax.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await mediamax._invoke_send(client, {"chatId": 1}, "FILE")

        self.assertEqual(result, {"payload": {"ok": True}})
        self.assertEqual(client.invoke_method.await_count, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_raises_after_exhausting_retries(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"error": mediamax.ATTACHMENT_NOT_READY}})

        with patch("mediamax.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                await mediamax._invoke_send(client, {"chatId": 1}, "FILE")

        self.assertIn("not ready after", str(ctx.exception))
        self.assertEqual(client.invoke_method.await_count, mediamax.SEND_RETRIES)
        self.assertEqual(sleep.await_count, mediamax.SEND_RETRIES)


class UploadMultipartTests(unittest.TestCase):
    def test_raises_on_bad_status_with_x_reason(self):
        response = Mock(status_code=400, headers={"X-Reason": "bad"}, text="body")

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                mediamax._upload_multipart("https://up", b"img", "p.jpg", "image/jpeg")
        self.assertIn("MAX photo upload failed: 400 bad", str(ctx.exception))

    def test_raises_on_bad_status_falls_back_to_text(self):
        response = Mock(status_code=500, headers={}, text="server error text")

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                mediamax._upload_multipart("https://up", b"img", "p.jpg", "image/jpeg")
        self.assertIn("500 server error text", str(ctx.exception))

    def test_returns_empty_dict_when_not_json(self):
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(side_effect=ValueError("nope"))

        with patch("mediamax.requests.post", return_value=response):
            result = mediamax._upload_multipart("https://up", b"i", "p.jpg", "image/jpeg")
        self.assertEqual(result, {})

    def test_returns_json_body_and_default_mime(self):
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={"photos": {}})

        with patch("mediamax.requests.post", return_value=response) as post:
            result = mediamax._upload_multipart("https://up", b"i", "p.jpg", "")

        self.assertEqual(result, {"photos": {}})
        # mime blank -> default image/jpeg used in the files tuple
        files = post.call_args.kwargs["files"]
        self.assertEqual(files["file"][2], "image/jpeg")


class ExtractPhotoTokenTests(unittest.TestCase):
    def test_dict_shape_returns_token(self):
        body = {"photos": {"p1": {"token": "T1"}}}
        self.assertEqual(mediamax._extract_photo_token(body), "T1")

    def test_list_shape_returns_token(self):
        body = {"photos": [{"token": "L1"}]}
        self.assertEqual(mediamax._extract_photo_token(body), "L1")

    def test_list_shape_skips_entries_without_token(self):
        body = {"photos": [{"nope": 1}, "junk", {"token": "L2"}]}
        self.assertEqual(mediamax._extract_photo_token(body), "L2")

    def test_dict_shape_skips_entries_without_token(self):
        body = {"photos": {"a": {"x": 1}, "b": {"token": "T2"}}}
        self.assertEqual(mediamax._extract_photo_token(body), "T2")

    def test_returns_none_when_no_photos(self):
        self.assertIsNone(mediamax._extract_photo_token({}))

    def test_returns_none_for_unexpected_shape(self):
        self.assertIsNone(mediamax._extract_photo_token({"photos": "string"}))


class UploadPhotoErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_when_slot_has_no_url(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"nope": 1}]}})

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.upload_photo(client, b"i", "p.jpg")
        self.assertIn("photo upload slot has no url", str(ctx.exception))

    async def test_raises_on_error_code_in_body(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"url": "https://up"}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={"error_code": 7})

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                await mediamax.upload_photo(client, b"i", "p.jpg")
        self.assertIn("photo upload error", str(ctx.exception))

    async def test_raises_when_no_token_returned(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"url": "https://up"}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={"photos": {}})

        with patch("mediamax.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                await mediamax.upload_photo(client, b"i", "p.jpg")
        self.assertIn("photo upload returned no token", str(ctx.exception))

    async def test_uses_top_level_url_when_slot_lacks_one(self):
        client = Mock()
        # _slot returns payload itself (no info list); url at top level.
        client.invoke_method = AsyncMock(
            return_value={"payload": {"url": "https://top-url"}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={"photos": {"p": {"token": "TK"}}})

        with patch("mediamax.requests.post", return_value=response) as post:
            token = await mediamax.upload_photo(client, b"i", "p.jpg")

        self.assertEqual(token, "TK")
        self.assertEqual(post.call_args.args[0], "https://top-url")


class UploadVideoErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_when_slot_incomplete(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"token": "T"}]}})  # no url/videoId

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.upload_video(client, b"v", "v.mp4")
        self.assertIn("video upload slot is incomplete", str(ctx.exception))

    async def test_returns_video_id_and_token(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "info": [{"url": "https://up", "videoId": 55, "token": "VT"}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={})

        with patch("mediamax.requests.post", return_value=response):
            video_id, token = await mediamax.upload_video(client, b"v", "v.mp4")

        self.assertEqual((video_id, token), (55, "VT"))


class UploadAudioErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_when_slot_incomplete(self):
        client = Mock()
        client.invoke_method = AsyncMock(
            return_value={"payload": {"info": [{"token": "T"}]}})  # no url/audioId

        with self.assertRaises(RuntimeError) as ctx:
            await mediamax.upload_audio(client, b"a", "voice.ogg")
        self.assertIn("audio upload slot is incomplete", str(ctx.exception))

    async def test_uses_audio_type_slot_and_returns_ids(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "info": [{"url": "https://up", "audioId": 88, "token": "AT"}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={})

        with patch("mediamax.requests.post", return_value=response):
            audio_id, token = await mediamax.upload_audio(client, b"a", "voice.ogg")

        self.assertEqual((audio_id, token), (88, "AT"))
        slot_call = client.invoke_method.await_args
        self.assertEqual(slot_call.kwargs["opcode"], mediamax.VIDEO_UPLOAD_SLOT_OPCODE)
        self.assertEqual(slot_call.kwargs["payload"]["type"], mediamax.AUDIO_UPLOAD_TYPE)

    async def test_falls_back_to_video_id_when_no_audio_id(self):
        client = Mock()
        client.invoke_method = AsyncMock(return_value={"payload": {
            "info": [{"url": "https://up", "videoId": 7}]}})
        response = Mock(status_code=200, headers={}, text="")
        response.json = Mock(return_value={})

        with patch("mediamax.requests.post", return_value=response):
            audio_id, token = await mediamax.upload_audio(client, b"a", "voice.ogg")

        self.assertEqual(audio_id, 7)
        self.assertIsNone(token)


if __name__ == "__main__":
    unittest.main()
