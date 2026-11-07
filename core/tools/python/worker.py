"""Persistent interpreter, executed by source inside Bubblewrap (never on host)."""

import ctypes
import io
import json
import os
from pathlib import Path
import selectors
import signal
import site
import sys
import threading
import time
import traceback


def children(pid):
    found = set()
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                found.update(map(int, (task / "children").read_text().split()))
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    return found


def cleanup_children(preserve=()):
    # Subreaper mode also adopts double-forked children, including new sessions.
    deadline = time.monotonic() + 0.4
    while True:
        pending, descendants = list(children(os.getpid())), set()
        while pending:
            pid = pending.pop()
            if pid not in descendants and pid not in preserve:
                descendants.add(pid)
                pending.extend(children(pid))
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in descendants:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not descendants or time.monotonic() >= deadline:
            return
        time.sleep(0.005)


class Capture:
    """Capture fd-level output, including subprocesses, with bounded memory."""

    def __init__(self, limit, null_fd, log_paths=None):
        self.limit, self.null_fd = limit, null_fd
        self.parts = {"stdout": bytearray(), "stderr": bytearray()}
        self.logs = {}
        for name, path in (log_paths or {}).items():
            if name not in self.parts:
                raise ValueError("Invalid log stream")
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            self.logs[name] = os.fdopen(descriptor, "wb", buffering=0)
        self.total, self.truncated = 0, False
        self.stop = threading.Event()
        self.selector = selectors.DefaultSelector()
        self.streams = []
        for fd, name in ((1, "stdout"), (2, "stderr")):
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            self.selector.register(read_fd, selectors.EVENT_READ, name)
            os.dup2(write_fd, fd)
            os.close(write_fd)
        for fd in (1, 2):
            self.streams.append(io.TextIOWrapper(io.FileIO(fd, "w", closefd=False),
                                                encoding="utf-8", errors="replace", write_through=True))
        sys.stdout, sys.stderr = self.streams
        self.thread = threading.Thread(target=self.drain, name="python-output", daemon=True)
        self.thread.start()

    def drain(self):
        stopping = None
        try:
            while self.selector.get_map():
                if self.stop.is_set() and stopping is None:
                    stopping = time.monotonic() + 0.2
                events = self.selector.select(0.02)
                if stopping is not None and (not events or time.monotonic() >= stopping):
                    break
                for key, _ in events:
                    data = os.read(key.fd, 65536)
                    if not data:
                        self.selector.unregister(key.fd)
                        os.close(key.fd)
                        continue
                    if key.data in self.logs:
                        self.logs[key.data].write(data)
                    keep = min(len(data), max(0, self.limit - self.total))
                    self.parts[key.data].extend(data[:keep])
                    self.total += keep
                    self.truncated |= keep < len(data)
        finally:
            for key in list(self.selector.get_map().values()):
                os.close(key.fd)
            self.selector.close()
            for stream in self.logs.values():
                stream.close()

    def finish(self):
        for stream in self.streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for fd in (1, 2):
            os.dup2(self.null_fd, fd)
        self.stop.set()
        self.thread.join()
        return {key: value.decode("utf-8", errors="replace") for key, value in self.parts.items()}


def main():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable child cleanup")
    reader = os.fdopen(os.dup(0), "rb")
    writer = os.fdopen(os.dup(1), "wb", buffering=0)
    null_fd = os.open("/dev/null", os.O_RDWR)
    for fd in (0, 1):
        os.dup2(null_fd, fd)
    # -I disables automatic user-site loading. Add only the sandbox's configured
    # read-only site, processing its .pth files inside the sandbox. Keep startup
    # prints off the control pipe; stderr still reaches the host diagnostics.
    sys.path.insert(0, os.environ.get("RECODING_PACKAGE_PATH","/opt/recoding/workspace-packages"))
    user_site = site.getusersitepackages()
    if user_site and os.path.isdir(user_site):
        before = sys.path[:]
        site.addsitedir(user_site)
        added = [path for path in sys.path if path not in before]
        system_sites = site.getsitepackages()
        position = next((i for i, path in enumerate(before) if path in system_sites), len(before))
        sys.path[:] = before[:position] + added + before[position:]
    os.dup2(null_fd, 2)
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    executing = False
    cancel_path = None

    def check_cancel():
        if cancel_path and os.path.exists(cancel_path):raise KeyboardInterrupt()

    def interrupt(signum, frame):
        if executing and (cancel_path is None or os.path.exists(cancel_path)):
            raise KeyboardInterrupt()

    def send(message):
        payload = memoryview(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        while payload:
            payload = payload[writer.write(payload):]

    signal.signal(signal.SIGINT, interrupt)
    send({"type": "ready", "pid": os.getpid(),
          "_context": {"cwd": os.getcwd(), "env": dict(os.environ)}})
    for line in reader:
        request = json.loads(line)
        context = request.get("context")
        if context is not None:
            os.chdir(context["cwd"])
            os.environ.clear()
            os.environ.update(context["env"])
        cancel_path = request.get('cancel_path')
        namespace['check_cancel'] = check_cancel
        previous_children = children(os.getpid())
        capture = Capture(request["max_output"], null_fd, request.get("log_paths"))
        exit_code, interrupted = 0, False
        signal.signal(signal.SIGINT, interrupt)
        try:
            executing = True
            send({"type": "started", "id": request["id"]})
            exec(compile(request["code"], "<python-tool>", "exec"), namespace, namespace)
        except KeyboardInterrupt:
            exit_code, interrupted = 130, True
        except SystemExit as exc:
            exit_code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
            if exc.code is not None and not isinstance(exc.code, int):
                print(str(exc.code), file=sys.stderr)
        except BaseException:
            exit_code = 1
            traceback.print_exc()
        finally:
            executing = False
            signal.signal(signal.SIGINT, interrupt)
            if interrupted:
                # Preserve background work from completed calls. Stop only the
                # current call's newly created descendants on interruption.
                cleanup_children(preserve=previous_children)
            output = capture.finish()
        try:
            cwd = os.getcwd()
        except FileNotFoundError:
            cwd = os.environ.get("RECODING_WORKSPACE","/workspace")
            os.chdir(cwd)
        send({"type": "result", "id": request["id"], "result": {
            **output, "exit_code": exit_code, "timed_out": False,
            "truncated": capture.truncated, "interrupted": interrupted,
            "_context": {"cwd": cwd, "env": dict(os.environ)}}})


if __name__ == "__main__":
    main()
