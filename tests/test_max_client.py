"""Unit tests for max_client.BrowserMaxClient.

BrowserMaxClient subclasses vkmax's MaxClient to (1) send browser-like
handshake headers so the MAX WebSocket doesn't 403, (2) reimplement
login_by_token/send_code without the KeyError('phone') crash, and (3) harden
the receive loop, invoke timeout, and teardown. These are characterization
tests: every WebSocket and event-loop interaction is mocked, so no real socket
is ever opened and the live recv/keepalive loops never run.
"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import max_client
from max_client import BrowserMaxClient, MaxAuthError


def _swallow_coro(coro, *_a, **_k):
    """create_task stand-in: close the coroutine so it isn't 'never awaited',
    and hand back a cancellable stand-in task."""
    coro.close()
    return MagicMock()


class _FakeConn:
    """An async-iterable fake WebSocket connection yielding preset frames."""

    def __init__(self, frames=None):
        self._frames = list(frames or [])
        self.closed = False
        self.wait_closed = AsyncMock()
        self.close = AsyncMock()

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for frame in self._frames:
            yield frame


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_injects_origin_and_chrome_user_agent(self):
        client = BrowserMaxClient()
        fake_conn = _FakeConn()
        fake_ws = MagicMock()
        fake_ws.connect = AsyncMock(return_value=fake_conn)
        # Stop the real recv loop from running; we only assert connect() wiring.
        with patch.object(max_client, "websockets", fake_ws), \
                patch.object(max_client, "WS_PROXY", None), \
                patch("max_client.asyncio.create_task",
                      side_effect=_swallow_coro) as create_task:
            result = await client.connect()

        self.assertIs(result, fake_conn)
        self.assertIs(client._connection, fake_conn)
        # Headers: positional arg is the host, kwargs carry the headers dict.
        self.assertEqual(fake_ws.connect.await_args.args[0], max_client.WS_HOST)
        headers = fake_ws.connect.await_args.kwargs["additional_headers"]
        self.assertEqual(headers["Origin"], "https://web.max.ru")
        self.assertIn("Chrome/137.0.0.0", headers["User-Agent"])
        # No proxy kwarg when WS_PROXY is unset.
        self.assertNotIn("proxy", fake_ws.connect.await_args.kwargs)
        # A receive task is spawned and the frame clock is initialised.
        create_task.assert_called_once()
        self.assertIsInstance(client._last_frame_time, float)

    async def test_connect_passes_proxy_when_configured(self):
        client = BrowserMaxClient()
        fake_ws = MagicMock()
        fake_ws.connect = AsyncMock(return_value=_FakeConn())
        with patch.object(max_client, "websockets", fake_ws), \
                patch.object(max_client, "WS_PROXY", "socks5://h:1080"), \
                patch("max_client.asyncio.create_task",
                      side_effect=_swallow_coro):
            await client.connect()
        self.assertEqual(
            fake_ws.connect.await_args.kwargs["proxy"], "socks5://h:1080")

    async def test_connect_raises_when_already_connected(self):
        client = BrowserMaxClient()
        client._connection = _FakeConn()
        with self.assertRaises(RuntimeError):
            await client.connect()


class RecvLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_pending_future_for_matching_seq(self):
        client = BrowserMaxClient()
        future = asyncio.get_running_loop().create_future()
        client._pending = {5: future}
        client._connection = _FakeConn(['{"seq": 5, "payload": {"ok": 1}}'])
        await client._recv_loop()
        self.assertTrue(future.done())
        self.assertEqual(future.result(), {"seq": 5, "payload": {"ok": 1}})
        # Future was popped from the pending map.
        self.assertNotIn(5, client._pending)

    async def test_does_not_set_result_on_already_done_future(self):
        # A future already resolved (e.g. timed out then a late reply arrives)
        # must not be re-set, which would raise InvalidStateError.
        client = BrowserMaxClient()
        future = asyncio.get_running_loop().create_future()
        future.set_result("first")
        client._pending = {5: future}
        client._connection = _FakeConn(['{"seq": 5, "payload": {}}'])
        await client._recv_loop()  # must not raise
        self.assertEqual(future.result(), "first")

    async def test_skips_unparseable_non_object_and_seqless_frames(self):
        client = BrowserMaxClient()
        dispatched = []

        async def callback(_c, packet):
            dispatched.append(packet)

        client._incoming_event_callback = callback
        client._connection = _FakeConn([
            "not json",                              # unparseable -> skipped
            "[1, 2, 3]",                             # JSON array -> skipped
            "42",                                    # JSON scalar -> skipped
            '{"opcode": 128, "payload": {"x": 1}}',  # event, no seq -> dispatch
        ])
        with self.assertLogs(max_client._logger, level="WARNING"):
            await client._recv_loop()
        await asyncio.sleep(0)  # let the create_task'd callback run
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["opcode"], 128)

    async def test_updates_last_frame_time_per_frame(self):
        client = BrowserMaxClient()
        client._last_frame_time = None
        client._connection = _FakeConn(['{"opcode": 1}'])
        await client._recv_loop()
        self.assertIsInstance(client._last_frame_time, float)

    async def test_callback_exception_is_caught_not_propagated(self):
        # The dispatch path itself raising must be logged, not bubble out of the
        # loop. We force the failure inside the try block via a poisoned pending
        # map whose .pop raises.
        client = BrowserMaxClient()

        class Exploding(dict):
            def pop(self, *_a, **_k):
                raise RuntimeError("boom")

        client._pending = Exploding()
        client._connection = _FakeConn(['{"seq": 1}'])
        with self.assertLogs(max_client._logger, level="ERROR"):
            await client._recv_loop()  # must not raise

    async def test_connection_error_ends_loop_with_warning(self):
        # An unexpected exception from iterating the socket ends the loop with a
        # warning rather than crashing the task.
        client = BrowserMaxClient()

        class BadConn:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise OSError("socket exploded")

        client._connection = BadConn()
        with self.assertLogs(max_client._logger, level="WARNING"):
            await client._recv_loop()  # must not raise

    async def test_cancelled_error_returns_cleanly(self):
        client = BrowserMaxClient()

        class CancellingConn:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise asyncio.CancelledError

        client._connection = CancellingConn()
        # Returns None without re-raising CancelledError.
        self.assertIsNone(await client._recv_loop())


class SendCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_token_and_sends_hello_then_start_auth(self):
        client = BrowserMaxClient()
        invoke = AsyncMock(side_effect=[
            {"payload": {}},                       # hello packet
            {"payload": {"token": "sms-tok"}},     # START_AUTH reply
        ])
        with patch.object(client, "invoke_method", invoke):
            token = await client.send_code("+79991234567")
        self.assertEqual(token, "sms-tok")
        # hello is opcode 6, auth request is opcode 17 with the phone payload.
        self.assertEqual(invoke.await_args_list[0].kwargs["opcode"], 6)
        auth_call = invoke.await_args_list[1]
        self.assertEqual(auth_call.kwargs["opcode"], 17)
        self.assertEqual(auth_call.kwargs["payload"]["phone"], "+79991234567")
        self.assertEqual(auth_call.kwargs["payload"]["type"], "START_AUTH")

    async def test_missing_token_raises_maxautherror_with_payload(self):
        client = BrowserMaxClient()
        invoke = AsyncMock(side_effect=[
            {"payload": {}},
            {"payload": {"error": "captcha.required"}},
        ])
        with patch.object(client, "invoke_method", invoke):
            with self.assertRaises(MaxAuthError) as ctx:
                await client.send_code("+79991234567")
        # The raw server payload is embedded so the caller can see why.
        self.assertIn("captcha.required", str(ctx.exception))


class LoginByTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_without_phone_does_not_raise_keyerror(self):
        # The whole reason this override exists: a successful token login whose
        # profile carries no "phone" must NOT crash.
        client = BrowserMaxClient()
        response = {"payload": {"profile": {"names": ["Me"]}}}  # no "phone"
        invoke = AsyncMock(side_effect=[{"payload": {}}, response])
        start_keepalive = AsyncMock()
        with patch.object(client, "invoke_method", invoke), \
                patch.object(client, "_start_keepalive_task", start_keepalive):
            result = await client.login_by_token("login-tok")
        self.assertIs(result, response)
        self.assertTrue(client._is_logged_in)
        start_keepalive.assert_awaited_once()
        # opcode 19 with the login token and the default sync counts.
        login_call = invoke.await_args_list[1]
        self.assertEqual(login_call.kwargs["opcode"], 19)
        self.assertEqual(login_call.kwargs["payload"]["token"], "login-tok")
        self.assertEqual(login_call.kwargs["payload"]["chatsCount"], 40)
        self.assertTrue(login_call.kwargs["payload"]["interactive"])

    async def test_custom_sync_counts_are_forwarded(self):
        client = BrowserMaxClient()
        invoke = AsyncMock(side_effect=[{"payload": {}}, {"payload": {}}])
        with patch.object(client, "invoke_method", invoke), \
                patch.object(client, "_start_keepalive_task", AsyncMock()):
            await client.login_by_token(
                "t", chats_sync=11, contacts_sync=22, chats_count=5)
        payload = invoke.await_args_list[1].kwargs["payload"]
        self.assertEqual(payload["chatsSync"], 11)
        self.assertEqual(payload["contactsSync"], 22)
        self.assertEqual(payload["chatsCount"], 5)

    async def test_error_payload_raises_and_does_not_log_in(self):
        client = BrowserMaxClient()
        invoke = AsyncMock(side_effect=[
            {"payload": {}},
            {"payload": {"error": "token.expired"}},
        ])
        start_keepalive = AsyncMock()
        with patch.object(client, "invoke_method", invoke), \
                patch.object(client, "_start_keepalive_task", start_keepalive):
            with self.assertRaises(MaxAuthError) as ctx:
                await client.login_by_token("dead-tok")
        self.assertIn("token.expired", str(ctx.exception))
        self.assertFalse(client._is_logged_in)
        start_keepalive.assert_not_awaited()


class InvokeMethodTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_keepalive_opcode_is_wrapped_in_timeout(self):
        client = BrowserMaxClient()
        sentinel = {"payload": {"ok": 1}}

        async def base_invoke(opcode, payload):
            return sentinel

        captured = {}

        async def fake_wait_for(coro, timeout):
            # Close the wrapped coroutine (we don't run it) and record the
            # timeout the production code asked for.
            coro.close()
            captured["timeout"] = timeout
            return sentinel

        with patch.object(max_client.MaxClient, "invoke_method",
                          side_effect=base_invoke, autospec=False), \
                patch("max_client.asyncio.wait_for", side_effect=fake_wait_for):
            result = await client.invoke_method(17, {"a": 1})
        self.assertIs(result, sentinel)
        # wait_for was given the configured MAX_INVOKE_TIMEOUT.
        self.assertEqual(captured["timeout"], max_client.MAX_INVOKE_TIMEOUT)

    async def test_keepalive_opcode_bypasses_timeout(self):
        client = BrowserMaxClient()
        sentinel = {"payload": {}}

        async def base_invoke(opcode, payload):
            return sentinel

        wait_for = AsyncMock()
        with patch.object(max_client.MaxClient, "invoke_method",
                          side_effect=base_invoke, autospec=False), \
                patch("max_client.asyncio.wait_for", wait_for):
            result = await client.invoke_method(1, {"interactive": False})
        self.assertIs(result, sentinel)
        # The keepalive ping (opcode 1) must NOT be time-limited.
        wait_for.assert_not_called()

    async def test_timeout_propagates_as_timeouterror(self):
        client = BrowserMaxClient()

        async def base_invoke(opcode, payload):
            await asyncio.sleep(10)  # never completes within the patched timeout

        with patch.object(max_client.MaxClient, "invoke_method",
                          side_effect=base_invoke, autospec=False), \
                patch.object(max_client, "MAX_INVOKE_TIMEOUT", 0.01):
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                await client.invoke_method(17, {})


class FailPendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unblocks_awaiters_with_connection_error(self):
        client = BrowserMaxClient()
        fut = asyncio.get_running_loop().create_future()
        client._pending = {1: fut}
        client._fail_pending()
        self.assertTrue(fut.done())
        with self.assertRaises(ConnectionError):
            fut.result()
        self.assertEqual(client._pending, {})

    async def test_skips_already_done_future(self):
        client = BrowserMaxClient()
        done = asyncio.get_running_loop().create_future()
        done.set_result("kept")
        client._pending = {1: done}
        client._fail_pending()
        # An already-resolved future keeps its result; map is still cleared.
        self.assertEqual(done.result(), "kept")
        self.assertEqual(client._pending, {})

    def test_empty_pending_is_a_clean_noop(self):
        client = BrowserMaxClient()
        client._pending = {}
        client._fail_pending()  # early return, must not raise
        self.assertEqual(client._pending, {})

    def test_missing_pending_attr_is_a_clean_noop(self):
        client = BrowserMaxClient()
        client._pending = None
        client._fail_pending()  # getattr falsy -> early return


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_tears_down_tasks_connection_and_pending(self):
        client = BrowserMaxClient()
        keepalive = MagicMock()
        recv = MagicMock()
        client._keepalive_task = keepalive
        client._recv_task = recv
        fut = asyncio.get_running_loop().create_future()
        client._pending = {7: fut}
        conn = _FakeConn()
        client._connection = conn

        await client.disconnect()

        keepalive.cancel.assert_called_once()
        self.assertIsNone(client._keepalive_task)
        recv.cancel.assert_called_once()
        conn.close.assert_awaited_once()
        self.assertIsNone(client._connection)
        # Pending futures were failed so awaiting callers unblock.
        self.assertTrue(fut.done())
        with self.assertRaises(ConnectionError):
            fut.result()

    async def test_safe_when_never_logged_in(self):
        # No keepalive task, no recv task, no connection: tear down what exists
        # without raising (vkmax's disconnect would raise here).
        client = BrowserMaxClient()
        client._keepalive_task = None
        client._recv_task = None
        client._connection = None
        client._pending = {}
        await client.disconnect()  # must not raise
        self.assertIsNone(client._connection)


class WaitClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_connection_when_present(self):
        client = BrowserMaxClient()
        conn = _FakeConn()
        client._connection = conn
        await client.wait_closed()
        conn.wait_closed.assert_awaited_once()

    async def test_noop_when_no_connection(self):
        client = BrowserMaxClient()
        client._connection = None
        await client.wait_closed()  # must not raise


class SecondsSinceLastFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_before_any_frame(self):
        client = BrowserMaxClient()
        # __init__ never sets _last_frame_time; getattr default is None.
        self.assertIsNone(client.seconds_since_last_frame())

    async def test_returns_none_when_explicitly_unset(self):
        client = BrowserMaxClient()
        client._last_frame_time = None
        self.assertIsNone(client.seconds_since_last_frame())

    async def test_returns_elapsed_seconds_after_a_frame(self):
        client = BrowserMaxClient()
        loop = asyncio.get_running_loop()
        client._last_frame_time = loop.time() - 5.0
        elapsed = client.seconds_since_last_frame()
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 4.0)


class HelloPacketTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_opcode_6_with_current_app_version(self):
        client = BrowserMaxClient()
        invoke = AsyncMock(return_value={"payload": {}})
        with patch.object(client, "invoke_method", invoke):
            await client._send_hello_packet()
        self.assertEqual(invoke.await_args.kwargs["opcode"], 6)
        ua = invoke.await_args.kwargs["payload"]["userAgent"]
        self.assertEqual(ua["appVersion"], max_client.APP_VERSION)
        self.assertEqual(ua["headerUserAgent"], max_client.USER_AGENT)
        # A fresh device id is generated each call.
        self.assertIn("deviceId", invoke.await_args.kwargs["payload"])


class MaxAuthErrorPayloadTests(unittest.TestCase):
    def test_send_code_error_message_is_pretty_json(self):
        # Sanity: the embedded payload is valid pretty-printed JSON so a human
        # reading the log can parse it. (Built the same way send_code does.)
        payload = {"error": "captcha", "details": {"id": 1}}
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        self.assertEqual(json.loads(rendered), payload)


if __name__ == "__main__":
    unittest.main()
