"""MAX message-mutation ops: edit text, set/remove an emoji reaction.

Thin wrappers over the reverse-engineered opcodes (see pr0bel1230/max-api-docs
`messaging.md`). Payloads match the documented shapes exactly — a wrong shape is
rejected by the server with a `cmd=3` error, so the field names/types below are
load-bearing.

Opcodes:
  67  MSG_EDIT          {chatId, messageId:str, text, elements:[], attachments:[]}
  178 MSG_REACT_SET     {chatId, messageId:int, reaction:{reactionType:"EMOJI", id:"<emoji>"}}
  179 MSG_REACT_REMOVE  {chatId, messageId:int}

NOTE: deleting a MAX message (opcode 66) is intentionally NOT wrapped here.
`forMe:false` has a documented cascade-delete bug (it can wipe every message up
to a server watermark, not just the targeted ids), and Telegram never notifies a
bot when a user deletes a message — so there is no safe trigger for a delete and
this module deliberately offers no way to send opcode 66.
"""
import logging

from vkmax.client import MaxClient

_logger = logging.getLogger(__name__)

EDIT_OPCODE = 67
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
