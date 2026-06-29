import unittest

import attaches


class SafeFilenameTests(unittest.TestCase):
    def test_non_string_falls_back_to_default(self):
        # line 29: not a str -> default name
        self.assertEqual(attaches._safe_filename(123), "файл")

    def test_blank_string_falls_back_to_default(self):
        self.assertEqual(attaches._safe_filename("   "), "файл")

    def test_none_falls_back_to_default(self):
        self.assertEqual(attaches._safe_filename(None), "файл")

    def test_strips_posix_path_components(self):
        self.assertEqual(attaches._safe_filename("/etc/passwd"), "passwd")

    def test_strips_windows_path_components(self):
        self.assertEqual(attaches._safe_filename("C:\\Windows\\evil.exe"), "evil.exe")

    def test_plain_name_preserved(self):
        self.assertEqual(attaches._safe_filename("report.pdf"), "report.pdf")


class ToIntTests(unittest.TestCase):
    def test_bool_returns_none(self):
        # line 35: bool is not treated as int
        self.assertIsNone(attaches._to_int(True))
        self.assertIsNone(attaches._to_int(False))

    def test_int_passthrough(self):
        self.assertEqual(attaches._to_int(42), 42)

    def test_float_truncates(self):
        # line 37: float -> int()
        self.assertEqual(attaches._to_int(3.9), 3)

    def test_non_number_returns_none(self):
        # line 38: fallthrough
        self.assertIsNone(attaches._to_int("12"))
        self.assertIsNone(attaches._to_int(None))


class FormatDurationTests(unittest.TestCase):
    def test_zero_returns_empty(self):
        # line 45: falsy seconds -> ""
        self.assertEqual(attaches._format_duration(0), "")

    def test_none_returns_empty(self):
        self.assertEqual(attaches._format_duration(None), "")

    def test_seconds_value_kept_as_is(self):
        self.assertEqual(attaches._format_duration(5), " (5 с)")

    def test_milliseconds_converted_to_seconds(self):
        # value > 1000 treated as ms and divided by 1000 with rounding
        self.assertEqual(attaches._format_duration(5400), " (5 с)")

    def test_milliseconds_rounding(self):
        self.assertEqual(attaches._format_duration(5600), " (6 с)")


class HumanSizeTests(unittest.TestCase):
    def test_none_returns_empty(self):
        # lines 54-55: TypeError path
        self.assertEqual(attaches._human_size(None), "")

    def test_non_numeric_string_returns_empty(self):
        # lines 54-55: ValueError path
        self.assertEqual(attaches._human_size("abc"), "")

    def test_bytes_unit_uses_zero_decimals(self):
        self.assertEqual(attaches._human_size(512), "512 Б")

    def test_kilobytes_unit_uses_one_decimal(self):
        # lines 57-59: loop into КБ
        self.assertEqual(attaches._human_size(2048), "2.0 КБ")

    def test_megabytes(self):
        self.assertEqual(attaches._human_size(5 * 1024 * 1024), "5.0 МБ")

    def test_terabytes_fallthrough(self):
        # line 60: exceeds all named units -> ТБ
        self.assertEqual(attaches._human_size(5 * 1024 ** 4), "5.0 ТБ")


class PhotoParseTests(unittest.TestCase):
    def test_photo_base_url(self):
        result = attaches._parse_one({"_type": "PHOTO", "baseUrl": "http://cdn/p.jpg"})
        self.assertEqual(result.kind, "photo")
        self.assertEqual(result.text, "🖼 Фото")
        self.assertEqual(result.url, "http://cdn/p.jpg")

    def test_photo_falls_back_to_url_key(self):
        # line 84: url present (no baseUrl) -> photo
        result = attaches._parse_one({"_type": "PHOTO", "url": "http://cdn/x.jpg"})
        self.assertEqual(result.kind, "photo")
        self.assertEqual(result.url, "http://cdn/x.jpg")

    def test_photo_mp4_becomes_animation(self):
        # line 82: mp4Url -> animation GIF
        result = attaches._parse_one(
            {"_type": "PHOTO", "baseUrl": "http://cdn/p.jpg", "mp4Url": "http://cdn/p.mp4"}
        )
        self.assertEqual(result.kind, "animation")
        self.assertEqual(result.text, "🖼 GIF")
        self.assertEqual(result.url, "http://cdn/p.mp4")

    def test_photo_no_url_returns_note(self):
        # line 85: neither mp4 nor url
        result = attaches._parse_one({"_type": "PHOTO"})
        self.assertEqual(result.kind, "note")
        self.assertIn("не удалось получить ссылку", result.text)


class StickerParseTests(unittest.TestCase):
    def test_sticker_url(self):
        result = attaches._parse_one({"_type": "STICKER", "url": "http://cdn/s.webp"})
        self.assertEqual(result.kind, "sticker")
        self.assertEqual(result.text, "🩷 Стикер")
        self.assertEqual(result.url, "http://cdn/s.webp")

    def test_sticker_lottie_url(self):
        # line 93: lottieUrl as fallback url
        result = attaches._parse_one({"_type": "STICKER", "lottieUrl": "http://cdn/s.json"})
        self.assertEqual(result.kind, "sticker")
        self.assertEqual(result.url, "http://cdn/s.json")

    def test_sticker_mp4_becomes_animation(self):
        # line 91: mp4Url -> animation
        result = attaches._parse_one(
            {"_type": "STICKER", "mp4Url": "http://cdn/s.mp4", "url": "http://cdn/s.webp"}
        )
        self.assertEqual(result.kind, "animation")
        self.assertEqual(result.url, "http://cdn/s.mp4")

    def test_sticker_no_url_returns_note(self):
        # line 94: nothing usable
        result = attaches._parse_one({"_type": "STICKER"})
        self.assertEqual(result.kind, "note")
        self.assertEqual(result.text, "🩷 Стикер")


class VideoParseTests(unittest.TestCase):
    def test_video_direct_url(self):
        # line 97-99: direct url
        result = attaches._parse_one({"_type": "VIDEO", "url": "http://cdn/v.mp4"})
        self.assertEqual(result.kind, "video")
        self.assertEqual(result.text, "🎞 Видео")
        self.assertEqual(result.url, "http://cdn/v.mp4")

    def test_video_resolve_by_video_id(self):
        # line 100-102: videoId path
        result = attaches._parse_one({"_type": "VIDEO", "videoId": 555})
        self.assertEqual(result.kind, "video_resolve")
        self.assertEqual(result.video_id, 555)
        self.assertIsNone(result.url)

    def test_video_resolve_by_id_fallback(self):
        result = attaches._parse_one({"_type": "VIDEO", "id": 777})
        self.assertEqual(result.kind, "video_resolve")
        self.assertEqual(result.video_id, 777)

    def test_video_no_ids_returns_note(self):
        # line 103: no url, no id
        result = attaches._parse_one({"_type": "VIDEO"})
        self.assertEqual(result.kind, "note")
        self.assertIn("открыть в MAX", result.text)


class AudioVoiceParseTests(unittest.TestCase):
    def test_audio_type_with_url_is_voice(self):
        result = attaches._parse_one(
            {"_type": "AUDIO", "url": "http://cdn/a.ogg", "duration": 5000}
        )
        self.assertEqual(result.kind, "voice")
        self.assertEqual(result.url, "http://cdn/a.ogg")
        self.assertEqual(result.text, "🎤 Голосовое (5 с)")

    def test_voice_detected_by_audio_id_despite_unsupported_type(self):
        # line 107: audioId present even though declared UNSUPPORTED
        result = attaches._parse_one(
            {"_type": "UNSUPPORTED", "audioId": 42, "token": "TOK", "duration": 3200}
        )
        self.assertEqual(result.kind, "audio_resolve")
        self.assertEqual(result.file_id, 42)
        self.assertEqual(result.token, "TOK")
        self.assertEqual(result.text, "🎤 Голосовое (3 с)")

    def test_audio_resolve_falls_back_to_id(self):
        result = attaches._parse_one({"_type": "AUDIO", "id": 99})
        self.assertEqual(result.kind, "audio_resolve")
        self.assertEqual(result.file_id, 99)
        self.assertIsNone(result.token)

    def test_audio_no_id_returns_note(self):
        # line 121: AUDIO type, no url, no audioId/id
        result = attaches._parse_one({"_type": "AUDIO"})
        self.assertEqual(result.kind, "note")
        self.assertIn("открыть в MAX", result.text)


class FileParseTests(unittest.TestCase):
    def test_file_with_url_is_document(self):
        # line 130: url present -> document
        result = attaches._parse_one(
            {"_type": "FILE", "url": "http://cdn/f.pdf", "name": "report.pdf", "size": 2048}
        )
        self.assertEqual(result.kind, "document")
        self.assertEqual(result.url, "http://cdn/f.pdf")
        self.assertEqual(result.filename, "report.pdf")
        self.assertEqual(result.size, 2048)
        self.assertEqual(result.text, "📎 report.pdf (2.0 КБ)")

    def test_file_resolve_by_file_id(self):
        # line 131-134: fileId path
        result = attaches._parse_one(
            {"_type": "FILE", "fileId": 321, "name": "doc.txt", "size": 100}
        )
        self.assertEqual(result.kind, "file_resolve")
        self.assertEqual(result.file_id, 321)
        self.assertEqual(result.filename, "doc.txt")
        self.assertEqual(result.size, 100)
        self.assertEqual(result.text, "📎 doc.txt (100 Б)")

    def test_file_resolve_sanitizes_name(self):
        result = attaches._parse_one(
            {"_type": "FILE", "fileId": 1, "name": "../../etc/passwd"}
        )
        self.assertEqual(result.filename, "passwd")

    def test_file_no_id_returns_note(self):
        # line 135: no url, no fileId/id
        result = attaches._parse_one({"_type": "FILE", "name": "x.bin"})
        self.assertEqual(result.kind, "note")
        self.assertIn("открыть в MAX", result.text)


class ShareParseTests(unittest.TestCase):
    def test_share_combines_title_url_host(self):
        result = attaches._parse_one(
            {"_type": "SHARE", "title": "News", "url": "http://site/x", "host": "site"}
        )
        self.assertEqual(result.kind, "link")
        self.assertEqual(result.text, "🔗 News\nhttp://site/x\nsite")

    def test_share_empty_falls_back_to_label(self):
        # parts all filtered -> default "🔗 Ссылка"
        result = attaches._parse_one({"_type": "SHARE"})
        self.assertEqual(result.kind, "link")
        self.assertEqual(result.text, "🔗 Ссылка")


class ContactParseTests(unittest.TestCase):
    def test_contact_full(self):
        result = attaches._parse_one(
            {"_type": "CONTACT", "firstName": "Ivan", "lastName": "Petrov", "phone": "+7900"}
        )
        self.assertEqual(result.kind, "note")
        self.assertEqual(result.text, "👤 Контакт: Ivan Petrov +7900")

    def test_contact_partial_name_only(self):
        result = attaches._parse_one({"_type": "CONTACT", "firstName": "Ivan"})
        self.assertEqual(result.text, "👤 Контакт: Ivan")


class LocationParseTests(unittest.TestCase):
    def test_location_with_coordinates(self):
        result = attaches._parse_one(
            {"_type": "LOCATION", "latitude": 55.7, "longitude": 37.6}
        )
        self.assertEqual(result.kind, "note")
        self.assertEqual(
            result.text, "📍 Геопозиция: https://maps.google.com/?q=55.7,37.6"
        )

    def test_location_without_coordinates(self):
        result = attaches._parse_one({"_type": "LOCATION"})
        self.assertEqual(result.kind, "note")
        self.assertEqual(result.text, "📍 Геопозиция")


class ServiceAndUnknownTypeTests(unittest.TestCase):
    def test_control_returns_none(self):
        self.assertIsNone(attaches._parse_one({"_type": "CONTROL"}))

    def test_widget_returns_none(self):
        self.assertIsNone(attaches._parse_one({"_type": "WIDGET"}))

    def test_empty_type_returns_none(self):
        self.assertIsNone(attaches._parse_one({}))

    def test_unknown_type_returns_generic_note(self):
        # line 161: fallthrough for unrecognized type
        result = attaches._parse_one({"_type": "MYSTERY"})
        self.assertEqual(result.kind, "note")
        self.assertEqual(result.text, "📦 Вложение: MYSTERY")

    def test_type_key_alias_is_used(self):
        # _attach_type reads "type" when "_type" missing
        result = attaches._parse_one({"type": "photo", "url": "http://cdn/p.jpg"})
        self.assertEqual(result.kind, "photo")


class ParseTests(unittest.TestCase):
    def test_parse_uses_attaches_key(self):
        message = {"attaches": [{"_type": "PHOTO", "url": "http://cdn/p.jpg"}]}
        result = attaches.parse(message)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "photo")

    def test_parse_uses_attachments_key_fallback(self):
        message = {"attachments": [{"_type": "STICKER", "url": "http://cdn/s.webp"}]}
        result = attaches.parse(message)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "sticker")

    def test_parse_non_list_returns_empty(self):
        # line 167: raw not a list
        self.assertEqual(attaches.parse({"attaches": "oops"}), [])

    def test_parse_skips_non_dict_items(self):
        # line 171: skip non-dict entries
        message = {"attaches": ["bad", {"_type": "PHOTO", "url": "http://cdn/p.jpg"}, 5]}
        result = attaches.parse(message)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "photo")

    def test_parse_drops_none_results(self):
        # CONTROL parses to None and is excluded
        message = {"attaches": [{"_type": "CONTROL"}, {"_type": "PHOTO", "url": "http://c/p.jpg"}]}
        result = attaches.parse(message)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "photo")

    def test_parse_missing_attaches_returns_empty(self):
        self.assertEqual(attaches.parse({}), [])


if __name__ == "__main__":
    unittest.main()
