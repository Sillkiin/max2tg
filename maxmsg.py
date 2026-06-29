"""MAX message-mutation ops: edit text, set/remove an emoji reaction.

Thin wrappers over the reverse-engineered opcodes (see pr0bel1230/max-api-docs
`messaging.md`). Payloads match the documented shapes exactly — a wrong shape is
rejected by the server with a `cmd=3` error, so the field names/types below are
load-bearing.

Opcodes:
  67  MSG_EDIT          {chatId, messageId:str, text, elements:[], attachments:[]}
  178 MSG_REACT_SET     {chatId, messageId:int, reaction:{reactionType:"EMOJI", id:"<emoji>"}}
  179 MSG_REACT_REMOVE  {chatId, messageId:int}

Deleting a MAX message is opcode 66. We default `for_me=True` (delete only on
our side) — that is the SAFE mode. `for_me=False` (delete for everyone) has a
documented cascade-delete bug, but ONLY when paired with opcode 92
(CHAT_ACTIVITY) right before it; this bridge never sends opcode 92, so even
for_me=False is safe here — still, the delete-button feature uses for_me=True.
"""
import logging

from vkmax.client import MaxClient

_logger = logging.getLogger(__name__)

EDIT_OPCODE = 67
DELETE_OPCODE = 66
REACT_SET_OPCODE = 178
REACT_REMOVE_OPCODE = 179


def _as_int_id(value):
    """Reactions want messageId as an int; MAX ids may arrive as huge strings.
    Python ints are arbitrary-precision so the value round-trips exactly. Leave
    a non-numeric id untouched rather than crash."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def edit_message(client: MaxClient, chat_id, message_id, text: str):
    """Edit a message's text (opcode 67). `elements` and `attachments` are
    required empty arrays for plain text or the server rejects the request."""
    return await client.invoke_method(
        opcode=EDIT_OPCODE,
        payload={
            "chatId": chat_id,
            "messageId": str(message_id),
            "text": text,
            "elements": [],
            "attachments": [],
        },
    )


async def set_reaction(client: MaxClient, chat_id, message_id, emoji: str):
    """Add/replace an emoji reaction (opcode 178). `reaction` MUST be an object
    {reactionType, id}; a flat string is rejected with cmd=3 'Expected map'."""
    return await client.invoke_method(
        opcode=REACT_SET_OPCODE,
        payload={
            "chatId": chat_id,
            "messageId": _as_int_id(message_id),
            "reaction": {"reactionType": "EMOJI", "id": emoji},
        },
    )


async def remove_reaction(client: MaxClient, chat_id, message_id):
    """Remove our reaction from a message (opcode 179)."""
    return await client.invoke_method(
        opcode=REACT_REMOVE_OPCODE,
        payload={"chatId": chat_id, "messageId": _as_int_id(message_id)},
    )


async def delete_message(client: MaxClient, chat_id, message_ids, for_me: bool = True):
    """Delete message(s) in a MAX chat (opcode 66). `for_me=True` (default) deletes
    only on our side — the safe mode that also avoids the for_me=False cascade bug.
    `message_ids` is a list."""
    return await client.invoke_method(
        opcode=DELETE_OPCODE,
        payload={"chatId": chat_id, "messageIds": list(message_ids), "forMe": for_me},
    )
