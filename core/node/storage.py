"""Node-owned atomic files and runtime directories; no cognitive state."""
import json,os,tempfile
from pathlib import Path

def protect_directory(path):
    """Exclude Session data even when the workspace belongs to another Git repo."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Session 存档目录不能是软链接。")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as file:
        file.write("*\n")

def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def read_json(path, limit=64 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Session data must be a regular file: {path.name}")
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Session data is too large: {path.name}")
    return json.loads(data)

def workspace_inputs(workspace):
    container=Path(workspace)/".recoding"
    protect_directory(container)
    directory=container/"inputs"
    if directory.is_symlink():raise ValueError("Workspace inputs cannot be a symlink")
    directory.mkdir(mode=0o700,exist_ok=True)
    return directory
