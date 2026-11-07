"""Remove only the dedicated test-data root after test resources have closed."""

from contextlib import ExitStack
from pathlib import Path
import fcntl
import os
import shutil


PROJECT = Path(__file__).resolve().parents[1]
TEST_SESSION_ROOT = PROJECT / "sessions" / "tests"


def cleanup_test_data():
    root = TEST_SESSION_ROOT
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("Refusing to clean a symlinked test-data root")
    if not root.exists():
        return 0
    # A leaked sandbox/Host must be closed before its archive can be removed.
    # Keep all acquired read locks until deletion finishes.
    with ExitStack() as leases:
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories
                              if name not in {"node_modules", ".venv", "python-packages", "cache", "__pycache__"}
                              and not (Path(current) / name).is_symlink()]
            for name in {"host.lock", "owner.lock"}.intersection(files):
                path = Path(current) / name
                if path.is_symlink():
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                leases.callback(os.close, fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Test resources are still active; close them before cleanup") from None
        children = list(root.iterdir())
        for child in children:
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
    return len(children)


if __name__ == "__main__":
    cleanup_test_data()
