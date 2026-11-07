"""A workspace-bound Session aggregate, independent of HTTP and presentation."""

from multiagent.storage import atomic_json
from multiagent.events import preview, inventory_snapshot
from multiagent.storage import MemberStore
from multiagent.messages import check_image_budget, copy_images
from multiagent.events import Progress
from multiagent.runtime_build import RUNTIME_REVISION, require_runtime_revision
from multiagent.config import configuration, validate_execution_config
from node.packages import package_directory
from multiagent import MultiAgentSystem
from multiagent.system import limit
import uuid
from .compaction import SessionCompaction
from .store import SessionStore, StoreLease
from .settings import validate_settings
from .metrics import speed_snapshot
from tools import definitions, prompt_fragments, execution_sources
from tools.registry import ToolRegistry
from tools.python import PythonTools
from tools.bash.service import BashTools
from tools.file_transfer.service import FileTransfers
from node.service import ExecutionService
from session.executions import visible_executions


class Session:
    """Own shared settings, the multi-agent system, and stop/maintenance policy.

    Web manages many of these objects. No server or UI is started here. The
    team observation stream also carries tagged sub-agent observations, so a
    shell needs one subscription for the whole Session.
    """
    def __init__(self, store, config=None, *, progress=None, autostart=True, initial_settings=None, host_lease=None):
        self.host_lease=host_lease
        self.execution = None
        initial_settings=validate_settings({**({"execution_mode":config["execution_mode"]} if config and "execution_mode" in config else {}),
                                           **(initial_settings or {})})
        lease=StoreLease(store.folder)
        try:
            store=SessionStore(store.folder)
        except BaseException:
            lease.close()
            raise
        try:
            config = configuration(store.settings(), config)
            previous_settings=store.settings()
            config.update(initial_settings)
            validate_execution_config(config)
        except BaseException:
            lease.close();raise
        self.store, self.config = store, config
        self.config["package_directory"] = str(package_directory(store.workspace))
        self.config.setdefault("non_builtin_tools", definitions())
        self.config["hidden_paths"] = [str(store.folder.parent)]
        self.config["system_fragments"] = [
            "Workspace files are shared within this Session; coordinate ownership before writing. "
            + ("Host execution runs tools as the current OS user without isolation; filesystem, credentials, "
             "Session archives and other user processes may be accessible. Do not modify Recoding's private "
             "archives or unrelated files/processes; coordinate work within the workspace." if config['execution_mode']=='host' else
             "Session state and credentials are private and inaccessible from tools.")]
        installed = {item['schema']['function']['name'] for item in self.config['non_builtin_tools']}
        if {'read_file','edit_files'} <= installed:
            self.config['system_fragments'].append(
                'For reliable code changes use read_file revisions with edit_files. Conflicts mean reread '
                'current files and revise autonomously; never guess ambiguous patch context or overwrite '
                'newer changes. Git is optional. A batch may partially commit; inspect its journal before undo.')
        if 'repo_query' in installed:
            self.config['system_fragments'].append(
                'Use repo_query with narrow paths/globs, bounded results and pagination to navigate large '
                'repositories. Outlines/definition candidates are not semantic references; inspect the '
                'reported backend and reread exact source before edits.')
        self.config['system_fragments'] += prompt_fragments(self.config['non_builtin_tools'])
        progress = progress or Progress()
        try:
            progress.stage("agents", "恢复 Agent 列表与通信状态")
            self.team = MultiAgentSystem(store, config, progress=progress, master_lease=lease,
                                         resource_request=self._resource_request, release_owner=self._release_owner)
            self.master = self.team.master
        except BaseException:
            lease.close()
            raise
        try:
            self.execution = ExecutionService(store,self.config,actor_info=self.team.tool_actor,
                notify=self.team.tool_notice,observe=self._tool_observation,
                adapters={'python':PythonTools,'bash':BashTools,'files':FileTransfers},
                sources=execution_sources(),registry_factory=ToolRegistry)
            self.compaction = SessionCompaction(self)
            if autostart:
                progress.stage('environment', '启动 Local 执行环境')
                self._prepare_local()
                self.team.start()
                if initial_settings:
                    store.update_settings(initial_settings)
                    self.team.configuration_notice(previous_settings,initial_settings)
                progress.attach(self.master.startup)
            else:
                self.master.startup.finish()
        except BaseException:
            self.master.cancel()
            if hasattr(self, "team"):
                self.team.stop_children("Session initialization failed")
            if self.execution:
                self.execution.close()
            if hasattr(self, "team"):
                self.team.pause()
            self.master.close()
            raise

    @classmethod
    def create(cls, workspace, name, *, config=None):
        return cls(SessionStore.create(workspace, name, config or {}), config)

    @classmethod
    def resume(cls, session_directory, *, config=None, initial_settings=None):
        return cls(SessionStore(session_directory), config, initial_settings=initial_settings)

    # The team owns the public observation channel, including tagged member
    # events. These properties keep transport code out of individual Agents.
    @property
    def id(self): return self.store.id
    @property
    def name(self): return self.master.name
    @name.setter
    def name(self, value): self.master.name = value
    @property
    def directory(self): return self.store.workspace
    @property
    def condition(self): return self.team.event_stream.condition
    @property
    def events(self): return self.team.event_stream.events
    @property
    def sequence(self): return self.team.event_stream.sequence
    @property
    def status(self):
        lease=self.host_lease or self.master.lease
        return 'run' if lease.fd is not None else 'stop'
    @property
    def run_id(self): return self.master.run_id
    @property
    def closed(self): return self.master.closed
    @property
    def active_model(self): return self.master.active_model
    @property
    def startup(self): return self.master.startup

    def publish(self, event):
        self.team.publish(event)

    def snapshot(self, *, events=True):
        data = self.master.snapshot(events=events)
        data.pop("agent_id", None)
        data["execution_mode"]=self.config["execution_mode"]
        data["session_directory"] = data.pop("storage_directory")
        data["master_status"]=data["status"]
        data["status"]=self.status
        data["read_only"]=False
        data.update(agents=self.team.listing(), master_icon=self.team.master_icon, team=self.team.summary(), max_subagents=self.team.maximum,
                    idle_sleep_seconds=self.team.idle_sleep_seconds, context_compactions=self.compaction.snapshot(),
                    agent_token_usage=self.team.token_usage())
        if events:
            data["model_rates"] += self.team.rates()
        data['python_pool']=self.python_resources()
        data['bash_pool']=self.execution.pool_snapshot('bash') if self.execution else {'processes':{},'executions':{}}
        data['nodes']=self.execution.listing()['nodes'] if self.execution else []
        data['executions']=self.list_executions('master')['executions']
        return data

    def _resource_request(self, source, action, fields, *, cancelled=None):
        """Session supplies execution and maintenance, not Agent coordination."""
        if action == "request_context_compaction":
            return self.compaction.request_from_agent(source, fields, cancelled=cancelled)
        if action in self.execution.names:
            return self.execution.request(source, action, fields, cancelled=cancelled)
        raise ValueError("Unknown resource request: " + action)

    def configure_remote_node(self,node_id,*,ssh_target,workspace,name=None,python='/usr/bin/python3',execution_mode='sandbox',ssh_config=None,lease_seconds=60):
        spec={'name':name or node_id,'ssh_target':ssh_target,'workspace':workspace,'python':python,
              'execution_mode':execution_mode,'lease_seconds':lease_seconds}
        if ssh_config:spec['ssh_config']=ssh_config
        result=self.execution.configure(node_id,spec)
        self.publish({'type':'nodes_updated',**result})
        for identifier in self.team.agents:
            actor=self.team.tool_actor(identifier)
            if actor and actor['status'] in {'starting','running','sleeping'}:
                self.team.tool_notice(identifier,'Node '+(name or node_id)+' is now available. Use list_nodes for node_id and tool_workspace; workspace is its Host-side binding, not a shell path. Files require explicit transfer_files.',{'event':'node_added','node_id':node_id})
        return result

    def connect_node(self,node_id):
        result=self.execution.connect(node_id);self.publish({'type':'nodes_updated',**result});return result

    def remove_node(self,node_id):
        result=self.execution.remove(node_id);self.publish({'type':'nodes_updated',**result});return result

    def _prepare_local(self):
        self.execution.ensure()
        self.config['execution_environment']=self.execution.environment.facts()
        self.master.config['execution_environment']=self.config['execution_environment']

    def _release_owner(self, identifier):
        if self.execution:
            self.execution.stop_owner(identifier)

    @property
    def python_pool(self):return self.execution.python_pool if self.execution else None

    def _tool_observation(self,language,kind,record):
        if language=='node':
            if kind=='target':
                runner=self.team.agents.get(record['source'])
                if runner:
                    event=next((e for e in reversed(runner.events) if e.get('type')=='tool_start' and e.get('name')==record['tool']),None)
                    if event:runner.publish({'type':'tool_node','call_id':event['call_id'],'run_id':event.get('run_id'),**record})
            else:self.publish({'type':'nodes_updated',**self.execution.listing()})
            return
        if kind=='inventory' and record.get('node_id','local')!='local':
            self.publish({'type':'nodes_updated',**self.execution.listing()});return
        if kind=='inventory':
            for runner in list(self.team.agents.values()):
                if runner is self.master or runner.status in {'starting','running','sleeping'}:
                    atomic_json(runner.store.folder/'tool-inventory.json',record)
                    runner.publish({'type':'tool_inventory','inventory':record})
            return
        self.publish({'type':'tool_service_updated','service':language+'_pool','kind':kind,'record':record})
        if kind=='execution':
            for identifier in {record.get('source'),record.get('owner')}:
                runner=self.team.agents.get(identifier)
                if runner:runner.publish({'type':'execution_updated','run_id':runner.run_id,'execution':record})

    def python_resources(self):return self.execution.pool_snapshot('python') if self.execution else {'processes':{},'executions':{}}

    def list_executions(self,agent_id='master'):
        self.agent_store(agent_id)
        pools = [self.execution.pool_snapshot('bash'), self.execution.pool_snapshot('python')] if self.execution else []
        return visible_executions(pools, agent_id)

    def execution_output(self,agent_id,execution_id,**options):
        self.agent_store(agent_id)
        for pool in self.execution.pools():
            record=pool.snapshot()['executions'].get(execution_id)
            if record:
                return pool.output(record,**options)
        raise ValueError('Execution not found')

    def output_speed(self):
        """Whole-Session call measurements, including stopped members and summaries."""
        with self.condition:
            master=[dict(item) for item in self.master.model_rates.values()]
        return speed_snapshot(master,self.team.rates(),started_at=self.store.metadata['created_at'],live=True)

    def navigation(self, entry):
        """Lightweight metadata for application catalogs; never load history."""
        with self.condition:
            info = entry.navigation_info()
            data = {**info, "status": self.status, "master_status": self.master.status, "model": self.config.get("model"),
                    "storage_missing": self.store.folder != entry.folder or not self.store.exists(),
                    "updated_at": self.events[-1]["timestamp"] if self.events else info["updated_at"]}
        data["active_agents"] = int(data["master_status"] in {"starting", "running", "sleeping"}) + sum(
            agent["status"] in {"starting", "running", "sleeping"} for agent in self.team.listing())
        return data

    @staticmethod
    def validate_input(prompt, attachments):
        if (not isinstance(attachments, list) or len(attachments) > 8 or
                any(not isinstance(value, str) or len(value) != 32 for value in attachments) or len(set(attachments)) != len(attachments)):
            raise ValueError("每次最多提交 8 张图片附件。")
        if not isinstance(prompt, str) or (not prompt.strip() and not attachments) or len(prompt.encode()) > 60000:
            raise ValueError("请输入指令或添加图片，文字最多 60000 字节。")

    def submit(self, prompt, attachments=(), *, agent_id="master"):
        attachments = list(attachments)
        self.validate_input(prompt,attachments)
        message = self.team.queue_user(agent_id,prompt,attachments)
        record = self.team.record(agent_id)
        return {"queued":message["id"],"run_id":record.get("run_id"),"status":record["status"]}

    def messages(self, **page):
        return self.team.conversation(**page)

    def submit_message(self, prompt, attachments=(), *, recipient="master"):
        """Human-only aggregate-view input; uploads belong to the Master store."""
        if not isinstance(attachments, (list, tuple)):
            raise ValueError("图片附件必须是列表。")
        attachments = list(attachments)
        self.validate_input(prompt, attachments)
        if not isinstance(recipient, str):
            raise ValueError("请选择消息接收者。")
        broadcast = recipient in {"all", "all_except_master"}
        with self.team.condition:
            if self.team.closing:
                raise ValueError("Session 正在关闭，消息未发送。")
            targets = self.team._recipients(exclude="master" if recipient == "all_except_master" else None) if broadcast else [recipient]
            for target in targets:
                self.team._valid_agent(target)
                if not self.team.accepting(target):
                    raise ValueError("该接收者已不在运行；请选择存活的 Agent，或进入它的页面留言。")
            if not targets:
                raise ValueError("当前没有存活的接收者，消息未发送。")
            sources = [self.store.uploads()[identifier] for identifier in attachments]
            pictures = [(item, self.store.upload_attachment(item["id"])["data_url"]) for item in sources]
            prepared = []
            for target in targets:
                store = self.store if target == "master" else self.agent_store(target)[0]
                check_image_budget(store, target, self.team.data["messages"], sources)
                copied = sources if target == "master" else copy_images(store, pictures)
                prepared.append((target, [item["id"] for item in copied]))
        group = uuid.uuid4().hex if broadcast else None
        receipts = []
        for target, pictures in prepared:
            try:
                message = self.team.queue_user(target,prompt,pictures,broadcast_id=group,
                    broadcast_scope=recipient if broadcast else None,active_only=True)
            except ValueError:
                if broadcast and not self.team.accepting(target):continue
                raise
            receipts.append({"agent_id": target, "message_id": message["id"]})
        if not receipts:
            raise ValueError("接收者均已停止，消息未发送。")
        return {"recipients": receipts, "broadcast_id": group, "broadcast_scope": recipient if broadcast else None}

    @staticmethod
    def validate_agent_limit(value):
        return limit(value)

    def configure(self, method, value):
        if method not in {"set_limit", "set_idle_sleep"}:
            raise ValueError("Unknown Session setting")
        with self.condition:
            return getattr(self.team, method)(value)

    def upload(self, data, name, *, agent_id="master"):
        store = self.store if agent_id == "master" else self.agent_store(agent_id)[0]
        with self.team.condition:
            return store.add_upload(data, name)

    def set_model(self, model):
        with self.condition:
            self.store.update_model(model)
            self.publish({"type": "model_changed", "model": model})
        self.team.set_model(model)

    def inspect_tools(self, agent_id="master"):
        # Inventory belongs to the shared execution environment, never an Agent worker.
        return self.execution.request(agent_id,'inspect_tools',{})

    def agent_store(self, agent_id):
        return self.team._store(agent_id),self.team._metadata(self.team.record(agent_id))

    def agent_snapshot(self, agent_id):
        if agent_id == 'master':
            return self.snapshot()
        store, descriptor = self.agent_store(agent_id)
        runner = self.team.agents.get(agent_id)
        if runner:
            data = runner.snapshot()
        else:
            events = [preview(event) for event in store.load_events()]
            data = {**store.info(), "events": events, "sequence": events[-1]["sequence"] if events else 0,
                    "tool_inventory": inventory_snapshot(store),
                    "history_truncated": bool(events and events[0]["sequence"] > 1)}
        data['executions']=self.list_executions(agent_id)['executions']
        if runner and runner.status in {'starting','running','sleeping'} and self.execution.environment.last_inventory:
            data['tool_inventory']=self.execution.environment.last_inventory
        return {**data, "agent_id": agent_id, "status": descriptor["status"],
                "session_directory": str(store.folder),
                "descriptor": descriptor, "read_only": False,
                "resumable": descriptor["resumable"]}

    def start(self):
        require_runtime_revision(RUNTIME_REVISION)
        self.execution.reopen()
        self._prepare_local()
        return self.team.start()

    def continue_task(self):
        return self.start()

    def cancel(self):
        changed = self.compaction.cancel()
        # Stop model loops before resource teardown can enqueue completion mail.
        # A global Stop must not accidentally wake a fresh model request.
        changed = self.team.stop() or changed
        self.execution.close()
        return changed

    def close(self):
        self.compaction.close()
        self.cancel()
        self.team.close()
        if self.host_lease:self.host_lease.close()
