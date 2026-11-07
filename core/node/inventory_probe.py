"""Read-only discovery child, run with -I -S inside the existing sandbox.

MODULES/BINARIES are supplied by the host. No third-party package is imported;
no .pth executable line, usercustomize or sitecustomize is executed.
"""

import importlib.machinery
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import site
import sys


def main():
    resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))
    user = site.getusersitepackages()
    package_root=os.environ.get("RECODING_PACKAGE_PATH","/opt/recoding/workspace-packages")
    workspace=os.environ.get("RECODING_WORKSPACE","/workspace")
    roots = [package_root, user, *site.getsitepackages()]
    paths, hooks = [], False
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        paths.append(root)
        if root == package_root:
            continue  # The real worker inserts this path; it does not run .pth here.
        for file in sorted(Path(root).glob("*.pth"))[:100]:
            try:
                for line in file.read_text(errors="replace")[:65536].splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith(("import ", "import\t")):
                        hooks = True
                        continue
                    candidate = Path(root)/line
                    if candidate.is_dir():
                        paths.append(str(candidate))
            except OSError:
                continue
    paths = list(dict.fromkeys([*paths, *sys.path]))
    def scope(path):
        if path and (str(path) == package_root or str(path).startswith(package_root+"/")):
            return "workspace"
        if path and str(path).startswith(workspace+"/"):
            return "workspace"
        return "user-site" if path and user and str(path).startswith(user+"/") else "system"
    modules = {}
    for name in MODULES:
        try:
            spec = importlib.machinery.PathFinder.find_spec(name, paths)
            origin = spec.origin or next(iter(spec.submodule_search_locations or []), "") if spec else ""
            modules[name] = {"found": spec is not None, "scope": scope(origin) if spec else None}
        except (OSError, ValueError, ImportError):
            modules[name] = {"found": False, "scope": None}
    packages, count = [], 0
    for distribution in importlib.metadata.distributions(path=paths):
        count += 1
        if len(packages) < 512:
            try:
                name, version = distribution.metadata.get("Name", "")[:200], distribution.version[:100]
                if name:
                    packages.append({"name": name, "version": version, "scope": scope(distribution.locate_file(""))})
            except (OSError, ValueError, TypeError):
                continue
    commands = {name: bool(shutil.which(name)) for name in BINARIES}
    commands.update(python3=Path("/usr/bin/python3").is_file(), bash=Path("/bin/bash").is_file())
    print(json.dumps({"python": "/usr/bin/python3", "python_version": sys.version.split()[0], "modules": modules,
        "commands": commands, "packages": sorted(packages, key=lambda item:item["name"].lower()),
        "package_count": count, "packages_truncated": count>512, "startup_hooks_skipped": hooks,
        "scope": "shared_host_fresh_python_discovery" if os.environ.get("RECODING_EXECUTION_MODE")=="host" else "shared_sandbox_fresh_python_discovery"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
