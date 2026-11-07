"""A CLI process owns at most one Session; switching closes the previous one."""

from pathlib import Path
from datetime import datetime
import threading

from .catalog import discover, select
from .client import Client,NoSessionClient
from .control import request
from .host import CliSessionHost
from session.store import SESSION_HOME,SessionStore


class CliSessionNavigator:
    def __init__(self, root, config_factory, *, initial=None, initial_client=None,
                 new_config_factory=None, new_name=None):
        self.root=Path(root).expanduser().resolve()
        self.directory=self.root
        self.config_factory=config_factory
        self.new_config_factory=new_config_factory or config_factory
        self.new_name=new_name
        self.current_host=initial
        self.current_client=initial_client
        self.lock=threading.RLock()
        self.closed=False

    def listing(self):
        directory=self.directory
        children=sorted((item for item in (directory.iterdir() if directory.is_dir() else ()) if not item.name.startswith('.') and
                         item.name not in {'node_modules','snapshots','__pycache__'} and item.is_dir() and not item.is_symlink()),
                        key=lambda item:item.name.casefold())
        return {'directory':str(directory),'parent':str(directory.parent) if directory.parent!=directory else None,
                'directories':[str(item) for item in children],'sessions':discover(directory,recursive=False)}

    def change_directory(self, value):
        candidate=Path(value).expanduser()
        if not candidate.is_absolute():candidate=self.directory/candidate
        if candidate.is_symlink():raise ValueError('不浏览软链接目录')
        chosen=candidate.resolve(strict=True)
        if not chosen.is_dir() or SESSION_HOME in chosen.parts:raise ValueError('请选择 workspace 目录')
        self.directory=chosen
        return self.listing()

    def create_with_message(self,content,target='master',images=()):
        """Only a real first message creates an archive and its owning Host."""
        with self.lock:
            if self.closed:raise ValueError('Session 导航已关闭')
            if self.current_host or self.current_client and not isinstance(self.current_client,NoSessionClient):
                raise ValueError('已有打开的 Session')
            if not content.strip() and not images:raise ValueError('请输入第一条消息')
            if target!='master':raise ValueError('新 Session 的第一条消息请发给 master')
            # Credentials are checked here, before creating a persistent archive.
            config=self.new_config_factory(self.root)
            store=SessionStore.create(self.root,self.new_name or datetime.now().strftime('session-%Y%m%d-%H%M%S'),config)
            owner=None
            try:
                owner=CliSessionHost(store.folder,config)
                client=Client(store.folder)
                client.send(content,target,images)
            except BaseException:
                if owner:owner.close()
                raise
            self.current_host,self.current_client=owner,client
            return client

    def restore(self, value):
        directory=select(value,self.directory,recursive=False)
        from session import SessionHostProtocol
        with self.lock:
            if self.closed:raise ValueError('Session 导航已关闭')
            if self.current_client and self.current_client.directory==directory:
                self.current_client.call('wake')
                return self.current_client
            # A busy target must not evict the currently owned Session.
            if not SessionHostProtocol.available(directory):raise ValueError('Session 正由其他 Host 独占，不能打开')
            config=self.config_factory(directory)
            self._close_current()
            try:
                self.current_host=CliSessionHost(directory,config)
                self.current_client=Client(directory)
            except Exception:
                self.current_client=NoSessionClient(self.root)
                # Switching has already released the old Session. Return an
                # unbound UI rather than continuing to browse a stopped archive.
                raise
            return self.current_client

    def _close_current(self):
        if self.current_host:self.current_host.close()
        elif self.current_client and not isinstance(self.current_client,NoSessionClient):
            # A disconnected control client must not stop a successor Host.
            request(self.current_client.directory,'stop',_host_id=self.current_client.host_id)
        self.current_host=self.current_client=None

    def stop(self):
        with self.lock:
            self._close_current()
            self.current_client=NoSessionClient(self.root)
            return self.current_client

    def wake(self,client):
        return client.call('wake')

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            self._close_current()
