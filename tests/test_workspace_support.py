"""Test fixtures remain valid within a command; command exit removes test data."""

from pathlib import Path
import unittest
import os
import shutil
import subprocess
import sys

from session.store import SessionStore
from workspace_support import TEST_SESSION_ROOT, temporary_workspace


class TestWorkspaceTests(unittest.TestCase):
    def test_unique_directories_keep_shared_fixtures_until_command_exit(self):
        with temporary_workspace(prefix="root-check-") as first, temporary_workspace(prefix="root-check-") as second:
            one, two = Path(first), Path(second)
            self.assertNotEqual(one, two)
            self.assertEqual(one.parent, TEST_SESSION_ROOT)
            self.assertEqual(two.parent, TEST_SESSION_ROOT)
            (two / "retention-proof.txt").write_text("second run")
            store = SessionStore.create(one, "test-root", {})
            self.assertTrue(store.folder.is_relative_to(TEST_SESSION_ROOT))
            self.assertTrue((two / "retention-proof.txt").exists())
        self.assertTrue((store.folder / "session.json").is_file())
        self.assertEqual((two / "retention-proof.txt").read_text(), "second run")
        self.assertTrue(TEST_SESSION_ROOT.is_dir())

    def test_context_cleanup_keeps_fixtures_until_command_exit(self):
        workspace = temporary_workspace(prefix="retention-hook-")
        marker = Path(workspace.name) / "marker.txt"
        marker.write_text("Retain after teardown and failures")
        workspace.cleanup()
        self.assertTrue(marker.is_file())
        with self.assertRaisesRegex(RuntimeError, "test failure"), workspace:
            raise RuntimeError("test failure")
        self.assertEqual(marker.read_text(), "Retain after teardown and failures")


    def test_outer_command_cleans_data_after_success_and_failure(self):
        with temporary_workspace(prefix="cleanup-command-") as directory:
            project = Path(directory) / "fixture-project"
            scripts = project / "tests"
            scripts.mkdir(parents=True)
            source = Path(__file__).parent
            for name in ("workspace_support.py", "workspace_cleanup.py"):
                shutil.copyfile(source / name, scripts / name)
            marker = project / "keep-project-file.txt"
            marker.write_text("not test data")
            env = dict(os.environ, PYTHONPATH=str(scripts))
            env.pop("RECODING_TEST_OWNER_PID", None)
            for status in (0, 7):
                program = ("from workspace_support import temporary_workspace; "
                           "from pathlib import Path; "
                           "w=temporary_workspace(); "
                           "Path(w.name,'artifact.txt').write_text('temporary'); "
                           "print(w.name); raise SystemExit(" + str(status) + ")")
                child = subprocess.run([sys.executable, "-c", program], cwd=project,
                                       env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(child.returncode, status, child.stderr)
                self.assertFalse(Path(child.stdout.strip()).exists(), child.stderr)
                self.assertEqual(list((project / "sessions" / "tests").iterdir()), [])
                self.assertEqual(marker.read_text(), "not test data")



if __name__ == "__main__":
    unittest.main()
