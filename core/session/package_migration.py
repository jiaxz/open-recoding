"""Explicit offline merge of legacy package files, never import/install them."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from node.packages import package_directory, package_lock
from multiagent.storage import StoreLease, atomic_json, protect_directory, read_json
from .package_inventory import _packages


def migrate_workspace(workspace, sources, environment):
    workspace = Path(workspace).resolve(strict=True)
    target = package_directory(workspace)
    container = workspace / ".recoding"
    protect_directory(container)
    report = {"workspace":str(workspace),"target":str(target),"sources":list(map(str,sources)),"status":"blocked","errors":[]}
    stage = None
    try:
        with package_lock(target), ExitStack() as leases:
            # Hold real archive resource/Host locks while publishing. New worker
            # package initialization also serializes on packages.lock.
            for child in sorted(container.iterdir()):
                if child.is_dir() and not child.is_symlink() and (child/"session.json").is_file():
                    leases.enter_context(StoreLease(child))
                    leases.enter_context(StoreLease(child,name="host.lock"))
            files, versions, target_digests = {}, {}, {}
            identity = {key:environment[key] for key in ("executable","version","implementation","cache_tag","platform")}
            for source in [target, *map(Path,sources)]:
                if source.is_symlink() or not source.resolve().is_relative_to(workspace):
                    raise ValueError("Migration source must be inside its workspace and not a symlink")
                if not source.exists():
                    continue
                for parent in [source,*source.parents]:
                    if parent == workspace.parent:break
                    if parent.is_symlink():raise ValueError("Migration source has a symlink ancestor")
                manifest=source.parent/"python.json"
                if source.parent.name == "runtime" and manifest.is_file():
                    old=read_json(manifest,65536)
                    if any(old.get(key)!=identity[key] for key in ("cache_tag","platform")):
                        raise ValueError("Interpreter/platform mismatch: "+str(source))
                issues=[]
                for package in _packages(source,issues):
                    name=re.sub(r"[-_.]+","-",package["name"]).lower()
                    if name in versions and versions[name]!=package["version"]:
                        raise ValueError("Package version conflict: "+name+" ("+versions[name]+" vs "+package["version"]+")")
                    versions[name]=package["version"]
                if issues:raise ValueError("Invalid package metadata: "+str(issues))
                for directory,dirs,names in os.walk(source,followlinks=False):
                    for name in dirs:
                        if (Path(directory)/name).is_symlink():raise ValueError("Symlink in package tree: "+str(Path(directory)/name))
                    dirs[:]=[name for name in dirs if name not in {"__pycache__",".cache"}]
                    for name in names:
                        path=Path(directory)/name
                        if path.is_symlink() or not path.is_file():raise ValueError("Non-regular package file: "+str(path))
                        relative=path.relative_to(source)
                        checksum=hashlib.sha256()
                        with path.open("rb") as stream:
                            for chunk in iter(lambda:stream.read(1024*1024),b""):
                                checksum.update(chunk)
                        digest=checksum.hexdigest()
                        if relative in files and files[relative][1]!=digest:
                            raise ValueError("File conflict: "+relative.as_posix())
                        files[relative]=(path,digest)
                        if source == target:target_digests[relative]=digest
            if not files or (target_digests and target_digests == {key:value[1] for key,value in files.items()}):
                report.update(status="unchanged",files=0)
                return report
            stage=Path(tempfile.mkdtemp(prefix=".packages-migration-",dir=target.parent))
            for relative,(source,_) in files.items():
                destination=stage/relative
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,destination)
            backup=None
            if target.exists():
                backup=target.with_name(".python-packages-before-"+uuid.uuid4().hex[:12])
                os.replace(target,backup)
            try:
                os.replace(stage,target)
            except BaseException:
                if backup is not None:os.replace(backup,target)
                raise
            atomic_json(target.parent/"python.json",identity)
            report.update(status="migrated",files=len(files),backup=str(backup) if backup else None)
    except (OSError,ValueError) as exc:
        report["errors"].append(str(exc))
        if stage is not None and stage.exists():report["staging_directory"]=str(stage)
    return report
