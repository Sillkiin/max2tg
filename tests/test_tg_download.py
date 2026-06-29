"""Unit tests for the streaming/download paths in tg.py.

These cover the parts NOT exercised by test_tg.py: _download (manual redirect
re-validation, Content-Length cap, mid-stream cap, too-many-redirects),
download_file_by_id (getFile -> stream, size caps, missing file_path), the
threaded fallback-upload branch in _send_media, and the remaining one-line
URL-path branches in send_animation/send_video/send_document/send_sticker.

requests is faked entirely: no real network/WebSocket is touched.
"""
import unittest
from unittest.mock import MagicMock, patch

import requests as real_requests

import tg


def _stream_resp(chunks=(), *, content_length=None, is_redirect=False,
                 location=None):
    """A fake streaming requests.Response usable as a context manager.

    Mimics the bits tg._download / download_file_by_id touch: .headers,
    .is_redirect, .raise_for_status(), .iter_content() and the with-block
    protocol (__enter__/__exit__).
    """
    resp = MagicMock()
    headers = {}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if location is not None:
        headers["Location"] = location
    resp.headers = headers
    resp.is_redirect = is_redirect
    resp.raise_for_status.return_value = None
    resp.iter_content.return_value = iter(chunks)
    # Used as `with requests.get(...) as response:` -> __enter__ returns resp.
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class DownloadTests(unittest.TestCase):
    """tg._download: validate each hop, stream bytes, enforce the size cap."""

    def test_returns_joined_chunks_on_direct_200(self):
        resp = _stream_resp([b"ab", b"cd"], content_length=4)
        with patch("tg.requests") as req:
            req.get.return_value = resp
            out = tg._download("https://cdn.example.com/file.bin")
        self.assertEqual(out, b"abcd")
        # Streamed, no redirects followed, browser UA + manual redirect handling.
        kwargs = req.get.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["User-Agent"], tg.BROWSER_UA)
        self.assertEqual(kwargs["timeout"], tg.UPLOAD_TIMEOUT)
        resp.raise_for_status.assert_called_once()

    def test_follows_redirect_and_revalidates_each_hop(self):
        redirect = _stream_resp(is_redirect=True,
                                location="https://cdn2.example.com/real.bin")
        final = _stream_resp([b"payload"], content_length=7)
        # Use the real urljoin so the relative/absolute join logic is exercised.
        with patch("tg.requests") as req:
            req.get.side_effect = [redirect, final]
            req.compat.urljoin = real_requests.compat.urljoin
            with patch("tg._assert_public_url") as guard:
                out = tg._download("https://cdn.example.com/start.bin")
        self.assertEqual(out, b"payload")
        self.assertEqual(req.get.call_count, 2)
        # Every hop (start URL + redirect target) is re-validated.
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(guard.call_args_list[0].args[0],
                         "https://cdn.example.com/start.bin")
        self.assertEqual(guard.call_args_list[1].args[0],
                         "https://cdn2.example.com/real.bin")
        # A redirect response is never validated as a body.
        redirect.raise_for_status.assert_not_called()

    def test_relative_redirect_location_is_joined_against_current_url(self):
        redirect = _stream_resp(is_redirect=True, location="/moved/here.bin")
        final = _stream_resp([b"x"], content_length=1)
        with patch("tg.requests") as req:
            req.get.side_effect = [redirect, final]
            req.compat.urljoin = real_requests.compat.urljoin
            with patch("tg._assert_public_url") as guard:
                tg._download("https://cdn.example.com/a/b.bin")
        # Relative Location resolved against the previous absolute URL.
        self.assertEqual(guard.call_args_list[1].args[0],
                         "https://cdn.example.com/moved/here.bin")

    def test_redirect_without_location_is_treated_as_body(self):
        # is_redirect True but no Location header -> falls through to body read.
        resp = _stream_resp([b"data"], content_length=4, is_redirect=True)
        with patch("tg.requests") as req:
            req.get.return_value = resp
            out = tg._download("https://cdn.example.com/x.bin")
        self.assertEqual(out, b"data")
        resp.raise_for_status.assert_called_once()

    def test_rejects_when_declared_content_length_exceeds_cap(self):
        resp = _stream_resp(content_length=tg.DOWNLOAD_SIZE_LIMIT + 1)
        with patch("tg.requests") as req:
            req.get.return_value = resp
            with self.assertRaises(ValueError) as ctx:
                tg._download("https://cdn.example.com/huge.bin")
        self.assertIn("too large", str(ctx.exception))
        # Cap hit before any streaming.
        resp.iter_content.assert_not_called()

    def test_missing_content_length_defaults_to_zero_and_streams(self):
        # No Content-Length header -> `int(... or 0)` -> 0, not an error.
        resp = _stream_resp([b"hi"])
        with patch("tg.requests") as req:
            req.get.return_value = resp
            out = tg._download("https://cdn.example.com/x.bin")
        self.assertEqual(out, b"hi")

    def test_rejects_when_stream_exceeds_cap_mid_download(self):
        # Content-Length lies (or is absent); the running total trips the cap.
        big = b"a" * (tg.DOWNLOAD_SIZE_LIMIT + 1)
        resp = _stream_resp([big])
        with patch("tg.requests") as req:
            req.get.return_value = resp
            with self.assertRaises(ValueError) as ctx:
                tg._download("https://cdn.example.com/x.bin")
        self.assertIn("exceeded size limit", str(ctx.exception))

    def test_too_many_redirects_raises(self):
        # Always redirect -> loop exhausts MAX_REDIRECTS+1 iterations.
        def _redir(*_a, **_k):
            return _stream_resp(is_redirect=True,
                                location="https://cdn.example.com/again.bin")

        with patch("tg.requests") as req:
            req.get.side_effect = _redir
            req.compat.urljoin = real_requests.compat.urljoin
            with patch("tg._assert_public_url"):
                with self.assertRaises(ValueError) as ctx:
                    tg._download("https://cdn.example.com/start.bin")
        self.assertIn("too many redirects", str(ctx.exception))
        self.assertEqual(req.get.call_count, tg.MAX_REDIRECTS + 1)

    def test_propagates_assert_public_url_rejection(self):
        # Guard failure on the first hop aborts before any HTTP request.
        with patch("tg.requests") as req:
            with patch("tg._assert_public_url",
                       side_effect=ValueError("blocked non-public address")):
                with self.assertRaises(ValueError):
                    tg._download("http://127.0.0.1/x")
        req.get.assert_not_called()

    def test_raise_for_status_error_propagates(self):
        resp = _stream_resp(content_length=4)
        resp.raise_for_status.side_effect = real_requests.HTTPError("404")
        with patch("tg.requests") as req:
            req.get.return_value = resp
            with self.assertRaises(real_requests.HTTPError):
                tg._download("https://cdn.example.com/x.bin")


class DownloadFileByIdTests(unittest.TestCase):
    """tg.download_file_by_id: getFile -> build file URL -> stream with cap."""

    def test_streams_and_returns_bytes_and_path(self):
        resp = _stream_resp([b"ab", b"cd"])
        with patch("tg.get_file",
                   return_value={"file_path": "docs/a.bin", "file_size": 4}) as gf, \
                patch("tg.requests") as req:
            req.get.return_value = resp
            data, path = tg.download_file_by_id("TOKEN", "FID")
        self.assertEqual(data, b"abcd")
        self.assertEqual(path, "docs/a.bin")
        gf.assert_called_once_with("TOKEN", "FID")
        # URL is built from the file API base with the token + path.
        url = req.get.call_args.args[0]
        self.assertIn("/file/botTOKEN/docs/a.bin", url)
        self.assertTrue(req.get.call_args.kwargs["stream"])

    def test_rejects_when_declared_file_size_exceeds_cap(self):
        big = tg.DOWNLOAD_SIZE_LIMIT + 1
        with patch("tg.get_file",
                   return_value={"file_path": "x", "file_size": big}), \
                patch("tg.requests") as req:
            with self.assertRaises(ValueError) as ctx:
                tg.download_file_by_id("T", "FID")
        self.assertIn("too large", str(ctx.exception))
        req.get.assert_not_called()  # bailed before streaming

    def test_rejects_when_file_path_missing(self):
        with patch("tg.get_file", return_value={"file_size": 1}), \
                patch("tg.requests") as req:
            with self.assertRaises(ValueError) as ctx:
                tg.download_file_by_id("T", "FID")
        self.assertIn("no file_path", str(ctx.exception))
        req.get.assert_not_called()

    def test_missing_file_size_defaults_to_zero(self):
        resp = _stream_resp([b"z"])
        with patch("tg.get_file", return_value={"file_path": "p"}), \
                patch("tg.requests") as req:
            req.get.return_value = resp
            data, path = tg.download_file_by_id("T", "FID")
        self.assertEqual(data, b"z")
        self.assertEqual(path, "p")

    def test_rejects_when_stream_exceeds_cap_mid_download(self):
        big = b"a" * (tg.DOWNLOAD_SIZE_LIMIT + 1)
        resp = _stream_resp([big])
        with patch("tg.get_file", return_value={"file_path": "p"}), \
                patch("tg.requests") as req:
            req.get.return_value = resp
            with self.assertRaises(ValueError) as ctx:
                tg.download_file_by_id("T", "FID")
        self.assertIn("exceeded size limit", str(ctx.exception))

    def test_raise_for_status_error_propagates(self):
        resp = _stream_resp()
        resp.raise_for_status.side_effect = real_requests.HTTPError("500")
        with patch("tg.get_file", return_value={"file_path": "p"}), \
                patch("tg.requests") as req:
            req.get.return_value = resp
            with self.assertRaises(real_requests.HTTPError):
                tg.download_file_by_id("T", "FID")


class UrlGuardNoHostTests(unittest.TestCase):
    """Line 48: a URL with no host must be rejected."""

    def test_rejects_url_with_no_host(self):
        with self.assertRaises(ValueError) as ctx:
            tg._assert_public_url("http:///path-only")
        self.assertIn("no host", str(ctx.exception))


class SendMediaThreadFallbackTests(unittest.TestCase):
    """Line 258: the fallback upload path sets message_thread_id when given."""

    def test_upload_fallback_includes_thread_id(self):
        with patch("tg._call", side_effect=RuntimeError("URL blocked")), \
                patch("tg._download", return_value=b"bytes"), \
                patch("tg._call_upload", return_value={"message_id": 11}) as up:
            out = tg.send_video("T", 1, "https://cdn/v.mp4", "cap",
                                message_thread_id=42)
        self.assertEqual(out, 11)
        self.assertEqual(up.call_args.args[1], "sendVideo")
        self.assertEqual(up.call_args.kwargs["message_thread_id"], 42)
        self.assertEqual(up.call_args.kwargs["caption"], "cap")
        # The downloaded bytes are uploaded under the correct field/filename.
        # _call_upload(token, method, files, **params) -> files is positional.
        files = up.call_args.args[2]
        field, value = next(iter(files.items()))
        self.assertEqual(field, "video")
        self.assertEqual(value, ("video.mp4", b"bytes"))

    def test_upload_fallback_omits_thread_id_when_none(self):
        with patch("tg._call", side_effect=RuntimeError("blocked")), \
                patch("tg._download", return_value=b"b"), \
                patch("tg._call_upload", return_value={"message_id": 1}) as up:
            tg.send_video("T", 1, "https://cdn/v.mp4")
        self.assertNotIn("message_thread_id", up.call_args.kwargs)


class SendMediaUrlPathTests(unittest.TestCase):
    """Lines 274/280/298/308: URL-path branches for the remaining wrappers."""

    def test_send_animation_url_path(self):
        with patch("tg._call", return_value={"message_id": 1}) as c:
            out = tg.send_animation("T", 1, "https://cdn/a.gif", "cap",
                                    message_thread_id=5)
        self.assertEqual(out, 1)
        self.assertEqual(c.call_args.args[1], "sendAnimation")
        self.assertEqual(c.call_args.kwargs["animation"], "https://cdn/a.gif")
        self.assertEqual(c.call_args.kwargs["message_thread_id"], 5)

    def test_send_video_url_path(self):
        with patch("tg._call", return_value={"message_id": 2}) as c:
            out = tg.send_video("T", 1, "https://cdn/v.mp4")
        self.assertEqual(out, 2)
        self.assertEqual(c.call_args.args[1], "sendVideo")
        self.assertEqual(c.call_args.kwargs["video"], "https://cdn/v.mp4")

    def test_send_document_url_path_with_custom_filename(self):
        with patch("tg._call", return_value={"message_id": 3}) as c:
            out = tg.send_document("T", 1, "https://cdn/doc.pdf", "cap",
                                   filename="report.pdf", message_thread_id=8)
        self.assertEqual(out, 3)
        self.assertEqual(c.call_args.args[1], "sendDocument")
        self.assertEqual(c.call_args.kwargs["document"], "https://cdn/doc.pdf")
        self.assertEqual(c.call_args.kwargs["message_thread_id"], 8)

    def test_send_document_default_filename_used_on_upload_fallback(self):
        # filename=None -> _send_media receives "file" as the default name.
        with patch("tg._call", side_effect=RuntimeError("blocked")), \
                patch("tg._download", return_value=b"d"), \
                patch("tg._call_upload", return_value={"message_id": 4}) as up:
            tg.send_document("T", 1, "https://cdn/doc.pdf")
        _, value = next(iter(up.call_args.args[2].items()))
        self.assertEqual(value, ("file", b"d"))

    def test_send_sticker_url_path_includes_thread_id(self):
        # Line 308: sendSticker URL path sets message_thread_id when given.
        with patch("tg._call", return_value={"message_id": 9}) as c:
            out = tg.send_sticker("T", 1, "https://cdn/s.webp",
                                  message_thread_id=77)
        self.assertEqual(out, 9)
        self.assertEqual(c.call_args.args[1], "sendSticker")
        self.assertEqual(c.call_args.kwargs["sticker"], "https://cdn/s.webp")
        self.assertEqual(c.call_args.kwargs["message_thread_id"], 77)


if __name__ == "__main__":
    unittest.main()
