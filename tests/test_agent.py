import io
import http.client
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock
import urllib.error

from multiagent.loop import AgentLoop as Agent
from multiagent.client import Client
from multiagent.prompts import TOOLS
from multiagent.errors import AgentError
from multiagent.client import NoRedirect


def call(identifier="call-1", name="python", arguments='{"code":"print(42)"}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments},
            "extra_content": {"google": {"thought_signature": "test-signature"}}}


class AgentTests(unittest.TestCase):
    def test_mixed_tool_batch_preserves_order_signatures_and_context(self):
        self.assertEqual([tool["function"]["name"] for tool in TOOLS], [])
        client, sandbox = Mock(), Mock()
        calls = [call("shell", "bash", '{"command":"echo 42"}'), call("restart", "restart_python", '{}'), call("check")]
        response = {"role": "assistant", "tool_calls": calls, "extra_content": {"provider": "preserved"}}
        client.complete.side_effect = [response, KeyboardInterrupt()]
        sandbox.execute.return_value = {"exit_code": 0}
        task = {"role": "user", "content": "分析数据，需要的依赖自行准备", "_agent_message_id": "execution"}
        agent = Agent(client, sandbox, input_provider=lambda messages: [] if any(m.get("_agent_message_id") == "execution" for m in messages) else [task])
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual([c.args for c in sandbox.execute.call_args_list], [(c["function"]["name"], c["function"]["arguments"]) for c in calls])
        self.assertEqual([m["tool_call_id"] for m in agent.messages if m["role"] == "tool"], ["shell", "restart", "check"])
        self.assertEqual(agent.messages[1], task)
        self.assertEqual(agent.messages[2], response)
        sandbox.reset_python.assert_not_called()

    def test_disabled_network_remains_in_own_system(self):
        sandbox = Mock()
        sandbox.network = False
        agent = Agent(Mock(), sandbox)
        self.assertIn("Session setting: network access is disabled.", agent.messages[0]["content"])
        self.assertFalse(hasattr(agent, "turn"))

    def test_no_model_call_count_limit(self):
        client, sandbox = Mock(), Mock()
        client.complete.side_effect = [{"role": "assistant", "tool_calls": [call(str(i))]} for i in range(20)] + [KeyboardInterrupt()]
        sandbox.execute.return_value = {"stdout": "42"}
        agent = Agent(client, sandbox)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual(client.complete.call_count, 21)
        self.assertEqual(sandbox.execute.call_count, 20)
        self.assertEqual(agent.model_step, 21)
        self.assertEqual(agent.tool_count, 20)

    def test_invalid_batch_rejected_before_execution(self):
        client, sandbox = Mock(), Mock()
        client.complete.return_value = {"role": "assistant", "tool_calls": [call(), call()]}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Agent(client, sandbox).run()
        sandbox.execute.assert_not_called()


class CancellationContextTests(unittest.TestCase):
    def test_cancellation_boundaries_keep_committed_result_and_close_pending(self):
        for boundary in ("model_end", "tool_start", "tool_end"):
            with self.subTest(boundary=boundary):
                client, sandbox = Mock(), Mock()
                response = {"role": "assistant", "tool_calls": [call("one"), call("two")]}
                client.complete.return_value = response
                sandbox.execute.return_value = {"stdout": "committed"}
                def observe(event):
                    if event["type"] == boundary:
                        raise KeyboardInterrupt()
                agent = Agent(client, sandbox, on_event=observe)
                with self.assertRaises(KeyboardInterrupt):
                    agent.run()
                self.assertEqual(agent.messages[1], response)
                outputs = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
                self.assertEqual(outputs[0], {"stdout": "committed"} if boundary == "tool_end" else {"status": "not_started", "error": "Agent 已停止，此操作未提交完整结果；文件可能已改变，恢复后先核验。"})
                self.assertEqual(outputs[1]["status"], "not_started")
                retained = deepcopy(agent.messages)
                resumed = Agent(client, sandbox)
                resumed.restore(retained, iteration=agent.model_step, tool_count=agent.tool_count)
                self.assertEqual(resumed.messages, retained)


class ClientTests(unittest.TestCase):
    def test_truncated_chunked_response_becomes_agent_error(self):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "partial"}}]}).encode()
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + f"{len(body) + 43:x}\r\n".encode() + body)
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(sock)
        response.begin()
        client = Client("test-only-key")
        client.opener = Mock()
        client.opener.open.return_value = response
        with self.assertRaisesRegex(AgentError, "IncompleteRead"):
            client.complete([{"role": "user", "content": "你好"}])
        client.opener.open.assert_called_once()

    def test_http_protocol_errors_do_not_escape_client(self):
        for failure in (http.client.BadStatusLine("invalid status"),
                        http.client.IncompleteRead(b"partial", 43)):
            with self.subTest(error=type(failure).__name__):
                client = Client("test-only-key")
                client.opener = Mock()
                client.opener.open.side_effect = failure
                with self.assertRaises(AgentError):
                    client.complete([])

    def test_truncated_http_error_body_retains_status_code(self):
        body = Mock()
        body.read.side_effect = http.client.IncompleteRead(b"partial", 43)
        client = Client("test-only-key")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(client.url, 502, "bad gateway", {}, body)
        with self.assertRaisesRegex(AgentError, "API HTTP 502"):
            client.complete([])

    def test_request_contract(self):
        client = Client("test-key", "https://gateway.invalid/v1", "fixture-model")
        message = {"role": "assistant", "content": "done"}
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())
        self.assertEqual(client.complete([{"role": "user", "content": "你好"}]), message)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "fixture-model")
        self.assertNotIn("tools", body)
        self.assertEqual(TOOLS, [])
        self.assertTrue(body["stream"])
        self.assertNotIn("reasoning_effort", body)

    def test_http_error_redacts_key(self):
        client = Client("test-secret")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(client.url, 401, "bad key", {}, io.BytesIO(b"bad test-secret"))
        with self.assertRaises(AgentError) as caught:
            client.complete([])
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertIn("401", str(caught.exception))

    def test_malformed_and_incomplete_responses(self):
        for payload in (b"not json", b'{}', b'{"choices":[]}',
                        b'{"choices":[{"finish_reason":"length","message":{"role":"assistant","content":"partial"}}]}'):
            with self.subTest(payload=payload):
                client = Client("test-key")
                client.opener = Mock()
                client.opener.open.return_value = io.BytesIO(payload)
                with self.assertRaises(AgentError):
                    client.complete([])

    def test_rejects_unsafe_urls_and_redirects(self):
        for url in ("http://example.com/v1", "https://user:password@example.com", "https://example.com?key=x"):
            with self.assertRaises(AgentError):
                Client("test-key", url)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))


if __name__ == "__main__":
    unittest.main()
