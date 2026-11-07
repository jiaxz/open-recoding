"""Private, same-user control channel for CLI-owned Sessions (no HTTP service)."""

import hashlib
import json
import os
from pathlib import Path
import socket
import stat

LIMIT = 32 * 1024 * 1024

def address(directory):
    root = Path('/tmp') / f'recoding-cli-{os.getuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Unsafe CLI control directory')
    digest = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:32]
    return str(root / (digest + '.sock'))


def request(directory, action, **fields):
    """None means no CLI owner. Never fall back after an owner accepted a request."""
    target = address(directory)
    if not Path(target).exists():
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(45)
        try:
            stream.connect(target)
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        stream.sendall(json.dumps({'action': action, **fields}, ensure_ascii=False).encode() + b'\n')
        try:
            with stream.makefile('rb') as reader:
                raw = reader.readline(LIMIT+1)
        except (ConnectionResetError, BrokenPipeError):
            if action in {'status', 'agents', 'messages', 'speed', 'executions', 'execution_output', 'python_pool'}:
                return None
            raise
        if not raw and action in {'status', 'agents', 'messages', 'speed', 'executions', 'execution_output', 'python_pool'}:
            return None
        if len(raw) > LIMIT or not raw.endswith(b'\n'):
            raise ValueError('Invalid CLI owner response')
        result = json.loads(raw)
        if not result.get('ok'):
            raise ValueError(result.get('error', 'CLI owner rejected request'))
        return result['result']
