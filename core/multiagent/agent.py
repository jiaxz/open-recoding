"""One team's Agent worker, durable input and bounded observations."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import configuration
from .events import Progress, inventory_snapshot, preview
from .prompts import role_fragments
from .runtime_build import RUNTIME_REVISION, require_runtime_revision
from .storage import StoreLease
from .tools import agent_tools, tool_schemas as execution_tool_schemas



def record_field(name, default=None):
    """Properties are views of team-owned state, never a second stored value."""
    def set_value(self, value):
        if value is None:self.record.pop(name, None)
        else:self.record[name] = value
    return property(lambda self:self.record.get(name, default), set_value)


class RuntimeState:
    status = record_field("status", "stopped")
    run_id = record_field("run_id")
    error = record_field("error")
    sleep = record_field("sleep")


class Agent(RuntimeState):
    def __init__(self, store, config=None, *, team, agent_id, progress=None, lease=None):
        config = configuration(store.settings(), config)

        config.setdefault("hidden_paths", [str(store.folder)] if store.folder.is_relative_to(store.workspace)
                          and store.folder != store.workspace else [])
        progress = progress or Progress()
        self.team, self.agent_id = team, agent_id
        self.record = team.record(agent_id)
        optional = execution_tool_schemas([item for item in config.get("non_builtin_tools", ())
                                          if agent_id == "master" or not item.get("master_only")], config.get("permissions"))
        self.tool_schemas = [*agent_tools(agent_id), *optional]
        config["execution_tools"] = [item["function"]["name"] for item in optional]
        self.prompt_fragments = [*config.get("system_fragments", ()), *role_fragments(agent_id)]
        self.mailbox = team.mailbox(agent_id)
        self.process = self.reader = self.drain = None
        self.startup = Progress()
        self.store, self.id, self.name = store, store.id, store.name
        self.directory, self.config = store.workspace, config
        self.lease = lease or StoreLease(store.folder)
        try:
            self.event_stream = team.stream(agent_id,progress)
        except BaseException:
            self.lease.close()
            raise
        self.condition = self.event_stream.condition
        self.events = self.event_stream.events
        self.model_rates = self.event_stream.model_rates
        self.write_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.context = None
        self.tool_inventory = inventory_snapshot(store)
        for event in self.events:
            if isinstance(event.get("context"), dict):
                self.context = dict(event["context"])
        self.run_id = None
        self.active = self.cancel_requested = self.cancel_sent = False
        self.gpu = self.history = None
        self.active_model = None
        self.sleep = None
        self.closed = False
        progress.stage("recovery", "整理中断任务与页面历史")
        try:
            self._recover_interrupted_preview()
        except BaseException:
            self.lease.close()
            raise

    def _launch(self):
        """Start an isolated worker. Ready will start its loop without any prompt."""
        self.startup = Progress()
        self.startup.stage("worker", "启动 Agent worker 进程")
        require_runtime_revision(RUNTIME_REVISION)
        self.process = subprocess.Popen([sys.executable, "-u", "-m", __package__ + ".worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", start_new_session=True, close_fds=True,
            cwd=Path(__file__).resolve().parents[len(__package__.split("."))], pass_fds=(self.lease.fd,))
        self._write({"directory": str(self.directory), "session_directory": str(self.store.folder),
                     **{k:v for k,v in self.config.items() if k not in {"non_builtin_tools", "remote_nodes"}}, "runtime_revision": RUNTIME_REVISION,
                     "agent_id": self.agent_id, "store_metadata": self.store.metadata,
                     "tool_schemas": self.tool_schemas, "prompt_fragments": self.prompt_fragments,
                     "hidden_paths": self.config.get("hidden_paths", [str(self.store.folder)])})
        self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True,
                                       name=f"agent-{self.id[:8]}")
        self.reader.start()
        self.drain = threading.Thread(target=self._drain, args=(self.process,), daemon=True)
        self.drain.start()

    def _write(self, payload, *, process=None):
        with self.write_lock:
            stream = (process or self.process).stdin
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()

    @property
    def sequence(self):
        return self.event_stream.sequence

    def publish(self, event):
        with self.condition:
            if event.get("type") == "tool_inventory" and isinstance(event.get("inventory"), dict):
                self.tool_inventory = event["inventory"]
            if isinstance(event.get("active_model"), str):
                self.active_model = event["active_model"]
            if isinstance(event.get("context"), dict):
                self.context = dict(event["context"])
        self.team.emit(self.agent_id,event)

    def _recover_interrupted_preview(self):
        runs = {}
        for event in self.events:
            if event.get("run_id"):
                runs.setdefault(event["run_id"], []).append(event)
        for run_id, events in runs.items():
            if not any(event["type"] in {"loop_start", "model_start", "tool_start"} for event in events):
                continue  # Standalone maintenance has observations, not an interrupted Agent loop.
            if any(event["type"] in {"loop_end", "loop_stopped"} for event in events):
                continue
            last = events[-1]
            model = next((event for event in reversed(events) if event["type"] == "model_start"), None)
            if model and not any(event["type"] == "model_end" and event.get("step") == model.get("step") for event in events):
                self.publish({"type": "model_transport", "run_id": run_id, "step": model.get("step"),
                    "phase": "cancelled", "elapsed_seconds": max(0, last["timestamp"] - model["timestamp"]),
                    "error": "上次运行环境已结束；耗时是最后记录的时间，不包含离线期间。"})
            self.publish({"type": "loop_error", "run_id": run_id, "reason": "cancelled", "elapsed": 0,
                "timestamp": last["timestamp"], "recovered": True, "cancellation": {"location": {"phase": "session_interrupted"},
                "completed_tool_calls": [event["call_id"] for event in events if event["type"] == "tool_end"]}})
            self.publish({"type": "loop_stopped", "run_id": run_id, "status": "stopped", "reason": "session_interrupted",
                          "timestamp": last["timestamp"], "recovered": True})

    def _drain(self, process):
        # Diagnostics must not fill a pipe; do not expose worker tracebacks or
        # environment values through the browser. Structured errors use stdout.
        for line in process.stderr:
            pass

    def _read(self, process):
        reported_stop = False
        try:
            for line in process.stdout:
                packet = json.loads(line)
                kind = packet["kind"]
                if kind == "startup":
                    self.startup.stage(packet["stage"], packet["label"], packet.get("detail", ""))
                    continue
                if kind == "coordination":
                    threading.Thread(target=self._coordinate, args=(packet, process), daemon=True).start()
                    continue
                with self.condition:
                    if process is not self.process:
                        return
                    if kind == "ready":
                        self.startup.finish()
                        self.gpu, self.history = packet["gpu"], packet["history"]
                        self.publish({"type": "session_ready", "gpu": self.gpu, "history": self.history,
                                      "context": packet.get("context"),
                                      "active_model": packet.get("active_model")})
                        self._begin_run()
                    elif kind == "active":
                        self.run_id = packet["run_id"]
                        self.active = True
                        self._signal_cancel()
                    elif kind == "event":
                        event = packet["event"]
                        self.run_id = packet["run_id"]
                        self.publish({**event, "run_id": self.run_id})
                    elif kind in {"stopped", "fatal"}:
                        reported_stop = True
                        self.active = False
                        self.run_id = None
                        self.sleep = None
                        status = "failed" if kind == "fatal" or packet.get("reason") == "failed" else "stopped"
                        self.error = packet.get("error") or ("Agent runtime failed" if status == "failed" else None)
                        self.startup.finish(self.error)
                        self.publish({"type": "loop_stopped", "status": status,
                                      **{key: value for key, value in packet.items() if key != "kind"}})
        except (OSError, ValueError, KeyError) as exc:
            self.error = "Worker communication failed: " + str(exc)
        finally:
            with self.condition:
                if process is not self.process:
                    return
                expected = self.status in {"stopped", "failed"} or self.closed or self.cancel_requested
                if not expected and not self.error:
                    code = process.poll()
                    self.error = f"Worker output closed unexpectedly (exit code {code if code is not None else 'not yet available'})"
                self.active = False
                self.sleep = None
                self.startup.finish(self.error or "运行环境已关闭")
                if not reported_stop:
                    self.publish({"type": "loop_stopped", "run_id": self.run_id,
                                  "status": "failed" if self.error else "stopped",
                                  "reason": "failed" if self.error else "stopped", "error": self.error})
                self.run_id = None
                self.publish({"type": "session_closed", "error": self.error})

    def _coordinate(self, packet, process):
        try:
            result = self.team.dispatch(self.agent_id, packet["action"], packet.get("fields", {}),
                                       cancelled=lambda: self.closed or self.cancel_requested or
                                       process is not self.process or process.poll() is not None)
        except Exception as error:
            result = {"error": str(error) or type(error).__name__}
        try:
            self._write({"action": "coordination_result", "request_id": packet["request_id"], "result": result}, process=process)
        except (OSError, ValueError):
            pass  # A cancelled RPC must not resurrect a closed runner.

    def _signal_cancel(self):
        if self.active and self.cancel_requested and not self.cancel_sent:
            self.cancel_sent = True
            try:
                os.kill(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def _begin_run(self):
        with self.team.condition:
            if self.status != "starting" or self.cancel_requested or self.closed:
                self._write({"action":"close"})
                return
            self.status = "running"
            self._write({"action":"run"})
        self.publish({"type":"loop_started", "status":"running"})

    def start_worker(self):
        """Start or resume the loop. The worker receives no task or user text."""
        with self.runtime_lock:
            with self.condition:
                if self.closed:
                    raise ValueError("Agent 运行环境已关闭，请重新建立 Agent 实例。")
                if self.process is not None and self.process.poll() is None:
                    raise ValueError("Agent 已经运行，或仍在停止中。")
            require_runtime_revision(RUNTIME_REVISION)
            # A stopped worker has exited. Recreate the
            # process for Resume; Python/Bash state is never deserialized.
            self._retire_worker()
            with self.condition, self.team.condition:
                if self.status != "starting":
                    raise ValueError("Agent 启动已取消。")
                self.run_id = None
                self.active = self.cancel_requested = self.cancel_sent = False
                self.error = None
                self.sleep = None
            self._launch()
            return {"status": self.status, "run_id": self.run_id}

    def _retire_worker(self):
        process = self.process
        if process is None:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in (self.reader,self.drain):
            if thread is not None and thread.ident is not None:thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def submit(self, prompt, attachments=()):
        """Every accepted human input uses the durable mailbox."""
        message = self.team.queue_user(self.agent_id,prompt,attachments)
        return {"queued":message["id"],"run_id":self.run_id,"status":self.status}

    def request_stop(self):
        with self.condition:
            if self.status not in {"starting", "running", "sleeping", "stopping"}:
                return False
            self.cancel_requested = True
            self.status = "stopping"
            self._signal_cancel()
            self.condition.notify_all()
        self.mailbox.wake()
        return True

    def start(self):
        return self.team.start_agent(self.agent_id)

    def cancel(self):
        return self.team.finish(self.agent_id, "stopped", "User stopped the Agent")

    def snapshot(self, *, events=True):
        with self.condition:
            try:
                info = self.store.info()
            except FileNotFoundError:
                info = {"storage_directory": str(self.store.folder),
                        "created_at": self.store.metadata["created_at"],
                        "updated_at": self.events[-1]["timestamp"] if self.events else 0,
                        "pinned": bool(self.store.metadata.get("pinned", False))}
            data = {**info, "id": self.id, "name": self.name, "directory": str(self.directory),
                    "status": self.status, "run_id": self.run_id, "sequence": self.sequence,
                    "gpu": self.gpu, "history": self.history, "error": self.error,
                    "startup": self.startup.snapshot(),
                    "sleep": dict(self.sleep) if self.sleep else None,
                    "network": self.config["network"], "gpu_requested": self.config["gpu"]}
            data["context"] = dict(self.context) if self.context else None
            data["tool_inventory"] = self.tool_inventory
            data["active_model"] = self.active_model
            data["idle_sleep_seconds"] = self.config.get("idle_sleep_seconds", 10)
            data["storage_directory"] = str(self.store.folder)
            data.pop("session_directory", None)
            data["storage_missing"] = not self.store.exists()
            if events:
                data["model_rates"] = list(self.model_rates.values())
                data["events"] = list(self.events)
                data["history_truncated"] = bool(self.events and self.events[0]["sequence"] > 1)
            data["agent_id"] = self.agent_id
            return data

    def stop_worker(self):
        with self.runtime_lock:
            self.request_stop()
            try:
                if self.process is not None and self.process.poll() is None:
                    self._write({"action":"close"})
            except (OSError,ValueError):
                pass
            self._retire_worker()

    def close(self):
        if self.closed:return
        self.closed = True
        try:self.stop_worker()
        finally:self.lease.close()
