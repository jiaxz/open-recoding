"""Current shallow picker flags and installed-entrypoint diagnostics."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from session import SessionStore
from recoding_cli.console import Console
from recoding_cli.sessions import CliSessionNavigator
from test_tui import ModelClient
from workspace_support import PROJECT, temporary_workspace


class ResumeNavigationTests(unittest.TestCase):
    def test_removed_recursive_flags_do_not_start_an_agent(self):
        with temporary_workspace(prefix="resume-flags-") as root:
            SessionStore.create(root, "local-session", {})
            SessionStore.create(Path(root)/"child", "nested-session", {})
            navigator = CliSessionNavigator(root, lambda _: self.fail("Flags must not start a Host"))
            self.addCleanup(navigator.close)
            console = Console(ModelClient(root), SimpleNamespace(), sessions=navigator)
            for command in ("/resume --recursive", "/resume --local"):
                with self.subTest(command=command), self.assertRaisesRegex(ValueError, "不递归"):
                    console.execute(command)

    def test_web_entrypoint_explains_wrong_interpreter_and_venv_entrypoint_works(self):
        environment={key:value for key,value in os.environ.items() if not key.startswith('GEMINI_') and key!='PYTHONPATH'}
        script=PROJECT/'recoding-web.py'
        # -S models an interpreter without the editable installation; no API or server is started.
        missing=subprocess.run([sys.executable,'-S',str(script),'--help'],cwd=PROJECT,env=environment,
                               text=True,capture_output=True,timeout=10)
        self.assertEqual(missing.returncode,1)
        self.assertIn('.venv/bin/python3 recoding-web.py',missing.stderr)
        self.assertIn("pip install -e '.[cli]'",missing.stderr)
        self.assertNotIn('Traceback',missing.stderr)
        installed=subprocess.run([sys.executable,str(script),'--help'],cwd=PROJECT,env=environment,
                                 text=True,capture_output=True,timeout=10)
        self.assertEqual(installed.returncode,0,installed.stderr)
        self.assertIn('--port',installed.stdout)


if __name__ == '__main__':
    unittest.main()
