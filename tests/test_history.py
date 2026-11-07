from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from multiagent.loop import AgentLoop as Agent
from multiagent.prompts import SYSTEM, TOOLS
from multiagent.storage import ContextHistory


def entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replay(records):
    messages = []
    for entry in records:
        if entry["type"] in {"session_start", "context_resume", "loop_error"}:
            messages = deepcopy(entry["messages"])
        elif entry["type"] == "message":
            messages = messages[:entry["index"]] + [deepcopy(entry["message"])]
        elif entry["type"] == "context_rollback":
            del messages[entry["from_index"]:]
        elif entry["type"] == "image_attachments_released":
            for index in entry["indices"]:
                messages[index].pop("_image_attachment", None)
    return messages


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def history(self):
        history = ContextHistory(self.root, system=SYSTEM, tools=TOOLS, model="test-model")
        self.addCleanup(history.close)
        return history

    def test_startup_records_system_and_tools_and_never_overwrites_a_file(self):
        instant = datetime(2026, 10, 16, 12, 34, 56, 123456, tzinfo=timezone.utc)
        with patch("multiagent.storage.datetime") as clock:
            clock.now.return_value = instant
            first, second = self.history(), self.history()
        self.assertNotEqual(first.path, second.path)
        self.assertRegex(first.path.name, r"^context_\d{8}_\d{6}_\d{6}\.jsonl$")
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        start = entries(first.path)[0]
        self.assertEqual(start["messages"], [{"role": "system", "content": SYSTEM}])
        self.assertEqual(start["tools"], TOOLS)
        self.assertEqual(start["model"], "test-model")

    def test_full_messages_signatures_and_outputs_survive_resume_in_archive(self):
        history, client, sandbox = self.history(), Mock(), Mock()
        response = {"role": "assistant", "content": "读取中文文件", "tool_calls": [{
            "id": "one", "type": "function", "function": {"name": "python", "arguments": '{"code":"print(42)"}'},
            "extra_content": {"google": {"thought_signature": "signature-preserved"}}}]}
        client.complete.side_effect = [response, {"role": "assistant", "content": "处理完成"}, KeyboardInterrupt()]
        output = "完整输出" * 2000
        sandbox.execute.return_value = {"stdout": output, "exit_code": 0}
        agent = Agent(client, sandbox, history=history)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        records = entries(history.path)
        self.assertEqual(replay(records), agent.messages)
        self.assertIn(response, [e["message"] for e in records if e["type"] == "message"])
        tool = next(e["message"] for e in records if e["type"] == "message" and e["message"]["role"] == "tool")
        self.assertEqual(json.loads(tool["content"])["stdout"], output)
        restored = Agent(client, sandbox, history=history)
        restored.restore(agent.messages, iteration=agent.model_step, tool_count=agent.tool_count)
        self.assertEqual(replay(entries(history.path)), restored.messages)
        self.assertIn("signature-preserved", history.path.read_text())
        self.assertEqual([e["sequence"] for e in entries(history.path)], list(range(1, len(entries(history.path)) + 1)))

    def test_interrupted_batch_and_stop_record_are_committed_to_journal(self):
        history, client, sandbox = self.history(), Mock(), Mock()
        calls = [{"id": identifier, "type": "function", "function": {"name": "python", "arguments": '{"code":"print(42)"}'}}
                 for identifier in ("one", "two", "three")]
        client.complete.return_value = {"role": "assistant", "tool_calls": calls}
        sandbox.execute.side_effect = [{"stdout": "42"}, KeyboardInterrupt()]
        agent = Agent(client, sandbox, history=history)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        records = entries(history.path)
        self.assertEqual(replay(records), agent.messages)
        stopped = next(e for e in records if e["type"] == "loop_error")
        self.assertEqual(stopped["reason"], "cancelled")
        results = [json.loads(m["content"]) for m in stopped["messages"] if m["role"] == "tool"]
        self.assertEqual(results[0]["stdout"], "42")
        self.assertEqual([r["status"] for r in results[1:]], ["interrupted", "not_started"])
        self.assertIn("[Agent停止记录]", stopped["messages"][-1]["content"])

    def test_replay_and_live_context_keep_explicit_view_image_snapshots(self):
        history, client, sandbox = self.history(), Mock(), Mock()
        call = {"id": "image", "type": "function", "function": {"name": "view_image", "arguments": '{"path":"image.png"}'}}
        client.complete.side_effect = [{"role": "assistant", "tool_calls": [call]}, KeyboardInterrupt()]
        sandbox.execute.return_value = {"image": {"path": "/workspace/image.png", "mime_type": "image/png",
            "width": 1, "height": 1, "bytes": 3, "sha256": "test-image-sha"}, "_image_data": "data:image/png;base64,YWJj"}
        agent = Agent(client, sandbox, history=history)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        records = entries(history.path)
        self.assertEqual(records[0]["format_version"], 5)
        self.assertEqual(replay(records), agent.messages)
        snapshot = next(e["message"] for e in records if e["type"] == "message" and e["message"]["role"] == "tool")
        self.assertEqual(snapshot["_image_attachment"]["data_url"], "data:image/png;base64,YWJj")

    def test_history_is_flushed_even_if_the_process_exits_without_cleanup(self):
        program = ("import os,sys\nfrom multiagent.storage import ContextHistory\n"
                   "h=ContextHistory(sys.argv[1],system='system prompt',tools=[],model='test')\n"
                   "h.record('message',index=1,message={'role':'user','content':'中文任务'})\n"
                   "os._exit(0)\n")
        result = subprocess.run([sys.executable, "-c", program, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = entries(next(self.root.glob("context_*.jsonl")))
        self.assertEqual(records[0]["messages"][0]["content"], "system prompt")
        self.assertEqual(records[1]["message"]["content"], "中文任务")





if __name__ == "__main__":
    unittest.main()
