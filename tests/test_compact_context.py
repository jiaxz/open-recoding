"""Compact API views keep the original messages, receipts and evidence intact."""

from copy import deepcopy
import json
import unittest

from workspace_support import temporary_workspace

from multiagent.context_window import token_units
from multiagent.tools import HostTools
from multiagent.messages import Mailbox
from multiagent.multimodal import make_tool_message, to_api_messages
from multiagent.prompts import compose_system
from multiagent.worker import format_input_events
from multiagent.prompts import TEAM_SYSTEM
from multiagent.system import MultiAgentSystem
from multiagent.tools import model_result_view
from session.store import SessionStore


class CompactContextTests(unittest.TestCase):
    def test_peer_envelope_is_slim_on_wire_and_complete_in_events(self):
        original = {"id": "mail-1", "source": "peer", "target": "master", "kind": "message",
                    "timestamp": 123.5, "attempt": 4, "broadcast_id": "batch-1", "content": "Verified result",
                    "images": [{"path": "/workspace/.recoding/inputs/one.png", "source": "/workspace/plot.png",
                                "sha256": "digest", "width": 800, "height": 600, "bytes": 10000}]}
        records = [deepcopy(original)]
        mailbox = Mailbox(target="master", records=lambda: records)
        pending = mailbox.pending([])
        raw = deepcopy(pending)
        wire = to_api_messages(pending)[0]
        self.assertIn('"from":"peer"', wire["content"])
        self.assertIn("Verified result", wire["content"])
        self.assertIn("/workspace/.recoding/inputs/one.png", wire["content"])
        self.assertNotIn("timestamp", wire["content"])
        self.assertNotIn("broadcast_id", wire["content"])
        self.assertNotIn("digest", wire["content"])
        self.assertEqual(pending[0]["content"], original["content"])
        self.assertEqual(pending[0]["_agent_message"], {key: value for key, value in original.items() if key != "content"})
        self.assertNotIn("_agent_message", wire)
        budget_view = {key: value for key, value in pending[0].items() if key != "_agent_message"}
        self.assertEqual(token_units(pending), token_units([budget_view]))
        self.assertIsInstance(wire["content"], str, "Peer images remain paths, not pixels")
        events, offset = format_input_events(pending, step=1, target="master", offset=0, received_at=124)
        self.assertEqual(events[0]["messages"], [{**original, "input_order": 0}])
        self.assertEqual(offset, 1)
        self.assertEqual(records, [original])
        self.assertEqual(pending, raw)
        self.assertEqual(mailbox.pending(["mail-1"]), [])
        recalled = HostTools(object(), lambda: pending).execute("recall_context", "{}")
        record = json.loads(recalled["records"][0]["text"])
        self.assertEqual(record["agent_message"]["source"], "peer")
        self.assertEqual(record["agent_message"]["target"], "master")
        self.assertEqual(record["images"][0]["path"], original["images"][0]["path"])
        self.assertEqual(record["content"], original["content"])

    def test_system_notice_and_real_user_keep_distinct_provenance(self):
        records = [dict(id="notice", source="system", target="master", kind="system_message",
                        timestamp=1, content="Limit is now 6", configuration={"max_subagents": 6}),
                   dict(id="human", source="user", target="master", kind="user_input",
                        timestamp=2, content="My exact instruction")]
        pending = Mailbox(target="master", records=lambda: records).pending([])
        wire = to_api_messages(pending)
        self.assertIn("宿主通知，非用户消息", wire[0]["content"])
        self.assertEqual(wire[1]["content"], "<user_query>My exact instruction</user_query>")
        self.assertEqual(pending[0]["_system_message"]["configuration"], {"max_subagents": 6})
        events, _ = format_input_events(pending, step=2, target="master", offset=0, received_at=3)
        self.assertEqual([e["type"] for e in events], ["system_message_applied", "user_input_applied"])

    def test_full_receipt_stays_in_history_while_wire_omits_duplicate_metadata(self):
        result = {"broadcast_id": "batch", "queued": ["msg"], "images": [{"sha256": "sender-image"}],
                  "recipients": [{"agent_id": "peer", "message_id": "msg", "images": [{"path": "copy.png"}]}]}
        owner = object.__new__(MultiAgentSystem)
        owner.request = lambda *args, **kwargs: deepcopy(result)
        received = owner.dispatch("master", "send_agent_message", {})
        message, public = make_tool_message([], "send-1", received)
        self.assertEqual(public, result)
        self.assertEqual(json.loads(message["content"]), result)
        wire = json.loads(to_api_messages([message])[0]["content"])
        self.assertEqual(wire, {"recipients": [{"agent_id": "peer", "message_id": "msg"}]})
        recalled = HostTools(object(), lambda: [message]).execute("recall_context", '{"start":0,"limit":1}')
        record = json.loads(recalled["records"][0]["text"])
        self.assertEqual(json.loads(record["content"]), result)

    def test_agent_listing_retains_capabilities_status_identity_and_failure_flag(self):
        result = {"agents": [{"id": "peer", "name": "Analyst", "icon": "🔬", "status": "failed",
                              "model": "grok-4.7", "resumable": True, "error_available": True,
                              "permissions": {"network": False}, "phase": "failed", "created_at": 1,
                              "updated_at": 2, "attempt": 3}], "max_subagents": 6}
        view = model_result_view("list_agents", result)
        self.assertEqual(view["agents"][0]["permissions"], {"network": False})
        self.assertTrue(view["agents"][0]["error_available"])
        self.assertEqual(view["agents"][0]["id"], "peer")
        self.assertNotIn("created_at", view["agents"][0])
        self.assertEqual(view["max_subagents"], 6)
        self.assertIsNone(model_result_view("list_agents", {"error": "Failed to list"}))

    def test_nonempty_outputs_and_errors_are_never_truncated(self):
        output = "Verified evidence\n" * 1000
        result = {"stdout": output, "stderr": "Warning: check assumptions", "exit_code": 0, "truncated": False}
        message, public = make_tool_message([], "run", result)
        wire = json.loads(to_api_messages([message])[0]["content"])
        self.assertEqual(wire["stdout"], output)
        self.assertEqual(wire["stderr"], result["stderr"])
        self.assertNotIn("truncated", wire)
        self.assertEqual(public, result)
        error = {"error": "Traceback\n" + output, "stdout": "", "exit_code": 1,
                 "_model_result": {"error": "short"}}
        failed, _ = make_tool_message([], "failed", error)
        self.assertEqual(json.loads(to_api_messages([failed])[0]["content"])["error"], error["error"])
        failed_process = {"stdout": "", "stderr": output, "exit_code": 1, "truncated": False}
        failed, _ = make_tool_message([], "failed-process", failed_process)
        self.assertEqual(json.loads(to_api_messages([failed])[0]["content"]), failed_process)

    def test_projection_survives_resume_is_append_stable_and_does_not_inflate_budget(self):
        original = {"role": "tool", "tool_call_id": "list", "content": "x" * 10000,
                    "_model_content": '{"status":"ok"}'}
        compact = {key: value for key, value in original.items() if key != "_model_content"}
        compact["content"] = original["_model_content"]
        self.assertEqual(token_units([original]), token_units([compact]))
        before = to_api_messages([original])
        with temporary_workspace() as root:
            store = SessionStore.create(root, "compact", {})
            store.save_state({"messages": [{"role": "system", "content": "test"}, original],
                              "iteration": 1, "tool_count": 1})
            saved = store.load_state()["messages"]
            self.assertEqual(saved[1], original)
            self.assertEqual(to_api_messages(saved)[1:], before)
        self.assertEqual(to_api_messages([original, {"role": "user", "content": "Continue"}])[:1], before)
        assistant = {"role": "assistant", "content": "Original", "_model_content": "Cannot override"}
        self.assertEqual(to_api_messages([assistant])[0]["content"], "Original")

    def test_batch_advice_explains_sequential_execution_and_result_dependencies(self):
        system = compose_system(network=False, fragments=[TEAM_SYSTEM])
        self.assertIn("multiple tool calls in ONE model response", system)
        self.assertIn("SEQUENTIALLY", system)
        self.assertIn("Put sleep last", system)
        self.assertIn("Wait for results", system)
        self.assertIn("Actively find useful parallelism", system)
        self.assertNotIn("[任务取消记录]", system)


if __name__ == "__main__":
    unittest.main()
