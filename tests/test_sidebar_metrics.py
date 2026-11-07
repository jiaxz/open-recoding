"""Focused numeric sidebar projections and compaction saving semantics."""
from copy import deepcopy
import unittest

from multiagent.context_window import ContextCompactor, token_units
from multiagent.storage import saved_token_usage
from multiagent.events import update_token_usage
from session import SavedSession
from test_context_window import history
import test_teams as team_fixture
from workspace_support import temporary_workspace
from pathlib import Path


class SidebarMetricsTests(unittest.TestCase):
    setUp = team_fixture.TeamTests.setUp
    create = team_fixture.TeamTests.create

    def test_latest_input_and_savings_survive_stop_without_leaking_into_tools(self):
        child = self.create("Analyst")
        for identifier, tokens, saved in [("master", 12000, 50000), (child, 23000, 60000)]:
            self.team.observe(identifier, {"type":"model_end", "run_id":"fixture", "step":1,
                "usage":{"prompt_tokens":tokens}, "context":{"compaction_saved_tokens":saved,"compaction_generation":2}})
        self.team.observe(child, {"type":"model_end", "run_id":"fixture", "step":2,
            "usage":{"prompt_tokens":24000}, "context":{"last_input_tokens":None}})
        usage = self.team.token_usage()
        self.assertEqual(usage[child]["input_tokens"],24000)
        self.assertEqual(usage[child]["compaction_saved_tokens"],60000)
        self.assertEqual(self.events[-1]["agent_token_usage"],usage)
        self.assertNotIn("token_usage", str(self.team.request("master","list_agents",{})))
        self.team.pause()
        self.assertEqual(SavedSession(self.store.folder).snapshot(events=False)["agent_token_usage"],usage)

    def test_missing_metadata_recovers_from_event_tail_without_writing_archive(self):
        event={"sequence":1,"type":"model_end","usage":{"prompt_tokens":34567},
               "context":{"compaction_saved_tokens":89000,"compaction_generation":1}}
        self.store.append_event(event)
        original=(self.store.folder/"events.jsonl").read_bytes()
        usage=saved_token_usage(self.store)
        self.assertEqual(usage,{"input_tokens":34567,"compaction_saved_tokens":89000,"compaction_generation":1})
        self.assertEqual((self.store.folder/"events.jsonl").read_bytes(),original)
        recovered=SavedSession(self.store.folder).snapshot(events=False)
        self.assertEqual(recovered["agent_token_usage"]["master"],usage)

    def test_missing_child_record_does_not_break_archive_metrics(self):
        child=self.create("Missing record")
        self.team.pause()
        self.team.data["token_usage"].pop(child,None)
        self.team._save()
        (self.store.folder/"agents"/child/"session.json").unlink()
        snapshot=SavedSession(self.store.folder).snapshot(events=False)
        self.assertEqual(snapshot["agent_token_usage"][child],{})
        self.assertEqual(snapshot["agents"][0]["id"],child)


class CompactionSavingTests(unittest.TestCase):
    def test_savings_compare_raw_and_projected_history_without_accumulation(self):
        with temporary_workspace() as directory:
            messages=history(length=4000)
            original=deepcopy(messages)
            compact=ContextCompactor(Path(directory)/"context-window.json",lambda *_:("Verified continuation memory",{}),
                model=lambda:"grok-4.7",threshold=8000,recent_blocks=3)
            self.assertEqual(compact.saved_tokens(messages),0)
            projected=compact(messages)
            expected=max(0,token_units(messages)-token_units(projected))
            self.assertGreater(expected,0)
            self.assertEqual(compact.saved_tokens(messages,projected),expected)
            self.assertEqual(compact.saved_tokens(messages,projected),expected)
            self.assertEqual(compact.state["saved_tokens_estimate"],expected)
            self.assertEqual(messages,original)
            restored=ContextCompactor(compact.path,lambda *_:self.fail("Should not summarize"),model=lambda:"grok-4.7")
            self.assertEqual(restored.saved_tokens(messages),expected)

    def test_invalid_numbers_do_not_erase_last_known_measurements(self):
        usage={"input_tokens":123,"compaction_saved_tokens":456}
        update_token_usage(usage,{"type":"model_end","usage":{"prompt_tokens":True},
            "context":{"compaction_saved_tokens":-1,"last_input_tokens":None}})
        self.assertEqual(usage,{"input_tokens":123,"compaction_saved_tokens":456})
        update_token_usage(usage,{"type":"model_end","usage":"damaged","context":42})
        self.assertEqual(usage,{"input_tokens":123,"compaction_saved_tokens":456})
