"""Persistent local/SSH Bash transport with per-command output framing (sandbox only)."""
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid


class ShellProcess:
    def __init__(self, cwd, env, ssh_argv, ports, timeout, cancelled=None):
        self.lock=threading.RLock();self.active=None;self.closed=False
        self.spawn=ports.get('spawn',subprocess.Popen);self.kill_tree=ports['kill_tree'];self.descendants=ports['descendants'];self.remote=bool(ssh_argv)
        self.prefix=('\x1eRECODING_'+uuid.uuid4().hex+':').encode()
        self.ready=set();self.ready_event=threading.Event();self.dead=set();self.diagnostics=bytearray()
        marker=self.prefix.decode()
        # stdin is the control stream. Actual submitted commands get /dev/null
        # or their own explicit redirection, never subsequent control frames.
        bootstrap=("trap 'if [[ ${__recoding_active-0} == 1 ]]; then return 130; fi' USR1\n"
                   "__recoding_eval(){ builtin eval -- \"$1\"; }\n"
                   "builtin printf '"+marker+"ready:0\x1f'\n"
                   "builtin printf '"+marker+"ready:0\x1f' >&2\n"
                   "while IFS= builtin read -r -d '' __recoding_frame; do\n"
                   "  builtin eval -- \"$__recoding_frame\" </dev/null\n"
                   "done\n")
        # Prefix is binary only in printf's format, argv itself cannot contain NUL.
        argv=([*ssh_argv, '/bin/bash --noprofile --norc -o pipefail -c '+shlex.quote(bootstrap)] if self.remote else
              ['/bin/bash','--noprofile','--norc','-o','pipefail','-c',bootstrap])
        self.process=self.spawn(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      cwd=cwd,env=env,start_new_session=True,close_fds=True)
        self.threads=[]
        for name,stream in [('stdout',self.process.stdout),('stderr',self.process.stderr)]:
            thread=threading.Thread(target=self.pump,args=(name,stream),daemon=True)
            thread.start();self.threads.append(thread)
        deadline=time.monotonic()+timeout
        try:
            while not self.ready_event.wait(.02):
                if cancelled and cancelled():raise InterruptedError("Process creation cancelled")
                if self.process.poll() is not None or time.monotonic()>=deadline:
                    raise RuntimeError('Bash/SSH startup failed: '+self.diagnostics.decode('utf-8','replace')[-2000:])
        except BaseException:self.close();raise

    def output(self,name,data):
        if not data:return
        with self.lock:
            if self.active is not None:
                stream=self.active['outputs'][name]
                stream.write(data)
            else:self.diagnostics.extend(data[:max(0,4096-len(self.diagnostics))])

    def pump(self,name,stream):
        buffer=bytearray()
        try:
            while True:
                chunk=os.read(stream.fileno(),65536)
                if not chunk:break
                buffer.extend(chunk)
                while buffer:
                    index=buffer.find(self.prefix)
                    if index<0:
                        keep=min(len(buffer),len(self.prefix)-1)
                        while keep and buffer[-keep:]!=self.prefix[:keep]:keep-=1
                        self.output(name,bytes(buffer[:-keep]) if keep else bytes(buffer))
                        if keep:buffer=buffer[-keep:]
                        else:buffer.clear()
                        break
                    if index:
                        self.output(name,bytes(buffer[:index]));del buffer[:index]
                    end=buffer.find(b'\x1f',len(self.prefix))
                    if end<0:
                        if len(buffer)>len(self.prefix)+100:
                            self.output(name,bytes(buffer[:1]));del buffer[:1];continue
                        break
                    content=bytes(buffer[len(self.prefix):end]).decode('ascii','replace')
                    identifier,_,status=content.partition(':')
                    with self.lock:
                        valid=identifier=='ready' or self.active is not None and identifier==self.active['id']
                        if valid and re.fullmatch(r'-?[0-9]+',status):
                            if identifier=='ready':
                                self.ready.add(name)
                                if len(self.ready)==2:self.ready_event.set()
                            else:
                                self.active['markers'].add(name);self.active['exit_code']=int(status)
                                if len(self.active['markers'])==2:self.active['done'].set()
                        else:self.output(name,bytes(buffer[:end+1]))
                    del buffer[:end+1]
        except (OSError,ValueError):pass
        finally:
            self.output(name,bytes(buffer))
            with self.lock:
                self.dead.add(name)
                if self.active is not None:self.active['wake'].set()

    def begin(self,identifier,code,folder):
        outputs={name:open(folder/(name+'.log'),'ab',buffering=0) for name in ('stdout','stderr')}
        with self.lock:
            if self.active is not None or self.closed or self.process.poll() is not None:
                for output in outputs.values():output.close()
                raise RuntimeError('Persistent Bash is unavailable')
            state={'id':identifier,'outputs':outputs,'markers':set(),'done':threading.Event(),
                   'wake':threading.Event(),'exit_code':None,'children':set() if self.remote else self.descendants(self.process.pid)}
            self.active=state
            marker=self.prefix.decode()+identifier+':%s\x1f'
            frame=('__recoding_active=1\n__recoding_eval '+shlex.quote(code)+' </dev/null\n'
                   '__recoding_status=$?\n__recoding_active=0\n'
                   'builtin printf '+shlex.quote(marker)+' \"$__recoding_status\"\n'
                   'builtin printf '+shlex.quote(marker)+' \"$__recoding_status\" >&2\n')
            try:
                self.process.stdin.write(frame.encode()+b'\0');self.process.stdin.flush()
            except BaseException:
                self.active=None
                for output in outputs.values():output.close()
                raise
            return state

    def end(self,state):
        if not self.remote and self.process.poll() is None:
            for pid in self.descendants(self.process.pid)-state['children']:
                try:os.kill(pid,signal.SIGKILL)
                except ProcessLookupError:pass
        with self.lock:
            if self.active is state:self.active=None
            for output in state['outputs'].values():output.close()

    def interrupt(self):
        if self.remote:
            # A non-PTY SSH channel cannot inject a reliable remote SIGINT.
            self.close()
            return False
        for pid in self.descendants(self.process.pid):
            try:os.kill(pid,signal.SIGINT)
            except ProcessLookupError:pass
        try:os.kill(self.process.pid,signal.SIGUSR1)
        except ProcessLookupError:pass
        return True

    def close(self):
        if self.closed:return
        self.closed=True
        self.kill_tree(self.process)
        for thread in self.threads:
            if thread is not threading.current_thread():thread.join(timeout=.5)
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
            if not stream.closed:stream.close()
