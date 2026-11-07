"""Keep a running coordinator and freshly spawned workers on one source build."""

import hashlib
from pathlib import Path


def source_revision(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if path.is_file() and not path.is_symlink():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


RUNTIME_REVISION = source_revision()


def require_runtime_revision(expected):
    if expected != RUNTIME_REVISION or source_revision() != RUNTIME_REVISION:
        raise ValueError("宿主与 Agent worker 的代码版本不一致，请重新启动宿主后再 Resume（Web 使用 recoding-web.py）。"
                         "已保存的对话和文件仍保留；此次未启动模型或执行工具。")
