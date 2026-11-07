"""Bash/SSH execution protocol only; Node owns process and Execution lifecycle."""
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

class Driver:
    def __init__(self,context,options,temporary,ports):
        self.context,self.ports,self.temporary=context,ports,temporary
        if set(options)-{'cwd','env','ssh_argv','keep_stdin_open'}:raise ValueError('Unknown Bash startup option')
        root=context['env'].get('RECODING_WORKSPACE','/workspace');cwd=options.get('cwd') or root
        if cwd=='/workspace' or cwd.startswith('/workspace/'):cwd=root+cwd[len('/workspace'):]
        self.cwd=str((Path(root)/cwd).resolve(strict=True))
        if not Path(self.cwd).is_dir():raise ValueError('cwd must be a directory')
        overrides=options.get('env',{})
        if not isinstance(overrides,dict) or any(not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',k) or not isinstance(v,str) or '\0' in v for k,v in overrides.items()):raise ValueError('Invalid Bash env')
        if sum(len(k.encode())+len(v.encode()) for k,v in overrides.items())>65536:raise ValueError('env exceeds 64 KiB')
        self.env={**context['env'],**overrides};self.ssh=options.get('ssh_argv',[])
        if any(not isinstance(a,str) or '\0' in a for a in self.ssh):raise ValueError('Invalid SSH argv')
        self.keep_stdin=options.get('keep_stdin_open',False);self.process=None;self.shell=None;self.stdin_lock=threading.RLock();self.lifecycle=threading.RLock();self.closed=False

    def start(self,deadline,cancelled):
        with self.lifecycle:
            if self.closed:raise RuntimeError('Bash process closed during creation')
            if not self.temporary:
                self.shell=ShellProcess(self.cwd,self.env,self.ssh,self.ports,max(.1,deadline-time.monotonic()),cancelled)
                self.process=self.shell.process
        return {'pid':self.process.pid if self.process else None,'cwd':self.cwd,'remote':bool(self.ssh)}

    def execute(self,code,identifier,folder,cancelled):
        if self.temporary:return self.oneshot(code,folder,cancelled)
        shell=self.shell;state=shell.begin(identifier,code,folder);sent=False
        try:
            while not state['done'].wait(.02):
                if shell.process.poll() is not None:raise RuntimeError('Bash/SSH exited before command completion')
                if cancelled.is_set() and not sent:
                    sent=True
                    if not shell.interrupt():raise RuntimeError('SSH interrupted; remote Shell state lost')
            code=state['exit_code']
            return {'exit_code':code,'interrupted':sent and code in {130,-2}}
        finally:shell.end(state)

    def oneshot(self,code,folder,cancelled):
        with open(folder/'stdout.log','ab',buffering=0) as stdout,open(folder/'stderr.log','ab',buffering=0) as stderr:
            if cancelled.is_set():return {'exit_code':130,'interrupted':True}
            with self.lifecycle:
                if self.closed:raise RuntimeError('Bash process closed before execution')
                process=self.ports['spawn'](['/bin/bash','--noprofile','--norc','-o','pipefail','-c',code],
                stdin=subprocess.PIPE if self.keep_stdin else subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                cwd=self.cwd,env=self.env,start_new_session=True,close_fds=True)
                self.process=process
            with self.stdin_lock:
                if process.stdin:os.set_blocking(process.stdin.fileno(),False)
            try:
                while process.poll() is None:
                    if cancelled.wait(.02):
                        try:os.killpg(process.pid,signal.SIGTERM)
                        except ProcessLookupError:pass
                        try:process.wait(timeout=.5)
                        except subprocess.TimeoutExpired:self.ports['kill_tree'](process)
                        break
                return {'exit_code':process.returncode,'interrupted':cancelled.is_set()}
            finally:
                self.ports['kill_tree'](process)
                with self.stdin_lock:
                    if process.stdin and not process.stdin.closed:process.stdin.close()

    def write(self,text,close_stdin):
        if not self.temporary or not self.keep_stdin:raise ValueError('stdin requires temporary async keep_stdin_open=true')
        data=text.encode()
        if len(data)>65536:raise ValueError('stdin exceeds 64 KiB')
        with self.stdin_lock:
            process=self.process
            if not process or process.poll() is not None or not process.stdin or process.stdin.closed:raise ValueError('Execution stdin is not open')
            try:written=os.write(process.stdin.fileno(),data) if data else 0
            except BlockingIOError:written=0
            closed=bool(close_stdin and written==len(data))
            if closed:process.stdin.close()
            return {'written_bytes':written,'unwritten_bytes':len(data)-written,'stdin_closed':closed}

    def close(self):
        with self.lifecycle:
            self.closed=True;shell=self.shell;process=self.process
        if shell:shell.close();return
        if process:self.ports['kill_tree'](process)
