"""Replace a verified local Web listener without touching unrelated services."""

import errno
import os
from pathlib import Path
import signal
import time
from session.config import default_env_file


PROC = Path("/proc")
PROJECT = default_env_file().parent


def _listener_pids(port):
    inodes = set()
    for name in ("tcp", "tcp6"):
        for line in (PROC / "net" / name).read_text().splitlines()[1:]:
            fields = line.split()
            address, number = fields[1].split(":")
            if (fields[3] == "0A" and int(number, 16) == port and
                    address in {"0100007F", "00000000", "0" * 32}):
                inodes.add("socket:[" + fields[9] + "]")
    owners = set()
    for process in PROC.iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    if str(descriptor.readlink()) in inodes:
                        owners.add(int(process.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return owners


def _is_web_server(pid):
    process = PROC / str(pid)
    if pid == os.getpid() or process.stat().st_uid != os.getuid():
        return False
    args = [os.fsdecode(value) for value in (process / "cmdline").read_bytes().split(b"\0") if value]
    if "-m" in args:
        index = args.index("-m") + 1
        return index < len(args) and args[index] in {"recoding_web", "recoding_web.server", "recoding", "recoding.web", "recoding.web.server", "codexing", "codexing.web", "codexing.web.server", "recurco", "recurco.web", "recurco.web.server",
                                                    "recode", "recode.web", "recode.web.server",
                                                    "mini_agent", "mini_agent.web", "mini_agent.web.server"}
    cwd = (process / "cwd").resolve()
    for value in args[1:]:
        if value.startswith("-"):
            continue
        candidate = Path(value)
        candidate = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
        # Keep recognizing old running instances after the entry file rename.
        if candidate in {PROJECT / "recoding-web.py", PROJECT / "codexing-web.py", PROJECT / "recurco-web.py", PROJECT / "web.py"}:
            return True
        if candidate.name in {"recoding-web", "codexing-web", "recurco-web", "recode-web", "mini-agent-web"} and candidate.is_file():
            source = candidate.read_text()[:2048]
            return any(value in source for value in ("from recoding_web.server import main", "from recoding.web.server import main", "from codexing.web.server import main", "from recurco.web.server import main", "from recode.web.server import main", "from mini_agent.web.server import main"))
        # The first non-option argument is the entry script, not a later path.
        return False
    return False


class _Process:
    """A pidfd prevents a recycled PID from receiving a termination signal."""

    def __init__(self, pid):
        self.pid = pid
        self.fd = os.pidfd_open(pid)

    def send(self, signum):
        try:
            signal.pidfd_send_signal(self.fd, signum)
        except ProcessLookupError:
            pass

    def alive(self):
        import select
        return not select.select([self.fd], [], [], 0)[0]

    def close(self):
        os.close(self.fd)


def _descendants(pid):
    found, pending = set(), [pid]
    while pending:
        parent = pending.pop()
        try:
            for task in (PROC / str(parent) / "execution").iterdir():
                try:
                    for value in (task / "children").read_text().split():
                        child = int(value)
                        if child not in found:
                            found.add(child)
                            pending.append(child)
                except OSError:
                    continue
        except OSError:
            continue
    return found


def _wait(processes, seconds):
    deadline = time.monotonic() + seconds
    while any(process.alive() for process in processes):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def replace_listener(port):
    """Stop only same-user recoding owners of a conflicting localhost port."""
    owners = _listener_pids(port)
    if not owners:
        raise OSError(errno.EADDRINUSE, f"端口 {port} 被占用，无法确认所属进程；未终止任何服务。")
    handles, children = [], []
    try:
        # Validate every owner before sending any signal.
        for pid in sorted(owners):
            handle = _Process(pid)
            handles.append(handle)
            if not _is_web_server(pid):
                raise OSError(errno.EADDRINUSE, f"端口 {port} 被其他程序占用（PID {pid}）；未终止该程序。")
        for pid in sorted({child for pid in owners for child in _descendants(pid)}):
            try:
                children.append(_Process(pid))
            except ProcessLookupError:
                continue
        print(f"Replacing Web server on port {port} · PID {', '.join(map(str, sorted(owners)))}", flush=True)
        print("正在关闭旧任务和 Python/Bash 运行环境；已保存的对话、图片和工作文件保留。", flush=True)
        for handle in handles:
            handle.send(signal.SIGINT)
        if not _wait(handles, 15):
            for handle in handles:
                handle.send(signal.SIGTERM)
            if not _wait(handles, 3):
                for handle in handles:
                    handle.send(signal.SIGKILL)
                if not _wait(handles, 3):
                    raise OSError("旧 Web 服务未退出，无法接管端口。")
        # Normal shutdown closes all workers. Clean up any survivors after a
        # forced stop, including their sandbox/tool descendants, before Resume.
        for child in children:
            if child.alive():
                child.send(signal.SIGTERM)
        if not _wait(children, 2):
            for child in children:
                child.send(signal.SIGKILL)
            if not _wait(children, 2):
                raise OSError("旧运行环境仍未退出，无法安全启动新服务。")
    finally:
        for handle in handles + children:
            handle.close()
