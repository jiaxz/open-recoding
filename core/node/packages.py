"""Workspace packages and interpreter metadata; no outer-layer imports."""

import json
import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess

from .storage import atomic_json, protect_directory, read_json


PACKAGE_PATH = "/opt/recoding/workspace-packages"
USER_PACKAGE_BASE = "/opt/recoding/host-user"
CACHE_PATH = "/opt/recoding/cache"
RUNTIME_PATH = "/opt/recoding/workspace-runtime"


def package_directory(workspace):
    return Path(workspace) / ".recoding" / "runtime" / "python-packages"


def home_directory():
    """Recoding-owned user data; HOME itself may also be a workspace."""
    return Path(os.environ.get("RECODING_HOME", str(Path.home() / ".recoding"))).expanduser().absolute()


def prepare_cache():
    directory = home_directory()
    _no_symlinks(directory)
    protect_directory(directory)
    cache = directory / "cache"
    _no_symlinks(cache)
    cache.mkdir(mode=0o700, exist_ok=True)
    return cache


def _no_symlinks(directory):
    for parent in [*reversed(directory.parents), directory]:
        if parent.is_symlink():
            raise ValueError("Runtime storage cannot contain symlinks")


@contextmanager
def package_lock(directory):
    """Cross-process lock shared with the managed package-install tool."""
    directory = Path(directory)
    _no_symlinks(directory.parent)
    protect_directory(directory.parent)
    fd = os.open(directory.parent / "packages.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)



def python_environment():
    """Inspect the tool interpreter without importing user packages/startup hooks."""
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(Path.home())}
    if os.environ.get("PYTHONUSERBASE"):
        environment["PYTHONUSERBASE"] = os.environ["PYTHONUSERBASE"]
    source = (
        "import json,site,sys,sysconfig; "
        "print(json.dumps(dict(executable=sys.executable,version=sys.version.split()[0],"
        "implementation=sys.implementation.name,cache_tag=sys.implementation.cache_tag,"
        "platform=sysconfig.get_platform(),user_site=site.getusersitepackages(),user_base=site.getuserbase(),system_sites=site.getsitepackages(),"
        f"sandbox_user_site=sysconfig.get_path('purelib','posix_user',vars={{'userbase':{USER_PACKAGE_BASE!r}}}))))"
    )
    result = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source],
                            env=environment, capture_output=True, text=True, check=True, timeout=5)
    return json.loads(result.stdout)


def prepare_packages(directory, environment):
    """Validate and record workspace storage without importing package code."""
    directory = Path(directory).absolute()
    _no_symlinks(directory)
    manifest = directory.parent / "python.json"
    identity = {key: environment[key] for key in ("executable", "version", "implementation", "cache_tag", "platform")}
    # Existing compatible storage need not wait behind an ongoing pip install.
    if directory.is_dir() and manifest.is_file() and (directory.parent/"packages.lock").is_file() and not (directory.parent/"packages.lock").is_symlink() and read_json(manifest, 65536) == identity:
        return directory
    with package_lock(directory):
        directory.mkdir(mode=0o700, exist_ok=True)
        if manifest.exists():
            previous = read_json(manifest, 65536)
            if any(previous.get(key) != identity[key] for key in ("cache_tag", "platform")) and any(directory.iterdir()):
                raise ValueError("Workspace packages belong to another Python/platform; rebuild .recoding/runtime/python-packages")
        atomic_json(manifest, identity)
    return directory
