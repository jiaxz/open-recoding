import json
import shlex
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
import unittest

from workspace_support import temporary_workspace
import zipfile
from unittest.mock import patch

from sandbox_support import tool_sandbox
from node.backends.sandbox import Sandbox, SandboxError


class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Required integration tests: an unavailable sandbox is a failure, not a skip.
        with temporary_workspace() as directory:
            sandbox = tool_sandbox(directory)
            try:
                sandbox.check()
            finally:
                sandbox.close()

    def setUp(self):
        self.temp = temporary_workspace(prefix="recoding-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "session"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "host-secret.txt"
        self.outside.write_text("outside-secret", encoding="utf-8")
        self.sandbox = tool_sandbox(self.root, timeout=3)
        self.addCleanup(self.sandbox.close)

    def execute(self, code):
        return self.bash("/usr/bin/python3 -c "+shlex.quote(code))

    def bash(self, command):
        return self.sandbox.execute("bash", json.dumps({"command": command}))

    def serve(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(self.root)))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        return f"http://127.0.0.1:{server.server_port}"

    def test_python_file_lifecycle(self):
        result = self.execute("""from pathlib import Path
directory = Path('nested')
directory.mkdir()
path = directory / '你好.txt'
path.write_text('hello', encoding='utf-8')
with path.open('a', encoding='utf-8') as stream:
    stream.write(' 世界')
print(path.read_text(encoding='utf-8'))
assert [item.name for item in directory.iterdir()] == ['你好.txt']
path = path.rename(directory / 'new.txt')
path.unlink()
directory.rmdir()
""")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "hello 世界\n")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name!=".recoding"], [])

    def test_python_cannot_access_outside_paths_or_external_symlinks(self):
        (self.root / "escape").symlink_to(self.outside)
        for path in ("../host-secret.txt", str(self.outside), "/workspace-other/file", "escape"):
            with self.subTest(path=path):
                result = self.execute(f"from pathlib import Path; print(Path({path!r}).read_text())")
                self.assertNotEqual(result["exit_code"], 0)
                self.assertNotIn("outside-secret", result["stdout"])
        result = self.execute("from pathlib import Path; Path('../new').write_text('bad')")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse((Path(self.temp.name) / "new").exists())



    def test_subprocess_execution_and_host_file_isolation(self):
        (self.root / "escape").symlink_to(self.outside)
        good = self.execute("import subprocess; result = subprocess.run(['/usr/bin/python3', '-I', '-c', 'print(42)'], capture_output=True, text=True, check=True); print(result.stdout, end='')")
        self.assertEqual(good["stdout"], "42\n")
        self.assertEqual(good["exit_code"], 0)
        for child_code in (
            f"print(open({str(self.outside)!r}).read())",
            "open('escape', 'w').write('bad')",
            "open('/usr/recoding-escape-test', 'w').write('bad')",
            "open('/outside-workspace', 'w').write('bad')",
        ):
            with self.subTest(code=child_code):
                result = self.execute(f"import subprocess; subprocess.run(['/usr/bin/python3', '-I', '-c', {child_code!r}], check=True)")
                self.assertNotEqual(result["exit_code"], 0, result)
        self.assertEqual(self.outside.read_text(), "outside-secret")

    def test_credentials_are_not_in_tool_environment(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "should-not-leak", "ANOTHER_SECRET": "also-secret"}):
            result = self.execute("import os; from pathlib import Path; print(dict(os.environ)); print(Path('/proc/1/environ').read_bytes())")
        self.assertNotIn("should-not-leak", result["stdout"])
        self.assertNotIn("also-secret", result["stdout"])

    def test_global_commands_are_visible_without_shadowing_system_python(self):
        local_bin = Path(self.temp.name) / "local-bin"
        local_bin.mkdir()
        for name, output in (("recoding-global-fixture", "global-command-ok"),
                             ("python3", "wrong-python")):
            executable = local_bin / name
            executable.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(output) + "\n")
            executable.chmod(0o755)
        original = self.sandbox.command

        def with_local_commands(argv, **options):
            command = original(argv, **options)
            boundary = command.index("--")
            command[boundary:boundary] = ["--ro-bind", str(local_bin), "/usr/local/bin"]
            return command

        with patch.object(self.sandbox, "command", side_effect=with_local_commands):
            result = self.bash("recoding-global-fixture; command -v python3")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "global-command-ok\n/usr/bin/python3\n")

    def test_public_native_runtime_data_is_visible_read_only(self):
        paths = [path for path in ("/etc/alternatives", "/etc/fonts", "/etc/matplotlibrc", "/etc/texmf",
                                   "/var/lib/texmf", "/var/cache/fontconfig")
                 if Path(path).exists()]
        if not paths:
            self.skipTest("Host has no optional public native runtime directories")
        result = self.execute(
            "import os; from pathlib import Path; "
            f"paths={paths!r}; "
            "assert all(Path(path).exists() for path in paths); "
            "assert all(os.statvfs(path).f_flag & os.ST_RDONLY for path in paths); "
            "assert not Path('/etc/shadow').exists(); print('runtime-data-read-only')")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "runtime-data-read-only\n")


    def test_network_http_dns_and_certificates_for_both_tools(self):
        (self.root / "network.txt").write_text("network-ok")
        url = self.serve() + "/network.txt"
        code = f"import urllib.request; print(urllib.request.urlopen({url!r}, timeout=1).read().decode())"
        self.assertEqual(self.execute(code)["stdout"], "network-ok\n")
        import shlex
        self.assertEqual(self.bash("python3 -c " + shlex.quote(code))["stdout"], "network-ok\n")
        result = self.execute("import socket, ssl; assert socket.getaddrinfo('localhost', 80); assert ssl.create_default_context().get_ca_certs(); print('dns-and-ca-ok')")
        self.assertEqual(result["stdout"], "dns-and-ca-ok\n", result)

    def test_no_network_option_blocks_both_tools(self):
        url = self.serve()
        sandbox = tool_sandbox(self.root, network=False)
        self.addCleanup(sandbox.close)
        code = f"import urllib.request; urllib.request.urlopen({url!r}, timeout=0.3)"
        import shlex
        for name, args in (("bash", {"command": "python3 -c " + shlex.quote(code)}),):
            with self.subTest(tool=name):
                result = sandbox.execute(name, json.dumps(args))
                self.assertNotEqual(result["exit_code"], 0, result)

    def test_bash_pipelines_files_fresh_shell_and_isolation(self):
        result = self.bash("printf '你好\\nworld\\n' | head -n 1 > greeting.txt; mkdir nested; cd nested; export ONE_CALL=yes; pwd")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"], "/workspace/nested\n")
        self.assertEqual((self.root / "greeting.txt").read_text(), "你好\n")
        self.assertEqual(self.bash('pwd; printf "%s" "${ONE_CALL-unset}"')["stdout"], "/workspace\nunset")
        self.assertNotEqual(self.bash("false | true")["exit_code"], 0)
        (self.root / "escape").symlink_to(self.outside)
        for command in (f"cat {self.outside}", "cat escape", "printf bad > /usr/agent-escape", "printf bad > ../escape"):
            with self.subTest(command=command):
                self.assertNotEqual(self.bash(command)["exit_code"], 0)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "bash-secret"}):
            self.assertNotIn("bash-secret", self.bash("env")["stdout"])

    def test_bash_output_limit_timeout_and_child_cleanup(self):
        self.sandbox.max_output = 100
        result = self.bash("python3 -c 'print(\"x\" * 1000000)'")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 100)
        self.sandbox.timeout = 0.2
        result = self.bash("sleep 30 & wait")
        self.assertTrue(result["timed_out"], result)
        self.sandbox.timeout = 3
        result = self.bash("python3 -c 'import time; from pathlib import Path; time.sleep(0.4); Path(\"late-bash.txt\").write_text(\"bad\")' >/dev/null 2>&1 &")
        self.assertEqual(result["exit_code"], 0, result)
        time.sleep(0.5)
        self.assertFalse((self.root / "late-bash.txt").exists())






    def test_output_cap_and_timeout_kill_children(self):
        self.sandbox.max_output = 100
        result = self.execute("print('x' * 1000000)")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"].encode()) + len(result["stderr"].encode()), 100)
        self.sandbox.timeout = 0.2
        delayed = "import time; from pathlib import Path; time.sleep(0.6); Path('late.txt').write_text('leaked')"
        result = self.execute(f"import subprocess, time; subprocess.Popen(['/usr/bin/python3', '-I', '-c', {delayed!r}]); time.sleep(10)")
        self.assertTrue(result["timed_out"])
        time.sleep(0.7)
        self.assertFalse((self.root / "late.txt").exists())
        result = self.execute("import os, time; os.close(1); os.close(2); time.sleep(10)")
        self.assertTrue(result["timed_out"])

    def test_invalid_tools_and_no_unsandboxed_fallback(self):
        with patch.object(self.sandbox.session, "request") as run:
            for name in ("unknown", "filesystem"):
                self.assertIn("error", self.sandbox.execute(name, "{}"))
            run.assert_not_called()
        self.assertIn("error", self.sandbox.execute("python", "not-json"))
        self.assertIn("error", self.sandbox.execute("python", '{"command":"pwd"}'))
        for name, args in (("bash", '{}'), ("bash", '{"command":42}'),
                           ("bash", '{"command":"pwd","extra":true}'),
                           ("restart_python", '{"code":"x=1"}'), ("restart_python", '[]')):
            self.assertIn("error", self.sandbox.execute(name, args))
        with patch.object(self.sandbox.session, "request", return_value={"exit_code": 1, "stdout": "", "stderr": "unavailable"}):
            with self.assertRaisesRegex(SandboxError, "refusing unsandboxed"):
                self.sandbox.check()


if __name__ == "__main__":
    unittest.main()
