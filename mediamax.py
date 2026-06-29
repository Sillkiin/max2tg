"""Resolve downloadable URLs for MAX file/video attachments.

The web client turns an attachment id + token into a temporary CDN URL via two
WS requests (reverse-engineered from web.max.ru):
  - FILE  : opcode 88, payload {fileId, chatId, messageId}  -> payload.url
  - VIDEO : opcode 83, payload {videoId, chatId, messageId} -> payload["MP4_<h>"]
Resolved URLs carry an `expires` query param (~24h).
"""
import logging
import mimetypes
import asyncio
from random import randint
from urllib.parse import quote

import requests
from vkmax.client import MaxClient

_logger = logging.getLogger(__name__)

FILE_RESOLVE_OPCODE = 88
VIDEO_RESOLVE_OPCODE = 83
# Voice/audio messages (UNSUPPORTED attach with audioId) resolve here, NOT via
# the file opcode 88 (which returns file.not.found for audio). Reverse-engineered
# from web.max.ru: getAudioSources -> invoke(301). Response is {opus?, m4a?, mp3?}
# of directly-fetchable CDN URLs; opus is audio/ogg = Telegram's native voice.
AUDIO_RESOLVE_OPCODE = 301
# Preference order + the Telegram-facing mime for each source key.
_AUDIO_SOURCE_MIMES = (("opus", "audio/ogg"), ("m4a", "audio/mp4"),
                       ("mp3", "audio/mpeg"))
# Upload-slot opcodes differ by media type (reverse-engineered from web.max.ru).
PHOTO_UPLOAD_SLOT_OPCODE = 80
VIDEO_UPLOAD_SLOT_OPCODE = 82
FILE_UPLOAD_SLOT_OPCODE = 87
# A native voice/audio upload reuses the video upload opcode (82) but with
# type=2 (audio) so the slot is audio-tagged (omu.okcdn.ru), then the bytes go
# up as multipart/form-data. Reverse-engineered from the MAX Android app
# (see docs/native-voice-research.md). type=dtg.E(3)=2 in the app.
AUDIO_UPLOAD_TYPE = 2
SEND_MESSAGE_OPCODE = 64
UPLOAD_TIMEOUT = (5, 120)
# MAX processes an uploaded attachment asynchronously; sending the message
# before it's done returns this error. We retry until it's ready.
ATTACHMENT_NOT_READY = "attachment.not.ready"
SEND_RETRIES = 15
SEND_RETRY_DELAY = 1.5


async def resolve_file_url(client: MaxClient, file_id: int | str,
                           chat_id: int | str, message_id: int | str) -> str:
    response = await client.invoke_method(
        opcode=FILE_RESOLVE_OPCODE,
        payload={"fileId": file_id, "chatId": chat_id, "messageId": message_id},
    )
    payload = response.get("payload", {})
    url = payload.get("url")
    if not url:
        raise RuntimeError(f"file resolve returned no url: {payload}")
    return url


async def resolve_video_url(client: MaxClient, video_id: int | str,
                            chat_id: int | str, message_id: int | str) -> str:
    """Return the highest-resolution MP4 URL for a video attachment."""
    response = await client.invoke_method(
        opcode=VIDEO_RESOLVE_OPCODE,
        payload={"videoId": video_id, "chatId": chat_id, "messageId": message_id},
    )
    payload = response.get("payload", {})
    best_url, best_height = None, -1
    for key, value in payload.items():
        if isinstance(key, str) and key.startswith("MP4_") and isinstance(value, str):
            try:
                height = int(key[4:])
            except ValueError:
                continue
            if height > best_height:
                best_height, best_url = height, value
    if not best_url:
        raise RuntimeError(f"video resolve returned no MP4 source: {payload}")
    return best_url


async def resolve_audio_url(client: MaxClient, audio_id: int | str,
                            chat_id: int | str, message_id: int | str,
                            token: str | None = None) -> tuple[str, str]:
    """Resolve a voice/audio attachment to a playable URL (opcode 301).

    Returns (url, mime). Prefers opus (audio/ogg → a native Telegram voice);
    falls back to m4a/mp3 (sent as audio). Raises if no source is returned.
    """
    payload = {"audioId": audio_id, "chatId": chat_id, "messageId": message_id}
    if token is not None:
        payload["token"] = token
    response = await client.invoke_method(opcode=AUDIO_RESOLVE_OPCODE, payload=payload)
    result = response.get("payload", {}) if isinstance(response, dict) else {}
    for key, mime in _AUDIO_SOURCE_MIMES:
        url = result.get(key)
        if isinstance(url, str) and url:
            return url, mime
    raise RuntimeError(f"audio resolve returned no source: {result}")


def _content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition for a possibly-Unicode name.

    HTTP header values must be latin-1 encodable, so a Cyrillic filename (e.g.
    'Фёдор.jpg') crashes requests. RFC 5987 `filename*` preserves the real name
    while an ASCII `filename=` stays as a fallback for older parsers.
    """
    ascii_name = (filename.encode("ascii", "ignore").decode("ascii")
                  .replace('"', "").strip() or "file")
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}")


def _upload_bytes(url: str, content: bytes, filename: str, mime_type: str) -> dict:
    """POST bytes to a MAX upload slot; return the parsed JSON body (or {}).

    Photo uploads return {"photos": [{"token": ...}]} in the body; file/video
    uploads usually return nothing useful (the id comes from the slot).
    """
    headers = {
        "Content-Type": mime_type or "application/octet-stream",
        "Content-Disposition": _content_disposition(filename),
        "Content-Range": f"0-{len(content) - 1}/{len(content)}",
    }
    response = requests.post(url, data=content, headers=headers, timeout=UPLOAD_TIMEOUT)
    if response.status_code not in (200, 201):
        reason = response.headers.get("X-Reason") or response.text[:500]
        raise RuntimeError(f"MAX upload failed: {response.status_code} {reason}")
    try:
        return response.json()
    except ValueError:
        return {}


def _slot(payload: dict) -> dict:
    """Return the slot dict whether the response wraps it in `info` or not."""
    info = payload.get("info")
    if isinstance(info, list) and info and isinstance(info[0], dict):
        return info[0]
    return payload


async def upload_file(client: MaxClient, content: bytes, filename: str,
                      mime_type: str | None = None) -> int | str:
    response = await client.invoke_method(
        opcode=FILE_UPLOAD_SLOT_OPCODE,
        payload={"count": 1},
    )
    _logger.info("file upload slot payload: %s", response.get("payload"))
    info = response.get("payload", {}).get("info", [])
    if not info:
        raise RuntimeError(f"file upload slot returned no info: {response.get('payload')}")
    slot = info[0]
    file_id = slot.get("fileId")
    url = slot.get("url")
    if file_id is None or not url:
        raise RuntimeError(f"file upload slot is incomplete: {slot}")
    mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    await asyncio.to_thread(_upload_bytes, url, content, filename, mime_type)
    return file_id


async def send_file_message(client: MaxClient, chat_id: int | str,
                            file_id: int | str, text: str = "",
                            reply_to_message_id: int | str | None = None,
                            notify: bool = True):
    message = {
        "text": text,
        "cid": randint(1750000000000, 2000000000000),
        "elements": [],
        "attaches": [{"_type": "FILE", "fileId": file_id}],
    }
    if reply_to_message_id is not None:
        message["link"] = {
            "type": "REPLY",
            "messageId": str(reply_to_message_id),
        }
    return await _invoke_send(
        client,
        {"chatId": chat_id, "message": message, "notify": notify},
        "FILE",
    )


async def send_uploaded_file(client: MaxClient, chat_id: int | str,
                             content: bytes, filename: str,
                             mime_type: str | None = None, text: str = "",
                             reply_to_message_id: int | str | None = None):
    file_id = await upload_file(client, content, filename, mime_type)
    return await send_file_message(
        client,
        chat_id,
        file_id,
        text=text,
        reply_to_message_id=reply_to_message_id,
    )


def _build_attach_message(attach: dict, text: str,
                          reply_to_message_id: int | str | None) -> dict:
    message = {
        "text": text,
        "cid": randint(1750000000000, 2000000000000),
        "elements": [],
        "attaches": [attach],
    }
    if reply_to_message_id is not None:
        message["link"] = {"type": "REPLY", "messageId": str(reply_to_message_id)}
    return message


async def _invoke_send(client: MaxClient, payload: dict, kind: str):
    """Send an attachment message, retrying while MAX is still processing it."""
    for attempt in range(SEND_RETRIES):
        response = await client.invoke_method(
            opcode=SEND_MESSAGE_OPCODE, payload=payload)
        result = response.get("payload", {}) if isinstance(response, dict) else {}
        if isinstance(result, dict) and result.get("error") == ATTACHMENT_NOT_READY:
            _logger.info("%s attachment not ready, retry %d/%d",
                         kind, attempt + 1, SEND_RETRIES)
            await asyncio.sleep(SEND_RETRY_DELAY)
            continue
        if isinstance(result, dict) and ("error" in result or "error_code" in result):
            raise RuntimeError(f"MAX rejected {kind} message: {result}")
        return response
    raise RuntimeError(f"MAX {kind} attachment not ready after {SEND_RETRIES} retries")


async def _send_attach(client: MaxClient, chat_id, attach: dict, text: str,
                       reply_to_message_id, notify: bool = True):
    return await _invoke_send(
        client,
        {
            "chatId": chat_id,
            "message": _build_attach_message(attach, text, reply_to_message_id),
            "notify": notify,
        },
        attach.get("_type", "media"),
    )


def _upload_multipart(url: str, content: bytes, filename: str,
                      mime_type: str) -> dict:
    """POST an image as multipart/form-data (field 'file'), return JSON body.

    MAX's photo endpoint wants a multipart upload (not the raw + Content-Range
    used for files/videos); raw bytes get rejected with BAD_REQUEST.
    """
    files = {"file": (filename, content, mime_type or "image/jpeg")}
    response = requests.post(url, files=files, timeout=UPLOAD_TIMEOUT)
    if response.status_code not in (200, 201):
        reason = response.headers.get("X-Reason") or response.text[:500]
        raise RuntimeError(f"MAX photo upload failed: {response.status_code} {reason}")
    try:
        return response.json()
    except ValueError:
        return {}


def _extract_photo_token(body: dict) -> str | None:
    """`photos` is a dict {photoId: {token}}; tolerate a list shape too."""
    photos = body.get("photos")
    if isinstance(photos, dict):
        for value in photos.values():
            if isinstance(value, dict) and value.get("token"):
                return value["token"]
    elif isinstance(photos, list):
        for value in photos:
            if isinstance(value, dict) and value.get("token"):
                return value["token"]
    return None


async def upload_photo(client: MaxClient, content: bytes, filename: str,
                       mime_type: str | None = None) -> str:
    """Upload a photo (opcode 80 slot) and return its photoToken.

    The token comes from the multipart upload response: {"photos": {id: {token}}}.
    """
    response = await client.invoke_method(
        opcode=PHOTO_UPLOAD_SLOT_OPCODE, payload={"count": 1})
    payload = response.get("payload", {})
    _logger.info("photo upload slot payload: %s", payload)
    slot = _slot(payload)
    url = slot.get("url") or payload.get("url")
    if not url:
        raise RuntimeError(f"photo upload slot has no url: {payload}")
    mime_type = mime_type or mimetypes.guess_type(filename)[0] or "image/jpeg"
    body = await asyncio.to_thread(_upload_multipart, url, content, filename, mime_type)
    _logger.info("photo upload body: %s", body)
    if "error_code" in body:
        raise RuntimeError(f"photo upload error: {body}")
    token = _extract_photo_token(body)
    if not token:
        raise RuntimeError(f"photo upload returned no token: {body}")
    return token


async def send_uploaded_photo(client: MaxClient, chat_id, content: bytes,
                              filename: str, mime_type: str | None = None,
                              text: str = "", reply_to_message_id=None):
    token = await upload_photo(client, content, filename, mime_type)
    return await _send_attach(
        client, chat_id, {"_type": "PHOTO", "photoToken": token},
        text, reply_to_message_id)


async def upload_video(client: MaxClient, content: bytes, filename: str,
                       mime_type: str | None = None) -> tuple:
    """Upload a video (opcode 82 slot); return (video_id, token)."""
    response = await client.invoke_method(
        opcode=VIDEO_UPLOAD_SLOT_OPCODE, payload={"count": 1})
    payload = response.get("payload", {})
    _logger.info("video upload slot payload: %s", payload)
    slot = _slot(payload)
    url = slot.get("url")
    video_id = slot.get("videoId")
    token = slot.get("token")
    if not url or video_id is None:
        raise RuntimeError(f"video upload slot is incomplete: {payload}")
    mime_type = mime_type or mimetypes.guess_type(filename)[0] or "video/mp4"
    await asyncio.to_thread(_upload_bytes, url, content, filename, mime_type)
    return video_id, token


async def send_uploaded_video(client: MaxClient, chat_id, content: bytes,
                              filename: str, mime_type: str | None = None,
                              text: str = "", reply_to_message_id=None):
    video_id, token = await upload_video(client, content, filename, mime_type)
    attach = {"_type": "VIDEO", "videoId": video_id}
    if token is not None:
        attach["token"] = token
    return await _send_attach(client, chat_id, attach, text, reply_to_message_id)


async def upload_audio(client: MaxClient, content: bytes,
                       filename: str = "voice.ogg") -> tuple:
    """Upload a voice/audio file as a NATIVE MAX voice; return (audio_id, token).

    Uses the video upload opcode (82) with type=2 so the slot is audio-tagged
    (omu.okcdn.ru), then uploads the bytes as multipart/form-data — the audio
    endpoint rejects the raw Content-Range body that videos use (HTTP 415).
    """
    response = await client.invoke_method(
        opcode=VIDEO_UPLOAD_SLOT_OPCODE,
        payload={"count": 1, "type": AUDIO_UPLOAD_TYPE, "uploaderType": 0},
    )
    payload = response.get("payload", {})
    _logger.info("audio upload slot payload: %s", payload)
    slot = _slot(payload)
    url = slot.get("url")
    audio_id = slot.get("audioId") or slot.get("videoId")
    token = slot.get("token")
    if not url or audio_id is None:
        raise RuntimeError(f"audio upload slot is incomplete: {payload}")
    await asyncio.to_thread(_upload_multipart, url, content, filename, "audio/ogg")
    return audio_id, token


async def send_uploaded_audio(client: MaxClient, chat_id, content: bytes,
                              duration_ms: int = 0, text: str = "",
                              reply_to_message_id=None,
                              filename: str = "voice.ogg"):
    """Send a (Telegram) voice into MAX as a native voice message — waveform +
    duration — rather than a generic file. Telegram voices are already ogg/opus,
    so the bytes are uploaded as-is."""
    audio_id, token = await upload_audio(client, content, filename)
    attach = {"_type": "AUDIO", "audioId": audio_id,
              "duration": int(duration_ms or 0)}
    if token is not None:
        attach["token"] = token
    return await _send_attach(client, chat_id, attach, text, reply_to_message_id)


async def send_uploaded_media(client: MaxClient, chat_id, content: bytes,
                              filename: str, mime_type: str | None = None,
                              kind: str = "file", text: str = "",
                              reply_to_message_id=None, duration_ms: int = 0):
    """Dispatch by media kind so photos/videos/voices use their proper MAX attach
    type instead of being sent as generic files (which recipients don't receive)."""
    if kind == "photo":
        return await send_uploaded_photo(
            client, chat_id, content, filename, mime_type, text, reply_to_message_id)
    if kind == "video":
        return await send_uploaded_video(
            client, chat_id, content, filename, mime_type, text, reply_to_message_id)
    if kind == "voice":
        # Native voice; if MAX rejects the audio (format/processing), fall back
        # to a plain .ogg file (which still plays in MAX) so nothing is lost.
        try:
            return await send_uploaded_audio(
                client, chat_id, content, duration_ms, text, reply_to_message_id)
        except Exception as exc:
            _logger.warning("Native voice upload failed (%s); sending as file.", exc)
            return await send_uploaded_file(
                client, chat_id, content, filename, mime_type, text,
                reply_to_message_id)
    return await send_uploaded_file(
        client, chat_id, content, filename, mime_type, text, reply_to_message_id)
