"""Agent checkpoints, assets, journals and member storage."""

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .config import AGENT_ICON, MASTER_ICON
from .events import model_rate, tool_source, update_token_usage, update_run
from .image_reader import MAX_IMAGE_BYTES, image_attachment
from .multimodal import attachments_for


class ContextHistory:
    def __init__(self, directory, *, system, tools, model):
        directory = Path(directory)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        for suffix in range(1000):
            extra = f"_{suffix}" if suffix else ""
            self.path = directory / f"context_{stamp}{extra}.jsonl"
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                break
            except FileExistsError:
                continue
        else:
            raise OSError("cannot create a unique session context history file")
        self.file = os.fdopen(fd, "w", encoding="utf-8")
        self.sequence = 0
        try:
            self.record("session_start", format_version=5, directory=str(directory), model=model,
                        tools=tools, messages=[{"role": "system", "content": system}])
        except BaseException:
            self.file.close()
            raise

    def record(self, kind, **fields):
        self.sequence += 1
        entry = {"sequence": self.sequence, "timestamp": datetime.now().astimezone().isoformat(),
                 "type": kind, **fields}
        try:
            self.file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.file.flush()
        except OSError as exc:
            raise OSError(f"cannot write context history {self.path}: {exc}") from None

    def close(self):
        self.file.close()


STATE_VERSION = 5

def instant():
    return datetime.now(timezone.utc).isoformat()


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


WORKSPACE_INPUT_PATH = "/workspace/.recoding/inputs"


def workspace_inputs(workspace):
    """Keep shared upload copies inside the one workspace metadata container."""
    workspace = Path(workspace)
    container = workspace / ".recoding"
    protect_directory(container)
    target, legacy = container / "inputs", workspace / ".agent-inputs"
    lock = os.open(container / "inputs.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_symlink() or legacy.is_symlink():
            raise ValueError("Workspace input directories cannot be symlinks")
        if legacy.exists():
            if not legacy.is_dir():
                raise ValueError("Workspace inputs must be a directory")
            children = list(legacy.iterdir())
            if any(child.is_symlink() or not child.is_file() for child in children):
                raise ValueError("Workspace input migration requires regular files")
            if not target.exists():
                legacy.rename(target)
            else:
                # Preflight before moving any file; never replace a different
                # existing upload or follow symlinks.
                for child in children:
                    destination = target / child.name
                    if destination.is_symlink():
                        raise ValueError("Workspace input migration requires regular files")
                    if destination.exists() and (not destination.is_file() or destination.read_bytes() != child.read_bytes()):
                        raise ValueError("Workspace input migration conflict: " + child.name)
                for child in children:
                    destination = target / child.name
                    if destination.exists():
                        child.unlink()
                    else:
                        child.rename(destination)
                legacy.rmdir()
        target.mkdir(mode=0o700, exist_ok=True)
        return target
    finally:
        os.close(lock)


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


class StoreLease:
    def __init__(self, folder, *, shared=False, name='owner.lock'):
        self.fd = None
        if name not in {'owner.lock','host.lock'}:raise ValueError('Invalid lock name')
        self.fd = os.open(Path(folder) / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise ValueError("这个 Session 正由另一个运行环境使用，请先关闭原运行环境。") from None

    @classmethod
    def available(cls, folder, *, name='owner.lock'):
        try:
            lease=cls(folder,shared=True,name=name)
        except ValueError:
            return False
        lease.close()
        return True

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


class AgentStore:
    """Own context, images, journal and lease; knows no Session or outer layer."""
    metadata_filename = "agent.json"

    def __init__(self, folder, *, workspace, metadata=None):
        folder = Path(folder).expanduser()
        if folder.is_symlink():
            raise ValueError("Agent storage cannot be a symlink")
        self.folder = folder.resolve(strict=True)
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        self.metadata = dict(metadata) if metadata is not None else read_json(self.folder / self.metadata_filename, 65536)
        self.id, self.name = self.metadata["id"], self.metadata["name"]
        if any((self.folder / name).is_symlink() for name in ("images", "runs")):
            raise ValueError("Agent images and journals cannot be symlinks")
        self._saved_images = set()

    @classmethod
    def create(cls, folder, *, workspace, name="Agent", settings=None):
        folder = Path(folder)
        protect_directory(folder)
        for child in ("images", "runs"):
            (folder / child).mkdir(mode=0o700, exist_ok=True)
        atomic_json(folder / "agent.json", {"id": uuid.uuid4().hex, "name": name,
                    "created_at": instant(), "settings": settings or {}})
        return cls(folder, workspace=workspace)

    def settings(self):
        return deepcopy(self.metadata.get("settings", {}))

    def info(self):
        return {"id": self.id, "name": self.name, "directory": str(self.workspace),
                "storage_directory": str(self.folder), "created_at": self.metadata["created_at"]}

    def exists(self):
        return (self.folder / self.metadata_filename).is_file()

    def save_attachment(self, attachment):
        packed = self.pack_messages([{"role": "tool", "_image_attachment": attachment}])
        return packed[0]["_image_attachment"]

    def image_asset(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("无效的图片 ID。")
        for suffix in (".png", ".jpg"):
            path = self.folder / "images" / (digest + suffix)
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise ValueError("图片资产必须是普通文件。")
                with path.open("rb") as file:
                    data = file.read(MAX_IMAGE_BYTES + 1)
                picture = image_attachment(data, "")
                if picture["sha256"] != digest:
                    raise ValueError("图片资产损坏。")
                return data, picture["mime_type"]
        raise FileNotFoundError("找不到图片资产。")

    def add_upload(self, data, name):
        picture = image_attachment(data, "")
        saved = self.save_attachment(picture)
        folder = workspace_inputs(self.workspace)
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        identifier = uuid.uuid4().hex
        filename = identifier + (".png" if picture["mime_type"] == "image/png" else ".jpg")
        try:
            try:
                ignore = os.open(".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except FileExistsError:
                pass
            else:
                with os.fdopen(ignore, "w") as file:
                    file.write("*\n")
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
        finally:
            os.close(directory)
        saved.update({"id": identifier, "name": str(name)[:180], "path": WORKSPACE_INPUT_PATH + "/" + filename})
        index = self.uploads()
        index[identifier] = saved
        atomic_json(self.folder / "uploads.json", index)
        return saved

    def uploads(self):
        path = self.folder / "uploads.json"
        return read_json(path) if path.exists() else {}

    def upload_attachment(self, identifier):
        item = dict(self.uploads()[identifier])
        if item.get("path", "").startswith(("/workspace/.agent-inputs/", "/workspace/.recoding.../inputs/")):
            item["path"] = WORKSPACE_INPUT_PATH + "/" + item["path"].rsplit("/", 1)[-1]
        data, mime = self.image_asset(item["sha256"])
        item["data_url"] = "data:" + mime + ";base64," + base64.b64encode(data).decode()
        item.pop("asset", None)
        return item

    def pack_messages(self, messages):
        result = deepcopy(messages)
        for message in result:
            for attachment in attachments_for(message):
                if "data_url" not in attachment:
                    continue
                url = attachment.pop("data_url")
                header, encoded = url.split(",", 1)
                if header not in {"data:image/png;base64", "data:image/jpeg;base64"}:
                    raise ValueError("不支持的 Session 图片格式。")
                digest = attachment["sha256"]
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("Session 图片摘要格式错误。")
                suffix = ".png" if header == "data:image/png;base64" else ".jpg"
                relative = "images/" + digest + suffix
                if digest not in self._saved_images:
                    data = base64.b64decode(encoded, validate=True)
                    if hashlib.sha256(data).hexdigest() != digest or len(data) != attachment["bytes"]:
                        raise ValueError("Session 图片内容与摘要不匹配。")
                    target = self.folder / relative
                    target.parent.mkdir(mode=0o700, exist_ok=True)
                    if target.is_symlink():
                        raise ValueError("Session 图片不能是软链接。")
                    if target.exists():
                        if target.read_bytes() != data:
                            raise ValueError("Session 中已有的图片资产损坏。")
                    else:
                        fd, temporary = tempfile.mkstemp(prefix=".image-", dir=target.parent)
                        try:
                            with os.fdopen(fd, "wb") as file:
                                file.write(data)
                                file.flush()
                                os.fsync(file.fileno())
                            os.replace(temporary, target)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                    self._saved_images.add(digest)
                attachment["asset"] = relative
        return result

    def save_state(self, state):
        value = {"iteration": 0, "tool_count": 0, "run_state": "stopped", "location": {},
                 **{key: deepcopy(value) for key, value in state.items() if key in {"iteration", "tool_count", "run_state", "location"}},
                 "version": STATE_VERSION, "updated_at": instant(),
                 "messages": self.pack_messages(state["messages"])}
        atomic_json(self.folder / "state.json", value)

    def load_state(self):
        if not (self.folder / "state.json").exists():
            return None
        state = read_json(self.folder / "state.json")
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(state.get("messages"), list):
            raise ValueError("Session 上下文存档格式错误。")
        for message in state["messages"]:
            if not isinstance(message, dict):
                raise ValueError("Session 消息格式错误。")
            for attachment in attachments_for(message):
                if "asset" not in attachment:
                    continue
                relative = attachment.pop("asset")
                if not isinstance(relative, str) or not re.fullmatch(r"images/[a-f0-9]{64}\.(png|jpg)", relative):
                    raise ValueError("Session 图片路径无效。")
                target = self.folder / relative
                if target.is_symlink() or not target.resolve().is_relative_to(self.folder):
                    raise ValueError("Session 图片路径越界。")
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != attachment["sha256"] or len(data) != attachment["bytes"]:
                    raise ValueError("Session 图片缺失或损坏，不能静默丢弃历史图片。")
                attachment["data_url"] = "data:" + attachment["mime_type"] + ";base64," + base64.b64encode(data).decode()
        return state

    def append_event(self, event):
        fd = os.open(self.folder / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def tool_source(self, run_id, call_id, sequence):
        if (not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id)
                or not isinstance(call_id, str) or not 0 < len(call_id) <= 4096
                or type(sequence) is not int or sequence < 1):
            raise ValueError("工具调用标识无效。")
        path = self.folder / "events.jsonl"
        if path.is_symlink():
            raise ValueError("Session 事件日志不能是软链接。")
        with path.open(encoding="utf-8") as file:
            for line in file:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get("type") == "tool_start"
                        and event.get("run_id") == run_id and event.get("call_id") == call_id and event.get("sequence") == sequence):
                    return tool_source(event.get("arguments", ""), event.get("name"))
        raise ValueError("找不到该工具调用的完整源码。")

    def iter_events(self):
        """Stream valid committed observations without loading message bodies together."""
        path = self.folder / "events.jsonl"
        if path.exists():
            if path.is_symlink():
                raise ValueError("Session 事件日志不能是软链接。")
            with path.open(encoding="utf-8") as file:
                for line in file:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # A process kill may leave an incomplete trailing event.
                    if isinstance(event, dict) and type(event.get("sequence")) is int:
                        yield event

    def load_model_rates(self):
        # Whole Session, including calls outside the 800-event UI window.
        samples = {}
        for event in self.iter_events():
            sample = model_rate(event)
            if sample is not None:
                samples[(sample["run_id"], sample["step"])] = sample
        return list(samples.values())

    def load_events(self, limit=800, before=None):
        events = deque(maxlen=limit)
        for event in self.iter_events():
            if before is None or event["sequence"] < before:
                events.append(event)
        return list(events)

class AgentHistory(ContextHistory):
    """Original full JSONL journal plus an atomic, portable context checkpoint."""
    def __init__(self, store, *, system, tools, model, state=None):
        self.store = store
        self.state = state or {"messages": [{"role": "system", "content": system}], "iteration": 0,
                               "tool_count": 0, "run_state": "stopped", "location": {"phase": "startup"}}
        self.initializing = True
        (store.folder / "runs").mkdir(mode=0o700, exist_ok=True)
        super().__init__(store.folder / "runs", system=system, tools=tools, model=model)
        self.initializing = False
        self.store.save_state(self.state)

    def record(self, kind, **fields):
        super().record(kind, **fields)
        if self.initializing:
            return
        state = self.state
        if kind == "message":
            state["messages"] = state["messages"][:fields["index"]] + [deepcopy(fields["message"])]
            state["location"] = {"phase": fields["message"]["role"], "step": state.get("location", {}).get("step", 0)}
        elif kind in {"context_resume", "loop_error"}:
            state["messages"] = deepcopy(fields["messages"])
            state["iteration"] = fields.get("iteration", state.get("iteration", 0))
            state["tool_count"] = fields.get("tool_count", state.get("tool_count", 0))
            previous = fields.get("previous_location")
            restored = previous.get("previous_location") if isinstance(previous, dict) else None
            state["location"] = deepcopy(fields.get("location", restored if isinstance(restored, dict) else state.get("location", {})))
        elif kind == "context_compaction_start":
            state["run_state"] = "running"
            state["iteration"] = fields["step"]
            state["location"] = {"phase": "context_compaction", "step": fields["step"], "id": fields["id"]}
        elif kind == "model_request":
            state["run_state"] = "running"
            state["iteration"] = fields["iteration"]
            state["location"] = {"phase": "model", "step": fields["step"]}
        elif kind == "tool_start":
            state["tool_count"] = fields["number"]
            state["run_state"] = "sleeping" if fields["name"] == "sleep" else "running"
            state["location"] = {"phase": "tool", "name": fields["name"],
                                 "step": fields["step"], "call_id": fields["call_id"]}
        elif kind == "tool_end":
            state["run_state"] = "running"
            state["tool_count"] = fields["number"]
        elif kind == "loop_end":
            state["location"] = fields.get("location", state.get("location", {}))
        elif kind == "agent_stopped":
            state["run_state"] = "failed" if fields.get("status") == "failed" or fields.get("reason") in {"error", "failed"} else "stopped"
        else:
            return
        self.store.save_state(state)


class MemberStore(AgentStore):
    metadata_filename = "session.json"

    def __init__(self, session, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid agent ID")
        if (session.folder / "agents").is_symlink():
            raise ValueError("Agent storage cannot be a symlink")
        self.session = session
        folder = session.folder / "agents" / identifier
        metadata = read_json(folder / "session.json", 65536)
        if (metadata.get("format"), metadata.get("version")) != (session.metadata.get("format"), session.metadata.get("version")):
            raise ValueError("Invalid Agent storage format")
        super().__init__(folder, workspace=session.workspace, metadata=metadata)

    @classmethod
    def create_agent(cls, session, identifier, name, settings, *, icon=AGENT_ICON):
        container = session.folder / "agents"
        protect_directory(container)
        folder = container / identifier
        folder.mkdir(mode=0o700)
        for child in ("images", "runs"):
            (folder / child).mkdir(mode=0o700)
        atomic_json(folder / "session.json", {"format": session.metadata["format"], "version": session.metadata["version"], "id": identifier,
            "name": name, "icon": icon, "created_at": instant(), "settings": settings})
        # The runner creates its own System-only context. Never copy parent
        # messages, images, signatures or tool-memory into a new child.
        return cls(session, identifier)


def saved_token_usage(store):
    """Recover numeric display fields from a bounded event tail, never raw context."""
    path = store.folder / "events.jsonl"
    usage = {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            start = max(0, os.fstat(stream.fileno()).st_size - 512 * 1024)
            stream.seek(start)
            lines = stream.read(512 * 1024).split(b"\n")
        if start:
            lines = lines[1:]
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            fields = {}
            update_token_usage(fields, event)
            for key, value in fields.items():
                usage.setdefault(key, value)
            if {"input_tokens", "compaction_saved_tokens", "compaction_generation"} <= usage.keys():
                break
    except (OSError, ValueError):
        pass  # Missing or damaged display records must not prevent browsing.
    return usage


def load_team(store):
    """Load one canonical team record for live and read-only projections."""
    path = store.folder / "agents/index.json"
    data = read_json(path) if path.exists() else {"version":store.metadata["version"], "agents":[], "messages":[]}
    if data.get("version") != store.metadata["version"] or not isinstance(data.get("agents"), list) or not isinstance(data.get("messages"), list):
        raise ValueError("Invalid current-version agent team checkpoint")
    # Current-version index fields converge on one durable correspondence log.
    data["messages"].extend({"kind":"human_output",**item} for item in data.pop("human_outbox",[]))
    data["messages"].extend({**item,"deferred":True,"published":False} for item in data.pop("deferred_system",[]))
    for message in data["messages"]:
        if message["kind"] != "message":message.setdefault("published",True)
    master = data.setdefault("master", {"id":"master", "name":"Master", "created_at":store.metadata["created_at"],
        "status":"stopped", "phase":"stopped", "permissions":{}, "attempt":0, "model":store.settings().get("model")})
    master.setdefault("icon", data.pop("master_icon", MASTER_ICON))
    master.setdefault("runs", data.pop("master_runs", {}))
    records = [master, *data["agents"]]
    if data.get("statistics_revision") != 2:
        for item in records:
            item["runs"] = {}
            source = store if item["id"] == "master" else MemberStore(store, item["id"])
            for event in source.iter_events():
                update_run(item["runs"], event)
        data["statistics_revision"] = 2
    usage = data.setdefault("token_usage", {})
    for item in records:
        if not usage.get(item["id"]):
            try:
                usage[item["id"]] = saved_token_usage(store if item["id"] == "master" else MemberStore(store, item["id"]))
            except (OSError, ValueError):
                usage[item["id"]] = {}
    return data
