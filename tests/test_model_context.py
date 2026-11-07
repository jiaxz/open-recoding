"""Focused lossless cross-model history conversion, without a network API."""

from copy import deepcopy
import io
import json
from pathlib import Path
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock

from multiagent.client import Client
from multiagent.models import MODELS, model_messages
from session.store import SessionStore
from multiagent.loop import AgentLoop as Agent
from endless_support import drive
from multiagent.errors import AgentError
from multiagent.image_reader import image_attachment
from multiagent.multimodal import to_api_messages


GEMINI, GROK = "gemini-3.8-flash", "grok-4.7"


def response(model, number, name="python"):
    return {"role": "assistant", "content": f"Action {number}", "_model": model,
            "reasoning_content": f"Readable analysis from {model}",
            "difrost_reasoning": {"r00": {"value": f"opaque-{model}", "scope": "internal_redactable"}},
            "encrypted_content": f"encrypted-{model}",
            "extra_content": {"google" if model.startswith("gemini-") else "xai": {"signature": f"message-{model}"}},
            "tool_calls": [{"id": f"call-{number}", "type": "function",
                            "function": {"name": name, "arguments": '{"code":"print(42)"}',
                                         "signature": f"function-{model}"},
                            "extra_content": {"google" if model.startswith("gemini-") else "xai": {
                                "thought_signature": f"call-{model}"}}}]}


def conversation():
    messages = [{"role": "system", "content": "Test system"}, {"role": "user", "content": "Work"}]
    for number, model in enumerate((GEMINI, GROK), 1):
        messages.extend([response(model, number), {"role": "tool", "tool_call_id": f"call-{number}",
                         "content": '{"stdout":"signature in ordinary tool data must stay"}'}])
    return messages


class ModelContextTests(unittest.TestCase):
    def test_argon_grok_flash_empty_and_partial_streams_preserve_original_history(self):
        argon = "gemini-4-argon"
        picture = image_attachment((Path(__file__).parent / "fixtures/small.jpg").read_bytes(), "/workspace/fixture.jpg")
        first = response(argon, 1)
        second_call = deepcopy(first["tool_calls"][0])
        second_call["id"] = "parallel-2"
        first["tool_calls"].append(second_call)
        messages = [{"role": "system", "content": "Synthetic switch test"},
                    {"role": "user", "content": "Check the fixture", "_image_attachments": [picture]}, first,
                    {"role": "tool", "tool_call_id": "call-1", "content": "42"},
                    {"role": "tool", "tool_call_id": "parallel-2", "content": "42", "_image_attachment": picture},
                    {"role": "assistant", "content": "42", "_model": argon},
                    {"role": "user", "content": "Acknowledge"},
                    {"role": "assistant", "content": "Acknowledged", "_model": GROK,
                     "reasoning_content": "The calculation completed.",
                     "difrost_reasoning": {"r00": {"value": "opaque-grok", "scope": "internal_redactable"}}}]

        class Reply(io.BytesIO):
            status = 200
            headers = {"content-type": "text/event-stream", "x-request-id": "switch-fixture"}

        def frame(delta, **choice):
            return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, **choice}]}) + "\n\n").encode()

        heartbeats = b": keepalive\n\n: keepalive\n\n"
        complete = frame({"content": "OK"}, finish_reason="stop")
        cases = [(heartbeats, "empty_response", False, 0),
                 (heartbeats + frame({"role": "assistant"}), "empty_response", False, 1),
                 (frame({"tool_calls": [{"index": 0, "id": "uncommitted", "type": "function",
                     "function": {"name": "python", "arguments": '{"code":"print(42)"}'}}]}),
                  "incomplete_response", True, 1),
                 (complete, "incomplete_response", True, 1),
                 (b'data: {"choices":[]}', "incomplete_response", False, 0)]
        for payload, category, output_received, events in cases:
            with self.subTest(category=category, output_received=output_received, events=events):
                client = Client("fixture-key", "https://fixture.invalid/v1", GEMINI)
                client.opener = Mock()
                client.opener.open.return_value = Reply(payload)
                sandbox = Mock()
                agent = Agent(client, sandbox, system="Synthetic switch test")
                agent.restore(messages)
                before = deepcopy(agent.messages)
                with self.assertRaises(AgentError):
                    drive(agent, "Proceed")
                sandbox.execute.assert_not_called()
                client.opener.open.assert_called_once()
                self.assertEqual(agent.messages[:len(before)], before)
                self.assertEqual(client.last_call["error_category"], category)
                self.assertEqual(client.last_call["response_bytes"], len(payload))
                self.assertEqual(client.last_call["heartbeat_count"], payload.count(b": keepalive"))
                self.assertEqual(client.last_call["stream_events"], events)
                self.assertEqual(client.last_call["model_output_received"], output_received)
                self.assertFalse(client.last_call["sse_done_received"])
                self.assertEqual(client.last_call["history_models"], {argon: 2, GROK: 1})
                self.assertEqual(client.last_call["foreign_assistant_messages"], 3)
                self.assertEqual(client.last_call["foreign_tool_calls"], 2)
                wire = json.loads(client.opener.open.call_args.args[0].data)["messages"]
                self.assertNotIn("opaque-", json.dumps(wire))
                call = next(m for m in wire if m.get("tool_calls"))
                self.assertEqual(call["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
                                 "skip_thought_signature_validator")
                self.assertNotIn("extra_content", call["tool_calls"][1])
                self.assertEqual(sum(1 for m in wire if isinstance(m.get("content"), list)
                                     for p in m["content"] if p.get("type") == "image_url"), 2)
                client.model = argon
                back = json.loads(client.prepare_request(agent.messages)[0].data)["messages"]
                self.assertEqual(back[2], to_api_messages([first])[0])

        client = Client("fixture-key", "https://fixture.invalid/v1", GEMINI)
        client.opener = Mock()
        client.opener.open.return_value = Reply(complete + b"data: [DONE]\n\n")
        self.assertEqual(client.complete(messages + [{"role": "user", "content": "Proceed"}])["content"], "OK")
        self.assertTrue(client.last_call["sse_done_received"])
        self.assertTrue(client.last_call["model_output_received"])

    def test_argon_catalog_and_three_model_context_round_trip(self):
        from recoding_web.host import GuiSessionHost
        from recoding_web.server import GuiHttpServer

        argon = "gemini-4-argon"
        self.assertIn(argon, MODELS)
        with temporary_workspace() as temp:
            app = GuiSessionHost(Path(temp), network=False, gpu=False)
            server = GuiHttpServer(("127.0.0.1", 0), app)
            try:
                self.assertIn(argon, server.configuration()["models"])
            finally:
                server.server_close()
                app.close()
        messages = conversation() + [response(argon, 3),
                                     {"role": "tool", "tool_call_id": "call-3", "content": "done"}]
        before = deepcopy(messages)
        for target in (GEMINI, GROK, argon, GEMINI, argon):
            client = Client("fixture-key", "https://fixture.invalid/v1", target)
            wire = json.loads(client.prepare_request(messages)[0].data)
            self.assertEqual(wire["model"], target)
            for original, sent in zip(messages, wire["messages"]):
                if original.get("role") != "assistant":
                    continue
                if original["_model"] == target:
                    self.assertEqual(sent, to_api_messages([original])[0])
                else:
                    self.assertNotIn("opaque-", json.dumps(sent))
                    self.assertNotIn("encrypted-", json.dumps(sent))
                    self.assertNotIn("reasoning_content", sent)
                    self.assertIn(original["reasoning_content"], sent["content"])
        self.assertEqual(messages, before)

    def test_a_b_a_keeps_each_models_original_thinking_and_no_foreign_opaque_fields(self):
        messages = conversation()
        original = deepcopy(messages)
        first_a = model_messages(messages, GEMINI)
        to_b = model_messages(messages, GROK)
        second_a = model_messages(messages, GEMINI)
        self.assertEqual(messages, original)
        self.assertEqual(first_a, second_a, "A's view must be rebuilt from originals, never B's converted view")
        for target, wire in ((GEMINI, second_a), (GROK, to_b)):
            own = next(m for m in wire if m.get("tool_calls") and m["tool_calls"][0]["id"] == ("call-1" if target == GEMINI else "call-2"))
            source = next(m for m in original if m.get("_model") == target)
            self.assertEqual(own, to_api_messages([source])[0])
            foreign = GROK if target == GEMINI else GEMINI
            encoded = json.dumps(wire)
            for prefix in ("opaque-", "encrypted-", "message-", "call-", "function-"):
                self.assertNotIn(prefix + foreign, encoded)
            converted = next(m for m in wire if m.get("tool_calls") and m is not own)
            self.assertNotIn("reasoning_content", converted)
            self.assertIn(f"[历史思考摘要 · {foreign}]", converted["content"])
            self.assertIn(source["content"], encoded)
            self.assertIn(f"Readable analysis from {foreign}", converted["content"])
            self.assertNotIn("_model", encoded)
            self.assertEqual([m.get("tool_call_id") for m in wire if m["role"] == "tool"], ["call-1", "call-2"])
            self.assertIn("signature in ordinary tool data must stay", encoded)

    def test_other_version_and_unknown_owner_do_not_inherit_opaque_signatures(self):
        message = response(GEMINI, 1)
        for source in (GEMINI, None):
            for target in (GROK, "gemini-another-version"):
                with self.subTest(source=source, target=target):
                    raw = deepcopy(message)
                    if source is None:
                        del raw["_model"]
                    wire = model_messages([raw], target)[0]
                    self.assertNotIn("opaque-", json.dumps(wire))
                    self.assertNotIn("encrypted-", json.dumps(wire))
                    self.assertNotIn("reasoning_content", wire)
                    self.assertIn("Readable analysis", wire["content"])
                    # A provider-looking name does not imply a protocol exception.
                    if target == "gemini-another-version":
                        self.assertNotIn("extra_content", wire["tool_calls"][0])

    def test_switch_keeps_user_and_view_images_but_excludes_display_images(self):
        picture = image_attachment((Path(__file__).parent / "fixtures/small.jpg").read_bytes(), "/workspace/image.jpg")
        first = response(GEMINI, 1)
        second_call = deepcopy(first["tool_calls"][0]);second_call["id"] = "call-parallel"
        first["tool_calls"].append(second_call)
        messages = [{"role": "system", "content": "test"},
                    {"role": "user", "content": "image", "_image_attachments": [picture]}, first,
                    {"role": "tool", "tool_call_id": "call-1", "content": "image result", "_image_attachment": picture},
                    {"role": "tool", "tool_call_id": "call-parallel", "content": "done"},
                    {"role": "assistant", "content": "final image", "_model": GEMINI,
                     "reasoning_content": "figure checked", "_display_images": [picture]}]
        before = deepcopy(messages)
        for target in (GEMINI, GROK):
            wire = model_messages(messages, target)
            self.assertEqual([m["role"] for m in wire], ["system", "user", "assistant", "tool", "tool", "user", "assistant"])
            images = [part["image_url"]["url"] for m in wire if isinstance(m.get("content"), list)
                      for part in m["content"] if part.get("type") == "image_url"]
            self.assertEqual(images, [picture["data_url"]] * 2)
        self.assertEqual(messages, before)

    def test_empty_foreign_summary_stays_empty_and_conversion_is_not_repeated(self):
        for summary in (None, "", " \n "):
            original = {"role": "assistant", "content": None, "_model": GEMINI,
                        "reasoning_content": summary, "tool_calls": response(GEMINI, 1)["tool_calls"]}
            converted = model_messages([original], GROK)[0]
            self.assertIsNone(converted["content"])
            self.assertNotIn("reasoning_content", converted)
        originals = conversation()
        for _ in range(3):
            wire = model_messages(originals, GROK)
            self.assertEqual(wire[2]["content"].count("[历史思考摘要 ·"), 1)

    def test_real_loop_and_request_builder_preserve_provenance_through_switches(self):
        client = Client("fixture-key", "https://fixture.invalid/v1", GEMINI)
        replies = [response(GEMINI, 1), response(GROK, 2), {"role": "assistant", "content": "done"}]
        for reply in replies:
            reply.pop("_model", None)  # API metadata must not be trusted as ownership.
        sent = []

        class Reply(io.BytesIO):
            status = 200
            headers = {"content-type": "application/json"}

        def open_request(request, **kwargs):
            sent.append(json.loads(request.data))
            return Reply(json.dumps({"model": client.model, "choices": [{"message": replies[len(sent) - 1], "finish_reason": "stop"}]}).encode())

        client.opener = Mock();client.opener.open.side_effect = open_request
        sandbox = Mock();sandbox.execute.side_effect = lambda *args: (setattr(client, "model", GROK if len(sent) == 1 else GEMINI)
                                                                     or {"stdout": "42"})
        agent = Agent(client, sandbox, system="test")
        self.assertEqual(drive(agent, "test switching"), "done")
        originals = [m for m in agent.messages if m["role"] == "assistant"]
        self.assertEqual([m["_model"] for m in originals], [GEMINI, GROK, GEMINI])
        self.assertIn("Readable analysis from " + GEMINI, sent[1]["messages"][2]["content"])
        self.assertNotIn("reasoning_content", sent[1]["messages"][2])
        self.assertEqual(sent[2]["messages"][2]["difrost_reasoning"], originals[0]["difrost_reasoning"])
        self.assertNotIn("difrost_reasoning", sent[2]["messages"][4])
        client.model = GROK
        back_to_b = json.loads(client.prepare_request(agent.messages)[0].data)
        self.assertEqual(back_to_b["messages"][4]["difrost_reasoning"], originals[1]["difrost_reasoning"])
        self.assertEqual(back_to_b["messages"][4]["reasoning_content"], originals[1]["reasoning_content"])



if __name__ == "__main__":
    unittest.main()
