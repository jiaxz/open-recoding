"""Local process launch and cleanup; shared RPC lives in NodeClient."""
import os,selectors,signal,subprocess,time
from pathlib import Path
from node.build import runtime_source

class LocalTransport:
    def start(self, deadline, cancelled=None):
        if self.stopped:raise RuntimeError('NodeRuntime is closed; create a new runtime on Resume')
        if self.process is not None and self.process.poll() is None:
            return
        if self.process is not None:
            self.close("Shared sandbox exited; Python, Bash and /tmp state lost")
        source = runtime_source(self.sandbox)
        descriptor=os.memfd_create('recoding-sandbox-runtime',os.MFD_CLOEXEC)
        try:
            data=memoryview(source.encode())
            while data:
                count=os.write(descriptor,data);data=data[count:]
            os.lseek(descriptor,0,os.SEEK_SET)
            self.process=subprocess.Popen(
                self.sandbox.command(['/usr/bin/python3','-I','-u','/opt/recoding/supervisor.py'],source_fd=descriptor),
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                start_new_session=True,close_fds=True,pass_fds=(descriptor,),**self.sandbox.process_options())
        finally:os.close(descriptor)
        self.generation += 1
        self.selector = selectors.DefaultSelector()
        self.buffer.clear()
        self.diagnostics.clear()
        for stream, name in ((self.process.stdout, "protocol"), (self.process.stderr, "diagnostics")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, name)
        os.set_blocking(self.process.stdin.fileno(), False)
        ready = self.wait(deadline, "ready", cancelled)
        expected={'protocol':1,'session_id':self.sandbox.execution_owner,'node_id':getattr(self.sandbox,'node_id','local'),'runtime_id':self.runtime_id}
        if ready.get('identity')!=expected:raise RuntimeError('Local runtime identity mismatch')
        self.supervisor_pid = ready["pid"] if self.sandbox.mode == "host" else self.find_process(ready["pid"])
        if self.sandbox.mode == "host" and self.supervisor_pid != self.process.pid:
            raise RuntimeError("Unexpected Host supervisor identity")

    def find_process(self, namespace_pid):
        pending, seen = [self.process.pid], set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("NSpid:") and int(line.split()[-1]) == namespace_pid:
                        return pid
                for task in Path(f"/proc/{pid}/task").iterdir():
                    pending.extend(map(int, (task / "children").read_text().split()))
            except (OSError, ValueError):
                continue
        raise RuntimeError("cannot locate sandbox supervisor")

    def abort(self):
        if self.result is not None:
            return self.result
        if self.supervisor_pid is None or self.pending is not None:
            self.close("Cancelled before request submission; shared sandbox state lost")
            return None
        try:
            os.kill(self.supervisor_pid, signal.SIGINT)
            return self.wait(time.monotonic() + 2, "result")["result"]
        except (OSError, ValueError, RuntimeError, TimeoutError):
            self.close("Sandbox did not recover from cancellation; Python, Bash and /tmp state lost")
            return None

    def descendants(self, pid):
        pending,found=[pid],set()
        while pending:
            current=pending.pop()
            try:
                for task in Path(f'/proc/{current}/task').iterdir():
                    for child in map(int,(task/'children').read_text().split()):
                        if child not in found:found.add(child);pending.append(child)
            except (OSError,ValueError):pass
        return found

    def _close(self, reason):
        process, self.process = self.process, None
        try:
            if process is not None:
                if process.poll() is None:
                    if self.supervisor_pid is not None:
                        try:
                            os.kill(self.supervisor_pid, signal.SIGTERM)
                            process.wait(timeout=5 if self.sandbox.mode=="host" else 1)
                        except (ProcessLookupError, subprocess.TimeoutExpired):
                            pass
                    if self.sandbox.mode=='host' and process.poll() is None:
                        for pid in self.descendants(process.pid):
                            try:os.kill(pid,signal.SIGKILL)
                            except ProcessLookupError:pass
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
        finally:
            if self.selector is not None:
                self.selector.close()
                self.selector = None
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            self.supervisor_pid = self.pending = self.result = None
            self.buffer.clear()
            self.session_state = {"pid": None, "cwd": self.sandbox.tool_workspace,
                                  "generation": self.generation,
                                  "execution": {"status": "reset", "reset_reason": reason}}

