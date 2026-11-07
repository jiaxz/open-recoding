"""Managed package installation; invoked in the selected execution environment."""

import fcntl
import json
import os
import subprocess
import sys


def main():
    request = json.loads(sys.stdin.read(1024*1024 + 1))
    packages = request["packages"]
    if any(not isinstance(item, str) or not item.strip() or item.strip().startswith("-") or "\0" in item for item in packages):
        raise ValueError("Packages must be requirement strings or wheel paths, not pip options")
    target = os.environ.get("RECODING_PACKAGE_PATH","/opt/recoding/workspace-packages")
    cache = os.environ.get("RECODING_CACHE_PATH","/opt/recoding/cache")+"/pip"
    fd = os.open(os.environ.get("RECODING_RUNTIME_PATH","/opt/recoding/workspace-runtime")+"/packages.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        command = ["/usr/bin/python3", "-m", "pip", "install", "--target", target, "--cache-dir", cache,
                   "--disable-pip-version-check", "--no-input", "--no-compile"]
        for key, option in (("upgrade", "--upgrade"), ("no_index", "--no-index"), ("no_deps", "--no-deps")):
            if request.get(key):
                command.append(option)
        command += [item.strip() for item in packages]
        environment = {**os.environ, "PIP_TARGET": target, "PIP_CACHE_DIR": cache,
                       "PYTHONPATH": target, "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE","/opt/recoding/host-user")}
        return subprocess.call(command, env=environment)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
