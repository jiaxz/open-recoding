"""Focused compaction checks using memory-only models and temporary archives."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from multiagent.loop import AgentLoop as Agent
from multiagent.image_reader import image_attachment
from multiagent.multimodal import context_stats, to_api_messages
from multiagent.context_window import ContextCompactor, ContextBudget, SOURCE_CHARS, token_units
from multiagent.tools import HostTools, CONTEXT_TOOLS
from multiagent.events import model_rate
from multiagent.models import model_messages
from multiagent.system import update_run


def call(identifier, name="python"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": '{"code":"print(1)"}'},
            "extra_content": {"google": {"thought_signature": "fixture-signature"}}}


def history(count=20, length=1500):
    messages = [{"role": "system", "content": "Immutable rules."},
                {"role": "user", "content": "EXACT_USER_REQUIREMENT", "_agent_message_id": "u1", "_user_input": {"id": "u1"}}]
    for i in range(count):
        messages.extend([{"role": "assistant", "content": None, "_model": "gemini-4-argon", "tool_calls": [call(str(i))]},
                         {"role": "tool", "tool_call_id": str(i), "content": f"record-{i}:" + "x"*length}])
    return messages


class ContextWindowTests(unittest.TestCase):
    def make(self, directory, summarize=None, **settings):
        return ContextCompactor(Path(directory)/"context-window.json", summarize or (lambda *_: ("Verified work and pending tasks; recall original indices for exact records.", {"prompt_tokens": 100, "completion_tokens": 10})),
                                model=lambda: "grok-4.7", threshold=8000, recent_blocks=3, **settings)

    def test_projection_preserves_rules_recent_batches_human_images_and_raw_history(self):
        with temporary_workspace() as directory:
            messages = history()
            picture = image_attachment((Path(__file__).parent/"fixtures/small.jpg").read_bytes(), "figure.jpg")
            messages[1]["_image_attachments"] = [deepcopy(picture)]
            messages[2]["tool_calls"].append(call("extra", "view_image"))
            messages.insert(4, {"role": "tool", "tool_call_id": "extra", "content": "image seen", "_image_attachment": deepcopy(picture)})
            original = deepcopy(messages)
            compact = self.make(directory)
            result = compact(messages)
            self.assertLess(context_stats(result)["context_chars"], context_stats(messages)["context_chars"])
            self.assertEqual(messages, original)
            self.assertEqual(result[0], messages[0])
            self.assertIn(messages[1], result)
            self.assertEqual(result[-6:], messages[-6:])
            self.assertEqual(context_stats(result)["images"], 2)
            # The image batch includes both tool calls and both results, never orphaned.
            Agent(Mock(), Mock(), system="test").restore(result)
            self.assertIn(messages[2], result)
            self.assertIn(messages[3], result)
            self.assertIn(messages[4], result)
            grok = json.dumps(model_messages(result, "grok-4.7"))
            self.assertNotIn("fixture-signature", grok)
            self.assertIn("fixture-signature", json.dumps(model_messages(result, "gemini-4-argon")))
            self.assertEqual(sum(part.get("type") == "image_url" for message in to_api_messages(result) for part in (message.get("content") or []) if isinstance(part, dict)), 2)

    def test_cache_resume_append_and_changed_prefix_validation(self):
        with temporary_workspace() as directory:
            summarize = Mock(return_value=("Concise verified memory.", {}))
            messages = history()
            compact = self.make(directory, summarize)
            first = compact(messages);self.assertTrue(summarize.called)
            calls = summarize.call_count
            self.assertEqual(compact(deepcopy(messages)), first)
            resumed = self.make(directory, summarize)
            self.assertEqual(resumed.view(deepcopy(messages)), first)
            self.assertEqual(summarize.call_count, calls)
            changed = deepcopy(messages);changed[3]["content"] = "altered original"
            self.assertEqual(resumed.view(changed), changed)
            self.assertIsNone(resumed.state)
            self.assertIsNotNone(compact.state)

    def test_live_tool_batch_statistics_keep_summary_and_historical_images(self):
        with temporary_workspace() as directory:
            messages = history()
            picture = image_attachment((Path(__file__).parent/"fixtures/small.jpg").read_bytes(), "seen.jpg")
            messages[3]["_image_attachment"] = deepcopy(picture)
            compact = self.make(directory)
            original_projection = compact(messages)
            memory = deepcopy(compact.state)
            self.assertLess(len(original_projection), len(messages))
            for step in range(3):
                first, second = f"live-{step}-a", f"live-{step}-b"
                messages.append({"role": "assistant", "tool_calls": [call(first), call(second)]})
                for result in [None, {"role": "tool", "tool_call_id": first, "content": "first complete"},
                               {"role": "tool", "tool_call_id": second, "content": "second complete"}]:
                    if result is not None:
                        messages.append(result)
                    # Runner telemetry reads the projection before all results exist.
                    original = deepcopy(messages)
                    projected = compact.view(messages)
                    self.assertEqual(compact.state, memory)
                    self.assertEqual(compact.saved_tokens(messages, projected), memory["saved_tokens_estimate"])
                    self.assertEqual(context_stats(projected)["images"], 1)
                    self.assertIn(messages[2], projected)
                    self.assertIn(messages[3], projected)
                    self.assertEqual(messages, original)
                    compact.reload()  # The same summary file must remain effective.
                    self.assertEqual(compact.state, memory)
                self.assertLess(len(compact.view(messages)), len(messages))

    def test_incremental_chunks_are_bounded_do_not_include_pixels_or_signatures(self):
        with temporary_workspace() as directory:
            requests, events = [], []
            def summarize(messages, model):
                requests.append(deepcopy(messages));return "Incremental remembered state.", {"completion_tokens": 5}
            compact = self.make(directory, summarize, on_event=events.append)
            messages = history(60, 3000)
            result = compact(messages)
            self.assertGreater(len(requests), 1)
            self.assertTrue(all(len(request[-1]["content"]) < SOURCE_CHARS+17000 for request in requests))
            self.assertNotIn("fixture-signature", json.dumps(requests))
            self.assertNotIn("data:image/", json.dumps(requests))
            self.assertIn("Incremental remembered state", requests[-1][-1]["content"])
            self.assertEqual(compact.state["generation"], len(requests))
            self.assertEqual(len([event for event in events if event["type"] == "context_compaction_end"]), len(requests))
            self.assertLess(context_stats(result)["context_chars"], 15000)

    def test_failure_and_cancel_never_commit_partial_memory(self):
        with temporary_workspace() as directory:
            messages, events = history(), []
            summarize = Mock(side_effect=OSError("fixture connection interrupted"))
            compact = self.make(directory, summarize, on_event=events.append)
            self.assertEqual(compact(messages), messages)
            compact(messages);self.assertEqual(summarize.call_count, 1, "Failures have a growth cooldown")
            self.assertFalse(compact.path.exists())
            self.assertEqual(events[-1]["type"], "context_compaction_error")
            interrupted = self.make(directory, Mock(side_effect=KeyboardInterrupt()), on_event=events.append)
            with self.assertRaises(KeyboardInterrupt):
                interrupted(messages)
            self.assertFalse(interrupted.path.exists())
            self.assertTrue(events[-1]["cancelled"])
            atomic = self.make(directory)
            with patch("multiagent.context_window.atomic_json", side_effect=OSError("disk full")):
                self.assertEqual(atomic(messages), messages)
                self.assertIsNone(atomic.state)

    def test_incomplete_tool_batch_is_not_compacted(self):
        with temporary_workspace() as directory:
            messages = history();messages.pop()
            summarize = Mock()
            self.assertEqual(self.make(directory, summarize)(messages), messages)
            summarize.assert_not_called()

    def test_large_recent_outputs_shrink_the_retained_tail_without_summary_thrash(self):
        with temporary_workspace() as directory:
            summarize = Mock(return_value=("Verified memory.", {}))
            compact = ContextCompactor(Path(directory)/"context-window.json", summarize, model=lambda: "grok-4.7", threshold=50000)
            messages = history(10, 15000)
            result = compact(messages)
            self.assertLess(compact.budget.measure(result)["estimated_input_tokens"], 50000)
            self.assertEqual(result[-8:], messages[-8:], "Retain at least four complete tool batches")
            calls = summarize.call_count
            messages.extend([{"role": "assistant", "content": None, "tool_calls": [call("new")]},
                             {"role": "tool", "tool_call_id": "new", "content": "x"*15000}])
            compact(messages)
            self.assertEqual(summarize.call_count, calls, "One new tool result must not trigger another summary immediately")

    def test_token_budget_uses_actual_input_not_total_or_lifetime_tokens_and_survives_resume(self):
        with temporary_workspace() as directory:
            selected = ["grok-4.7"]
            path = Path(directory)/"context-budget.json"
            budget = ContextBudget(path, model=lambda: selected[0], tools=CONTEXT_TOOLS)
            messages = history(2, 100)
            budget.mark_request(messages, selected[0])
            budget.record({"prompt_tokens": 1234, "completion_tokens": 500, "total_tokens": 1734})
            measured = budget.measure(messages)
            self.assertEqual(measured["estimated_input_tokens"], 1234)
            self.assertEqual(measured["trigger_tokens"], 400000)
            self.assertEqual(measured["estimate_method"], "api_input_plus_estimated_delta")
            added = [{"role": "user", "content": "新的输入"*20}]
            self.assertEqual(budget.measure(messages+added)["estimated_input_tokens"], 1234+token_units(added))
            budget.defer({**measured, "estimated_input_tokens": 400001})
            resumed = ContextBudget(path, model=lambda: selected[0], tools=CONTEXT_TOOLS)
            self.assertEqual(resumed.measure(messages)["trigger_tokens"], 425001)
            self.assertEqual(resumed.measure(messages)["estimated_input_tokens"], 1234)
            selected[0] = "gemini-4-argon"
            self.assertEqual(resumed.measure(messages)["context_window_tokens"], 1048576)
            self.assertEqual(resumed.measure(messages)["trigger_tokens"], 838860)
            self.assertEqual(resumed.measure(messages)["estimate_method"], "local_estimate")
            # New pixels add an image budget, never their base64 length.
            picture = image_attachment((Path(__file__).parent/"fixtures/small.jpg").read_bytes(), "new.jpg")
            photographed = messages+[{"role": "user", "content": "look", "_image_attachments": [picture]}]
            first = resumed.measure(photographed)["estimated_input_tokens"]
            photographed[-1]["_image_attachments"][0]["data_url"] = "data:image/jpeg;base64,"+"A"*100000
            self.assertEqual(resumed.measure(photographed)["estimated_input_tokens"], first)

    def test_background_budget_request_is_nonblocking_and_does_not_summarize(self):
        with temporary_workspace() as directory:
            messages, summarize = history(), Mock()
            compact = self.make(directory, summarize)
            request = compact.background_request(messages)
            self.assertGreater(request["estimated_input_tokens"], request["trigger_tokens"])
            self.assertEqual(request["snapshot_count"], len(messages))
            self.assertEqual(compact.view(messages), messages)
            self.assertFalse(compact.path.exists());summarize.assert_not_called()
            sibling = self.make(Path(directory)/"sub", Mock(return_value=("Independent memory.", {})))
            (Path(directory)/"sub").mkdir()
            self.assertLess(len(sibling(messages)), len(messages))
            compact.reload()
            self.assertEqual(compact.view(messages), messages, "A peer summary cannot alter this Agent")

    def test_resume_changed_system_keeps_actual_usage_and_journal_recovery_verifies_request(self):
        from multiagent.client import Client
        from multiagent.storage import ContextHistory
        with temporary_workspace() as directory:
            folder=Path(directory);(folder/"runs").mkdir()
            original=history(20,30000)
            picture=image_attachment((Path(__file__).parent/"fixtures/small.jpg").read_bytes(),"history.jpg")
            original[1]["_image_attachments"]=[deepcopy(picture) for _ in range(13)]
            client=Client("fixture", "https://localhost/v1", "grok-4.7",1,tools=CONTEXT_TOOLS)
            _,metadata=client.prepare_request(original)
            journal=ContextHistory(folder/"runs", system=original[0]["content"], tools=CONTEXT_TOOLS, model="grok-4.7")
            journal.record("context_resume",messages=original)
            journal.record("model_transport",phase="complete", model="grok-4.7",**metadata,
                           usage={"prompt_tokens":156559,"completion_tokens":50})
            journal.close()
            resumed=deepcopy(original)
            resumed[0]["content"]="Updated System rules."
            resumed.append({"role":"user","content":"Resume after Stop"})
            budget=ContextBudget(folder/"context-budget.json",model=lambda:"grok-4.7",tools=CONTEXT_TOOLS)
            self.assertGreater(budget.measure(resumed)["estimated_input_tokens"],400000,"Reproduce the uncalibrated false alarm")
            budget.recover(folder,resumed)
            info=budget.measure(resumed)
            self.assertTrue(info["calibrated"])
            self.assertEqual(info["last_reported_input_tokens"],156559)
            self.assertEqual(info["estimated_input_tokens"],156559+token_units(resumed[:1])+token_units(resumed[-1:]))
            self.assertLess(info["estimated_input_tokens"],400000,"Measured historical pixels must not be re-estimated at 8192 each")
            # Altering any old body invalidates the anchor, never reassign usage.
            resumed[3]["content"]="Changed old tool output"
            budget.data["anchors"]={};budget.recover(folder,resumed)
            self.assertFalse(budget.measure(resumed)["calibrated"])

    def test_later_chunk_failure_discards_whole_candidate_and_preserves_previous_cache(self):
        with temporary_workspace() as directory:
            messages=history(20,3000)
            compact=self.make(directory)
            compact(messages)
            previous=compact.path.read_bytes();state=deepcopy(compact.state)
            more=history(65,3000)
            compact.summarize=Mock(side_effect=[("First tentative chunk.",{}),OSError("second chunk failed")])
            before=compact.view(more)
            self.assertEqual(compact(more),before)
            self.assertEqual(compact.path.read_bytes(),previous)
            self.assertEqual(compact.state,state)
            self.assertTrue(compact.failed)

    def test_oversized_initial_input_without_old_batches_cannot_be_summarized(self):
        with temporary_workspace() as directory:
            summarize = Mock()
            compact = self.make(directory, summarize)
            messages = [{"role":"system","content":"rules"},{"role":"user","content":"x"*20000}]
            self.assertIsNone(compact.background_request(messages))
            self.assertEqual(compact.view(messages), messages)
            summarize.assert_not_called()

    def test_own_history_recall_pages_exact_text_without_pixels_or_opaque_fields(self):
        messages = history()
        messages[3]["content"] = "A long original tool result: " + "x"*40000
        tool = HostTools(Mock(), lambda: messages)
        chunks, offset = [], 0
        while True:
            result = tool.execute("recall_context", json.dumps({"start": 3, "limit": 1, "offset": offset}))
            chunks.append(result["records"][0]["text"]);offset = result["next_offset"]
            if not offset:
                break
        self.assertEqual(json.loads("".join(chunks))["content"], messages[3]["content"])
        found = tool.execute("recall_context", '{"query":"EXACT_USER_REQUIREMENT","limit":1}')
        self.assertEqual(found["records"][0]["index"], 1)
        self.assertNotIn("fixture-signature", json.dumps(tool.execute("recall_context", '{"start":2,"limit":1}')))
        self.assertIn("error", tool.execute("recall_context", '{"agent_id":"other"}'))
        denied = HostTools(Mock(), lambda: messages, permissions={"tools": ["python"]})
        self.assertIn("error", denied.execute("recall_context", "{}"))
        self.assertEqual(CONTEXT_TOOLS[0]["function"]["parameters"]["type"], "object")

    def test_core_projects_before_draining_new_inputs_and_keeps_original_journal(self):
        pending, requests, events = [], [], []
        def prepare(messages):
            pending.append({"role": "user", "content": "ARRIVED_DURING_COMPRESSION"})
            return [messages[0], {"role": "user", "content": "MEMORY"}]
        model = Mock()
        model.complete.side_effect = lambda messages: requests.append(deepcopy(messages)) or {"role": "assistant", "content": "reply"}
        model.last_usage = {}
        def inputs(_):
            result = list(pending);pending.clear();return result
        def observe(event):
            events.append(event)
            if event["type"] == "model_end":
                raise KeyboardInterrupt()
        agent = Agent(model, Mock(), system="rules", input_provider=inputs, context_processor=prepare, on_event=observe)
        agent.restore([{"role": "system", "content": "rules"}, {"role": "user", "content": "ORIGINAL_HISTORY"}])
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual([message["content"] for message in requests[0]], ["rules", "MEMORY", "ARRIVED_DURING_COMPRESSION"])
        self.assertEqual(agent.messages[1]["content"], "ORIGINAL_HISTORY")
        self.assertEqual(next(event for event in events if event["type"] == "model_start")["messages"], 3)

    def test_compaction_usage_is_counted_without_changing_loop_iterations(self):
        jobs, run_id = {}, "run"
        for event in [
            {"type": "context_compaction_start", "step": 10, "id": "compress"},
            {"type": "context_compaction_end", "step": 10, "id": "compress", "elapsed": 1, "usage": {"prompt_tokens": 100, "completion_tokens": 5}},
            {"type": "model_start", "step": 10},
            {"type": "model_end", "step": 10, "elapsed": 2, "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
            {"type": "loop_end", "model_elapsed": 2},
        ]:
            update_run(jobs, {**event, "run_id": run_id})
        self.assertEqual(jobs[run_id]["model_calls"], 2)
        self.assertEqual(jobs[run_id]["iterations"], 1)
        self.assertEqual(jobs[run_id]["model_elapsed"], 3)
        self.assertEqual(jobs[run_id]["prompt_tokens"], 120)
        self.assertEqual(jobs[run_id]["completion_tokens"], 7)
        rate = model_rate({"type": "context_compaction_end", "id": "compress", "run_id": run_id, "step": 10, "elapsed": 1, "timestamp": 1,
                           "usage": {"completion_tokens": 5}})
        self.assertEqual(rate["purpose"], "context_compaction")
        self.assertNotEqual(rate["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
