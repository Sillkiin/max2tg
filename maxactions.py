"""Telegram-command -> MAX actions: DM people, join channels/chats.

Thin wrappers over the MAX opcodes with link parsing and defensive response
handling. Every public coroutine returns a CommandResult (never raises).

Opcodes:
  57  join by link (channels: https://max.ru/<name>; groups: join/<hash>) + 75 subscribe
  46  find contact by phone (CONTACT_INFO_BY_PHONE) -> payload.contact
  64  send message: by chatId (existing chat) OR by userId (opens a 1:1 dialog)

/dm opens a 1:1 dialog by sending opcode 64 with a top-level `userId` (not
`chatId`): MAX has no separate "open dialog" call — it lazily creates the dialog
and returns its real chatId. (The earlier failure put the user_id in the `chatId`
slot, which addresses the wrong chat.)

NOT wired:
- Free-text NAME search (opcode 60 PUBLIC_SEARCH): payload schema unconfirmed; a
  bad payload makes MAX drop the socket. So channels are found by link/@username
  (/join), and people by phone (/dm).
"""
import logging
import re
from dataclasses import dataclass
from random import randint

_logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")


@dataclass
class CommandResult:
    """A command's Telegram reply text. (No send target is carried: a user-id is
    NOT a dialog chatId in MAX, so it must never become a send destination.)"""
    text: str


def _short(value, limit: int = 200) -> str:
    """Clamp a third-party string (MAX error / exception) before echoing it."""
    return str(value)[:limit]


def _norm_link(raw: str) -> str | None:
    """MAX link payload (opcode 57) from a raw string: a group invite
    (join/<hash>), a max.ru/<name> link, or a bare @username."""
    s = raw.strip()
    # Match a join hash only as a path segment (string start or after '/'), so a
    # query like 'max.ru/news?ref=join/x' isn't misread as a group invite.
    m = re.search(r"(?:^|/)join/([A-Za-z0-9_-]+)", s)
    if m:
        return f"join/{m.group(1)}"
    m = re.search(r"max\.ru/([A-Za-z0-9_.]+)", s)
    if m:
        return f"https://max.ru/{m.group(1)}"
    s = s.lstrip("@")
    if _USERNAME_RE.match(s):
        return f"https://max.ru/{s}"
    return None


def _chat_from_payload(payload: dict):
    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else payload
    chat_id = chat.get("id") or chat.get("chatId")
    title = (chat.get("title") or chat.get("name") or "").strip()
    return chat_id, title


def _normalize_phone(s: str) -> str | None:
    """E.164-ish phone from user input, or None if implausible. Maps a Russian
    local '8XXXXXXXXXX' to '+7XXXXXXXXXX'."""
    digits = re.sub(r"\D", "", s)
    if not 7 <= len(digits) <= 15:
        return None
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return "+" + digits


def _looks_like_phone(s: str) -> bool:
    """A phone number rather than a numeric id or other text."""
    digits = re.sub(r"\D", "", s)
    return (re.fullmatch(r"[+\d\s()\-]+", s) is not None
            and (s.startswith("+") or len(digits) >= 11
                 or (bool(re.search(r"[+\s()\-]", s)) and len(digits) >= 7)))


async def join(client, raw: str) -> CommandResult:
    """Join a MAX channel/group/chat by link or @username (opcode 57 + subscribe)."""
    link = _norm_link(raw)
    if not link:
        return CommandResult(
            "🤔 Не похоже на ссылку. Пришлите ссылку вида max.ru/имя или @username.\n"
            "Пример: /join https://max.ru/join/AbCdEf")
    try:
        data = await client.invoke_method(opcode=57, payload={"link": link})
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        if "error" in payload:
            return CommandResult(f"⚠️ MAX не дал вступить: {_short(payload.get('error'))}")
        chat_id, title = _chat_from_payload(payload)
        if chat_id is not None:
            try:
                await client.invoke_method(
                    opcode=75, payload={"chatId": chat_id, "subscribe": True})
            except Exception as exc:
                _logger.warning("subscribe after join %s: %s", chat_id, exc)
        name = title or (f"чат {chat_id}" if chat_id else "чат")
        return CommandResult(
            f"✅ Готово, вы вступили: {name}\n"
            "Чат появится отдельной темой, как только придёт первое сообщение.")
    except Exception as exc:
        _logger.warning("join failed: %s", exc)
        return CommandResult(f"⚠️ Не удалось вступить: {_short(exc)}")


async def _resolve_user_id(client, recipient) -> int | None:
    """A bare numeric user_id as-is, or look one up from a phone (opcode 46)."""
    s = str(recipient).strip()
    if _looks_like_phone(s):
        phone = _normalize_phone(s)
        if not phone:
            return None
        try:
            data = await client.invoke_method(opcode=46, payload={"phone": phone})
        except Exception as exc:
            _logger.warning("dm phone lookup failed: %s", exc)
            return None
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        contact = payload.get("contact")
        if isinstance(contact, dict) and contact.get("id"):
            return int(contact["id"])
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    return None


async def start_dm(client, recipient: str, text: str) -> CommandResult:
    """Message a person by **phone or numeric user_id**. Sends opcode 64 with a
    top-level `userId` (NOT `chatId`): MAX creates the 1:1 dialog and returns its
    real chatId. The peer's reply then arrives as its own topic."""
    body = (text or "").strip()
    if not body:
        return CommandResult("⚠️ Пустое сообщение. Пример: /dm +79991234567 привет")
    if len(body) > 4000:
        return CommandResult("⚠️ Слишком длинное сообщение (макс. 4000 символов).")
    uid = await _resolve_user_id(client, recipient)
    if uid is None:
        return CommandResult(
            "⚠️ Кому писать? Укажите телефон или числовой id.\n"
            "Примеры: /dm +79991234567 привет   ·   /dm 21243808 привет")
    try:
        data = await client.invoke_method(opcode=64, payload={
            "userId": uid,
            "message": {
                "text": body,
                "cid": randint(1750000000000, 2000000000000),
                "elements": [],
                "attaches": [],
            },
            "notify": True,
        })
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        if payload.get("error"):
            return CommandResult(f"⚠️ MAX не принял сообщение: {_short(payload.get('error'))}")
        return CommandResult(
            f"✅ Отправлено! Диалог с человеком (id {uid}) создан — его ответ "
            "придёт отдельной темой, дальше переписывайтесь там.")
    except Exception as exc:
        _logger.warning("start_dm to %s failed: %s", uid, exc)
        return CommandResult(f"⚠️ Не удалось отправить: {_short(exc)}")
