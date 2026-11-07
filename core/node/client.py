"""Host transport to the one persistent Bubblewrap supervisor per sandbox."""

import json
import os
import selectors
import threading
import time
import uuid

from .protocol import MAX_IMAGE_BYTES
from .transports.local import LocalTransport


class NodeClient(LocalTransport):
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.lock = threading.RLock()
        self.process = self.selector = self.supervisor_pid = None
        self.request_id = 0
        self.generation = 0
        self.stopped=False
        self.runtime_id=uuid.uuid4().hex
        self.session_state = {"pid": None, "cwd": self.sandbox.tool_workspace, "execution": {"status": "ready"}}
        self.buffer, self.diagnostics = bytearray(), bytearray()
        self.pending = self.result = None



    def wait(self, deadline, kind, cancelled=None):
        limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 2 * 1024 * 1024 + self.sandbox.max_output * 6
        while True:
            if cancelled and cancelled():
                raise KeyboardInterrupt()
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("type") != kind:
                    raise RuntimeError("invalid sandbox supervisor response")
                if kind == "result":
                    if message.get("id") != self.request_id or not isinstance(message.get("result"), dict):
                        raise RuntimeError("mismatched sandbox result")
                    self.result = message["result"]
                    self.session_state = self.result.get("session_state", self.session_state)
                    self.session_state["generation"] = self.generation
                return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("sandbox request timed out")
            for key, _ in self.selector.select(min(remaining, 0.05)):
                if key.data == "input":
                    count = os.write(key.fd, self.pending[:65536])
                    self.pending = self.pending[count:]
                    if not self.pending:
                        self.pending = None
                        self.selector.unregister(self.process.stdin)
                    continue
                data = os.read(key.fd, 65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    if key.data == "protocol":
                        raise RuntimeError("sandbox supervisor exited: " +
                                           self.diagnostics.decode("utf-8", errors="replace"))
                    continue
                if key.data == "diagnostics":
                    self.diagnostics.extend(data[:max(0, self.sandbox.max_output - len(self.diagnostics))])
                else:
                    self.buffer.extend(data)
                    if len(self.buffer) > limit:
                        raise RuntimeError("sandbox response exceeds output budget")


    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def request(self, kind, *, cancelled=None, timeout=None, wait_timeout=None, **arguments):
        with self.lock:
            return self._request(kind, cancelled=cancelled, timeout=timeout, wait_timeout=wait_timeout, **arguments)

    def operation(self,kind,*,cancelled=None,timeout=None,wait_on_cancel=False,**arguments):
        identifier=uuid.uuid4().hex
        runtime_timeout=self.sandbox.timeout if timeout is None else timeout
        request={'kind':kind,'timeout':runtime_timeout,'max_output':self.sandbox.max_output,**arguments}
        result=self.request('operation_submit',operation_id=identifier,request=request,timeout=3)
        if result.get('error'):return result
        interrupted=False
        try:
            while True:
                state=self.request('operation_status',operation_id=identifier,timeout=3)
                if state.get('error'):return state
                if state.get('done'):
                    self.request('operation_forget',operation_id=identifier,timeout=3)
                    return {**state['result'],'session_state':state.get('session_state',self.session_state)}
                if not interrupted and cancelled and cancelled():
                    self.request('operation_cancel',operation_id=identifier,wait_for_result=wait_on_cancel,timeout=3)
                    interrupted=True
                    if not wait_on_cancel:return {'cancelled':True,'exit_code':130,'operation_id':identifier}
                time.sleep(.02)
        except KeyboardInterrupt:
            self.request('operation_cancel',operation_id=identifier,timeout=3)
            raise

    def inspect_tools(self):
        if self.process is None or self.process.poll() is not None:return None
        if not self.lock.acquire(blocking=False):return None
        self.lock.release()
        return self.operation('tool_inventory',timeout=min(4,self.sandbox.timeout))

    def inspect_executions(self):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self.process is None or self.process.poll() is not None:
                return None
            return self._request("runtime_updates", timeout=2)
        finally:
            self.lock.release()

    def _request(self, kind, *, cancelled=None, timeout=None, wait_timeout=None, **arguments):
        self.result = None
        timeout = min(4, self.sandbox.timeout) if kind == "tool_inventory" else self.sandbox.timeout if timeout is None else timeout
        deadline = float("inf") if wait_timeout == 0 or timeout == 0 else time.monotonic() + (wait_timeout if wait_timeout is not None else timeout + 2)
        try:
            self.start(deadline, cancelled)
            self.request_id += 1
            request = {"kind": kind, "id": self.request_id, "timeout": timeout,
                       "max_output": self.sandbox.max_output, **arguments}
            self.pending = memoryview(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "input")
            return self.wait(deadline, "result", cancelled)["result"]
        except KeyboardInterrupt:
            self.abort()
            raise
        except TimeoutError:
            result = self.abort() or {"stdout": "", "stderr": "Shared sandbox was stopped.",
                                     "exit_code": -9, "truncated": False}
            return {**result, "timed_out": True,
                    "session_state": self.session_state}
        except (OSError, ValueError, RuntimeError) as exc:
            self.close("Sandbox process or protocol failed; Python, Bash and /tmp state lost")
            return {"error": str(exc), "stdout": "", "stderr": "", "exit_code": -9,
                    "timed_out": False, "truncated": False,
                    "session_state": self.session_state}

    def close(self, reason="Session closed; Python, Bash and /tmp state cleared"):
        with self.lock:
            return self._close(reason)


