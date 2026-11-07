"""Call-boundary preparation and compact Worker dispatch without a real API."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from multiagent.loop import AgentLoop
from multiagent.tools import HostTools, SLEEP_TOOL
from multiagent.worker import InterruptScope, WorkerRuntime


class WorkerTests(unittest.TestCase):
    def test_stop_is_armed_before_announcing_active(self):
        runtime = WorkerRuntime()
        runtime.commands = Mock()
        runtime.commands.get.return_value = {"action": "run"}
        runtime.saved, runtime.history, runtime.agent, runtime.rpc = None, Mock(), Mock(), Mock()
        def immediately_stop(kind, **fields):
            self.assertEqual(kind, "active")
            self.assertTrue(InterruptScope.active)
            raise KeyboardInterrupt()
        with patch("multiagent.worker.send", side_effect=immediately_stop):
            runtime.run()
        runtime.rpc.assert_not_called()
        runtime.agent.run.assert_not_called()
        self.assertEqual(runtime.terminal, {"reason": "stopped"})
        self.assertFalse(InterruptScope.active)

    def test_projection_precedes_single_drain_and_model_choice_is_applied_to_that_call(self):
        runtime = WorkerRuntime.__new__(WorkerRuntime)
        runtime.config = {"demo": False, "agent_id": "master"}
        runtime.model, runtime.run_id = "gemini-4-argon", "fixture-run"
        runtime.budget = Mock()
        runtime.compactor = Mock(state=None)
        runtime.compactor.view.side_effect = lambda messages: list(messages)
        runtime.compactor.saved_tokens.return_value = 0
        runtime.compactor.background_request.return_value = None
        runtime.last_input_tokens, runtime.image_view_calls = None, 0
        runtime.viewed_images, runtime.drawn_images = set(), set()
        queued, rpc_calls, requests = [], [], []
        def reload():
            queued.append({"role": "user", "content": "Arrived during projection loading",
                           "_agent_message_id": "fresh", "_user_input": {"timestamp": 1, "attachments": []}})
        runtime.compactor.reload.side_effect = reload
        def rpc(action, fields):
            rpc_calls.append(action)
            if action == "drain":
                records = queued[:]
                queued.clear()
                return {"messages": records, "model": "grok-4.7", "idle_sleep_seconds": 17}
            return {"reason": "timeout"}
        runtime.rpc = rpc
        runtime.client = Mock(model=runtime.model, last_usage={})
        def complete(messages):
            requests.append((runtime.client.model, deepcopy(messages)))
            raise KeyboardInterrupt()
        runtime.client.complete.side_effect = complete
        runtime.agent = AgentLoop(runtime.client, HostTools(rpc, lambda: runtime.agent.messages, [SLEEP_TOOL]),
                                  system="fixture", input_provider=runtime.pending_inputs,
                                  context_processor=runtime.process_context, on_event=runtime.observe)
        with patch("multiagent.worker.send"), self.assertRaises(KeyboardInterrupt):
            runtime.agent.run()
        self.assertEqual(rpc_calls, ["drain"])
        self.assertEqual(requests[0][0], "grok-4.7")
        self.assertEqual(requests[0][1][-1]["content"], "Arrived during projection loading")
        self.assertEqual(runtime.agent.idle_sleep_seconds, 17)
        self.assertEqual(runtime.budget.mark_request.call_args.args[0], requests[0][1])

    def test_recall_uses_local_dispatch_and_denies_peer_history(self):
        rpc = Mock()
        executor = HostTools(rpc, lambda: [{"role": "user", "content": "original evidence"}])
        arguments = json.dumps({"start": 0, "_step_note": "Read my own evidence"})
        result = executor.execute("recall_context", arguments)
        self.assertIn("original evidence", result["records"][0]["text"])
        self.assertIn("error", executor.execute("recall_context", '{"agent_id":"peer"}'))
        self.assertIn("error", HostTools(rpc, lambda: [], permissions={"tools": []}).execute("recall_context", "{}"))
        rpc.assert_not_called()
