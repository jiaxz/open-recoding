"""Read-only package inventory across archives, including stopped/running Sessions.

Never opens context/history, takes a Host lease, imports discovered modules, or
installs anything. Results are observations of a potentially changing filesystem.
"""

from collections import defaultdict
from datetime import datetime, timezone
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import stat
import time

from node.packages import package_directory, python_environment
from node.inventory import collection_packages

from .store import FORMAT, SESSION_HOME


SKIP = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", "snapshots",
        ".cache", ".npm", ".cargo", ".rustup", ".local", ".python-packages", ".agent-inputs"}
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]*")


def _read(path, maximum=1024*1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("metadata is not a regular file")
        data = file.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("metadata exceeds size limit")
    return data


def _json(path):
    value = json.loads(_read(path, 65536))
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value


def _packages(directory, errors):
    """Read only distribution records, not package code or executable .pth lines."""
    result = []
    if directory.is_symlink():
        errors.append({"path": str(directory), "error": "package directory is a symlink; skipped"})
        return result
    if not directory.exists():
        return result
    try:
        with os.scandir(directory) as entries:
            records = sorted((Path(item.path) for item in entries if not item.is_symlink() and
                item.name.endswith((".dist-info", ".egg-info"))), key=str)
        for record in records:
            try:
                metadata = record / ("METADATA" if record.name.endswith(".dist-info") else "PKG-INFO") if record.is_dir() else record
                value = BytesParser().parsebytes(_read(metadata), headersonly=True)
                name, version = value.get("Name", ""), value.get("Version", "")
                if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
                    raise ValueError("invalid distribution name/version")
                direct = record / "direct_url.json"
                origin = _json(direct) if record.is_dir() and direct.exists() else None
                source = "index-or-unknown"
                if origin is not None:
                    source = "editable" if origin.get("dir_info", {}).get("editable") else "direct-reference"
                # Direct references can contain local paths or URL credentials.
                dependencies = [line.split("@", 1)[0] + "@ <reference omitted>" if "@" in line else line
                                for line in value.get_all("Requires-Dist", [])]
                result.append({"name": name, "version": version, "source": source,
                    "requires_python": value.get("Requires-Python"), "requires_dist": dependencies,
                    "metadata_directory": str(record)})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append({"path": str(record), "error": str(exc)})
    except OSError as exc:
        errors.append({"path": str(directory), "error": str(exc)})
    return result


def scan_packages(roots, *, max_directories=200000, environment=None, exclude=()):
    """Read workspace packages once; link Sessions without hydrating their histories."""
    environment = environment or python_environment()
    errors, sessions, legacy, legacy_sessions, visited, workspaces = [], [], [], [], set(), {}
    pending = [Path(value).expanduser().absolute() for value in roots]
    roots = list(dict.fromkeys(map(str, pending)))
    excluded = [Path(value).expanduser().absolute() for value in exclude]
    started = time.monotonic()

    def workspace(path):
        key = str(path)
        if key not in workspaces:
            packages = package_directory(path)
            runtime = packages.parent
            interpreter = None
            if runtime.is_symlink():
                errors.append({"path":str(runtime),"error":"runtime is a symlink; skipped"})
                installed = []
            else:
                if (runtime / "python.json").exists():
                    interpreter = _json(runtime / "python.json")
                installed = _packages(packages, errors)
            workspaces[key] = {"directory":key,"package_directory":str(packages),
                               "interpreter":interpreter,"packages":installed,"session_ids":[]}
        return workspaces[key]

    while pending:
        directory = pending.pop()
        if any(directory.is_relative_to(path) for path in excluded):
            continue
        if directory.is_symlink():
            errors.append({"path":str(directory),"error":"search root is a symlink; use its real path"})
            continue
        try:
            identity = directory.stat()
            key = (identity.st_dev, identity.st_ino)
            if key in visited:
                continue
            if len(visited) >= max_directories:
                errors.append({"path":str(directory),"error":"directory scan limit reached; narrow search roots"})
                break
            visited.add(key)
            metadata = directory / "session.json"
            if metadata.is_file():
                data = _json(metadata)
                if data.get("format") == FORMAT:
                    root = directory.parent.parent if directory.parent.name == SESSION_HOME else directory.parent
                    item = workspace(root)
                    item["session_ids"].append(data.get("id"))
                    sessions.append({"id":data.get("id"),"name":data.get("name"),"format_version":data.get("version"),
                        "session_directory":str(directory),"workspace":str(root),"package_directory":item["package_directory"]})
                    old = directory / "runtime" / "python-packages"
                    if old.exists():
                        if old.parent.is_symlink():
                            errors.append({"path":str(old.parent),"error":"legacy runtime is a symlink"})
                        else:
                            legacy_sessions.append({"workspace":str(root),"session_directory":str(directory),"package_directory":str(old),
                                "interpreter":_json(old.parent/"python.json") if (old.parent/"python.json").exists() else None,
                                "packages":_packages(old, errors)})
                    continue
            if directory.name == SESSION_HOME and (directory / "runtime" / "python-packages").exists():
                workspace(directory.parent)
            old = directory / ".python-packages"
            if old.is_dir() and not old.is_symlink():
                legacy.append({"workspace":str(directory),"package_directory":str(old),
                    "note":"Legacy workspace package directory; not injected by current tools.","packages":_packages(old, errors)})
            with os.scandir(directory) as entries:
                children = sorted([Path(item.path) for item in entries if item.is_dir(follow_symlinks=False)
                    and not item.is_symlink() and item.name not in SKIP
                    and not (directory.name == SESSION_HOME and item.name in {"runtime","inputs","cache","server-logs","maintenance"})
                    and not item.name.endswith((".dist-info",".egg-info"))], key=str)
            pending.extend(reversed(children))
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"path":str(directory),"error":str(exc)})
    public = _packages(Path(environment["user_site"]), errors)
    system = []
    for directory in dict.fromkeys(environment.get("system_sites", [])):
        system.extend(_packages(Path(directory),errors))
    installed, system_installed = defaultdict(set), defaultdict(set)
    canonical = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    for package in public:
        installed[canonical(package["name"])].add(package["version"])
    for package in system:
        system_installed[canonical(package["name"])].add(package["version"])
    tracked = collection_packages()
    grouped = {}
    for item in workspaces.values():
        interpreter = item["interpreter"] or {}
        compatible = all(interpreter.get(key) == environment[key] for key in ("cache_tag","platform"))
        for package in item["packages"]:
            name = canonical(package["name"])
            row = grouped.setdefault(name,{"name":name,"versions":set(),"installations":[]})
            row["versions"].add(package["version"])
            row["installations"].append({"workspace":item["directory"],"version":package["version"],
                "session_ids":item["session_ids"],"source":package["source"],"interpreter_compatible":compatible})
    candidates = []
    for name,row in sorted(grouped.items()):
        row["versions"],row["user_versions"] = sorted(row["versions"]),sorted(installed[name])
        row["system_versions"] = sorted(system_installed[name])
        row["host_versions"] = row["user_versions"] or row["system_versions"]
        row["collection_status"] = "tracked" if name in tracked else "unreviewed"
        row["workspace_count"] = len(row["installations"])
        row["session_count"] = sum(len(item["session_ids"]) for item in row["installations"])
        if len(row["versions"]) > 1:
            row["status"] = "conflicting-workspace-versions"
        elif row["user_versions"]:
            row["status"] = "already-shared" if row["user_versions"] == row["versions"] else "different-user-version"
        elif row["system_versions"]:
            row["status"] = "already-system" if row["system_versions"] == row["versions"] else "different-system-version"
        elif any(item["source"] != "index-or-unknown" or not item["interpreter_compatible"] for item in row["installations"]):
            row["status"] = "needs-review"
        else:
            row["status"] = "missing-from-host"
            candidates.append(name+"=="+row["versions"][0])
    # Older package trees are observations too, but are not promoted into the
    # current-runtime install candidates before ABI/version/migration review.
    historical = {}
    for item in legacy + legacy_sessions:
        for package in item["packages"]:
            name = canonical(package["name"])
            row = historical.setdefault(name,{"name":name,"versions":set(),"package_directories":set()})
            row["versions"].add(package["version"])
            row["package_directories"].add(item["package_directory"])
    for name,row in historical.items():
        row["versions"] = sorted(row["versions"])
        row["package_directories"] = sorted(row["package_directories"])
        row["host_versions"] = sorted(installed[name] or system_installed[name])
        row["missing_from_host"] = not row["host_versions"]
        row["collection_status"] = "tracked" if name in tracked else "unreviewed"
    missing_host = sorted(name for name in set(grouped) | set(historical) if not installed[name] and not system_installed[name])
    return {"schema":"recoding.package-inventory","version":2,"generated_at":datetime.now(timezone.utc).isoformat(),
        "roots":roots,"excluded_roots":list(map(str,excluded)),"complete":not errors,"directories_scanned":len(visited),"elapsed_seconds":round(time.monotonic()-started,3),
        "tool_python":{key:environment[key] for key in ("executable","version","cache_tag","platform","user_site","system_sites") if key in environment},
        "workspaces":sorted(workspaces.values(),key=lambda item:item["directory"]),"sessions":sorted(sessions,key=lambda item:item["session_directory"]),
        "packages":sorted(grouped.values(),key=lambda item:item["name"]),"legacy_workspaces":legacy,"legacy_session_packages":legacy_sessions,
        "user_packages":public,"system_packages":system,
        "package_collection":{"tracked":sorted(tracked),"unreviewed":sorted((set(grouped)|set(historical))-tracked)},
        "host_missing_packages":missing_host,"legacy_host_comparison":sorted(historical.values(),key=lambda row:row["name"]),
        "candidate_requirements":candidates if not errors else [],"errors":errors,
        "notes":["Read-only best-effort snapshot. Each workspace package directory is counted once.",
                 "Legacy Session/workspace packages remain untouched; explicit --migrate requires stopped Sessions.",
                 "No histories, package imports, startup hooks, model API or Host lease are used by scans.",
                 "Search roots only; symlinks and internal cache/runtime directories are skipped.",
                 "Candidates are absent from both the system and user sites of the tool Python, not from every interpreter on this machine.",
                 "Review dependencies/sources before Host installation; promote reusable unreviewed packages into the project collection separately."]}
