import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maxactions  # noqa: E402


class NormLinkTests(unittest.TestCase):
    def test_group_invite_hash(self):
        self.assertEqual(maxactions._norm_link("https://max.ru/join/Ab9_xZ"), "join/Ab9_xZ")
        self.assertEqual(maxactions._norm_link("join/Ab9_xZ"), "join/Ab9_xZ")

    def test_channel_or_user_link(self):
        self.assertEqual(maxactions._norm_link("https://max.ru/durov"), "https://max.ru/durov")
        self.assertEqual(maxactions._norm_link("max.ru/durov"), "https://max.ru/durov")

    def test_bare_username(self):
        self.assertEqual(maxactions._norm_link("@durov"), "https://max.ru/durov")
        self.assertEqual(maxactions._norm_link("durov"), "https://max.ru/durov")

    def test_rejects_garbage(self):
        self.assertIsNone(maxactions._norm_link("привет мир"))
        self.assertIsNone(maxactions._norm_link("ab"))

    def test_join_in_query_not_misread_as_invite(self):
        # 'join/' inside a query string must NOT be treated as a group invite.
        self.assertEqual(maxactions._norm_link("https://max.ru/news?ref=join/x"),
                         "https://max.ru/news")


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_subscribes_and_reports_title(self):
        client = AsyncMock()
        client.invoke_method.side_effect = [
            {"payload": {"chat": {"id": -123, "title": "Канал Х"}}},  # opcode 57
            {"payload": {}},  # opcode 75 subscribe
        ]
        res = await maxactions.join(client, "@kanalx")
        self.assertIn("вступили", res.text.lower())
        self.assertIn("Канал Х", res.text)
        self.assertEqual(client.invoke_method.call_args_list[0].kwargs["opcode"], 57)
        sub = client.invoke_method.call_args_list[1].kwargs
        self.assertEqual(sub["opcode"], 75)
        self.assertTrue(sub["payload"]["subscribe"])

    async def test_join_bad_link(self):
        client = AsyncMock()
        res = await maxactions.join(client, "не ссылка")
        self.assertIn("ссылк", res.text.lower())
        client.invoke_method.assert_not_called()

    async def test_join_reports_max_error(self):
        client = AsyncMock(invoke_method=AsyncMock(return_value={"payload": {"error": "not.found"}}))
        res = await maxactions.join(client, "@nope")
        self.assertIn("не дал вступить", res.text)


class StartDmTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_by_user_id_not_chat_id(self):
        # Opens a 1:1 dialog by user_id: opcode 64 with `userId` (NOT `chatId`),
        # which is what makes MAX create the dialog and return its real chatId.
        client = AsyncMock(invoke_method=AsyncMock(
            return_value={"payload": {"chatId": 7268926, "message": {"id": 1}}}))
        res = await maxactions.start_dm(client, "21243808", "привет")
        self.assertIn("Отправлено", res.text)
        call = client.invoke_method.call_args.kwargs
        self.assertEqual(call["opcode"], 64)
        self.assertEqual(call["payload"]["userId"], 21243808)
        self.assertNotIn("chatId", call["payload"])       # never in the chatId slot
        self.assertEqual(call["payload"]["message"]["text"], "привет")

    async def test_rejects_unknown_recipient(self):
        client = AsyncMock()
        res = await maxactions.start_dm(client, "не-число", "привет")
        self.assertIn("Кому писать", res.text)
        client.invoke_method.assert_not_called()

    async def test_phone_recipient_resolved_then_dm(self):
        # /dm by phone: resolve to a user_id (opcode 46), then send by userId.
        client = AsyncMock(invoke_method=AsyncMock(side_effect=[
            {"payload": {"contact": {"id": 21243808}}},        # opcode 46 lookup
            {"payload": {"chatId": 7, "message": {"id": 1}}},  # opcode 64 send
        ]))
        res = await maxactions.start_dm(client, "+7 999 123-45-67", "привет")
        self.assertIn("Отправлено", res.text)
        calls = client.invoke_method.call_args_list
        self.assertEqual(calls[0].kwargs["opcode"], 46)
        self.assertEqual(calls[0].kwargs["payload"]["phone"], "+79991234567")
        self.assertEqual(calls[1].kwargs["opcode"], 64)
        self.assertEqual(calls[1].kwargs["payload"]["userId"], 21243808)

    async def test_rejects_empty_text(self):
        client = AsyncMock()
        res = await maxactions.start_dm(client, "5", "   ")
        self.assertIn("Пустое", res.text)
        client.invoke_method.assert_not_called()

    async def test_surfaces_max_error(self):
        client = AsyncMock(invoke_method=AsyncMock(
            return_value={"payload": {"error": "user.not.found"}}))
        res = await maxactions.start_dm(client, "5", "привет")
        self.assertIn("не принял", res.text)


if __name__ == "__main__":
    unittest.main()
