"""Private control client. CLI never silently opens an offline archive."""

import base64
import json
from pathlib import Path
import subprocess
import sys
import time

from .control import request
from .host import resolve_agent


class Client:
    def __init__(self, directory):
        self.directory=Path(directory).resolve()
        self.host_id=None

    def call(self, action, **fields):
        if self.host_id is None:self.host_id=request(self.directory,'identity')
        result=request(self.directory,action,_host_id=self.host_id,**fields)
        if result is None:raise ValueError('此 Session 没有 CLI Host，请使用 /resume 或 chat -s 路径恢复；CLI 不提供只读浏览')
        return result

    def send(self, content, target='master', images=()):
        if len(images) > 8 or any(Path(path).stat().st_size > 5*1024*1024 for path in images):
            raise ValueError('At most 8 PNG/JPEG images, each at most 5 MiB')
        return self.call('send', content=content, to=target, images=[
            {'name': Path(path).name, 'data': base64.b64encode(Path(path).read_bytes()).decode()} for path in images])


class NoSessionClient(Client):
    """Empty TUI landing state; never reads a stopped Session archive."""
    def call(self,action,**fields):
        if action=='status':return {'status':'stop','name':'未打开 Session','settings':{},'agents':[]}
        if action=='agents':return []
        if action=='messages':return {'messages':[],'after':None,'has_more':False}
        if action=='speed':return {'samples':[],'live':False}
        if action=='python_pool':return {'processes':{},'executions':{}}
        if action=='executions':return {'agent_id':'master','executions':[]}
        if action in {'stop','shutdown'}:return {'status':'stop'}
        raise ValueError('请先使用 /resume 选择并恢复 Session')


def detached(directory, config):
    existing = request(directory, 'status')
    if existing is not None:
        return existing
    log_path = Path(directory)/'cli.log'
    with log_path.open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'recoding_cli', '_host'],
            stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
    try:
        process.stdin.write(json.dumps({'directory': str(directory), 'config': config}).encode()+b'\n')
        process.stdin.close()
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            result = request(directory, 'status')
            if result is not None:
                return result
            if process.poll() is not None:
                raise ValueError(f'CLI host failed to start; inspect {log_path}')
            time.sleep(.05)
        raise TimeoutError(f'CLI host startup timed out; inspect {log_path}')
    except BaseException:
        # Only stop the child we just created, never some other application.
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        raise


class MessageStream:
    def __init__(self, client, renderer, *, scope='human', agent=None, history=20):
        self.client, self.renderer = client, renderer
        renderer.session_directory=client.directory
        self.scope, self.agent, self.history = scope, agent, history
        self.cursor = None
        self.ready = False

    def accepts(self, item):
        return (self.scope == 'all' or item['kind'] != 'peer') and (
            not self.agent or self.agent in {item['source'], item['target']})

    def poll(self):
        self.renderer.agents = {item['id']: item for item in self.client.call('agents')}
        page = self.client.call('messages', **({'after': self.cursor or 'start', 'limit': 200} if self.ready else
            {'limit': max(1, min(200, self.history))}))
        emitted = []
        while True:
            for item in page['messages']:
                self.cursor = item['id']
                if (self.ready or self.history) and self.accepts(item):
                    self.renderer.message(item)
                    emitted.append(item)
            was_ready = self.ready
            self.ready = True
            # First read chooses the newest page; later reads drain every page.
            if not was_ready or not page['has_more']:
                break
            page = self.client.call('messages', after=self.cursor or 'start', limit=200)
        return emitted
