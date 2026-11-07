"""Focused lazy Session discovery; no server, sandbox or external model."""

import json
import os
from pathlib import Path
import shutil
import threading
import unittest

from workspace_support import temporary_workspace
from unittest.mock import patch

from session.store import SESSION_HOME, SessionStore
from recoding_web.host import GuiSessionHost
from session import Session
from multiagent import Agent


class LazyCatalogTests(unittest.TestCase):
    def test_removed_child_invalidates_tree_even_if_parent_timestamp_is_unchanged(self):
        with temporary_workspace(prefix="recoding-directory-cache-") as directory:
            root = Path(directory)
            ordinary = root / "ordinary"
            removed = ordinary / "removed"
            removed.mkdir(parents=True)
            manager = GuiSessionHost(root, {"demo": True}, discover=False)
            self.addCleanup(manager.close)
            snapshot = manager.catalog.snapshot(expanded=[str(ordinary)])
            self.assertIn(str(removed), next(row["children"] for row in snapshot["directory_tree"]
                                            if row["directory"] == str(ordinary)))
            real_stat, unchanged = os.stat, ordinary.stat()
            removed.rmdir()
            def coarse_stat(path, *args, **kwargs):
                return unchanged if Path(path) == ordinary else real_stat(path, *args, **kwargs)
            with patch("recoding_web.catalog.os.stat", side_effect=coarse_stat):
                snapshot = manager.catalog.snapshot(expanded=[str(ordinary)])
            self.assertNotIn(str(removed), next(row["children"] for row in snapshot["directory_tree"]
                                               if row["directory"] == str(ordinary)))

    def test_full_workspace_tree_lists_ordinary_branches_lazily_and_tracks_changes(self):
        with temporary_workspace() as temp, temporary_workspace() as outside:
            root = Path(temp)
            store = SessionStore.create(root / "projects" / "nested", "known", {})
            ordinary = root / "ordinary"
            (ordinary / "child").mkdir(parents=True)
            (root / "node_modules" / "package").mkdir(parents=True)
            (root / "link").symlink_to(outside, target_is_directory=True)
            manager = GuiSessionHost(root, {"demo": True}, discover=False)
            self.addCleanup(manager.close)
            with patch.object(SessionStore, "load_events", side_effect=AssertionError("Tree must stay metadata-only")):
                catalog = manager.catalog.snapshot()
                tree = {item["directory"]: item["children"] for item in catalog["directory_tree"]}
                self.assertIn(str(ordinary), tree[str(root)])
                self.assertIn(str(root / "node_modules"), tree[str(root)])
                self.assertNotIn(str(root / "link"), tree[str(root)])
                self.assertNotIn(str(ordinary), tree, "Collapsed ordinary directories aren't scanned recursively for navigation")
                self.assertIn(str(store.workspace), tree)
                self.assertNotIn(str(store.workspace / SESSION_HOME), tree[str(store.workspace)])
                expanded = manager.catalog.snapshot(expanded=[str(ordinary), str(root / "node_modules"), outside, str(root / "link")])
                tree = {item["directory"]: item["children"] for item in expanded["directory_tree"]}
                self.assertEqual(tree[str(ordinary)], [str(ordinary / "child")])
                self.assertEqual(tree[str(root / "node_modules")], [str(root / "node_modules" / "package")])
                self.assertNotIn(outside, tree)
                self.assertNotIn(str(root / "link"), tree)
                (ordinary / "new").mkdir()
                tree = {item["directory"]: item["children"] for item in manager.catalog.snapshot(expanded=[str(ordinary)])["directory_tree"]}
                self.assertIn(str(ordinary / "new"), tree[str(ordinary)])
                (ordinary / "new").rmdir()
                tree = {item["directory"]: item["children"] for item in manager.catalog.snapshot(expanded=[str(ordinary)])["directory_tree"]}
                self.assertNotIn(str(ordinary / "new"), tree[str(ordinary)])

    def test_directory_cache_tracks_nested_changes_without_relisting_unchanged_files(self):
        with temporary_workspace() as temp:
            root = Path(temp)
            first = SessionStore.create(root / "projects" / "first", "first", {})
            data = root / "data"
            data.mkdir()
            for index in range(400):
                (data / f"ordinary-{index}.txt").touch()
            manager = GuiSessionHost(root, {"demo": True, "network": False, "gpu": False})
            manager.catalog.snapshot()  # The first registry write can change directory timestamps once.
            result = manager.catalog.snapshot()
            self.assertEqual(result["sync"]["entries_listed"], 0)
            self.assertEqual(result["sync"]["directories_checked"], result["sync"]["directories_reused"])
            (data / "ordinary-0.txt").write_text("file contents do not change directory structure")
            self.assertEqual(manager.catalog.snapshot()["sync"]["entries_listed"], 0)
            first.update_metadata(name="renamed")
            self.assertEqual(manager.catalog.snapshot()["sessions"][0]["name"], "renamed")
            added = SessionStore.create(root / "projects" / "new", "added", {})
            result = manager.catalog.snapshot()
            self.assertEqual({item["id"] for item in result["sessions"]}, {first.id, added.id})
            moved = root / "moved" / SESSION_HOME / added.folder.name
            moved.parent.mkdir(parents=True)
            shutil.move(added.folder, moved)
            result = manager.catalog.snapshot()
            self.assertEqual(next(item["directory"] for item in result["sessions"] if item["id"] == added.id), str(root / "moved"))
            shutil.rmtree(moved)
            self.assertEqual([item["id"] for item in manager.catalog.snapshot()["sessions"]], [first.id])
            (root / "link").symlink_to(first.workspace, target_is_directory=True)
            self.assertEqual([item["id"] for item in manager.catalog.snapshot()["sessions"]], [first.id])
            manager.close()

    def test_catalog_is_metadata_only_cached_and_loads_only_the_selected_archive(self):
        with temporary_workspace() as temp:
            root = Path(temp)
            first = SessionStore.create(root / "a", "first", {"model": "gemini-4-argon", "network": True})
            second = SessionStore.create(root / "b", "second", {"model": "grok-4.7"})
            first.save_state({"messages": [{"role": "system", "content": "fixture"}], "iteration": 0, "tool_count": 0})
            first.append_event({"type": "model_end", "run_id": "fixture", "step": 1, "sequence": 1,
                                "timestamp": 1, "model": "gemini-4-argon", "elapsed": 2,
                                "usage": {"completion_tokens": 20}})
            (second.folder / "state.json").write_text("must never be read during discovery")
            (second.folder / "events.jsonl").write_text("must never be read during discovery")
            reads, original = [], Path.open

            def track(path, *args, **kwargs):
                if not args or "r" in args[0]:
                    reads.append(path)
                return original(path, *args, **kwargs)

            with patch.object(Path, "open", track),\
                    patch.object(SessionStore, "info", side_effect=AssertionError("Full info is not navigation")),\
                    patch.object(SessionStore, "settings", side_effect=AssertionError("Settings must stay lazy")),\
                    patch.object(SessionStore, "load_events", side_effect=AssertionError("No history in navigation")):
                manager = GuiSessionHost(root, {"demo": False, "network": True, "gpu": False})
                listing = manager.catalog.snapshot()["sessions"]
                self.assertEqual({item["name"] for item in listing}, {"first", "second"})
                self.assertTrue(all("model" not in item and "preferences" not in item for item in listing))
                self.assertTrue(all(store.navigation_only and "settings" not in store.metadata for store in manager.catalog.stores.values()))
                self.assertEqual([path.name for path in reads].count("session.json"), 2)
                reads.clear()
                manager.catalog.snapshot()
                self.assertEqual(reads, [], "Unchanged navigation must reuse metadata, not reopen files")
                reads.clear()
                restarted = GuiSessionHost(root, {"demo": False, "network": True, "gpu": False})
                restarted.catalog.snapshot()
                self.assertFalse(any(path.name in {"session.json", "state.json", "events.jsonl"} for path in reads),
                                 "Restart must reuse the persisted lightweight index when archive fingerprints match")
                restarted.close()
                registry_path = root / SESSION_HOME / "registry.json"
                registry = json.loads(registry_path.read_text())
                registry["entries"][str(second.folder)]["metadata"]["format"] = {}
                registry_path.write_text(json.dumps(registry))
                reads.clear()
                recovered = GuiSessionHost(root, {"demo": False, "network": True, "gpu": False})
                self.assertEqual({item["id"] for item in recovered.catalog.snapshot()["sessions"]}, {first.id, second.id})
                self.assertEqual([path.name for path in reads].count("session.json"), 1,
                                 "A bad index hint must fall back to the actual archive header")
                recovered.close()
                first.update_metadata(name="renamed", pinned=True)
                reads.clear()
                renamed = manager.catalog.snapshot()["sessions"]
                self.assertEqual(renamed[0]["name"], "renamed")
                self.assertTrue(renamed[0]["pinned"])
                self.assertEqual([path.name for path in reads].count("session.json"), 1)
                self.assertFalse(any(path.name in {"state.json", "events.jsonl"} for path in reads))

            reads.clear()
            with patch.object(Path, "open", track):
                details = manager.inspect(first.id)
            self.assertEqual(details["model"], "gemini-4-argon")
            self.assertEqual(details["model_rates"][0]["rate"], 10)
            self.assertTrue(manager.catalog.stores[first.id].navigation_only)
            self.assertTrue(manager.catalog.stores[second.id].navigation_only)
            self.assertFalse(any(path.is_relative_to(second.folder) for path in reads))

            moved_workspace = root / "nested" / "moved"
            destination = moved_workspace / SESSION_HOME / first.folder.name
            destination.parent.mkdir(parents=True)
            shutil.move(first.folder, destination)
            shutil.rmtree(second.folder)
            added = SessionStore.create(root / "new", "new", {})
            changed = manager.catalog.snapshot()["sessions"]
            self.assertEqual({item["id"] for item in changed}, {first.id, added.id})
            self.assertEqual(next(item["directory"] for item in changed if item["id"] == first.id), str(moved_workspace))
            shutil.rmtree(added.folder)
            manager.catalog.snapshot()
            self.assertFalse(added.folder.exists())
            self.assertEqual([item["id"] for item in manager.catalog.snapshot()["sessions"]], [first.id])
            self.assertEqual(len(manager.sessions), 0)
            manager.close()


if __name__ == "__main__":
    unittest.main()
