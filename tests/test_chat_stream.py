from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch

from multiagent.loop import AgentLoop as Agent
from endless_support import drive
from multiagent.client import Client
from multiagent.errors import AgentError


def frame(value):
    return b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\r\n\r\n'


class Response(io.BytesIO):
    status = 200
    headers = {'content-type': 'text/event-stream; charset=utf-8', 'x-request-id': 'stream-fixture'}


class StreamTests(unittest.TestCase):
    def client(self, payload):
        client = Client('test-secret')
        client.opener = Mock()
        client.opener.open.return_value = Response(payload)
        return client

    def test_text_multiline_events_usage_and_done_without_eof(self):
        payload = (b': keepalive\r\n\r\n' +
                   b'data: {"model":"fixture","choices":\r\ndata: [{"index":0,"delta":{"role":"assistant"}}]}\r\n\r\n' +
                   frame({'choices': [{'index': 0, 'delta': {'content': '你'}}]}) +
                   frame({'choices': [{'index': 0, 'delta': {'content': '好'}, 'finish_reason': 'stop'}]}) +
                   frame({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12,
                       'prompt_tokens_details': {'cached_tokens': 4}, 'completion_tokens_details': {'reasoning_tokens': 1}}}) +
                   b'data: [DONE]\r\n\r\nTHIS MUST NOT BE READ')
        client = self.client(payload)
        self.assertEqual(client.complete([{'role': 'user', 'content': '你好'}]), {'role': 'assistant', 'content': '你好'})
        self.assertEqual(client.last_usage['total_tokens'], 12)
        self.assertEqual(client.last_call['cached_tokens'], 4)
        self.assertEqual(client.last_call['reasoning_tokens'], 1)
        self.assertEqual(client.last_call['heartbeat_count'], 1)
        self.assertIn('first_output_seconds', client.last_call)
        self.assertEqual(client.last_call['phase'], 'complete')

    def test_reasoning_alias_fragments_survive_tools_and_event_projection(self):
        from multiagent.protocol import REASONING_FIELDS, readable_reasoning
        for field in REASONING_FIELDS:
            with self.subTest(field=field):
                chunks = [
                    {"role": "assistant", field: "**核对**输入，"},
                    {field: "再读取文件。"},
                    {field: None, "tool_calls": [{"index": 0, "id": "sleep-note", "type": "function",
                        "function": {"name": "sleep", "arguments": '{"seconds":0}'},
                        "extra_content": {"google": {"thought_signature": "original-signature"}}}]},
                ]
                payload = b"".join(frame({"choices": [{"delta": chunk}]}) for chunk in chunks)
                payload += frame({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]" + bytes([10, 10])
                client = self.client(payload)
                message = client.complete([{"role": "user", "content": "检查配置"}])
                self.assertEqual(message[field], "**核对**输入，再读取文件。")
                self.assertEqual(readable_reasoning(message), message[field])
                self.assertIn("first_output_seconds", client.last_call)
                original = deepcopy(message)
                model = Mock()
                model.complete.side_effect = [message, KeyboardInterrupt()]
                executor = Mock()
                executor.execute.return_value = {"reason": "timeout"}
                events = []
                loop = Agent(model, executor, on_event=events.append)
                with self.assertRaises(KeyboardInterrupt):
                    loop.run()
                event = next(item for item in events if item["type"] == "model_end")
                self.assertEqual(event["reasoning_content"], original[field])
                self.assertEqual(loop.messages[1], original)
                self.assertEqual(message, original)
                self.assertEqual(executor.execute.call_count, 1)

    def test_reasoning_projection_ignores_opaque_fields_and_keeps_model_provenance(self):
        from multiagent.protocol import readable_reasoning
        from multiagent.models import model_messages
        original = {"role": "assistant", "content": "已核对。", "thinking": "检查 **配置**。",
                    "_model": "source-model", "difrost_reasoning": {"value": "opaque-signature"}}
        saved = deepcopy(original)
        self.assertEqual(readable_reasoning(original), "检查 **配置**。")
        translated = model_messages([original], "another-model")[0]
        self.assertIn("检查 **配置**。", translated["content"])
        self.assertIn("source-model", translated["content"])
        self.assertNotIn("thinking", translated)
        self.assertNotIn("difrost_reasoning", translated)
        self.assertEqual(original, saved)
        self.assertEqual(model_messages([original], "source-model")[0]["thinking"], original["thinking"])
        self.assertIsNone(readable_reasoning({"thinking": {"signature": "opaque"},
                                             "reasoning": 100, "reasoning_content": "   ",
                                             "difrost_reasoning": "opaque"}))
        self.assertEqual(readable_reasoning({"reasoning_content": "摘要", "thinking": "同一摘要"}),
                         "摘要")

    def test_interleaved_tool_arguments_and_late_gemini_signatures(self):
        chunks = [
            {'tool_calls': [{'index': 1, 'id': 'bash-1', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{"command":'}}]},
            {'tool_calls': [{'index': 0, 'id': 'py-1', 'type': 'function', 'function': {'name': 'py', 'arguments': '{"code":"print('}}]},
            {'tool_calls': [{'index': 0, 'function': {'name': 'thon', 'arguments': '42)"}'}},
                            {'index': 1, 'function': {'arguments': '"pwd"}'}}]},
            {'tool_calls': [{'index': 0, 'extra_content': {'google': {'thought_signature': 'complete-signature'}}}]},
        ]
        payload = b''.join(frame({'choices': [{'index': 0, 'delta': chunk}]}) for chunk in chunks)
        payload += frame({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}) + b'data: [DONE]\n\n'
        client = self.client(payload)
        client.opener.open.side_effect = [Response(payload), io.BytesIO(json.dumps({'choices': [{'message': {
            'role': 'assistant', 'content': 'done'}}]}).encode())]
        sandbox = Mock()
        sandbox.execute.return_value = {'stdout': '42', 'exit_code': 0}
        agent = Agent(client, sandbox)
        self.assertEqual(drive(agent, 'run'), 'done')
        self.assertEqual([c.args for c in sandbox.execute.call_args_list],
                         [('python', '{"code":"print(42)"}'), ('bash', '{"command":"pwd"}')])
        self.assertEqual(agent.messages[2]['tool_calls'][0]['extra_content']['google']['thought_signature'], 'complete-signature')
        self.assertEqual(agent.messages[2]['tool_calls'][1]['id'], 'bash-1')

    def test_null_delta_fields_do_not_clear_role_or_completed_tool_metadata(self):
        chunks = [
            {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'py-1', 'type': 'function',
                'function': {'name': 'python', 'arguments': '{"code":'}}]},
            {'role': None, 'tool_calls': [{'index': 0, 'id': None, 'type': None,
                'function': {'name': None, 'arguments': '"print(42)"}'}}]},
        ]
        payload = b''.join(frame({'choices': [{'index': 0, 'delta': delta}]}) for delta in chunks)
        payload += frame({'choices': [{'delta': {'role': None}, 'finish_reason': 'tool_calls'}]}) + b'data: [DONE]\n\n'
        client = self.client(payload)
        message = client.complete([{'role': 'user', 'content': 'run'}])
        self.assertEqual(message['role'], 'assistant')
        self.assertEqual(message['tool_calls'][0], {'id': 'py-1', 'type': 'function',
            'function': {'name': 'python', 'arguments': '{"code":"print(42)"}'}})

    def test_gateway_empty_role_tail_preserves_text_and_vendor_metadata(self):
        payload = frame({'choices': [{'delta': {'role': 'assistant', 'content': 'Hello!'}}]})
        payload += frame({'choices': [{'delta': {'role': '', 'content': None, 'tool_calls': None,
            'difrost_reasoning': {'r00': {'value': 'signature', 'scope': 'internal_redactable'}}}}]})
        payload += frame({'choices': [{'delta': {'role': ''}, 'finish_reason': 'stop'}]}) + b'data: [DONE]\n\n'
        message = self.client(payload).complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(message['role'], 'assistant')
        self.assertEqual(message['content'], 'Hello!')
        self.assertEqual(message['difrost_reasoning']['r00']['value'], 'signature')

    def test_interrupted_stream_never_executes_partial_tool_code(self):
        client = self.client(frame({'choices': [{'index': 0, 'delta': {'tool_calls': [{
            'index': 0, 'id': 'partial', 'type': 'function', 'function': {'name': 'python', 'arguments': '{"code":"print('}}]}}]}))
        sandbox = Mock()
        agent = Agent(client, sandbox)
        with self.assertRaisesRegex(AgentError, r'before \[DONE\]'):
            drive(agent, 'run')
        sandbox.execute.assert_not_called()
        self.assertEqual(client.last_call['error_category'], 'incomplete_response')
        self.assertEqual(agent.messages[1]["role"], "user")
        self.assertTrue(agent.messages[-1]["content"].startswith("[Agent停止记录]"))

    def test_http_200_stream_error_is_not_a_success_and_is_redacted(self):
        client = self.client(frame({'error': {'type': 'upstream_unavailable', 'code': 'credential_unavailable',
                                           'message': 'expired test-secret'}}))
        with self.assertRaisesRegex(AgentError, 'credential_unavailable') as error:
            client.complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(client.last_call['http_status'], 200)
        self.assertEqual(client.last_call['error_category'], 'upstream_credentials')
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertNotIn('test-secret', str(error.exception) + json.dumps(client.last_call))

    def test_cancel_during_stream_keeps_completed_tool_and_discards_partial_message(self):
        class Interrupted(Response):
            def readline(self, *args):
                raw = super().readline(*args)
                if not raw: raise KeyboardInterrupt()
                return raw
        partial = Interrupted(frame({'choices': [{'delta': {'content': 'uncommitted partial text'}}]}))
        tool = {'id': 'one', 'type': 'function', 'function': {'name': 'python', 'arguments': '{"code":"print(42)"}'}}
        client = self.client(b'')
        client.opener.open.side_effect = [io.BytesIO(json.dumps({'choices': [{'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [tool]}}]}).encode()), partial]
        sandbox = Mock()
        sandbox.execute.return_value = {'stdout': '42', 'exit_code': 0}
        agent = Agent(client, sandbox)
        with self.assertRaises(KeyboardInterrupt): drive(agent, 'compute')
        self.assertEqual(sandbox.execute.call_count, 1)
        self.assertEqual(json.loads(agent.messages[3]['content'])['stdout'], '42')
        self.assertTrue(agent.messages[-1]['content'].startswith('[Agent停止记录]\n'))
        self.assertNotIn('uncommitted partial text', json.dumps(agent.messages))
        self.assertTrue(partial.closed)
        self.assertEqual(client.last_call['phase'], 'cancelled')

    def test_stream_size_and_tool_count_limits_apply_before_execution(self):
        for payload, limit in [(frame({'choices': [{'delta': {'content': 'x' * 200}}]}), 100),
                               (frame({'choices': [{'delta': {'tool_calls': [{'index': 16}]}}]}), 4096)]:
            with self.subTest(limit=limit):
                client = self.client(payload)
                sandbox = Mock()
                with patch('multiagent.client.MAX_RESPONSE_BYTES', limit), self.assertRaises(AgentError):
                    drive(Agent(client, sandbox), 'run')
                sandbox.execute.assert_not_called()


if __name__ == '__main__': unittest.main()
