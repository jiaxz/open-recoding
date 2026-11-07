"""Lightweight archive discovery, separate from hosting live Sessions."""

from datetime import datetime
import os
from pathlib import Path
import stat
import threading
import time

from session.store import NAVIGATION_FIELDS, SESSION_HOME, SESSION_HOMES, VERSION, SessionStore, atomic_json, protect_directory, read_json
from session import SessionHostProtocol


class SessionCatalog:
    SKIP_SCAN = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".python-packages", "snapshots", ".recurco", ".recurco-sessions", ".recode-sessions", ".agent-sessions"}


    def __init__(self, root, runtimes, *, discover=True):
        self.root=Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.runtimes=runtimes
        self.stores={}
        self.visible_ids=set()
        self.root_generation=0
        self.watch_directories={self.root}
        self.lock=threading.RLock()
        self.scan_errors=[]
        self.discovery_complete=False
        self._navigation_cache={}
        self._registry_cache=None
        self._directory_cache={}
        self._tree_cache={}
        self.scan_metrics={}
        if discover:self.refresh()

    def _registry(self):
        return self.root / SESSION_HOME / "registry.json"

    def _registry_data(self):
        entries = {str(store.folder): {"stamp": list(store.navigation_stamp),
                   "metadata": {key: value for key, value in store.metadata.items() if key in NAVIGATION_FIELDS}}
                   for store in self.stores.values() if store.id in self.visible_ids}
        return {"version": VERSION, "locations": sorted(entries), "entries": entries}

    def remember(self, data=None):
        container = self.root / SESSION_HOME
        if container.is_symlink():
            raise ValueError("Session 存档目录不能是软链接。")
        protect_directory(container)
        data = data if data is not None else self._registry_data()
        atomic_json(self._registry(), data)
        self._registry_cache = (self._stamp(self._registry()), data)

    def register(self, store):
        previous = self.stores.get(store.id)
        if previous and previous.folder != store.folder and previous.folder.exists():
            raise ValueError("发现重复的 Session ID；请移动原存档，不要同时使用两个相同副本。")
        self.stores[store.id] = store
        self.visible_ids.add(store.id)
        self.watch_directories.add(store.workspace)
        return store

    @staticmethod
    def _stamp(path):
        value = os.stat(path, follow_symlinks=False)
        return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_size

    def _navigation_entry(self, folder, hint=None):
        folder = Path(folder).expanduser()
        if folder.parent.name != SESSION_HOME:
            raise ValueError("Only current .recoding archives are supported")
        cached = self._navigation_cache.get(folder)
        if cached:
            if folder.is_symlink():
                raise ValueError("Session 存档不能是软链接。")
            if cached[0] == self._stamp(folder / "session.json"):
                return cached[1]
        entry = SessionStore(folder, navigation_only=True, navigation_cache=hint)
        self._navigation_cache[folder] = (entry.navigation_stamp, entry)
        return entry

    def load_store(self, identifier):
        """Hydrate only the Session selected by a detail/open action."""
        store = self.stores[identifier]
        if store.navigation_only:
            loaded = SessionStore(store.folder)
            if loaded.id != identifier:
                raise ValueError("Session 存档已发生变化，请刷新列表后重试。")
            self.stores[identifier] = store = loaded
        return store

    def _directory_entries(self, directory):
        """Cache structure, not contents: file edits do not require relisting a directory."""
        value = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(value.st_mode):
            return (), ()
        stamp = (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)
        self.scan_metrics["directories_checked"] += 1
        cached = self._directory_cache.get(directory)
        if cached and cached[0] == stamp:
            self.scan_metrics["directories_reused"] += 1
            return cached[1:]
        children, files = [], []
        with os.scandir(directory) as entries:
            for entry in entries:
                self.scan_metrics["entries_listed"] += 1
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in self.SKIP_SCAN and not (directory.name == SESSION_HOME and entry.name in {"runtime", "inputs", "cache", "server-logs", "maintenance"}):
                        children.append(directory / entry.name)
                elif entry.name == "session.json" and entry.is_file(follow_symlinks=False):
                    files.append(entry.name)
        result = (tuple(children), tuple(files))
        self._directory_cache[directory] = (stamp, *result)
        return result

    def refresh(self, progress=None):
        with self.lock:
            if progress:
                progress.stage("registry", "读取 Session 目录索引", self.root)
            started = time.perf_counter()
            self.scan_metrics = {"directories_checked": 0, "directories_reused": 0, "entries_listed": 0}
            self.scan_errors = []
            known = []
            registry = {}
            if self._registry().is_file():
                try:
                    stamp = self._stamp(self._registry())
                    if self._registry_cache and self._registry_cache[0] == stamp:
                        registry = self._registry_cache[1]
                    else:
                        registry = read_json(self._registry(), 8 * 1024 * 1024)
                    if not isinstance(registry, dict) or not isinstance(registry.get("locations", []), list):
                        raise ValueError("Session 目录索引格式错误，仍继续搜索当前目录。")
                    if registry.get("version") != VERSION:
                        registry = {}  # Derived cache only; rebuild for the current schema.
                    known = registry.get("locations", [])
                    self._registry_cache = (stamp, registry)
                except (OSError, ValueError) as error:
                    self.scan_errors.append(str(error))
            seen = set()
            hints = registry.get("entries", {}) if isinstance(registry, dict) else {}
            if not isinstance(hints, dict):
                hints = {}
            for folder in known:
                if not isinstance(folder, str) or not Path(folder).is_relative_to(self.root):
                    continue
                if progress:
                    progress.stage("metadata", "核对 Session 名称与位置", folder)
                try:
                    entry = self._navigation_entry(folder, hints.get(str(folder)))
                    self.register(entry)
                    seen.add(entry.folder)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            roots = []
            for candidate in sorted(self.watch_directories, key=lambda path: (len(path.parts), str(path))):
                if not any(candidate.is_relative_to(root) for root in roots):
                    roots.append(candidate)
            visited = set()
            for root in roots:
                pending = [root]
                while pending:
                    here = pending.pop()
                    if here in seen:
                        continue  # Indexed archives never need their image/run/agent directories listed.
                    visited.add(here)
                    if progress:
                        progress.stage("scan", "扫描工作目录", f"已检查 {self.scan_metrics['directories_checked']} 个目录 · {here}")
                    try:
                        children, files = self._directory_entries(here)
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        self.scan_errors.append(str(error))
                        continue
                    if "session.json" in files:
                        try:
                            store = self._navigation_entry(here, hints.get(str(here)))
                        except (OSError, ValueError, KeyError):
                            if here.parent.name != SESSION_HOME:
                                pending.extend(children)  # Unrelated workspace metadata is not an archive.
                            continue  # Unsupported archives stay opaque, including their child Agents.
                        try:
                            self.register(store)
                            seen.add(store.folder)
                        except ValueError as error:
                            self.scan_errors.append(str(error))
                    else:
                        pending.extend(children)
            for identifier in list(self.stores):
                if self.stores[identifier].folder not in seen and identifier not in self.runtimes:
                    del self.stores[identifier]
            self.visible_ids = {identifier for identifier, store in self.stores.items() if store.folder in seen}
            folders = {store.folder for store in self.stores.values()}
            self._navigation_cache = {path: value for path, value in self._navigation_cache.items() if value[1].folder in folders}
            self._directory_cache = {path: value for path, value in self._directory_cache.items() if path in visited}
            registry_started = time.perf_counter()
            if progress:
                progress.stage("index", "同步 Session 目录索引", f"发现 {len(self.stores)} 个 Sessions")
            if self.stores:
                data = self._registry_data()
                if data != registry:
                    try:
                        self.remember(data)
                    except OSError as error:
                        self.scan_errors.append(str(error))
            self.discovery_complete = True
            self.scan_metrics.update(scan_seconds=time.perf_counter() - started,
                                     registry_seconds=time.perf_counter() - registry_started)

    def resolve(self, value):
        if not self.discovery_complete:
            self.refresh()
        if value in self.stores:
            return value
        matches = [identifier for identifier, store in self.stores.items() if store.name == value]
        if len(matches) == 1:
            return matches[0]
        raise KeyError("找不到唯一的 Session，请选择存档或提供 Session ID。")

    @staticmethod
    def validate_workspace(directory):
        path = Path(directory).expanduser().resolve()
        protected = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev")
        if path == Path("/") or SESSION_HOMES.intersection(path.parts) or any(path.is_relative_to(Path(item).resolve()) for item in protected):
            raise ValueError("请选择项目目录，不能使用系统目录或 Session 存档目录作为工作区。")
        return path

    def listing(self, progress=None):
        self.refresh(progress)
        with self.lock:
            items = []
            for identifier, store in self.stores.items():
                if identifier not in self.visible_ids:
                    continue
                try:
                    item=self.runtimes[identifier].navigation(store) if identifier in self.runtimes else {**store.navigation_info(),
                        'status':'stop' if SessionHostProtocol.available(store.folder) else 'run','active_agents':0}
                    item['owned']=identifier in self.runtimes
                    item['busy']=item['status']=='run' and not item['owned']
                    items.append(item)
                except FileNotFoundError:
                    continue  # Deletion can race the scan; reconcile on the next poll.
            return sorted(items, key=lambda item: (not item.get("pinned", False), item["directory"], -item["updated_at"]))

    def _workspace_tree(self, sessions, expanded):
        """List immediate subdirectories only where navigation needs them."""
        if not isinstance(expanded, (list, tuple)) or any(not isinstance(path, str) for path in expanded):
            raise ValueError("expanded 必须是目录路径列表。")
        paths = {self.root}
        for session in sessions:
            directory = Path(session["directory"])
            while directory.is_relative_to(self.root):
                paths.add(directory)
                if directory == self.root:
                    break
                directory = directory.parent
        for value in expanded:
            directory = Path(value)
            if directory.is_absolute() and directory.is_relative_to(self.root) and SESSION_HOME not in directory.relative_to(self.root).parts:
                paths.add(directory)
        result = []
        for directory in sorted(paths):
            try:
                # Do not follow a workspace link, including links in ancestors.
                resolved = directory.resolve(strict=True)
                if not resolved.is_relative_to(self.root) or resolved != directory:
                    continue
                value = os.stat(directory, follow_symlinks=False)
                if not stat.S_ISDIR(value.st_mode):
                    continue
                stamp = (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)
                cached = self._tree_cache.get(directory)
                if cached and cached[0] == stamp:
                    try:
                        # Fast create/delete can share a filesystem timestamp.
                        # A removed/replaced child invalidates the cached listing.
                        if any(not stat.S_ISDIR(os.stat(child, follow_symlinks=False).st_mode)
                               for child in cached[1]):
                            cached = None
                    except FileNotFoundError:
                        cached = None
                if cached and cached[0] == stamp:
                    children = cached[1]
                else:
                    with os.scandir(directory) as entries:
                        children = sorted(str(directory / entry.name) for entry in entries
                                          if entry.name != SESSION_HOME and entry.is_dir(follow_symlinks=False))
                    self._tree_cache[directory] = (stamp, children)
                result.append({"directory": str(directory), "children": children})
            except OSError as error:
                result.append({"directory": str(directory), "children": [], "error": str(error)})
        self._tree_cache = {path: value for path, value in self._tree_cache.items() if path in paths}
        return result

    def snapshot(self, progress=None, *, expanded=()):
        """Lightweight Sessions plus the currently browsed directory branches."""
        started = time.perf_counter()
        if progress:
            progress.stage("lock", "等待 Session 管理器", "等待目录扫描或其他 Session 操作释放锁")
        with self.lock:
            lock_wait = time.perf_counter() - started
            sessions = self.listing(progress)
            if progress:
                progress.stage("directories", "读取展开目录的子目录")
            directory_tree = self._workspace_tree(sessions, expanded)
            if progress:
                progress.stage("navigation", "整理 Session 列表", f"{len(sessions)} 个 Sessions")
            return {"sessions": sessions, "root": str(self.root), "root_generation": self.root_generation,
                    "workspaces": sorted({str(self.root), *[item["directory"] for item in sessions]}),
                    "directory_tree": directory_tree,
                    "scan_errors": list(self.scan_errors),
                    "sync": {**self.scan_metrics, "lock_wait_seconds": lock_wait,
                             "elapsed_seconds": time.perf_counter() - started}}

    def change_workspace(self, directory, progress=None):
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("请输入服务器上的工作目录。")
        chosen = self.validate_workspace(directory)
        if not chosen.is_dir():
            raise ValueError("请选择服务器上已经存在的目录。")
        with self.lock:
            self.root = chosen
            self.root_generation += 1
            self.watch_directories = {chosen}
            self._tree_cache.clear()
            # Running environments keep their original workspaces and remain
            # addressable. They are not navigation entries in the new scope.
            self.stores = {identifier: runner.store for identifier, runner in self.runtimes.items()}
            self.visible_ids.clear()
            self._navigation_cache.clear()
            self._directory_cache.clear()
            self._registry_cache = None
            self.discovery_complete = False
            return self.snapshot(progress)

    def workspaces(self):
        with self.lock:
            return sorted({str(self.root), *[str(store.workspace) for store in self.stores.values()]})

    def directories(self, value=None):
        chosen = Path(value).expanduser() if value else self.root
        if not chosen.is_absolute():
            chosen = self.root / chosen
        chosen = chosen.resolve(strict=True)
        if not chosen.is_dir():
            raise ValueError("不是目录。")
        entries = []
        for child in sorted(chosen.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_dir() and not child.is_symlink() and child.name not in self.SKIP_SCAN:
                entries.append({"name": child.name, "path": str(child), "session": (child / "session.json").is_file()})
        return {"directory": str(chosen), "parent": str(chosen.parent), "entries": entries[:500],
                "truncated": len(entries) > 500}

