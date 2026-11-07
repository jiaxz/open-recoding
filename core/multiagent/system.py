"""Durable native-Agent inboxes and runtime scheduling; no frontend imports."""

import json
import threading
import time
import uuid
from copy import deepcopy
from contextlib import nullcontext

from .agent import Agent
from .config import AGENT_ICON, validate_icon
from .events import EventStream, METADATA, preview, summary as team_summary, update_run, update_runtime, update_token_usage
from .messages import Mailbox, MessageObservation, check_image_budget, copy_images, lifecycle_instruction as cycle_instruction, load_images, message_page, message_record
from .models import validate_model
from .storage import MemberStore, StoreLease, atomic_json, protect_directory, read_json, load_team
from .tools import MANAGEMENT_NAMES, OBSERVATION_NAMES, TOOL_PARAMETERS, model_result_view, validate_request



LIVE = {"starting", "running", "sleeping", "stopping"}
ACCEPTING = {"starting", "running", "sleeping"}
RESUMABLE = {"stopped", "failed"}

BASE_TOOLS = {"recall_context"}


def limit(value):
    if type(value) is not int or not 0 <= value <= 99:
        raise ValueError("max_subagents 必须是 0–99 的整数。")
    return value


class MultiAgentSystem:
    def __init__(self, store, config, runner_factory=None, *, publish=None,
                 progress=None, master_lease=None, resource_request=None, release_owner=None):
        self.store, self.config = store, config
        self.factory = runner_factory or self._create_member
        self.event_stream = EventStream(store, progress=progress, on_event=publish)
        self.streams = {"master":self.event_stream}
        self._master_options = {"progress": progress, "lease": master_lease}
        self.config.setdefault("package_directory", str(store.workspace / ".recoding/runtime/python-packages"))
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.inboxes = {}
        self.folder = store.folder / "agents"
        protect_directory(self.folder)
        self.path = self.folder / "index.json"
        self.data = load_team(store)
        self.observation = MessageObservation(self.data)
        self.maximum = limit(config.get("max_subagents", 16))
        self.agents, self.cleaners, self.closing = {}, [], False
        self.launches = {}
        self.resource_request = resource_request
        self.release_owner = release_owner or (lambda identifier: None)
        self.generation = 0
        for item in [self.record("master"), *self.data["agents"]]:
            if item["status"] in LIVE:
                lease = StoreLease(self.folder / item["id"]) if item["id"] != "master" else None
                try:
                    status = item.pop("stop_status", "stopped")
                    item.update(status=status, phase=status, updated_at=time.time())
                    item.pop("sleep", None)
                finally:
                    if lease:lease.close()
        self.agents["master"] = self.factory(store, config, self, "master")
        notices = []
        for item in self.data["agents"]:
            if item["status"] == "failed":
                with self.condition:
                    notice = self._failure_notice(item, releasing=False)
                if notice:
                    notices.append(notice)
        self._save()
        for notice in notices:
            self._publish_message(notice)
        self._flush_publications()

    @property
    def master(self):
        return self.agents["master"]

    def publish(self, event):
        self.event_stream.publish(event)

    def start(self):
        """Resume only Master; peers remain under its explicit management."""
        self.start_agent("master")
        return {"status":self.master.status,"run_id":self.master.run_id}

    def stop(self, reason="User stopped the Session"):
        changed = self.stop_children(reason)
        return self.finish("master", "stopped", reason) or changed

    def close(self):
        self.pause()
        self.master.close()

    def tool_actor(self,identifier):
        with self.condition:
            try:item=self.record(identifier)
            except KeyError:return None
            return {'id':identifier,'status':item['status'],'permissions':deepcopy(item.get('permissions',{})),
                    'epoch':item.get('attempt',0)}

    def tool_notice(self,target,content,configuration):
        with self.condition:
            for existing in self.data['messages']:
                if configuration.get('execution_id') and existing.get('configuration',{}).get('event')==configuration['event'] and existing.get('configuration',{}).get('execution_id')==configuration['execution_id'] and existing.get('target')==target:
                    return existing['id']
            notice=self._enqueue('system',target,content,'system_message',configuration=configuration);self._save()
        self._publish_message(notice)
        return notice['id']

    @property
    def idle_sleep_seconds(self):
        return self.config.get("idle_sleep_seconds", 10)

    def mailbox(self, identifier):
        with self.condition:
            self.record(identifier)
            if identifier not in self.inboxes:
                self.inboxes[identifier] = Mailbox(target=identifier,records=lambda:self.data["messages"],lock=self.lock)
            return self.inboxes[identifier]

    def dispatch(self, source, action, fields, *, cancelled=None):
        """Keep coordination inside the team; delegate only external resources."""
        if not isinstance(fields, dict):
            raise ValueError("Invalid request fields")
        if cancelled and cancelled():
            raise ValueError("Agent request cancelled")
        if action in TOOL_PARAMETERS or action in {"drain", "cycle"}:
            result = self.request(source, action, fields, cancelled=cancelled)
            view = model_result_view(action, result)
            return {**result, "_model_result": view} if view is not None else result
        if self.resource_request:
            return self.resource_request(source, action, fields, cancelled=cancelled)
        if action == "request_context_compaction":
            return {"status": "unavailable"}
        raise ValueError("Unknown resource request: " + action)

    def _create_member(self, store, config, owner, identifier):
        options = self._master_options if identifier == "master" else {}
        return Agent(store, config, team=self, agent_id=identifier, **options)

    def stream(self, identifier, progress=None):
        if identifier not in self.streams:
            stream = EventStream(self._store(identifier),progress=progress)
            with self.condition:self.streams.setdefault(identifier,stream)
        return self.streams[identifier]

    def emit(self, identifier, event):
        value,sample = self.stream(identifier).publish(event)
        if identifier != "master":self.publish({"type":"agent_event","agent_id":identifier,"event":preview(value)})
        self.observe(identifier,value,sample)
        return value

    def _save(self):
        with self.condition:
            try:
                atomic_json(self.path, self.data)
            except FileNotFoundError:
                pass  # External deletion must not prevent runtime cleanup.
            self.condition.notify_all()  # Metadata observers only, never inbox listeners.

    def _agent(self, identifier):
        for item in self.data["agents"]:
            if item["id"] == identifier:
                return item
        raise KeyError("Unknown Agent")

    def record(self, identifier):
        return self.data["master"] if identifier == "master" else self._agent(identifier)

    def _valid_agent(self, identifier):
        self.record(identifier)
        return identifier

    def accepting(self, identifier):
        """Peer mail requires a live recipient; human input has its own gate."""
        with self.condition:
            self._valid_agent(identifier)
            if self.closing:
                return False
            runtime = self.agents.get(identifier)
            return (self.record(identifier)["status"] in ACCEPTING
                    and not getattr(runtime, "cancel_requested", False)
                    and not getattr(runtime, "closed", False))

    def assert_accepting(self, identifier):
        if not self.accepting(identifier):
            raise ValueError("Agent is not accepting peer messages; resume it before sending Agent mail")

    def _recipients(self, exclude=None):
        return [identifier for identifier in ["master", *[item["id"] for item in self.data["agents"]]]
                if identifier != exclude and self.accepting(identifier)]

    def listing(self):
        """Web descriptors and tool metadata never contain tasks or model history."""
        with self.condition:
            return [self._metadata(item) for item in self.data["agents"]]

    def _metadata(self, item):
        metadata = {key:deepcopy(value) for key,value in item.items() if key in METADATA}
        metadata.setdefault("icon", AGENT_ICON)
        metadata["permissions"] = {key:deepcopy(value) for key,value in item.get("permissions",{}).items() if key in {"network","gpu","tools"}}
        if "sleep" in metadata:
            metadata["sleep"] = {key:metadata["sleep"][key] for key in ("seconds","deadline") if key in metadata["sleep"]}
        return {**metadata,"resumable":item["status"] in RESUMABLE}

    @property
    def master_icon(self):
        with self.condition:
            return self.record("master")["icon"]

    def public_agents(self):
        with self.condition:
            agents = [self._metadata(item) for item in [self.record("master"), *self.data["agents"]]]
            for item in agents:
                if "error" in item:
                    # Raw API/tool exceptions can echo request or code content.
                    # Full reasons stay in Web descriptors and host notices.
                    item["error_available"] = bool(item.pop("error"))
            return agents

    def changed(self):
        self.publish({"type": "agents_updated", "agents": self.listing(), "master_icon": self.master_icon, "team": self.summary(), "agent_token_usage": self.token_usage()})

    def token_usage(self):
        with self.condition:
            return deepcopy(self.data.get("token_usage", {}))

    def activity(self, action, identifier):
        run_id = getattr(self.master, "run_id", None)
        if run_id:
            with self.condition:
                item = self._agent(identifier)
                event = {"type": "agent_activity", "run_id": run_id, "action": action,
                         "step": self.record("master")["runs"].get(run_id, {}).get("iterations", 0),
                         "agent_id": identifier, "name": item["name"], "status": item["status"], "attempt": item.get("attempt", 0)}
            self.publish(event)

    def summary(self):
        with self.condition:
            return team_summary(self.data,self.maximum,self.idle_sleep_seconds)

    def rates(self):
        with self.condition:
            return [deepcopy(sample) for agent in self.data["agents"] for sample in agent.get("rates", [])]

    def set_model(self, model):
        model = validate_model(model)
        with self.condition:
            self.config["model"] = model
            for item in [self.record("master"), *self.data["agents"]]:
                item["model"] = model
            runners = list(self.agents.values())
            self._save()
        for runner in runners:
            runner.config["model"] = model
        self.changed()

    def _enqueue(self, source, target, content, kind="message", **fields):
        attempt = self.record(source).get("attempt",0) if source not in {"user","system"} else 0
        message = message_record(source,target,content,kind,attempt,**fields)
        if kind in {"user_input","message","human_output"}:self.observation.append(message)
        if kind != "message":message.setdefault("published",False)
        if source == "system" and not self.accepting(target):message["deferred"] = True
        self.data["messages"].append(message)
        if target != "user":self.mailbox(target).wake()
        return message

    def queue_user(self, identifier, content, attachments=(), *, broadcast_id=None, broadcast_scope=None, active_only=False):
        """Host-only human provenance; persistence precedes publication and reading."""
        runtime = self.agents.get(identifier)
        with runtime.condition if runtime else nullcontext():
            with self.condition:
                self._valid_agent(identifier)
                if self.closing:raise ValueError("Session is closing; user input was not queued")
                if active_only:self.assert_accepting(identifier)
                store = self._store(identifier)
                pictures = [store.uploads()[value] for value in attachments]
                check_image_budget(store,identifier,self.data["messages"],pictures)
                group = {"broadcast_id":broadcast_id,"broadcast_scope":broadcast_scope} if broadcast_id else {}
                message = self._enqueue("user",identifier,content,"user_input",attachments=pictures,**group)
                self._save()
            self._publish_message(message)
        return deepcopy(message)

    def conversation(self, *, before=None, after=None, limit=100):
        with self.condition:
            return message_page(self.data, before=before, after=after, limit=limit)

    def _store(self, identifier):
        return self.store if identifier == "master" else MemberStore(self.store, identifier)

    def _publish_message(self, message, *, recover=False):
        if message.get("published") or message.get("deferred") or message["kind"] == "message":return
        identifier = message["source"] if message["target"] == "user" else message["target"]
        with self.condition:
            self.condition.wait_for(lambda:identifier in self.agents or self.record(identifier)["status"] not in ACCEPTING,timeout=1)
            runtime = self.agents.get(identifier)
            if runtime is None and self.record(identifier)["status"] in LIVE:return
        event = {"type":{"user_input":"user_input_queued","human_output":"human_message","system_message":"system_message_queued"}[message["kind"]],
                 "message_id":message["id"],**{key:deepcopy(message[key]) for key in
                 ("source","target","content","timestamp","attachments","images","configuration") if key in message},
                 "run_id":runtime.run_id if runtime else None}
        lease = nullcontext()
        if runtime is None:
            try:lease = StoreLease(self._store(identifier).folder)
            except ValueError:return  # A concurrent constructor flushes after registration.
        with lease:
            stream = self.stream(identifier)
            with stream.condition:
                if message.get("published"):return
                existing = next((item for item in stream.store.iter_events() if item.get("message_id") == message["id"] and item.get("type") == event["type"]),None) if recover else None
                if existing is None:
                    runtime.publish(event) if runtime else self.emit(identifier,event)
                elif identifier != "master" and not any(item.get("type") == "agent_event" and item.get("event",{}).get("message_id") == message["id"] for item in self.store.iter_events()):
                    self.publish({"type":"agent_event","agent_id":identifier,"event":preview(existing)})
                with self.condition:
                    message["published"] = True
                    self._save()

    def _flush_publications(self, identifier=None):
        for message in self.data["messages"]:
            owner = message["source"] if message["target"] == "user" else message["target"]
            if message.get("published") is False and (identifier is None or owner == identifier):
                self._publish_message(message,recover=True)

    def _flush_system(self, target):
        ready = [item for item in self.data["messages"] if item["target"] == target and item.get("deferred")]
        for item in ready:item.pop("deferred",None)
        if ready:self.mailbox(target).wake()
        return ready

    def set_limit(self, maximum):
        maximum = limit(maximum)
        with self.condition:
            previous = self.maximum
            active = sum(agent["status"] in LIVE for agent in self.data["agents"])
            message = None
            if previous != maximum:
                self.store.update_agent_limit(maximum)
                self.maximum = self.config["max_subagents"] = maximum
                content = (f"Session 并发 sub-agent 上限已从 {previous} 调整为 {maximum}（不含 Master）；当前占用 {active} 个名额。"
                           "降低上限不会停止已有 Agent（sleep 也占名额）；占用数低于新上限时才允许创建或恢复。请调整并行计划。")
                message = self._enqueue("system", "master", content, "system_message", configuration={
                    "previous_max_subagents": previous, "max_subagents": maximum, "active": active})
                self._save()
        if message:
            self._publish_message(message)
            self.changed()
        return {"max_subagents": maximum, "previous_max_subagents": previous, "active": active,
                "queued": message["id"] if message and not message.get("deferred") else None,
                "deferred": message["id"] if message and message.get("deferred") else None,
                "scope": "session", "message": deepcopy(message)}

    def configuration_notice(self, previous, values):
        changes={key:value for key,value in values.items() if previous.get(key)!=value}
        if not changes:return
        with self.condition:
            message=self._enqueue('system','master','Resume 已应用当前窗口暂存的 Session 设置：'+json.dumps(changes,ensure_ascii=False),
                                  'system_message',configuration=changes)
            self._save()
        self._publish_message(message)
        self.changed()

    def set_idle_sleep(self, seconds):
        if type(seconds) not in (int, float) or not .1 <= seconds <= 3600:
            raise ValueError("idle_sleep_seconds 必须是 0.1–3600 的有限秒数。")
        with self.condition:
            previous, message = self.idle_sleep_seconds, None
            if previous != seconds:
                self.store.update_idle_sleep(seconds)
                self.config["idle_sleep_seconds"] = seconds
                message = self._enqueue("system", "master", f"Session 默认空闲休眠已从 {previous} 秒调整为 {seconds} 秒；下一次 Model call 前生效，Agent 仍可自行选择工作或 sleep。",
                                        "system_message", configuration={"idle_sleep_seconds": seconds, "previous_idle_sleep_seconds": previous})
                self._save()
        if message:
            self._publish_message(message)
            self.changed()
        return {"idle_sleep_seconds": seconds, "previous_idle_sleep_seconds": previous,
                "queued": message["id"] if message and not message.get("deferred") else None,
                "deferred": message["id"] if message and message.get("deferred") else None, "message": deepcopy(message)}

    def pending(self, target, seen):
        return self.mailbox(target).pending(seen)

    def request(self, source, action, fields, *, cancelled=None):
        """Validate one request, then enter its explicit coordination operation."""
        if not isinstance(fields, dict):
            raise ValueError("Invalid coordination fields")
        with self.condition:
            self._valid_agent(source)
        if cancelled and cancelled():
            raise ValueError("Agent request cancelled")
        if action == "drain":
            if set(fields) - {"seen"}:
                raise ValueError("Invalid inbox request")
            with self.condition:
                self.assert_accepting(source)
                return {"messages": self.pending(source, fields.get("seen", [])), "model": self.config.get("model"),
                        "idle_sleep_seconds": self.idle_sleep_seconds}
        if action == "cycle":
            return self._cycle(source, fields)
        validate_request(source, action, fields)
        if action in OBSERVATION_NAMES:
            with self.condition:
                self.assert_accepting(source)
                people = {"master", "user", *(agent["id"] for agent in self.data["agents"])}
                if action == "read_global_messages":
                    return self.observation.read(fields, people)
                return self.observation.listen(fields, people, condition=self.condition,
                    accepting=lambda: self.accepting(source), pending=lambda seen: self.pending(source, seen), cancelled=cancelled)
        if action == "list_agents":
            return {"agents": self.public_agents(), "max_subagents": self.maximum}
        if action == "create_agent":
            return self.create_agent(fields)
        if action == "set_agent_icon":
            return self._set_icon(fields)
        if action == "rename_agent":
            return self._rename(fields)
        if action == "resume_agent":
            return self.resume_agent(fields["agent_id"])
        if action == "stop_agent":
            self._agent(fields["agent_id"])
            if not fields["reason"].strip() or len(fields["reason"]) > 8000:
                raise ValueError("Stop reason must contain 1–8000 characters")
            self.finish(fields["agent_id"], "stopped", fields["reason"])
            return {"agent_id": fields["agent_id"], "status": self._agent(fields["agent_id"])["status"]}
        if action in {"send_agent_message", "broadcast_agent_message", "send_user_message"}:
            return self._send(source, action, fields)
        return self.sleep(source, fields["seconds"], fields.get("seen", []), cancelled=cancelled)

    def _cycle(self, source, fields):
        if set(fields) != {"phase"}:
            raise ValueError("Invalid lifecycle notification")
        instruction = cycle_instruction(fields["phase"])
        with self.condition:
            self.assert_accepting(source)
            flushed = self._flush_system(source)
            store = self._store(source)
            path = store.folder / "state.json"
            saved = read_json(path) if path.exists() else {}
            descriptor = self.record(source)
            identity = f"身份：{descriptor['name']}，agent_id={source}，role={'MASTER' if source == 'master' else 'SUB'}。"
            previous = f"先前状态/停止原因：{descriptor.get('stop_reason', saved.get('run_state', '无'))}；上次位置：{json.dumps(saved.get('location', {}), ensure_ascii=False)}。" if fields["phase"] == "resume" else ""
            if fields['phase']=='resume':instruction+=' 请查询进程池：Agent 停止后持久资源可能已交接给 Master，不能假定旧 owner 或进程状态；不重放代码。Session 整体重启不恢复工具内存。'
            message = self._enqueue("system", source, identity + previous + instruction, "system_message",
                                    configuration={"lifecycle": fields["phase"]})
            self._save()
        for notice in flushed:
            self._publish_message(notice)
        self._publish_message(message)
        return {"queued": message["id"]}

    def _set_icon(self, fields):
        identifier, icon = fields["agent_id"], validate_icon(fields["icon"])
        with self.condition:
            self._valid_agent(identifier)
            if identifier == "master":
                self.record("master")["icon"] = icon
            else:
                item, child = self._agent(identifier), self._store(identifier)
                metadata = read_json(child.folder / "session.json")
                metadata["icon"] = icon
                atomic_json(child.folder / "session.json", metadata)
                item.update(icon=icon, updated_at=time.time())
                runtime = self.agents.get(identifier)
                if runtime:
                    runtime.store.metadata = metadata
            self._save()
        self.changed()
        return {"agent_id": identifier, "icon": icon}

    def _rename(self, fields):
        identifier, name = fields["agent_id"], fields["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError("Agent name must contain 1–64 characters")
        with self.condition:
            item = self._agent(identifier)
            child = self._store(identifier)
            metadata = read_json(child.folder / "session.json")
            metadata["name"] = name.strip()
            atomic_json(child.folder / "session.json", metadata)
            item.update(name=name.strip(), updated_at=time.time())
            runtime = self.agents.get(identifier)
            if runtime:
                runtime.store.metadata = metadata
                runtime.store.name = name.strip()
                runtime.name = name.strip()
            self._save()
        self.changed()
        return {"agent_id": identifier, "name": name.strip()}

    def _send(self, source, action, fields):
        content = fields["message"]
        paths = fields.get("images", [])
        if not isinstance(content, str) or (not content.strip() and not paths) or len(content) > (60000 if action == "send_user_message" else 8000):
            raise ValueError("Messages need text or images within the channel size limit")
        with self.condition:
            self.assert_accepting(source)
            if action == "send_user_message":
                targets = []
            elif action == "broadcast_agent_message":
                targets = self._recipients(exclude=source)
            else:
                if ("agent_id" in fields) == ("agent_ids" in fields):
                    raise ValueError("Provide agent_id OR agent_ids, never both")
                targets = fields.get("agent_ids", [fields.get("agent_id")])
                if not isinstance(targets, list) or not 1 <= len(targets) <= 100 or any(not isinstance(target, str) for target in targets) or len(set(targets)) != len(targets):
                    raise ValueError("agent_ids must contain 1–100 distinct Agent IDs")
                for target in targets:
                    self.assert_accepting(target)
                    if target == source:
                        raise ValueError("Send to another Agent, not yourself")
            pictures = load_images(self._store(source), paths, self.config)
            if action == "send_user_message":
                from .media import AgentMedia
                sender_images = AgentMedia(self._store(source), self.config.get("private_files", ()),
                                           self.config.get("hidden_paths", ())).human_message_images(content, paths)
            else:
                sender_images = copy_images(self._store(source), pictures)
            # Prepare every recipient's immutable assets before enqueueing
            # anything; all actual recipients were validated above.
            prepared=[(target,copy_images(self._store(target),pictures)) for target in targets]
            batch_id=uuid.uuid4().hex
            recipients=[]
            if action == "send_user_message":
                message = self._enqueue(source,"user",content,"human_output",images=sender_images)
            else:
                for target, images in prepared:
                    delivered=self._enqueue(source,target,content,images=images,broadcast_id=batch_id)
                    recipients.append({"agent_id":target,"message_id":delivered['id'],"images":deepcopy(images)})
            if recipients or action=='send_user_message':self._save()
            if action!='send_user_message':
                return {'broadcast_id':batch_id,'recipients':recipients,'images':sender_images,
                        'queued':[item['message_id'] for item in recipients]}
        self._publish_message(message)
        return {'message_id':message['id'],'target':'user','images':deepcopy(sender_images)}

    def sleep(self, source, seconds, seen, *, cancelled=None):
        return self.mailbox(source).sleep(seconds,seen,cancelled=cancelled,accepting=lambda:self.accepting(source))

    def _settings(self, permissions):
        return {**self.config, "permissions": deepcopy(permissions),
                "network": self.config["network"] and permissions.get("network", True),
                "gpu": self.config["gpu"] and permissions.get("gpu", True)}

    def _launch(self, item, generation, attempt):
        identifier = item["id"]
        with self.condition:
            launched = self.launches[(identifier, attempt)]
        try:
            with self.condition:
                current = self.record(identifier)
                stale = current["attempt"] != attempt
                aborted = ((identifier != "master" and not self.accepting("master")) or generation != self.generation
                           or current["status"] not in ACCEPTING or stale)
            if aborted:
                if not stale:
                    self.finish(identifier, "stopped", "Agent launch cancelled by Session Stop")
                return
            runner = self.agents.get(identifier) or self.factory(self._store(identifier), self._settings(item["permissions"]), self, identifier)
            with self.condition:
                current = self.record(identifier)
                aborted = ((identifier != "master" and not self.accepting("master")) or generation != self.generation
                           or current["attempt"] != attempt or current["status"] not in ACCEPTING)
                if not aborted:
                    self.agents[identifier] = runner
                    self.condition.notify_all()
            if aborted:
                runner.stop_worker() if identifier == "master" else runner.close()
                with self.condition:
                    current = self.record(identifier)
                    cancel = current["attempt"] == attempt and current["status"] in ACCEPTING
                if cancel:self.finish(identifier,"stopped","Agent launch cancelled by Session Stop")
            else:
                runner.start_worker()
                self._flush_publications(identifier)
        except Exception as error:
            with self.condition:
                current = self.record(identifier)["attempt"] == attempt
            if current:self.finish(identifier,"failed",str(error))
            return error
        finally:
            with self.condition:
                launched.set()
                self.launches.pop((identifier, attempt), None)
                self.condition.notify_all()

    def create_agent(self, fields):
        if not isinstance(fields, dict) or set(fields) - {"name", "icon", "permissions"}:
            raise ValueError("create_agent accepts only name, icon and optional permissions")
        name, permissions = fields.get("name"), fields.get("permissions", {})
        icon = validate_icon(fields.get("icon", AGENT_ICON))
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError("Give the Agent a descriptive name (1–64 characters)")
        if not isinstance(permissions, dict) or set(permissions) - {"network", "gpu", "tools"}:
            raise ValueError("Invalid delegated permissions")
        for key in ("network", "gpu"):
            if key in permissions and (type(permissions[key]) is not bool or permissions[key] and not self.config[key]):
                raise ValueError("Delegation cannot increase Session permissions")
        allowed = permissions.get("tools")
        if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(name, str) or name not in (BASE_TOOLS | {item["schema"]["function"]["name"] for item in self.config.get("non_builtin_tools", [])}) for name in allowed)):
            raise ValueError("Invalid tool allowlist")
        with self.condition:
            self.assert_accepting("master")
            generation = self.generation
            if sum(agent["status"] in LIVE for agent in self.data["agents"]) >= self.maximum:
                raise ValueError(f"At most {self.maximum} sub-agents may run concurrently")
            identifier = uuid.uuid4().hex
            config = self._settings(permissions)
            model = validate_model(config["model"])
            MemberStore.create_agent(self.store, identifier, name.strip(), {key: value for key, value in config.items() if key in {
                "network", "gpu", "model", "base_url", "tool_timeout", "api_timeout", "max_output", "idle_sleep_seconds", "context_limits"}}, icon=icon)
            item = {"id": identifier, "name": name.strip(), "icon": icon, "created_at": time.time(), "updated_at": time.time(),
                    "status": "starting", "phase": "starting", "model": model, "permissions": deepcopy(permissions),
                    "runs": {}, "rates": [], "attempt": 0}
            self.data["agents"].append(item)
            self.launches[(identifier,0)] = threading.Event()
            self._save()
        self.changed()
        self.activity("created", identifier)
        self._launch(item, generation, 0)
        return {"agent_id": identifier, "name": item["name"], "icon": icon, "status": item["status"], "initial_history": "blank"}

    def observe_tool_inventory(self, identifier, report):
        # Host setup is shared; only Master issues a user-facing request, once
        # per meaningful severe change, rather than one warning per worker/tick.
        if identifier != "master" or report.get("status") != "ready":
            return
        signature = {"level": report.get("level"), "missing": report.get("missing_capabilities", [])}
        notice = None
        with self.condition:
            if self.data.get("tool_inventory_notice") == signature:
                return
            self.data["tool_inventory_notice"] = signature
            if report.get("level") == "severe":
                packages = ", ".join(report.get("host_packages", []))
                notice = self._enqueue("system", "master",
                    f"沙箱工具完整度检查：{report.get('score')}/100，常用扩展工具缺失较多。"
                    f"缺失能力：{'、'.join(signature['missing'])}。建议 Host 为沙箱使用的 /usr/bin/python3 配置这些基础包：{packages}。"
                    "请向用户简要说明并请求其在 Host 环境安装；不要声称已经安装或自行越过沙箱修改 Host。"
                    "这是可发现性检查，并非逐包导入验证或任务成功率。Session 自行安装仍可用，按需选择；"
                    "检查结果相同无需反复催促用户，安装后可刷新检查，已有 Python 缓存按需要创建新的 Python Pool 进程。",
                    "system_message", configuration={"event": "host_tools_incomplete", "score": report.get("score"), "packages": report.get("host_packages", [])})
            self._save()
        if notice:
            self._publish_message(notice)

    def observe(self, identifier, event, sample=None):
        if event.get("recovered"):
            return
        kind = event.get("type")
        if kind == "tool_inventory":
            self.observe_tool_inventory(identifier, event.get("inventory", {}))
            return
        observed = {"loop_start", "loop_started", "loop_stopped", "model_start", "model_end", "tool_start", "tool_end", "loop_end", "loop_error",
                    "context_compaction_start", "context_compaction_end", "context_compaction_error"}
        transport = kind in {"model_transport", "context_compaction_transport"} and event.get("phase") == "complete"
        if kind not in observed and not transport:
            return
        stop = None
        with self.condition:
            item = self.record(identifier)
            update_run(item.setdefault("runs", {}), event)
            update_token_usage(self.data.setdefault("token_usage", {}).setdefault(identifier, {}), event)
            if kind != "loop_stopped":
                update_runtime(item, event)
            item["updated_at"] = time.time()
            if sample and identifier != "master":
                item["rates"].append({**sample,"agent_id":identifier,"agent_name":item["name"]})
            if kind == "loop_stopped" and item["status"] in LIVE:
                stop = ("failed" if event.get("status") == "failed" else "stopped", event.get("error") or event.get("reason") or "Agent stopped")
            self._save()
        self.changed()
        if stop:
            self.finish(identifier, *stop)

    def _failure_notice(self, item, *, releasing):
        if item.get("failure_notice_attempt") == item.get("attempt", 0):
            return None
        reason = item.get("error") or "Agent process failed"
        content = (f"Agent {item['name']} ({item['id']}) failed：{reason}。执行位置："
                   + json.dumps(item.get("location", {}), ensure_ascii=False)
                   + ("。资源正在回收；检查状态后再恢复。" if releasing else "。运行环境已停止，可显式恢复。"))
        notice = self._enqueue("system", "master", content, "system_message", configuration={
            "event": "agent_failed", "agent_id": item["id"], "name": item["name"], "attempt": item.get("attempt", 0),
            "error": reason, "location": deepcopy(item.get("location", {})), "resources_releasing": releasing})
        item["failure_notice_attempt"] = item.get("attempt", 0)
        return notice

    def finish(self, identifier, status, reason):
        if status not in RESUMABLE:
            raise ValueError("Agent may only be stopped or failed")
        with self.condition:
            item = self.record(identifier)
            if item["status"] in RESUMABLE | {"stopping"}:
                return False
            item.update(status="stopping",phase="stopping",stop_status=status,stop_reason=str(reason)[:32000])
            attempt = item["attempt"]
            item.pop("sleep",None)
            notice = None
            if status == "failed":
                item["error"] = str(reason)[:32000]
                if identifier != "master":notice = self._failure_notice(item,releasing=True)
            runner = self.agents.get(identifier)
            self._save()
            self.mailbox(identifier).wake()
        if runner:runner.request_stop()
        if notice:self._publish_message(notice)
        self.changed()
        if identifier != "master":self.activity("stopping",identifier)
        def release():
            with self.condition:launching = self.launches.get((identifier,attempt))
            if launching:launching.wait()
            with self.condition:
                if self.record(identifier)["attempt"] != attempt:return
                runtime = self.agents.get(identifier)
            self.release_owner(identifier)
            if runtime:
                runtime.stop_worker() if identifier == "master" else runtime.close()
            with self.condition:
                item = self.record(identifier)
                if item["attempt"] != attempt:return
                item.update(status=status,phase=status,updated_at=time.time(),stopped_at=time.time())
                item.pop("stop_status",None)
                if identifier != "master":self.agents.pop(identifier,None)
                self._save()
                self.mailbox(identifier).wake()
            self._flush_publications(identifier)
            self.changed()
            if identifier != "master":self.activity(status,identifier)
        thread = threading.Thread(target=release,daemon=True,name=f"agent-release-{identifier[:8]}")
        self.cleaners.append(thread)
        thread.start()
        return True

    def stop_children(self, reason="Stopped with Master"):
        with self.condition:
            self.generation += 1
            identifiers = [item["id"] for item in self.data["agents"] if item["status"] in LIVE]
        changed = False
        for identifier in identifiers:changed = self.finish(identifier,"stopped",reason) or changed
        return changed

    def pause(self, master_active=False):
        with self.condition:
            self.closing = True
            for inbox in self.inboxes.values():inbox.wake()
        self.stop("Session runtime closed; explicit resume is required")
        for thread in self.cleaners:thread.join()

    def start_agent(self, identifier):
        with self.condition:
            item = self.record(identifier)
            if identifier != "master":
                self.assert_accepting("master")
                if sum(a["status"] in LIVE for a in self.data["agents"]) >= self.maximum:
                    raise ValueError(f"At most {self.maximum} sub-agents may run concurrently")
            if item["status"] not in RESUMABLE:
                raise ValueError("Only stopped or failed Agents can resume")
            item.update(status="starting",phase="resuming",
                        attempt=item.get("attempt",0)+1,updated_at=time.time(),model=self.config["model"])
            attempt,generation = item["attempt"],self.generation
            self.launches[(identifier,attempt)] = threading.Event()
            item.pop("error",None)
            self._save()
        self.changed()
        if identifier != "master":self.activity("resuming",identifier)
        error = self._launch(item,generation,attempt)
        if error and identifier == "master":raise error
        return {"agent_id":identifier,"name":item["name"],"status":item["status"],"attempt":attempt,"history":"preserved"}

    def resume_agent(self, identifier):
        self._agent(identifier)  # Model management cannot target Master.
        return self.start_agent(identifier)
