import unittest
from unittest.mock import AsyncMock, Mock, patch

import mediamax


class MediaMaxUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_uploaded_file_uses_upload_slot_and_file_attach(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"info": [{"fileId": 777, "url": "https://upload.example/file"}]}},
            {"payload": {"ok": True}},
        ])
        response = Mock(status_code=201, headers={}, text="")

        with patch("mediamax.requests.post", return_value=response) as post:
            await mediamax.send_uploaded_file(
                client,
                555,
                b"content",
                "sticker.webp",
                "image/webp",
                text="caption",
                reply_to_message_id="m1",
            )

        client.invoke_method.assert_any_await(opcode=mediamax.FILE_UPLOAD_SLOT_OPCODE, payload={"count": 1})
        post.assert_called_once()
        send_call = client.invoke_method.await_args_list[-1]
        self.assertEqual(send_call.kwargs["opcode"], mediamax.SEND_MESSAGE_OPCODE)
        message = send_call.kwargs["payload"]["message"]
        self.assertEqual(message["text"], "caption")
        self.assertEqual(message["attaches"], [{"_type": "FILE", "fileId": 777}])
        self.assertEqual(message["link"]["messageId"], "m1")


class MediaUploadDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_uses_photo_slot_and_photo_token_attach(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"info": [{"url": "https://upload.example/photo"}]}},
            {"payload": {"ok": True}},
        ])
        # The multipart upload body carries the photoToken; `photos` is a dict
        # keyed by photoId.
        post = Mock(status_code=200, headers={}, text="",
                    json=Mock(return_value={"photos": {"p1": {"token": "PT42"}}}))

        with patch("mediamax.requests.post", return_value=post):
            await mediamax.send_uploaded_media(
                client, 999, b"img", "telegram-photo.jpg", "image/jpeg",
                kind="photo", text="cap")

        slot_call = client.invoke_method.await_args_list[0]
        self.assertEqual(slot_call.kwargs["opcode"], mediamax.PHOTO_UPLOAD_SLOT_OPCODE)
        send_call = client.invoke_method.await_args_list[-1]
        attach = send_call.kwargs["payload"]["message"]["attaches"][0]
        self.assertEqual(attach, {"_type": "PHOTO", "photoToken": "PT42"})

    async def test_video_uses_video_slot_and_video_attach(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"info": [{"url": "https://up/v", "videoId": 7, "token": "VT"}]}},
            {"payload": {"ok": True}},
        ])
        post = Mock(status_code=200, headers={}, text="",
                    json=Mock(return_value={}))

        with patch("mediamax.requests.post", return_value=post):
            await mediamax.send_uploaded_media(
                client, 999, b"vid", "telegram-video.mp4", "video/mp4",
                kind="video")

        slot_call = client.invoke_method.await_args_list[0]
        self.assertEqual(slot_call.kwargs["opcode"], mediamax.VIDEO_UPLOAD_SLOT_OPCODE)
        attach = client.invoke_method.await_args_list[-1].kwargs["payload"]["message"]["attaches"][0]
        self.assertEqual(attach, {"_type": "VIDEO", "videoId": 7, "token": "VT"})

    async def test_send_retries_while_attachment_not_ready(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"info": [{"fileId": 5, "url": "https://up/f"}]}},  # slot
            {"payload": {"error": "attachment.not.ready"}},  # send try 1
            {"payload": {"ok": True}},  # send try 2 succeeds
        ])
        post = Mock(status_code=200, headers={}, text="", json=Mock(return_value={}))
        with patch("mediamax.requests.post", return_value=post), \
                patch("mediamax.asyncio.sleep", new=AsyncMock()):
            await mediamax.send_uploaded_media(
                client, 1, b"x", "f.bin", "application/octet-stream", kind="file")
        self.assertEqual(client.invoke_method.await_count, 3)  # slot + 2 sends

    async def test_file_kind_still_uses_file_attach(self):
        client = Mock()
        client.invoke_method = AsyncMock(side_effect=[
            {"payload": {"info": [{"fileId": 11, "url": "https://up/f"}]}},
            {"payload": {"ok": True}},
        ])
        post = Mock(status_code=201, headers={}, text="", json=Mock(return_value={}))
        with patch("mediamax.requests.post", return_value=post):
            await mediamax.send_uploaded_media(
                client, 999, b"doc", "report.pdf", "application/pdf", kind="file")
        attach = client.invoke_method.await_args_list[-1].kwargs["payload"]["message"]["attaches"][0]
        self.assertEqual(attach, {"_type": "FILE", "fileId": 11})


class ContentDispositionTests(unittest.TestCase):
    def test_cyrillic_filename_is_header_safe(self):
        header = mediamax._content_disposition("Фёдор приёмка.jpg")
        # Must be latin-1 encodable (HTTP header requirement) — this is what
        # previously crashed requests with a 'latin-1' codec error.
        header.encode("latin-1")
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%D1%91", header)  # percent-encoded 'ё'

    def test_blank_filename_falls_back(self):
        header = mediamax._content_disposition("ёжик")
        header.encode("latin-1")
        self.assertIn('filename="file"', header)

    def test_ascii_filename_preserved(self):
        header = mediamax._content_disposition("report.pdf")
        self.assertIn('filename="report.pdf"', header)


if __name__ == "__main__":
    unittest.main()
