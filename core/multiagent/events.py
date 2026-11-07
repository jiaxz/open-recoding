"""Runtime state, durable observations, statistics and startup progress."""

import json
import math
import threading
import time
from collections import deque
from copy import deepcopy


def model_rate(event):
    """End-to-end output throughput, never an estimated streaming token rate."""
    compression = event.get("type") in {"context_compaction_end", "context_compaction_error"}
    if compression and not isinstance(event.get("id"), str):
        return None
    if (event.get("type") != "model_end" and not compression or not isinstance(event.get("run_id"), str)
            or not event["run_id"] or type(event.get("step")) is not int or event["step"] < 1):
        return None
    usage = event.get("usage")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    elapsed, timestamp = event.get("elapsed"), event.get("timestamp")
    if (type(tokens) is not int or tokens < 0
            or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0
            or type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0):
        return None
    identity = {"run_id": event["run_id"] + "/context/" + event["id"], "purpose": "context_compaction"} if compression else {"run_id": event["run_id"]}
    return {**identity, "step": event.get("step"), "timestamp": timestamp,
            "output_tokens": tokens, "elapsed": elapsed, "rate": tokens / elapsed,
            "model": event.get("model")}


METADATA = {"id", "name", "icon", "created_at", "updated_at", "status", "phase", "model", "permissions", "attempt", "error", "sleep"}


def update_token_usage(usage, event):
    """Numeric sidebar projection; no history or context bodies are exposed."""
    context = event.get("context")
    context = context if isinstance(context, dict) else {}
    fields = {"input_tokens": context.get("last_input_tokens"),
              "compaction_saved_tokens": context.get("compaction_saved_tokens"),
              "compaction_generation": context.get("compaction_generation")}
    if event.get("type") == "model_end":
        reported = event.get("usage")
        fields["input_tokens"] = reported.get("prompt_tokens") if isinstance(reported, dict) else None
    for key, value in fields.items():
        if type(value) is int and value >= 0:
            usage[key] = value


def update_run(runs, event):
    kind, run_id = event.get("type"), event.get("run_id")
    transport = kind in {"model_transport", "context_compaction_transport"}
    if not run_id or (kind not in {"model_start", "model_end", "tool_start", "tool_end", "loop_end", "loop_error",
                               "context_compaction_start", "context_compaction_end", "context_compaction_error"} and not transport):
        return
    if transport and event.get("phase") != "complete":
        return
    stats = runs.setdefault(run_id, {})
    cache_key = "context:" + str(event.get("id")) if kind.startswith("context_compaction") else "model:" + str(event.get("step"))
    if transport:
        # Capture only the final transport observation, paired with its own
        # result. A missing/invalid cache value must never imply a cache miss.
        stats["_cache_report"] = {"key": cache_key, "tokens": event.get("cached_tokens")}
        return
    if kind == "model_start":
        first = stats.setdefault("first_iteration", event.get("step", 1))
        stats["iterations"] = max(stats.get("iterations", 0), event.get("step", first) - first + 1)
        stats["model_calls"] = stats["iterations"] + stats.get("compaction_calls", 0)
    if kind == "context_compaction_start":
        stats["compaction_calls"] = stats.get("compaction_calls", 0) + 1
        stats["model_calls"] = stats.get("iterations", 0) + stats["compaction_calls"]
    if kind == "tool_start":
        first = stats.setdefault("first_tool", event.get("number", 1))
        stats["tool_calls"] = max(stats.get("tool_calls", 0), event.get("number", first) - first + 1)
    if kind in {"model_end", "tool_end", "context_compaction_end", "context_compaction_error"}:
        field = "tool_elapsed" if kind == "tool_end" else "model_elapsed"
        stats[field] = stats.get(field, 0) + event.get("elapsed", 0)
    if kind in {"context_compaction_end", "context_compaction_error"}:
        stats["compaction_elapsed"] = stats.get("compaction_elapsed", 0) + event.get("elapsed", 0)
    if kind in {"model_end", "context_compaction_end", "context_compaction_error"}:
        usage = event.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                stats[key] = stats.get(key, 0) + value
        prompt = usage.get("prompt_tokens")
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        report = stats.pop("_cache_report", {})
        if cached is None and report.get("key") == cache_key:
            cached = report.get("tokens")
        if type(prompt) is int and prompt >= 0:
            if type(cached) is int and 0 <= cached <= prompt:
                for key, value in (("cached_tokens", cached), ("uncached_tokens", prompt-cached), ("cache_known_input_tokens", prompt)):
                    stats[key] = stats.get(key, 0) + value
            else:
                stats["cache_unknown_input_tokens"] = stats.get("cache_unknown_input_tokens", 0) + prompt
    if kind in {"loop_end", "loop_error"}:
        stats.pop("_cache_report", None)
    for source, target in (("model_elapsed", "model_elapsed"), ("tool_elapsed", "tool_elapsed"), ("images_drawn", "drawn"), ("images_viewed", "viewed")):
        if source in event:
            stats[target] = event[source] + (stats.get("compaction_elapsed", 0) if source == "model_elapsed" else 0)



def summary(data, maximum, idle):
    agents=data.get('agents',[])
    runs=[*data.get('master',{}).get('runs',{}).values(),*[run for agent in agents for run in agent.get('runs',{}).values()]]
    totals={key:sum(run.get(key,0) for run in runs) for key in
            ('iterations','model_calls','tool_calls','drawn','viewed','model_elapsed','tool_elapsed',
             'prompt_tokens','completion_tokens','total_tokens','cached_tokens','uncached_tokens',
             'cache_known_input_tokens','cache_unknown_input_tokens')}
    return {**totals,'scope':'session_lifetime','max_subagents':maximum,'idle_sleep_seconds':idle,
            'created':len(agents),'active':sum(agent['status'] in {'starting','running','sleeping','stopping'} for agent in agents),
            'stopped':sum(agent['status']=='stopped' for agent in agents),'failed':sum(agent['status']=='failed' for agent in agents)}


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.ended = None
        self.steps = []
        self.error = None
        self.runtime = None

    def stage(self, key, label, detail=""):
        now = time.monotonic()
        with self.lock:
            if self.ended is not None:
                return
            if self.steps and self.steps[-1]["key"] == key:
                self.steps[-1]["detail"] = str(detail)
                return
            if self.steps:
                self.steps[-1]["ended"] = now
            self.steps.append({"key": key, "label": label, "detail": str(detail), "started": now})

    def finish(self, error=None):
        with self.lock:
            if self.ended is None:
                self.ended = time.monotonic()
                self.error = str(error) if error else None
                if self.steps:
                    self.steps[-1]["ended"] = self.ended

    def attach(self, runtime):
        with self.lock:
            self.runtime = runtime

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            result = {"status": "error" if self.error else "done" if self.ended is not None else "running",
                      "elapsed_seconds": (self.ended or now) - self.started,
                      "error": self.error,
                      "steps": [{"key": step["key"], "label": step["label"], "detail": step["detail"],
                                 "status": "done" if "ended" in step else "running",
                                 "elapsed_seconds": step.get("ended", now) - step["started"]}
                                for step in self.steps]}
            runtime = self.runtime
        if runtime is not None:
            result["runtime"] = runtime.snapshot()
        return result


def tool_source(arguments, name):
    source = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    language = "json"
    try:
        value = json.loads(source)
    except ValueError:
        return {"source": source, "language": language}
    key = {"python": "code", "bash": "command"}.get(name)
    # Host-injected Python tools use the same source convention without the
    # standalone Agent depending on any collaboration tool names.
    if not key and isinstance(value, dict) and isinstance(value.get("code"), str):
        return {"source": value["code"], "language": "python"}
    if key and isinstance(value, dict) and isinstance(value.get(key), str):
        return {"source": value[key], "language": name, "execution_mode": value.get("mode", "sync")}
    return {"source": json.dumps(value, ensure_ascii=False, indent=2), "language": language}


def update_runtime(state, event):
    """Derive live phase once; stopping resources cannot become active again."""
    if state.get("status") in {"stopping", "stopped", "failed", "closed"}:
        return
    kind = event["type"]
    state["phase"] = kind + (f" #{event['step']}" if event.get("step") else "")
    if kind == "tool_start" and event.get("name") == "sleep":
        arguments = event.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        seconds = arguments.get("seconds") if isinstance(arguments, dict) else None
        if type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 3600:
            state.update(status="sleeping", sleep={"seconds": seconds, "wake_on_message": True,
                                                  "deadline": time.time() + seconds})
        else:
            state["status"] = "running"
            state.pop("sleep", None)
    elif kind in {"loop_start", "loop_started", "model_start", "tool_start", "tool_end"}:
        state["status"] = "running"
        state.pop("sleep", None)
    if kind in {"model_start", "tool_start"}:
        state["location"] = {key: event[key] for key in ("type", "run_id", "step", "model", "name", "call_id") if key in event}


def preview(value):
    if isinstance(value, str):
        return value if len(value) <= 16000 else value[:16000] + "\n… 界面预览已截断；完整内容保存在 context 档案。"
    if isinstance(value, dict):
        result = {key: preview(item) for key, item in value.items() if key not in {"_image_data", "_image_attachment", "_image_attachments"}}
        if value.get("type") == "tool_start":
            if "source_preview" in value and "source_length" in value:
                return result
            source = tool_source(value.get("arguments", ""), value.get("name"))
            result.update(source_preview=source["source"][:16000], source_length=len(source["source"]),
                          source_language=source["language"], source_truncated=len(source["source"]) > 16000)
            if "execution_mode" in source:
                result["execution_mode"] = source["execution_mode"]
        return result
    if isinstance(value, list):
        return [preview(item) for item in value]
    return value


def inventory_snapshot(store):
    from .storage import read_json
    try:
        value = read_json(store.folder / "tool-inventory.json", 256*1024)
        return value if isinstance(value, dict) and value.get("version") == 1 else None
    except (OSError, ValueError):
        return None


class EventStream:
    def __init__(self, store, *, progress=None, on_event=None):
        self.store, self.on_event = store, on_event
        self.condition = threading.Condition(threading.RLock())
        if progress:
            progress.stage("history", "加载 Session 对话与工具历史", store.name)
        self.events = deque((preview(event) for event in store.load_events()), maxlen=800)
        if progress:
            progress.stage("rates", "加载历史测速数据", f"已加载 {len(self.events)} 条展示事件")
        self.model_rates = {(sample["run_id"], sample["step"]): sample for sample in store.load_model_rates()}
        self.sequence = self.events[-1]["sequence"] if self.events else 0

    def publish(self, event):
        with self.condition:
            self.sequence += 1
            value = {"timestamp": time.time(), **deepcopy(event), "sequence": self.sequence}
            try:
                self.store.append_event(value)
            except FileNotFoundError:
                pass  # External archive deletion must not prevent cleanup.
            sample = model_rate(value)
            if sample is not None:
                self.model_rates[(sample["run_id"], sample["step"])] = sample
            self.events.append(preview(value))
            self.condition.notify_all()
        if self.on_event:
            self.on_event(deepcopy(value))
        return value, sample
