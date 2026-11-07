"""Explicit unsandboxed execution, sharing the existing supervisor and pools."""
from pathlib import Path
from .sandbox import Sandbox


class HostExecution(Sandbox):
    mode = "host"

    def command(self, argv, *, source_fd=None):
        if source_fd is None:
            return list(argv)
        return [*argv[:-1], f"/proc/self/fd/{source_fd}"]

    def environment(self):
        env=super().environment()
        # A convenience default, not filesystem or /tmp isolation.
        temporary=self.runtime_directory/"tmp"/self.execution_owner
        temporary.mkdir(mode=0o700,parents=True,exist_ok=True)
        env["TMPDIR"]=str(temporary)
        return env

    def process_options(self):
        return {"env":self.environment(),"cwd":str(self.directory)}
