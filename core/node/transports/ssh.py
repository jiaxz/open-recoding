"""Reconnect an SSH bridge to the same leased NodeRuntime; never replay work."""
import json,os,selectors,shlex,signal,subprocess,time
from node.spec import ssh_arguments,ssh_environment


class SshTransport:
    def start(self,deadline,cancelled=None):
        if getattr(self,'stopped',False):raise RuntimeError('Remote NodeRuntime is closed')
        if self.process is not None and self.process.poll() is None:return
        self._close('Reconnect SSH bridge')
        backend=self.sandbox
        program='import sys;sys.path.insert(0,'+repr(backend.code)+');from node.bridge import main;main()'
        command=shlex.join([backend.spec['python'],'-I','-u','-c',program])
        self.process=subprocess.Popen([*ssh_arguments(backend.spec),command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,close_fds=True,env=ssh_environment())
        self.selector=selectors.DefaultSelector();self.buffer.clear();self.diagnostics.clear()
        for stream,name in ((self.process.stdout,'protocol'),(self.process.stderr,'diagnostics')):
            os.set_blocking(stream.fileno(),False);self.selector.register(stream,selectors.EVENT_READ,name)
        os.set_blocking(self.process.stdin.fileno(),False)
        auth={'address':backend.address,'token':backend.token,'protocol':1,'session_id':backend.execution_owner,'node_id':backend.node_id,'runtime_id':self.runtime_id}
        self.pending=memoryview(json.dumps(auth).encode()+b'\n');self.selector.register(self.process.stdin,selectors.EVENT_WRITE,'input')
        ready=self.wait(deadline,'ready',cancelled)
        expected={key:auth[key] for key in ('protocol','session_id','node_id','runtime_id')}
        if ready.get('identity')!=expected:
            self._close('Identity mismatch');raise RuntimeError('Remote runtime identity mismatch')
        self.supervisor_pid=ready['pid'];self.generation=1
        backend.update(ready);backend.status='connected'

    def is_alive(self):
        # Serialize the liveness check with reconnect: another caller must not
        # observe the deliberately empty transport halfway through a handshake.
        with self.lock:
            if getattr(self,'stopped',False):return False
            if self.process is not None and self.process.poll() is None:return True
            if time.monotonic()<getattr(self,'next_connect',0):return False
            try:
                self.start(time.monotonic()+12)
                self.next_connect=0
                return True
            except (OSError,ValueError,RuntimeError,TimeoutError) as error:
                self.next_connect=time.monotonic()+1;self.last_connect_error=str(error)
                self._close('Remote runtime disconnected');self.sandbox.status='disconnected';return False

    def abort(self):
        # A lost/partial RPC has unknown effects. Reconnect and query its existing ID.
        self._close('Remote request interrupted; no execution was replayed')
        return {'error':'Remote response interrupted; execution state is unknown. Query the existing execution ID; no code was replayed.','unknown':True,'exit_code':-1}

    def _close(self,reason):
        process,self.process=self.process,None
        if process is not None:
            try:
                process.stdin.close()
                try:process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                        process.wait()
            finally:
                for stream in (process.stdout,process.stderr):stream.close()
        if self.selector:self.selector.close();self.selector=None
        self.supervisor_pid=self.pending=self.result=None;self.buffer.clear()
        self.sandbox.status='disconnected'
