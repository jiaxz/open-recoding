"""Metadata-only environment inspection, launched with -I -S in the sandbox."""

import importlib.metadata
import json
import os
import site
import sys


def main():
    roots = [("workspace", os.environ.get("RECODING_PACKAGE_PATH","/opt/recoding/workspace-packages")), ("host-user", site.getusersitepackages())]
    packages = []
    total = 0
    for scope, path in roots:
        for item in importlib.metadata.distributions(path=[path]):
            total += 1
            if len(packages) < 512:
                packages.append({"name": item.metadata.get("Name", ""), "version": item.version, "scope": scope})
    print(json.dumps({"python":sys.executable,"version":sys.version.split()[0],
        "package_roots":[{"scope":scope,"path":path} for scope,path in roots],
        "system_sites":site.getsitepackages(),"cache":os.environ.get("RECODING_CACHE_PATH","/opt/recoding/cache"),
        "packages":packages,"package_count":total,"truncated":total>512,
        "startup_hooks_executed":False}))


if __name__ == "__main__":
    main()
