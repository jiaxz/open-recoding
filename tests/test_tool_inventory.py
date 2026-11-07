from python_pool_support import run_python,fresh_python,close_pools
"""Focused discovery, scoring and shared-runtime isolation checks."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from sandbox_support import tool_sandbox
from sandbox_support import Sandbox
from node.inventory import CAPABILITIES, OPTIONAL, PACKAGES, ToolInventoryMonitor, evaluate


def discovery(full=False):
    names = {name for _, _, _, modules, _ in CAPABILITIES for name in modules} | set(OPTIONAL)
    return {"modules": {name: {"found": full, "scope": "system" if full else None} for name in names},
            "commands": {"python3": True, "bash": True}, "python": "/usr/bin/python3",
            "packages": [], "package_count": 0}


class ToolInventoryTests(unittest.TestCase):
    def test_score_is_weighted_capability_coverage_with_alternatives_not_package_count(self):
        complete = discovery(True)
        self.assertEqual(evaluate(complete)["score"], 100)
        for name in OPTIONAL:
            complete["modules"][name]["found"] = False
        self.assertEqual(evaluate(complete)["score"], 100, "Optional ML never reduces the baseline")
        for name in ("pandas", "matplotlib", "PIL", "pip"):
            complete["modules"][name]["found"] = False
        complete["commands"]["uv"] = True
        self.assertEqual(evaluate(complete)["score"], 100, "Equivalent alternatives retain their capability points")
        sparse = evaluate(discovery())
        self.assertEqual((sparse["score"], sparse["level"]), (10, "severe"))
        self.assertIn("Pillow", sparse["host_packages"])
        self.assertEqual(PACKAGES["fitz"], "PyMuPDF")
        self.assertIn("pypdf", sparse["host_packages"])
        self.assertNotIn("fitz", sparse["host_packages"], "Distribution suggestions must use the correct PyPI name")
        # The list of optional binaries is information, not extra penalty.
        complete["commands"]["ffmpeg"] = False
        self.assertEqual(evaluate(complete)["score"], 100)

    def test_monitor_updates_periodically_stops_and_does_not_report_failures_as_zero(self):
        sandbox = Mock()
        sandbox.session.inspect_tools.return_value = {"inventory": discovery()}
        reports, done = [], threading.Event()
        def publish(value):
            reports.append(value)
            if len(reports) >= 2:
                done.set()
        monitor = ToolInventoryMonitor(sandbox, publish, interval=.02)
        monitor.start()
        try:
            self.assertTrue(done.wait(1))
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
        self.assertGreaterEqual(len(reports), 2)
        self.assertTrue(all(item["status"] == "ready" for item in reports))
        errors, failed = [], threading.Event()
        sandbox.session.inspect_tools.return_value = {"error": "fixture timeout"}
        monitor = ToolInventoryMonitor(sandbox, lambda value:(errors.append(value),failed.set()), interval=300)
        monitor.start()
        try:
            self.assertTrue(failed.wait(1))
        finally:
            monitor.close()
        self.assertEqual(errors[0]["status"], "error")
        self.assertNotIn("score", errors[0])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
    def test_real_probe_reuses_sandbox_discovers_new_packages_and_never_executes_package_or_pth(self):
        with temporary_workspace() as directory:
            root = Path(directory)
            packages = root/".recoding"/"runtime"/"python-packages";packages.mkdir(parents=True)
            (packages/"numpy.py").write_text("from pathlib import Path\nPath('/workspace/SHOULD_NOT_RUN').touch()\nraise RuntimeError('never import me')\n")
            host_base = root/"host-user-base"
            with patch.dict(os.environ, {"PYTHONUSERBASE": str(host_base)}):
                user_site = Path(subprocess.check_output(["/usr/bin/python3", "-I", "-S", "-c", "import site;print(site.getusersitepackages())"], text=True).strip())
                user_site.mkdir(parents=True)
                (user_site/"unsafe.pth").write_text("import pathlib; pathlib.Path('/workspace/PTH_SHOULD_NOT_RUN').touch()\n")
                sandbox = tool_sandbox(root, timeout=10, network=False, gpu=False)
            info = packages/"inventory_fixture-1.2.3.dist-info";info.mkdir()
            (info/"METADATA").write_text("Metadata-Version: 2.1\nName: inventory-fixture\nVersion: 1.2.3\n")
            try:
                sandbox.check()
                # Inspect before starting Python: executing the deliberately
                # unsafe .pth must not be needed for discovery.
                result = sandbox.session.inspect_tools()
                self.assertIn("inventory", result, result)
                self.assertFalse((root/"SHOULD_NOT_RUN").exists())
                self.assertFalse((root/"PTH_SHOULD_NOT_RUN").exists())
                found = result["inventory"]
                self.assertTrue(found["modules"]["numpy"]["found"])
                self.assertEqual(found["modules"]["numpy"]["scope"], "workspace")
                self.assertTrue(found["startup_hooks_skipped"])
                self.assertIn({"name": "inventory-fixture", "version": "1.2.3", "scope": "workspace"}, found["packages"])
                # The real Python worker adds workspace without executing its
                # .pth lines either. Persistent memory/shell survive each scan.
                (user_site/"unsafe.pth").unlink()
                python = run_python(sandbox,"kept=42\nimport os\nprint(os.getpid())")
                self.assertEqual(python["exit_code"], 0, python)
                supervisor = sandbox.session_state["pid"]
                sandbox.execute("bash", '{"command":"ordinary=kept"}')
                (packages/"matplotlib.py").write_text("raise RuntimeError('do not import')\n")
                fresh = sandbox.session.inspect_tools()
                self.assertEqual(fresh["inventory"]["modules"]["matplotlib"]["scope"], "workspace")
                self.assertEqual(sandbox.session_state["pid"], supervisor)
                self.assertEqual(run_python(sandbox,'print(kept)')["stdout"], "42\n")
                self.assertEqual(sandbox.execute("bash", json.dumps({"command": 'printf %s "${ordinary-unset}"'}))["stdout"], "unset")
                blocked = []
                with sandbox.session.lock:
                    thread = threading.Thread(target=lambda:blocked.append(sandbox.session.inspect_tools()))
                    thread.start();thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(blocked, [None], "A busy foreground transport is skipped, never interleaved")
            finally:
                sandbox.close()


if __name__ == "__main__":
    unittest.main()
