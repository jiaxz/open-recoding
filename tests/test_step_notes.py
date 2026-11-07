"""Model notes survive signed history while tool executors see only business inputs."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from multiagent.tools import HostTools, CONTEXT_TOOLS
from multiagent.tools import HostTools, SLEEP_TOOL, combine_schemas, tool
from multiagent.loop import AgentLoop
from multiagent.tools import FIELD, attach_step_note, with_step_notes


class StepNoteTests(unittest.TestCase):
    def test_batch_preserves_original_calls_and_strips_notes_before_execution(self):
        schemas = combine_schemas(CONTEXT_TOOLS, [SLEEP_TOOL])
        original_schemas = deepcopy(schemas)
        exposed = with_step_notes(schemas)
        for schema in exposed:
            self.assertIn(FIELD, schema['function']['parameters']['required'])
        self.assertEqual(schemas, original_schemas)
        calls = [
            {'id': 'recall', 'type': 'function', 'function': {'name': 'recall_context',
                'arguments': json.dumps({'limit': 1, FIELD: '核对历史，再等待消息。'}, ensure_ascii=False)},
             'extra_content': {'google': {'thought_signature': 'original-signature'}}},
            {'id': 'sleep', 'type': 'function', 'function': {'name': 'sleep',
                'arguments': json.dumps({'seconds': 0.1, FIELD: '核对历史，再等待消息。'}, ensure_ascii=False)}},
        ]
        response = {'role': 'assistant', 'content': None, 'tool_calls': calls,
                    '_model': 'gemini-4-argon'}
        original_response = deepcopy(response)
        client = Mock()
        client.complete.side_effect = [response, KeyboardInterrupt()]
        rpc = Mock(return_value={'reason': 'timeout'})
        events = []
        executor = HostTools(rpc, lambda: loop.messages, schemas)
        loop = AgentLoop(client, executor, on_event=lambda event: events.append(
            attach_step_note(event) if event['type'] == 'model_end' else event))
        with self.assertRaises(KeyboardInterrupt):
            loop.run()
        self.assertEqual(response, original_response)
        self.assertEqual(loop.messages[1], original_response)
        self.assertEqual(next(event for event in events if event['type'] == 'model_end')['step_note'],
                         '核对历史，再等待消息。')
        rpc.assert_called_once_with('sleep', {'seconds': 0.1, 'seen': []})
        results = [json.loads(m['content']) for m in loop.messages if m['role'] == 'tool']
        self.assertIn('records', results[0])
        self.assertEqual(results[1], {'reason': 'timeout'})
        self.assertEqual(client.complete.call_count, 2)  # No extra request to generate prose.

    def test_text_is_never_overwritten_and_bad_or_missing_notes_are_not_invented(self):
        event = {'type': 'model_end', 'content': '原始正文', 'calls': [
            {'function': {'arguments': '{"_step_note":"简述"}'}},
            {'function': {'arguments': '{"_step_note":"简述"}'}},
            {'function': {'arguments': '{"_step_note":"另一项检查"}'}},
            {'function': {'arguments': '{"_step_note":null}'}},
            {'function': {'arguments': 'broken JSON'}},
        ]}
        original = deepcopy(event)
        result = attach_step_note(event)
        self.assertEqual(result['content'], '原始正文')
        self.assertEqual(result['step_note'], '简述\n\n另一项检查')
        self.assertEqual(event, original)
        self.assertNotIn('step_note', attach_step_note({'content': None, 'calls': []}))

    def test_outgoing_message_is_a_truthful_fallback_not_a_generated_summary(self):
        event = {"content": None, "calls": [{"function": {
            "name": "send_user_message", "arguments": '{"message":"已检查配置，暂未发现错误。"}'}}]}
        original = deepcopy(event)
        result = attach_step_note(event)
        self.assertEqual(result["step_note"], "已检查配置，暂未发现错误。")
        self.assertEqual(result["step_note_source"], "tool_message")
        self.assertEqual(event, original)
        event["calls"].append({"function": {"arguments": '{"_step_note":"已完成检查，发送报告。"}'}})
        result = attach_step_note(event)
        self.assertEqual(result["step_note"], "已完成检查，发送报告。")
        self.assertEqual(result["step_note_source"], "tool_arguments")

    def test_reserved_field_cannot_collide_and_unknown_tool_stays_rejected(self):
        collision = tool('bad', 'fixture', {FIELD: {'type': 'string'}})
        with self.assertRaisesRegex(ValueError, 'reserved'):
            with_step_notes([collision])
        rpc = Mock()
        executor = HostTools(rpc, lambda: [], [SLEEP_TOOL])
        result = executor.execute('not_installed', '{"_step_note":"ignored"}')
        self.assertIn('not installed', result['error'])
        rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
