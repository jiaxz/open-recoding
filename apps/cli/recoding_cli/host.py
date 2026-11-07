"""Own one Session and expose its public operations to CLI clients only."""

import base64
import json
import os
from pathlib import Path
import signal
import secrets
import socketserver
import struct
import threading
import time

from session import SessionHostProtocol
from multiagent.storage import atomic_json
from .control import LIMIT, address


class CliSessionHost:
    def __init__(self, directory, config, *, initial_settings=None):
        self.directory = Path(directory).resolve()
        self.done = threading.Event()
        self.commands = threading.RLock()
        self._closed=False
        self.host_id=secrets.token_hex(16)
        self.config=dict(config)
        self.socket = address(self.directory)
        self.server=self.thread=None
        self.socket_identity=None
        self.session = SessionHostProtocol.resume(self.directory, config=config,initial_settings=initial_settings if initial_settings is not None else
                                      {key:value for key,value in config.items() if key in {'model','max_subagents','idle_sleep_seconds','execution_mode'}})
        try:
            # We already hold owner.lock; a stale socket has no live Session owner.
            Path(self.socket).unlink(missing_ok=True)
            owner = self
            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    self.request.settimeout(15)
                    if hasattr(socketserver.socket, 'SO_PEERCRED'):
                        uid = struct.unpack('3i', self.request.getsockopt(socketserver.socket.SOL_SOCKET,
                            socketserver.socket.SO_PEERCRED, 12))[1]
                        if uid != os.getuid():
                            return
                    try:
                        raw = self.rfile.readline(LIMIT+1)
                        if len(raw) > LIMIT or not raw.endswith(b'\n'):
                            raise ValueError('Request exceeds size limit')
                        packet = json.loads(raw)
                        with owner.commands:
                            result = owner.dispatch(packet)
                        response = {'ok': True, 'result': result}
                    except Exception as error:
                        response = {'ok': False, 'error': str(error)}
                    try:
                        self.wfile.write(json.dumps(response, ensure_ascii=False).encode()+b'\n')
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = True
            self.server = Server(self.socket, Handler)
            os.chmod(self.socket, 0o600)
            socket_stat=Path(self.socket).stat()
            self.socket_identity=(socket_stat.st_dev,socket_stat.st_ino)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='cli-control')
            self.thread.start()
            atomic_json(self.directory/'cli-runtime.json', {'version': 1, 'pid': os.getpid(),'host_id':self.host_id,
                'session_directory': str(self.directory), 'socket': self.socket, 'started_at': time.time()})
        except BaseException:
            self._unpublish()
            try:
                if self.server:
                    if self.thread and self.thread.is_alive():
                        self.server.shutdown()
                        self.thread.join(timeout=2)
                    self.server.server_close()
            finally:self.session.close()
            raise

    def dispatch(self, packet):
        if packet.get('_host_id') not in {None,self.host_id}:raise ValueError('原 CLI Host 已关闭；不能操作后来接管的 Host')
        action = packet['action']
        if action=='identity':return self.host_id
        session = self.session
        if session is None:raise ValueError('CLI 已关闭此 Session，请重新 Resume')
        if action == 'status':
            return {**session.snapshot(events=False), 'live': True, 'settings': session.store.settings(),
                    'compaction': session.compaction.snapshot()}
        if action == 'speed':return session.output_speed()
        if action == 'python_pool':return session.python_resources()
        if action == 'executions':return session.list_executions(packet.get('agent_id','master'))
        if action == 'execution_output':return session.execution_output(packet.get('agent_id','master'),packet['execution_id'],
            **{key:packet[key] for key in ('stdout_offset','stderr_offset','max_bytes') if key in packet})
        if action == 'agents':
            return session.team.public_agents()
        if action == 'messages':
            return session.messages(**{key: packet[key] for key in ('before', 'after', 'limit') if key in packet})
        if action == 'send':
            recipient = resolve_agent(session.team.public_agents(), packet.get('to', 'master'))
            images = packet.get('images', [])
            owner = 'master' if recipient in {'all', 'all_except_master'} else recipient
            attachments = [session.upload(base64.b64decode(item['data'], validate=True), item['name'], agent_id=owner)['id'] for item in images]
            if recipient in {'all', 'all_except_master'}:
                return session.submit_message(packet['content'], attachments, recipient=recipient)
            return session.submit(packet['content'], attachments, agent_id=recipient)
        if action == 'stop':
            self._unpublish()
            session.close()
            self.session=None
            self.done.set()
            return {'status':'stop'}
        if action == 'resume':
            session.start()
            return {'status':'run','master_status':session.master.status}
        if action == 'wake':
            status=session.master.status
            if status in {'stopped','failed'}:
                return {**session.start(),'action':'started'}
            if status=='sleeping':
                receipt=session.submit('[用户命令 /wake] 请醒来检查当前任务和未读消息，自行决定下一步。',agent_id='master')
                return {**receipt,'action':'signalled'}
            if status in {'starting','running'}:
                return {'status':'run','master_status':status,'action':'active'}
            raise ValueError('Master 正在停止或运行环境已关闭，请稍后使用 /wake')
        if action == 'config':
            values = packet['values']
            # Validate all values before applying any of them.
            from multiagent.models import validate_model
            if set(values)-{'model', 'max_subagents', 'idle_sleep_seconds'}:
                raise ValueError('Unsupported setting')
            if 'model' in values: validate_model(values['model'])
            if 'max_subagents' in values: session.validate_agent_limit(values['max_subagents'])
            if 'idle_sleep_seconds' in values and not .1 <= values['idle_sleep_seconds'] <= 3600:
                raise ValueError('idle_sleep_seconds must be 0.1–3600')
            for key, value in values.items():
                if key == 'model': session.set_model(value)
                else: session.configure('set_limit' if key == 'max_subagents' else 'set_idle_sleep', value)
            return session.store.settings()
        if action == 'rename':
            session.store.update_metadata(name=packet.get('name'), pinned=packet.get('pinned'))
            session.name = session.store.name
            return session.store.info()
        if action == 'compact':
            return session.compaction.cancel_request({'request_id': packet['request_id']})
        if action == 'shutdown':
            self.done.set()
            return {'status':'run','closing':True}
        if action=='delete':
            import shutil
            self._unpublish()
            identifier=session.id;workspace=str(session.directory)
            # Release the protocol lock only after deleting this owned archive.
            host_lease=session.host_lease;session.host_lease=None
            try:
                session.close()
                shutil.rmtree(self.directory)
            finally:
                host_lease.close()
                self.session=None
                self.done.set()
            return {'deleted':identifier,'workspace_preserved':workspace}
        raise ValueError('Unknown CLI action')

    def _unpublish(self):
        try:
            current=Path(self.socket).stat()
            if (current.st_dev,current.st_ino)==self.socket_identity:Path(self.socket).unlink()
        except FileNotFoundError:pass
        record=self.directory/'cli-runtime.json'
        try:
            value=json.loads(record.read_text())
            if value.get('host_id')==self.host_id:record.unlink(missing_ok=True)
        except (FileNotFoundError,ValueError):pass

    def close(self):
        with self.commands:
            if self._closed:return
            self._closed=True
            self.done.set()
            self._unpublish()
            if self.session:self.session.close();self.session=None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def resolve_agent(agents, value):
    if value in {'all', 'all_except_master', 'subs'}:
        return 'all_except_master' if value == 'subs' else value
    matches = [item['id'] for item in agents if item['id'] == value or item['name'] == value]
    if not matches:
        matches = [item['id'] for item in agents if item['id'].startswith(value)]
    if len(matches) != 1:
        raise ValueError('Agent not found or ambiguous; use its ID.')
    return matches[0]


def serve(directory, config):
    host = CliSessionHost(directory, config)
    def stop(*_): host.done.set()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        host.done.wait()
    finally:
        host.close()
        for sig, handler in previous.items(): signal.signal(sig, handler)
