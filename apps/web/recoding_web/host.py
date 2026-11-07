"""One GUI client, many explicitly hosted Sessions, and read-only catalog views."""

from datetime import datetime
import os
from pathlib import Path
import threading
import time
import shutil
from contextlib import contextmanager

from session.store import SESSION_HOME, StoreLease, SessionStore, UnsupportedSession
from multiagent.models import validate_model
from multiagent.model_config import api_base_url, default_model
from multiagent.media import AgentMedia
from .progress import Progress
from multiagent.agent import preview
from session import Session, SessionHostProtocol
from session.settings import validate_settings


from .catalog import SessionCatalog
from session.config import default_env_file


class GuiBusyError(PermissionError):
    pass


class GuiSessionHost:
    def __init__(self, root, config=None, *, discover=True, defer_discovery=False, gui_timeout=30,
                 demo=False,network=True,gpu=True,execution_mode="sandbox",tool_timeout=30,api_timeout=120,max_output=16000,private_files=()):
        started=time.monotonic()
        self.config={"execution_mode":execution_mode,"demo":demo,"network":network,"gpu":gpu,"tool_timeout":tool_timeout,
                     "api_timeout":api_timeout,"max_output":max_output,"idle_sleep_seconds":10,**(config or {})}
        self.config['private_files']=[str(path) for path in {default_env_file().resolve(),*(Path(p).resolve() for p in private_files)}]

        self.sessions={}
        self.catalog=SessionCatalog(root,self.sessions,discover=discover and not defer_discovery)
        self.lock=self.catalog.lock
        self.views={}
        self.closing=set()
        self.released=threading.Condition(self.lock)
        self.closed=threading.Event()
        self.gui_lock=threading.Lock()
        self.gui_timeout=gui_timeout
        self.gui_page=None
        self.gui_document=None
        self.gui_revoked=set()
        self.gui_seen=0
        self.startup_ms=round((time.monotonic()-started)*1000,3)

    def _gui_identity(self, page, document):
        if not isinstance(page,str) or not page or len(page)>64 or not all(c.isalnum() or c in '_-' for c in page):
            raise GuiBusyError('缺少有效的 GUI 页面身份，请重新打开网页')
        document=document or (self.gui_document if page==self.gui_page else page)
        if not isinstance(document,str) or not document or len(document)>64 or not all(c.isalnum() or c in '_-' for c in document):
            raise GuiBusyError('缺少有效的 GUI 文档身份，请重新打开网页')
        return page,document

    def connect_gui(self, page, document=None, *, takeover=False):
        with self.gui_lock:
            identity=self._gui_identity(page,document)
            now=time.monotonic()
            if self.closed.is_set():raise GuiBusyError('Host 已关闭')
            if identity in self.gui_revoked and not takeover:
                raise GuiBusyError('此页面的连接已释放或被其他页面接管；Session 仍在服务器运行。可明确接管后重新连接。')
            if self.gui_page not in {None,page} and now-self.gui_seen<self.gui_timeout and not takeover:
                raise GuiBusyError('此 Web Host 已连接另一个 GUI 页面；请关闭旧页面，等待连接释放后重试')
            previous=self.gui_page if (self.gui_page,self.gui_document)!=identity else None
            if previous:self.gui_revoked.add((self.gui_page,self.gui_document))
            self.gui_revoked.discard(identity)
            self.gui_page,self.gui_seen=page,now
            self.gui_document=identity[1]
            return previous

    def require_gui(self, page, document=None):
        with self.gui_lock:
            if self.closed.is_set() or self._gui_identity(page,document)!=(self.gui_page,self.gui_document):
                raise GuiBusyError('此页面的连接已被接管，请明确接管后重新连接')

    def release_gui(self, page, document):
        if not document:raise ValueError('A document identity is required for release')
        with self.gui_lock:
            identity=self._gui_identity(page,document)
            if identity!=(self.gui_page,self.gui_document):return False
            self.gui_revoked.add(identity)
            self.gui_page=self.gui_document=None
            self.gui_seen=0
            return True

    def running(self, identifier):
        key=self.catalog.resolve(identifier)
        if key in self.closing:raise PermissionError('Session 正在 Stop，请等待资源释放')
        if key not in self.sessions:raise PermissionError('Session 处于 stop 只读状态，请先 Resume')
        return self.sessions[key]

    def _saved(self,key):
        folder=self.catalog.stores[key].folder
        lease=SessionHostProtocol.read(folder)
        try:
            if key not in self.views or self.views[key].store.folder!=folder:self.views[key]=SessionHostProtocol.browse(folder)
            return self.views[key]
        finally:lease.close()

    @contextmanager
    def archive_read(self, identifier):
        with self.lock:
            key=self.catalog.resolve(identifier)
            if key in self.closing:raise PermissionError('Session 正在关闭')
            lease=None if key in self.sessions else SessionHostProtocol.read(self.catalog.stores[key].folder)
        try:yield key
        finally:
            if lease:lease.close()

    @contextmanager
    def read_request(self,path):
        parts=path.strip('/').split('/')
        if len(parts)>=3 and parts[:2]==['api','sessions']:
            with self.archive_read(parts[2]):yield
        else:yield

    def update(self,identifier,fields):
        if not fields or set(fields)-{'name','pinned'}:raise ValueError('只支持修改 Session 名称或置顶状态')
        if 'name' in fields:SessionStore.validate_name(fields['name'])
        if 'pinned' in fields and type(fields['pinned']) is not bool:raise ValueError('pinned 必须是布尔值')
        with self.lock:
            session=self.running(identifier)
            session.store.update_metadata(**fields)
            session.name=session.store.name
            session.publish({'type':'session_updated','name':session.name,'pinned':session.store.metadata.get('pinned',False)})
            self.catalog.stores[session.id]=session.store
            return session.store.info()

    def nodes(self,identifier,fields):
        session=self.running(identifier)
        if not isinstance(fields,dict):raise ValueError('Node configuration must be an object')
        action=fields.get('action','add')
        if action in {'connect','remove','cancel_add'}:
            if set(fields)!={'action','node_id'}:raise ValueError('Provide action and node_id')
            if action=='cancel_add':return session.execution.cancel_add(fields['node_id'])
            return session.connect_node(fields['node_id']) if action=='connect' else session.remove_node(fields['node_id'])
        allowed={'action','node_id','name','ssh_target','workspace','python','execution_mode','ssh_config','lease_seconds'}
        if action!='add' or set(fields)-allowed:raise ValueError('Invalid node operation')
        import uuid
        if not {'ssh_target','workspace'}<=fields.keys():raise ValueError('Provide ssh_target and workspace')
        values={key:value for key,value in fields.items() if key not in {'action','node_id'}}
        return session.configure_remote_node(fields.get('node_id') or 'node-'+uuid.uuid4().hex[:8],**values)

    def set_model(self,identifier,fields):
        if not isinstance(fields,dict) or set(fields)!={'model'}:raise ValueError('请提供 model 字段')
        model=validate_model(fields['model'])
        if self.config['demo']:raise ValueError('OFFLINE DEMO 不能切换真实模型')
        with self.lock:
            session=self.running(identifier)
            session.set_model(model)
            self.catalog.stores[session.id]=session.store
            return {'model':model,'scope':'all_agents','active_model':session.active_model}

    def upload(self, identifier, data, name, agent_id="master"):
        return self.running(identifier).upload(data, name, agent_id=agent_id)

    def set_agent_limit(self, identifier, fields):
        if not isinstance(fields, dict) or set(fields) != {"max_subagents"}:
            raise ValueError("请只提供 max_subagents 字段。")
        return self._configure_team(identifier, "set_limit", Session.validate_agent_limit(fields["max_subagents"]))

    def set_idle_sleep(self, identifier, fields):
        if not isinstance(fields, dict) or set(fields) != {"idle_sleep_seconds"}:
            raise ValueError("请只提供 idle_sleep_seconds 字段。")
        return self._configure_team(identifier, "set_idle_sleep", fields["idle_sleep_seconds"])

    def _configure_team(self, identifier, method, value):
        with self.lock:
            key = self.catalog.resolve(identifier)
            session=self.running(key)
            result=session.configure(method,value)
            self.catalog.stores[key].metadata=session.store.metadata
            return result

    def submit(self, identifier, agent_id, prompt, attachments=()):
        return self.running(identifier).submit(prompt, attachments, agent_id=agent_id)

    def image(self, identifier, *, digest=None, path=None):
        with self.archive_read(identifier) as key,self.lock:
            store = self.catalog.load_store(key)
            if digest is not None:
                return store.image_asset(digest)
            return AgentMedia(store, self.config.get("private_files", ()), [store.folder.parent]).bytes(path)

    def delete(self,identifier):
        with self.lock:
            session=self.running(identifier)
            store=session.store
            # Retain the host lease across shutdown and deletion to prevent a
            # second Host taking ownership between the two operations.
            host_lease=session.host_lease
            session.host_lease=None
            try:
                session.close()
                lease=StoreLease(store.folder)
                try:shutil.rmtree(store.folder)
                finally:lease.close()
            finally:
                host_lease.close()
                self.sessions.pop(session.id,None)
                self.views.pop(session.id,None)
            del self.catalog.stores[session.id]
            self.catalog.remember()
            return {'deleted':session.id,'workspace_preserved':str(store.workspace)}

    def _check_capacity(self):
        if len(self.sessions) >= 8:
            raise ValueError("最多同时运行 8 个 Session；请先停止一个运行环境。")

    def continue_task(self, identifier, settings=None):
        with self.lock:
            if self.closed.is_set():raise ValueError("Host 已关闭")
            key=self.catalog.resolve(identifier)
            if key in self.closing:raise PermissionError('Session 正在 Stop，请等待资源释放')
            if key in self.sessions:
                session=self.sessions[key]
                if settings and settings.get('execution_mode',session.config['execution_mode'])!=session.config['execution_mode']:
                    raise ValueError('Stop the entire Session before changing execution_mode')
                result=session.continue_task()
                return {**result,'status':'run','master_status':session.master.status,'read_only':False}
            self._check_capacity()
            settings=validate_settings(settings or {})
            store=self.catalog.load_store(key)
            config={**self.config,**store.settings(),'demo':self.config['demo']}
            for setting in ('network','gpu'):config[setting]=self.config[setting] and config.get(setting,True)
            session=SessionHostProtocol.resume(store.folder,config=config,initial_settings=settings)
            self.sessions[key]=session
            return {'status':'run','master_status':session.master.status,'read_only':False}

    def open(self, name=None, *, directory=None, path=None, identifier=None, action=None, network=True, gpu=True, model=None, execution_mode=None, max_subagents=16, progress=None):
        progress = progress or Progress()
        Session.validate_agent_limit(max_subagents)
        if model is not None:
            validate_model(model)
        progress.stage("lock", "等待 Session 管理器", "等待目录扫描或其他 Session 操作释放锁")
        with self.lock:
            if self.closed.is_set():raise ValueError("Host 已关闭")
            if not self.catalog.discovery_complete:
                self.catalog.refresh(progress)
            progress.stage("session", "定位 Session 存档", path or identifier or name or self.catalog.root)
            browse = action == "browse" or action is None and bool(path or identifier)
            if action in {"resume", "browse"} or path or identifier:
                if path:
                    candidate = Path(path).expanduser()
                    if not candidate.is_absolute():
                        candidate = self.catalog.root / candidate
                    if candidate.name == "session.json":
                        candidate = candidate.parent
                    if candidate.parent.name != SESSION_HOME:
                        raise UnsupportedSession("请选择 .recoding 下的当前版本 Session 存档。")
                    try:
                        store = self.catalog.register(SessionStore(candidate))
                    except (FileNotFoundError, NotADirectoryError) as error:
                        raise KeyError("Session 存档已不存在或已移动，请从当前目录列表重新选择。") from error
                else:
                    store = self.catalog.stores[self.catalog.resolve(identifier or name)]
            else:
                existing = [store for store in self.catalog.stores.values() if store.name == name and store.workspace == self.catalog.root]
                if action != "create" and len(existing) == 1 and directory is None:
                    store = existing[0]
                    browse = action != "resume"
                else:
                    self._check_capacity()
                    if name is None or (isinstance(name, str) and not name.strip()):
                        name = datetime.now().strftime("session-%Y%m%d-%H%M%S")
                    chosen = Path(directory).expanduser() if directory else self.catalog.root
                    if not chosen.is_absolute():
                        chosen = self.catalog.root / chosen
                    workspace = self.catalog.validate_workspace(chosen)
                    settings = {**self.config, "network": self.config["network"] and network,
                                "gpu": self.config["gpu"] and gpu,
                                "execution_mode":execution_mode or self.config["execution_mode"],
                                "max_subagents": max_subagents,
                                "model": model or default_model(),
                                "base_url": api_base_url()}
                    store = self.catalog.register(SessionStore.create(workspace, name, settings))
            self.catalog.validate_workspace(store.workspace)
            if browse:
                if store.id in self.sessions:return self.sessions[store.id]
                return self._saved(store.id)
            if store.id in self.sessions:
                existing = self.sessions[store.id]
                if not existing.closed:
                    if execution_mode is not None and execution_mode!=existing.config['execution_mode']:
                        raise ValueError('Stop the entire Session before changing execution_mode')
                    progress.attach(existing.startup)
                    return existing
                existing.close()
                del self.sessions[store.id]
            if not browse:
                self._check_capacity()
            progress.stage("settings", "加载 Session 设置", store.name)
            store = self.catalog.load_store(store.id)
            progress.stage("remember", "更新 Session 目录索引")
            self.catalog.remember()
            config = {**self.config, **store.settings(), "demo": self.config["demo"]}
            config["network"] = self.config["network"] and config.get("network", True)
            config["gpu"] = self.config["gpu"] and config.get("gpu", True)
            session = SessionHostProtocol.resume(store.folder,config=config,initial_settings={"execution_mode":execution_mode} if execution_mode is not None else None,progress=progress)
            self.sessions[store.id] = session
            return session

    def get(self, identifier):
        with self.lock:
            key = self.catalog.resolve(identifier)
            return self.sessions[key] if key in self.sessions else self._saved(key)

    def agent_store(self, identifier, agent_id):
        session = self.get(identifier)
        store, descriptor = session.agent_store(agent_id)
        return session, store, descriptor

    def agent_snapshot(self, identifier, agent_id):
        return self.get(identifier).agent_snapshot(agent_id)

    def inspect(self, identifier):
        with self.lock:
            key = self.catalog.resolve(identifier)
            if key in self.sessions:
                return self.sessions[key].snapshot()
            return self._saved(key).snapshot()

    def history_page(self, identifier, before):
        with self.archive_read(identifier) as key,self.lock:
            store = self.catalog.load_store(key)
            events = [preview(event) for event in store.load_events(limit=400, before=before)]
            return {"events": events, "has_more": bool(events and events[0]["sequence"] > 1)}

    def tool_source(self, identifier, run_id, call_id, sequence):
        with self.archive_read(identifier) as key,self.lock:
            return self.catalog.load_store(key).tool_source(run_id,call_id,sequence)

    def close_session(self,identifier):
        with self.released:
            key=self.catalog.resolve(identifier)
            while key in self.closing:self.released.wait()
            session=self.sessions.get(key)
            if session is None:return
            self.closing.add(key)
        try:
            session.close()
        except BaseException:
            with self.released:
                self.closing.discard(key)
                self.released.notify_all()
            raise
        with self.released:
            self.closing.discard(key)
            if self.sessions.get(key) is session:del self.sessions[key]
            self.views.pop(key,None)
            self.released.notify_all()

    def close(self):
        with self.lock:
            self.closed.set()
            identifiers=list(self.sessions)
        error=None
        for identifier in identifiers:
            try:self.close_session(identifier)
            except Exception as caught:error=error or caught
        if error:raise error
