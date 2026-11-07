from python_pool_support import run_python,fresh_python,close_pools
"""Real user-site mounts, import precedence and workspace pip installations."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest

from workspace_support import temporary_workspace
from unittest.mock import patch
import zipfile

from sandbox_support import tool_sandbox
from node.backends.sandbox import Sandbox, SandboxError, user_site_mount
from node.packages import PACKAGE_PATH, package_directory
from session import Session, SessionStore


class UserPackagesTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace(prefix="recoding-packages-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "session"
        self.root.mkdir()
        self.store = SessionStore.create(self.root, "package-fixture", {})
        self.packages = package_directory(self.root)
        self.user_base = self.parent / "user-base"
        # Match /usr/bin/python3 even when the Web server/tests use another interpreter.
        path = subprocess.check_output(
            ["/usr/bin/python3", "-I", "-S", "-c", "import site; print(site.getusersitepackages())"],
            env={"PYTHONUSERBASE": str(self.user_base)}, text=True).strip()
        self.user_site = Path(path)
        self.user_site.mkdir(parents=True)
        package = self.user_site / "agent_user_fixture"
        package.mkdir()
        (package / "__init__.py").write_text("VALUE = 'host-user-version'\n")
        (package / "data.txt").write_text("user-package-data")
        (self.user_base / "private.txt").write_text("host-secret")
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            self.sandbox = tool_sandbox(self.root, timeout=10, package_directory=self.packages)
        self.addCleanup(self.sandbox.close)
        self.addCleanup(close_pools)
        self.mounted_site = self.sandbox.user_site[1]

    def python(self, code):
        return run_python(self.sandbox,code)

    def bash_python(self, code):
        return self.sandbox.execute("bash", json.dumps({"command": "python3 -c " + shlex.quote(code)}))

    def test_user_packages_and_data_available_in_both_interpreters(self):
        code = """import agent_user_fixture as fixture
import importlib.resources, site, sys, os
assert os.environ["PIP_TARGET"] == "/opt/recoding/workspace-packages"
assert os.environ["PIP_CACHE_DIR"] == "/opt/recoding/cache/pip"
print(fixture.VALUE)
print(importlib.resources.files(fixture).joinpath('data.txt').read_text())
print(fixture.__file__)
assert sys.path.index('/opt/recoding/workspace-packages') < sys.path.index(site.getusersitepackages())
assert all(sys.path.index(site.getusersitepackages()) < sys.path.index(p)
           for p in site.getsitepackages() if p in sys.path)
"""
        for execute in (self.python, self.bash_python):
            with self.subTest(execute=execute.__name__):
                result = execute(code)
                self.assertEqual(result["exit_code"], 0, result)
                self.assertEqual(result["stdout"].splitlines(), ["host-user-version", "user-package-data",
                    self.mounted_site + "/agent_user_fixture/__init__.py"])
        self.assertEqual([p.name for p in self.root.iterdir()], [".recoding"])

    def test_user_site_is_read_only_and_home_siblings_stay_hidden(self):
        for execute in (self.python, self.bash_python):
            for code in (
                f"open({self.mounted_site + '/agent_user_fixture/__init__.py'!r}, 'w').write('changed')",
                f"open({self.mounted_site + '/new-package.py'!r}, 'w').write('changed')",
                f"print(open({str(self.user_base / 'private.txt')!r}).read())",
                f"print(open({str(self.user_site / 'agent_user_fixture/data.txt')!r}).read())",
            ):
                with self.subTest(tool=execute.__name__, code=code):
                    result = execute(code)
                    self.assertNotEqual(result["exit_code"], 0, result)
                    self.assertNotIn("host-secret", result["stdout"])
        self.assertEqual((self.user_site / "agent_user_fixture/__init__.py").read_text(),
                         "VALUE = 'host-user-version'\n")
        self.assertFalse((self.user_site / "new-package.py").exists())

    def test_model_credentials_and_session_metadata_are_masked_in_both_tools(self):
        secret = self.root / "model-settings.env"
        secret.write_text("GEMINI_API_KEY=host-only-fixture\n")
        metadata = self.root / ".recoding" / "example"
        metadata.mkdir(parents=True)
        (metadata / "state.json").write_text("private-context-fixture")
        self.sandbox.close()
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            self.sandbox = tool_sandbox(self.root, timeout=10, hidden_files=[secret, self.parent / "missing.env"])
        self.addCleanup(self.sandbox.close)
        self.addCleanup(close_pools)
        code = """from pathlib import Path
try:
    assert Path('model-settings.env').read_text() == ''
except PermissionError:
    pass
assert sorted(p.name for p in Path('.recoding').iterdir()) == ['inputs', 'runtime']
try:
    Path('model-settings.env').write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('model credentials should be read-only and masked')
print('host-data-masked')
"""
        for execute in (self.python, self.bash_python):
            result = execute(code)
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["stdout"], "host-data-masked\n")
        self.assertEqual(secret.read_text(), "GEMINI_API_KEY=host-only-fixture\n")
        self.assertEqual((metadata / "state.json").read_text(), "private-context-fixture")

    def test_pth_paths_and_hooks_run_only_inside_sandbox_without_breaking_worker_protocol(self):
        extra = self.user_site / "extra-libs"
        extra.mkdir()
        (extra / "agent_pth_fixture.py").write_text("VALUE = 'pth-path-ok'\n")
        (self.user_site / "fixture.pth").write_text(
            "extra-libs\n"
            "import os; os.environ['AGENT_PTH_TEST']='sandbox'; print('startup-hook'); "
            "open('/workspace/pth-ran.txt', 'a').write('sandbox\\n')\n")
        # Discovery must not import user code on the host, including .pth hooks.
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            self.assertEqual(user_site_mount(), self.sandbox.user_site)
        self.assertFalse((self.root / "pth-ran.txt").exists())
        code = "import agent_pth_fixture, os; print(agent_pth_fixture.VALUE, os.environ['AGENT_PTH_TEST'])"
        result = self.python(code)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "pth-path-ok sandbox\n")
        result = self.bash_python(code)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "startup-hook\npth-path-ok sandbox\n")
        self.assertEqual((self.root / "pth-ran.txt").read_text(), "sandbox\nsandbox\n")

    def test_workspace_pip_overrides_user_package_and_other_sessions_share_it(self):
        wheel = self.root / "agent_user_fixture-2.0-py3-none-any.whl"
        dist = "agent_user_fixture-2.0.dist-info/"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("agent_user_fixture/__init__.py", "VALUE = 'session-version'\n")
            archive.writestr(dist + "METADATA", "Metadata-Version: 2.1\nName: agent-user-fixture\nVersion: 2.0\n")
            archive.writestr(dist + "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(dist + "RECORD", "")
        self.assertEqual(self.python("import agent_user_fixture; print(agent_user_fixture.VALUE)")["stdout"],
                         "host-user-version\n")
        result = self.sandbox.execute("bash", json.dumps({"command":
            f"python3 -m pip install --upgrade --no-index --no-deps --no-compile {wheel.name}"}))
        self.assertEqual(result["exit_code"], 0, result)
        self.assertTrue((self.packages / "agent_user_fixture/__init__.py").is_file())
        self.assertEqual(self.bash_python("import agent_user_fixture; print(agent_user_fixture.VALUE)")["stdout"],
                         "session-version\n")
        self.assertEqual(self.python("print(agent_user_fixture.VALUE)")["stdout"], "host-user-version\n")
        self.assertEqual(fresh_python(self.sandbox)["exit_code"], 0)
        code = "import agent_user_fixture; print(agent_user_fixture.VALUE); print(agent_user_fixture.__file__)"
        expected = "session-version\n/opt/recoding/workspace-packages/agent_user_fixture/__init__.py\n"
        self.assertEqual(self.python(code)["stdout"], expected)
        self.sandbox.close()
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            reopened = tool_sandbox(self.root, timeout=10, package_directory=self.packages)
            other_store = SessionStore.create(self.root, "other-session", {})
            other = tool_sandbox(self.root, timeout=10, package_directory=package_directory(other_store.workspace))
        self.addCleanup(reopened.close)
        self.addCleanup(other.close)
        self.assertEqual(run_python(reopened,code)["stdout"], expected)
        self.assertEqual(run_python(other,code)["stdout"].splitlines()[0],
                         "session-version")
        self.assertEqual((self.user_site / "agent_user_fixture/__init__.py").read_text(),
                         "VALUE = 'host-user-version'\n")
        self.assertFalse((self.root / ".python-packages").exists())
        # Moving an archive alone leaves project dependencies at the workspace.
        import shutil
        reopened.close()
        other.close()
        moved_workspace = self.parent / "moved-workspace"
        destination = moved_workspace / ".recoding" / self.store.folder.name
        destination.parent.mkdir(parents=True)
        shutil.move(self.store.folder, destination)
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            moved = tool_sandbox(moved_workspace, timeout=10)
        self.addCleanup(moved.close)
        self.assertEqual(run_python(moved,code)["stdout"].splitlines()[0], "host-user-version")
        self.assertTrue((self.packages / "agent_user_fixture/__init__.py").is_file())

    def test_session_config_binds_all_agents_to_one_workspace_directory_without_browse_writes(self):
        untouched = SessionStore.create(self.root, "read-only-config", {})
        session = Session(untouched, {"demo": True, "network": False, "gpu": False}, autostart=False)
        self.addCleanup(session.close)
        expected = str(package_directory(untouched.workspace))
        self.assertEqual(session.config["package_directory"], expected)
        self.assertEqual(session.master.config["package_directory"], expected)
        self.assertEqual(session.team._settings({})["package_directory"], expected)
        self.assertFalse((untouched.folder/"runtime/python-packages").exists())

    def test_symlink_runtime_cannot_become_a_writable_host_mount(self):
        bad = self.root / "unsafe-runtime"
        bad.symlink_to(self.user_base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            tool_sandbox(self.root, package_directory=bad / "python-packages")

    def test_nested_archive_masks_do_not_hide_the_private_package_mount(self):
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            sandbox = tool_sandbox(self.root, hidden_paths=[self.store.folder], package_directory=self.packages)
        self.addCleanup(sandbox.close)
        result = run_python(sandbox,"from pathlib import Path\nassert sorted(p.name for p in Path('/workspace/.recoding').iterdir()) == ['inputs', 'runtime']\n"
            "Path('/opt/recoding/workspace-packages/visible.txt').write_text('private')\nprint('ok')")
        self.assertEqual(result["stdout"], "ok\n", result)
        self.assertEqual((self.packages / "visible.txt").read_text(), "private")

    def test_home_cache_is_shared_across_workspaces_and_can_coexist_with_home_sessions(self):
        home = self.parent / "fake-home"
        home.mkdir()
        from recoding_web.catalog import SessionCatalog
        with patch.dict(os.environ, {"RECODING_HOME": str(home/".recoding"), "PYTHONUSERBASE": str(self.parent/"empty-site")}):
            first = tool_sandbox(home, network=False, gpu=False)
            second = tool_sandbox(self.root, network=False, gpu=False)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertEqual(first.cache_directory, second.cache_directory)
        code = "from pathlib import Path\nPath('/opt/recoding/cache/common').write_text('cache')"
        self.assertEqual(run_python(first,code)["exit_code"],0)
        self.assertEqual(run_python(second,"print(open('/opt/recoding/cache/common').read())")["stdout"],"cache\n")
        actual = SessionStore.create(home,"home-session",{})
        hidden = SessionStore.create(home/".recoding"/"cache","must-not-discover",{})
        catalog = SessionCatalog(home,{})
        from python_pool_support import client
        # run_python created a legitimate pool fixture Session in this workspace.
        self.assertEqual({row["id"] for row in catalog.snapshot()["sessions"]},
                         {actual.id, client(first).pool.store.id})
        self.assertNotIn(hidden.id, {row["id"] for row in catalog.snapshot()["sessions"]})

    def test_missing_user_site_keeps_system_python_working(self):
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.parent / "missing-base")}):
            sandbox = tool_sandbox(self.root)
        self.addCleanup(sandbox.close)
        self.assertIsNone(sandbox.user_site)
        sandbox.check()
        result = run_python(sandbox,"import json; print(json.dumps([42]))")
        self.assertEqual(result["stdout"], "[42]\n", result)

    def test_rejects_user_site_symlink_to_whole_home(self):
        import shutil
        shutil.rmtree(self.user_site)
        self.user_site.symlink_to(Path.home(), target_is_directory=True)
        with patch.dict(os.environ, {"PYTHONUSERBASE": str(self.user_base)}):
            with self.assertRaisesRegex(SandboxError, "dedicated package directory"):
                tool_sandbox(self.root)


if __name__ == "__main__":
    unittest.main()
