"""Mixed server/worker builds fail before touching a Session or model API."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from workspace_support import temporary_workspace

from multiagent.runtime_build import source_revision


class RuntimeBuildTests(unittest.TestCase):
    def test_revision_tracks_python_contents_not_timestamp_or_runtime_data(self):
        with temporary_workspace() as folder:
            root = Path(folder)
            module = root / "module.py"
            module.write_text("value=1\n")
            stamp, before = module.stat(), source_revision(root)
            (root / "session.json").write_text('{"private":"fixture"}')
            self.assertEqual(source_revision(root), before)
            module.write_text("value=2\n")
            os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertNotEqual(source_revision(root), before)

    def test_fresh_worker_rejects_old_or_missing_parent_revision_before_setup(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GEMINI_")}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for config in ({}, {"runtime_revision": "older-server"}):
            with self.subTest(config=config):
                process = subprocess.run([sys.executable, "-u", "-m", "multiagent.worker"],
                    input=json.dumps(config)+"\n", text=True, capture_output=True, timeout=5,
                    cwd=Path(__file__).resolve().parents[1], env=environment, check=True)
                events = [json.loads(line) for line in process.stdout.splitlines()]
                self.assertEqual([event["kind"] for event in events], ["fatal"])
                self.assertIn("代码版本不一致", events[0]["error"])
                self.assertIn("重新启动宿主", events[0]["error"])
                self.assertIn("recoding-web.py", events[0]["error"])
                self.assertEqual(process.stderr, "")
