"""One process owns one persistent Agent; SIGINT stops its complete run."""

import json
import os
import shlex
import signal
import sys
import threading
import time
import uuid
from contextlib import AbstractContextManager
from copy import deepcopy
from queue import Empty, Queue

from .client import Client
from .context_window import ContextBudget, ContextCompactor
from .errors import AgentError
from .loop import AgentLoop
from .media import AgentMedia
from .messages import format_input_events
from .model_config import api_base_url, api_key, default_model
from .multimodal import context_stats, make_user_message
from .prompts import TOOLS, compose_system
from .runtime_build import require_runtime_revision
from .storage import AgentHistory, AgentStore
from .tools import CONTEXT_TOOLS, HostTools, attach_step_note, combine_schemas, seen_messages, with_step_notes



send_lock = threading.Lock()


def send(kind, **fields):
    payload = json.dumps({"kind": kind, **fields}, ensure_ascii=False)
    with send_lock:
        print(payload, flush=True)


class DemoClient:
    """Offline scripted model: it still returns every real tool call explicitly."""

    last_usage = {}

    def __init__(self, agent_id="master", tool_names=()):
        self.agent_id = agent_id
        self.tool_names = set(tool_names)
        self.handled = set()

    def complete(self, messages):
        def call(name, fields):
            return {"role": "assistant", "tool_calls": [{"id": f"demo-{len(messages)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(fields, ensure_ascii=False)}}]}

        incoming = [m for m in messages if m.get("_agent_message_id") not in self.handled
                    and m.get("_agent_message_id") and not m.get("_system_message")]
        if incoming:
            self.handled.update(m["_agent_message_id"] for m in incoming)
            prompt = str(incoming[-1].get("content", ""))
        elif messages[-1]["role"] == "tool":
            identifier = messages[-1]["tool_call_id"]
            previous = next((c["function"]["name"] for m in reversed(messages) for c in m.get("tool_calls", [])
                             if c["id"] == identifier), "")
            result = json.loads(messages[-1]["content"])
            if previous in {"python","bash"}:
                body = result.get("stdout", result.get("error", ""))
                return call("send_user_message", {"message": "## Demo 执行完成\n\n固定响应驱动，未调用模型 API；Python 在真实沙箱中运行。\n\n```text\n" + body + "\n```"})
            return call("sleep", {"seconds": 300})
        else:
            return call("sleep", {"seconds": 300})
        if 'bash' not in self.tool_names:
            return call('send_user_message', {'message':'Demo 已收到消息。当前独立 Agent 未安装 Bash 执行工具。'})
        code = ("import time; print('Waiting for cancellation', flush=True); time.sleep(30)"
                if any(word in prompt.lower() for word in ("等待", "sleep", "cancel")) else
                "from pathlib import Path\nimport json\ndemo_runs = globals().get('demo_runs', 0) + 1\n"
                "print(json.dumps({'run': demo_runs, 'cwd': str(Path.cwd()), "
                "'files': sorted(p.name for p in Path('.').iterdir())[:12]}, ensure_ascii=False, indent=2))")
        return {**call("bash", {"command": "/usr/bin/python3 -c " + shlex.quote(code)}), "content": "我先通过 Python 检查当前 workspace。"}


class InterruptScope(AbstractContextManager):
    active = False

    def __enter__(self):
        InterruptScope.active = True
        return self

    def __exit__(self, *args):
        # Keep SIGINT active until cancellation bookkeeping has completed.
        return False


def interrupt(signum, frame):
    if InterruptScope.active:
        raise KeyboardInterrupt()


class WorkerRuntime:
    """One run's model, context and IPC; only this thread mutates the loop."""
    def __init__(self):
        self.history = self.terminal = None
        self.run_id = None

    def initialize(self, config):
        self.config = config
        require_runtime_revision(config.get("runtime_revision"))
        self.store = AgentStore(config["session_directory"], workspace=config["directory"], metadata=config["store_metadata"])
        self.replies, self.reply_lock, self.commands = {}, threading.Lock(), Queue()
        self.model = config.get("model", default_model())
        send("startup", stage="ready", label="建立 worker 通信通道")
        threading.Thread(target=self.receive, name="session-input", daemon=True).start()
        send("startup", stage="state", label="恢复模型 context 与图片状态")
        self.saved = self.store.load_state()
        send("startup", stage="environment", label="读取 Host 提供的执行环境信息")
        environment = deepcopy(config.get('execution_environment') or {
            'network':config['network'], 'gpu':None, 'workspace':config['directory'],
            'execution_mode':config.get('execution_mode','sandbox')})
        send("startup", stage="history", label="建立 System 与本次 context 日志")
        system = compose_system(network=environment.get("network",config["network"]), gpu=environment.get("gpu"),
                                fragments=config.get("prompt_fragments", ()), execution=environment)
        schemas = combine_schemas(TOOLS, CONTEXT_TOOLS, config.get("tool_schemas", []))
        tools = with_step_notes(schemas)
        self.budget = ContextBudget(self.store.folder / "context-budget.json", model=lambda:self.model, tools=tools,
                                   limits=config.get("context_limits"))
        self.history = AgentHistory(self.store, system=system, tools=tools, state=self.saved,
                                    model="offline-demo" if config["demo"] else self.model)
        self.history.record("sandbox_configuration", execution_mode=environment.get("execution_mode","sandbox"),
                            gpu=environment.get("gpu"), network=environment.get("network"), frontend="host",
                            workspace=config["directory"], session_directory=str(self.store.folder))
        send("startup", stage="client", label="初始化模型客户端")
        if config["demo"]:
            self.client = DemoClient(config["agent_id"], [item["function"]["name"] for item in tools])
            if self.saved:
                self.client.handled.update(seen_messages(self.saved["messages"]))
        else:
            key = api_key()
            if not key:
                raise AgentError("请在项目 .env 或启动环境中设置 RECODING_API_KEY。")
            self.client = Client(key, config.get("base_url", api_base_url()), self.model, config["api_timeout"],
                                 tools=tools, max_retries=3)
        self.last_input_tokens = None
        self.compactor = None
        self.viewed_images, self.drawn_images = set(), set()
        self.image_view_calls = 0
        self.media = AgentMedia(self.store, config.get("private_files", ()), config.get("hidden_paths", ()))
        executor = HostTools(self.rpc, lambda:self.agent.messages, schemas, config.get("permissions"))
        send("startup", stage="restore", label="恢复 Agent 对话与历史图片")
        self.agent = AgentLoop(self.client, executor, system=system, history=self.history, on_event=self.observe,
                               response_processor=self.prepare_response, input_provider=self.pending_inputs,
                               idle_sleep_seconds=config.get("idle_sleep_seconds", 10),
                               context_processor=self.process_context if not config["demo"] else None)
        if self.saved:
            messages = self.saved["messages"]
            messages[0] = {"role":"system", "content":system}
            self.agent.restore(messages, iteration=self.saved["iteration"], tool_count=self.saved["tool_count"], runtime_note={
                "workspace":config["directory"], "previous_location":self.saved.get("location"),
                "note":"完整对话与历史图片已恢复。请查询进程池：Session 重启不恢复内存，单个 Agent 恢复不重放旧工具，也不重建已转交进程。"})
        if not config["demo"]:
            self.compactor = ContextCompactor(self.store.folder / "context-window.json", None,
                                             model=lambda:self.model, budget=self.budget)
            if self.saved:
                self.budget.recover(self.store.folder, self.compactor.view(self.agent.messages))
                self.last_input_tokens = self.budget.measure(self.compactor.view(self.agent.messages))["last_reported_input_tokens"]
        send("ready", gpu=environment.get("gpu"), history=str(self.history.path), context=self.current_context(),
             active_model="offline-demo" if config["demo"] else self.model)

    def rpc(self, action, fields):
        identifier, response = uuid.uuid4().hex, Queue()
        with self.reply_lock:
            self.replies[identifier] = response
        try:
            send("coordination", request_id=identifier, action=action, fields=fields)
            timeout = None if action in self.config.get('execution_tools',[]) else 5 if action == "request_context_compaction" else float(fields.get("seconds", fields.get("budget_seconds", 30))) + 30
            try:
                result = response.get(timeout=timeout)
            except Empty:
                raise AgentError(f"Coordinator did not answer {action} within {timeout:g}s") from None
            if result.get("error"):
                raise ValueError(f"Coordinator action {action!r} failed: {result['error']}")
            return result
        finally:
            with self.reply_lock:
                self.replies.pop(identifier, None)

    def receive(self):
        try:
            for line in sys.stdin:
                command = json.loads(line)
                if command.get("action") == "coordination_result":
                    with self.reply_lock:
                        response = self.replies.get(command["request_id"])
                        if response is not None:
                            response.put(command["result"])
                else:
                    self.commands.put(command)
        except Exception as exc:
            self.commands.put({"action":"input_error", "error":str(exc)})
        finally:
            self.commands.put({"action":"close"})
            if InterruptScope.active:
                os.kill(os.getpid(), signal.SIGINT)

    def hydrate_inputs(self, records, messages):
        prepared = []
        for incoming in records:
            user = incoming.get("_user_input")
            if user:
                pictures = [self.store.upload_attachment(item["id"]) for item in user.get("attachments", [])]
                incoming = {**incoming, **make_user_message([*messages, *prepared], incoming["content"], pictures)}
            prepared.append(incoming)
        return prepared

    def process_context(self, messages):
        # No summary is awaited. Read the committed projection before draining
        # fresh mail, then use that same projection to account the actual call.
        self.compactor.reload()
        self.prepared = self.compactor.view(messages)
        return self.prepared

    def pending_inputs(self, messages):
        result = self.rpc("drain", {"seen":seen_messages(messages)})
        self.model = result.get("model") or self.model
        self.agent.idle_sleep_seconds = result.get("idle_sleep_seconds", self.config.get("idle_sleep_seconds", 10))
        pending = self.hydrate_inputs(result["messages"], messages)
        if not self.config["demo"]:
            self.client.model = self.model
            request = self.compactor.background_request(messages, pending)
            if request:
                try:
                    self.rpc("request_context_compaction", {**request, "step":self.agent.model_step})
                except (ValueError, OSError, AgentError) as error:
                    self.budget.defer(request)
                    send("event", run_id=self.run_id, event={"type":"context_compaction_schedule_error",
                         "step":self.agent.model_step, "error":str(error)})
            self.budget.mark_request([*self.prepared, *pending], self.model)
        return pending

    def prepare_response(self, message, messages):
        return self.media.prepare_response({**message, "_model":"offline-demo" if self.config["demo"] else self.model}, messages)

    def current_context(self):
        view = self.compactor.view(self.agent.messages) if self.compactor else self.agent.messages
        return {**context_stats(view), "last_input_tokens":self.last_input_tokens,
                "full_messages":len(self.agent.messages), "compaction_generation":self.compactor.state["generation"] if self.compactor and self.compactor.state else 0,
                "compaction_saved_tokens":self.compactor.saved_tokens(self.agent.messages, view) if self.compactor else 0}

    def observe(self, event):
        kind = event["type"]
        if kind == "inputs_received":
            inputs, _ = format_input_events(event["messages"], step=event["step"], target=self.config["agent_id"],
                                             offset=0, received_at=time.time())
            for incoming in inputs:
                send("event", run_id=self.run_id, event=incoming)
            return
        if kind in {"model_start", "model_end"}:
            model = "offline-demo" if self.config["demo"] else self.model
            event = {**event, "model":model, "active_model":model}
        if kind == "tool_end" and event.get("name") == "view_image":
            result = event.get("result", {})
            if not result.get("error") and result.get("image", {}).get("sha256"):
                self.viewed_images.add(result["image"]["sha256"])
                self.image_view_calls += 1
        if kind == "tool_end" and event.get("name") in {"send_user_message", "send_agent_message", "broadcast_agent_message"}:
            self.drawn_images.update(image["sha256"] for image in event.get("result", {}).get("images", []))
        if kind == "model_end":
            event = attach_step_note(event)
            self.drawn_images.update(image["sha256"] for image in event.get("images", []))
            if not self.config["demo"]:
                self.budget.record(event.get("usage", {}))
            value = event.get("usage", {}).get("prompt_tokens")
            self.last_input_tokens = value if type(value) is int and value >= 0 else None
        if kind in {"tool_end", "model_end", "loop_end", "loop_error"}:
            event = {**event, "images_drawn":len(self.drawn_images), "images_viewed":len(self.viewed_images),
                     "image_view_calls":self.image_view_calls}
        if kind == "model_start":
            stats = {key:event[key] for key in ("messages", "context_chars", "images", "image_bytes")}
            event = {**event, "context":{**stats, "last_input_tokens":self.last_input_tokens}}
        elif kind in {"loop_start", "model_end", "feedback", "loop_end", "loop_error", "context_compaction_end", "context_compaction_error"}:
            event = {**event, "context":self.current_context()}
        send("event", run_id=self.run_id, event=event)

    def run(self):
        command = self.commands.get()
        if command["action"] == "close":
            return
        if command["action"] != "run":
            raise AgentError(command.get("error") or "Worker only accepts run and close commands; user inputs must use the durable inbox")
        self.run_id = uuid.uuid4().hex
        try:
            with InterruptScope():
                send("active", run_id=self.run_id)
                self.rpc("cycle", {"phase":"resume" if self.saved else "startup"})
                self.agent.run()
        except KeyboardInterrupt:
            self.terminal = {"reason":"stopped"}
        except Exception as exc:
            self.terminal = {"reason":"failed", "error":str(exc)}
        finally:
            InterruptScope.active = False
        self.history.record("agent_stopped", **self.terminal)

    def close(self):
        InterruptScope.active = False
        if self.history is not None:
            try:
                self.history.record("session_end")
            finally:
                self.history.close()
        if self.terminal is not None:
            send("stopped", run_id=self.run_id, **self.terminal, context=self.current_context(), context_retained=True)


def main():
    signal.signal(signal.SIGINT, interrupt)
    runtime = WorkerRuntime()
    try:
        runtime.initialize(json.loads(sys.stdin.readline()))
        runtime.run()
    except Exception as exc:
        send("fatal", error=str(exc))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
