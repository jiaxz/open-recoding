"""Portable, data-only Session archives. No frontend, credentials or object pickle."""

from datetime import datetime
import os
from pathlib import Path
import re
import stat
import uuid

from multiagent.storage import AgentStore, AgentHistory, StoreLease, atomic_json, instant, protect_directory, read_json
from multiagent.models import validate_model
from multiagent.config import validate_execution_config


SESSION_HOME = ".recoding"
SESSION_HOMES = frozenset({SESSION_HOME})
FORMAT = "recoding.session"
VERSION = 5
SETTINGS = {"execution_mode", "network", "gpu", "tool_timeout", "api_timeout", "max_output", "model", "base_url", "max_subagents", "idle_sleep_seconds", "context_limits"}
NAVIGATION_FIELDS = {"format", "version", "id", "name", "created_at", "pinned", "pinned_at"}


class UnsupportedSession(ValueError):
    """Not a current-format archive; discovery skips it without conversion."""


class SessionStore(AgentStore):
    metadata_filename = "session.json"

    @staticmethod
    def validate_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[\w-][\w .-]{0,63}", name):
            raise ValueError("Session 名称限 1–64 个字母、数字、中文、空格、下划线或连字符。")
        return name

    def __init__(self, folder, *, navigation_only=False, navigation_cache=None):
        folder = Path(folder).expanduser()
        if folder.is_symlink():
            raise ValueError("Session 存档不能是软链接。")
        self.folder = folder.resolve(strict=True)
        metadata_stat = os.stat(self.folder / "session.json", follow_symlinks=False)
        if not stat.S_ISREG(metadata_stat.st_mode):
            raise ValueError("Session metadata must be a regular file: session.json")
        self.navigation_stamp = (metadata_stat.st_dev, metadata_stat.st_ino, metadata_stat.st_mtime_ns, metadata_stat.st_size)
        self.navigation_updated_at = metadata_stat.st_mtime
        cached = navigation_cache.get("metadata") if isinstance(navigation_cache, dict) else None
        valid_cache = (navigation_only and isinstance(cached, dict)
                       and navigation_cache.get("stamp") == list(self.navigation_stamp)
                       and isinstance(cached.get("format"), str)
                       and cached.get("format") == FORMAT and cached.get("version") == VERSION
                       and re.fullmatch(r"[a-f0-9]{32}", str(cached.get("id", "")))
                       and isinstance(cached.get("name"), str) and isinstance(cached.get("created_at"), str))
        self.metadata = dict(cached) if valid_cache else read_json(self.folder / "session.json", 65536)
        if not isinstance(self.metadata, dict) or self.metadata.get("format") != FORMAT or self.metadata.get("version") != VERSION:
            raise UnsupportedSession("不支持此 Session 存档版本；仅支持当前格式，不自动转换旧存档。")
        if not re.fullmatch(r"[a-f0-9]{32}", str(self.metadata.get("id", ""))):
            raise ValueError("不是有效的 Agent Session 存档。")
        if not isinstance(self.metadata.get("name"), str) or not isinstance(self.metadata.get("created_at"), str):
            raise ValueError("Session 名称或创建时间格式错误。")
        self.id, self.name = self.metadata["id"], self.metadata["name"]
        self.workspace = self.folder.parent.parent if self.folder.parent.name in SESSION_HOMES else self.folder.parent
        self.navigation_only = navigation_only
        if navigation_only:
            # Listing needs identity, location and pinning, never model settings,
            # checkpoints, image assets or journals.
            self.metadata = {key: value for key, value in self.metadata.items() if key in NAVIGATION_FIELDS}
        elif any((self.folder / name).is_symlink() for name in ("images", "runs")):
            raise ValueError("Session 图片和运行记录目录不能是软链接。")
        self._saved_images = set()

    @classmethod
    def create(cls, workspace, name, settings):
        validate_execution_config(settings)
        workspace = Path(workspace).expanduser().resolve()
        cls.validate_name(name)
        workspace.mkdir(parents=True, exist_ok=True)
        container = workspace / SESSION_HOME
        if container.is_symlink():
            raise ValueError("Session 存档目录不能是软链接。")
        protect_directory(container)
        identifier = uuid.uuid4().hex
        folder = container / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + identifier[:8])
        folder.mkdir(mode=0o700)
        protect_directory(folder)
        for child in ("images", "runs"):
            (folder / child).mkdir(mode=0o700)
        atomic_json(folder / "session.json", {"format": FORMAT, "version": VERSION, "id": identifier,
            "name": name, "created_at": instant(), "workspace_binding": "parent-directory",
            "settings": {"execution_mode":"sandbox", "max_subagents": 16, "idle_sleep_seconds": 10, **{key: value for key, value in settings.items() if key in SETTINGS}}})
        return cls(folder)

    def settings(self):
        value = self.metadata.get("settings", {})
        if not isinstance(value, dict):
            raise ValueError("Session 设置格式错误。")
        result = {key: item for key, item in value.items() if key in SETTINGS}
        limits = result.get("context_limits", {})
        if not isinstance(limits, dict) or any(not isinstance(key, str) or type(size) is not int or size < 1024 for key, size in limits.items()):
            raise ValueError("context_limits 必须是模型 ID 到至少 1024 的 token 上限的映射。")
        seconds = result.setdefault("idle_sleep_seconds", 10)
        if type(seconds) not in (int, float) or not .1 <= seconds <= 3600:
            raise ValueError("idle_sleep_seconds 必须是 0.1–3600 的有限秒数。")
        if "max_subagents" in result and (type(result["max_subagents"]) is not int or not 0 <= result["max_subagents"] <= 99):
            raise ValueError("max_subagents 必须是 0–99 的整数。")
        for key in ("network", "gpu"):
            if key in result and type(result[key]) is not bool:
                raise ValueError("Session 网络/GPU 设置格式错误。")
        for key in ("tool_timeout", "api_timeout", "max_output"):
            if key in result and (type(result[key]) not in (int, float) or not 0 < result[key] < float("inf")):
                raise ValueError("Session 超时或输出设置格式错误。")
        result.setdefault("execution_mode","sandbox")
        validate_execution_config(result)
        return result

    def update_model(self, model):
        model = validate_model(model)
        metadata = read_json(self.folder / "session.json", 65536)
        metadata.setdefault("settings", {})["model"] = model
        atomic_json(self.folder / "session.json", metadata)
        self.metadata = metadata
        return model

    def update_settings(self, values):
        from .settings import validate_settings
        values=validate_settings(values)
        if values.get('execution_mode')=='host':
            # A valid Host launch has already required both to be enabled.
            # Keep the archive consistent when explicit startup overrides
            # replace restrictions saved by an earlier sandbox launch.
            values.update(network=True,gpu=True)
        metadata=read_json(self.folder/'session.json',65536)
        metadata.setdefault('settings',{}).update(values)
        atomic_json(self.folder/'session.json',metadata)
        self.metadata=metadata

    def update_agent_limit(self, maximum):
        if type(maximum) is not int or not 0 <= maximum <= 99:
            raise ValueError("max_subagents 必须是 0–99 的整数。")
        metadata = read_json(self.folder / "session.json", 65536)
        metadata.setdefault("settings", {})["max_subagents"] = maximum
        atomic_json(self.folder / "session.json", metadata)
        self.metadata = metadata
        return maximum

    def update_idle_sleep(self, seconds):
        if type(seconds) not in (int, float) or not .1 <= seconds <= 3600:
            raise ValueError("idle_sleep_seconds 必须是 0.1–3600 的有限秒数。")
        metadata = read_json(self.folder / "session.json", 65536)
        metadata.setdefault("settings", {})["idle_sleep_seconds"] = seconds
        atomic_json(self.folder / "session.json", metadata)
        self.metadata = metadata
        return seconds

    def update_metadata(self, *, name=None, pinned=None):
        metadata = read_json(self.folder / "session.json", 65536)
        if name is not None:
            metadata["name"] = self.validate_name(name)
        if pinned is not None:
            if type(pinned) is not bool:
                raise ValueError("pinned 必须是布尔值。")
            metadata["pinned"] = pinned
            metadata["pinned_at"] = instant() if pinned else None
        atomic_json(self.folder / "session.json", metadata)
        self.metadata, self.name = metadata, metadata["name"]














    def navigation_info(self):
        return {"id": self.id, "name": self.name, "directory": str(self.workspace),
                "pinned": bool(self.metadata.get("pinned", False)), "pinned_at": self.metadata.get("pinned_at"),
                "session_directory": str(self.folder), "status": "stop",
                "created_at": self.metadata["created_at"], "updated_at": self.navigation_updated_at}

    def info(self):
        state = self.folder / "state.json"
        return {**self.navigation_info(),
                "updated_at": state.stat().st_mtime if state.exists() else self.folder.stat().st_mtime,
                "model": self.settings().get("model"), "max_subagents": self.settings().get("max_subagents", 16),
                "idle_sleep_seconds": self.settings().get("idle_sleep_seconds", 10)}


