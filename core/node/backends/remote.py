"""Control-side remote backend; process operations use the shared NodeClient protocol."""
import json,secrets,shlex,subprocess,uuid
from node.client import NodeClient
from node.transports.ssh import SshTransport
from node.spec import ssh_arguments,ssh_environment
from node.deployment import BOOTSTRAP,deployment
from node.protocol import validate_execution_config


class RemoteClient(SshTransport,NodeClient):
    pass


class RemoteBackend:
    def __init__(self,spec,config,*,node_id,owner,sources,registry_factory):
        self.spec=spec;self.node_id=node_id;self.execution_owner=owner;self.mode=spec['execution_mode']
        self.timeout=config.get('tool_timeout',30);self.max_output=config.get('max_output',16000)
        validate_execution_config({**config,'execution_mode':self.mode})
        self.tool_workspace='/workspace' if self.mode=='sandbox' else spec['workspace']
        self.network=config.get('network',True);self.gpu_info=None;self.paths={};self.status='starting'
        self.registry=registry_factory(config.get('non_builtin_tools',()),isolated=False)
        runtime_id=uuid.uuid4().hex;self.token=secrets.token_hex(32)
        self.address='recoding-'+runtime_id
        packet={'deployment':deployment(config.get('non_builtin_tools',()),sources),
            'config':{'session_id':owner,'node_id':node_id,'runtime_id':runtime_id,'address':self.address,'token':self.token,
                'workspace':spec['workspace'],'execution_mode':self.mode,'lease_seconds':spec['lease_seconds'],
                'network':self.network,'gpu':config.get('gpu',True),'tool_timeout':self.timeout,'max_output':self.max_output}}
        command=shlex.join([spec['python'],'-I','-u','-c',BOOTSTRAP])
        result=subprocess.run([*ssh_arguments(spec),command],input=json.dumps(packet),text=True,capture_output=True,timeout=25,env=ssh_environment())
        if result.returncode:raise RuntimeError('Remote node startup failed: '+result.stderr[-3000:])
        ready=json.loads(result.stdout);self.code=ready['code']
        if ready['runtime_id']!=runtime_id:raise RuntimeError('Remote deployment identity mismatch')
        self.session=RemoteClient(self);self.session.runtime_id=runtime_id
        check=self.session.request('check')
        if check.get('stdout','').strip()!='sandbox-ok':
            self.close();raise RuntimeError(check.get('error','Remote node handshake failed'))

    def update(self,ready):
        facts=ready['facts'];self.workspace=facts['workspace'];self.network=facts['network'];self.gpu_info=facts['gpu'];self.paths=facts['paths'];self.tool_workspace=facts['tool_workspace'];self.capabilities=facts['resources']
        for name,command in ready.get('commands',{}).items():self.registry.entries[name]['command']=command

    def runtime_present(self):
        # A reachable machine and a free kernel lock prove that this Runtime ended.
        # Failure to probe remains unknown; never infer death from SSH EOF alone.
        folder=self.spec['workspace']+'/.recoding/runtime/nodes/'+self.execution_owner+'/'+self.node_id
        program=("import fcntl,json,os;from pathlib import Path; p=Path("+repr(folder)+")/'runtime.lock'; "
                 "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)\n"
                 "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);print('stopped')\n"
                 "except BlockingIOError:print('running')\n")
        command=shlex.join([self.spec['python'],'-I','-u','-c',program])
        result=subprocess.run([*ssh_arguments(self.spec),command],capture_output=True,text=True,timeout=15,env=ssh_environment())
        if result.returncode:raise RuntimeError('Remote runtime probe failed; state remains unknown')
        return result.stdout.strip()=='running'

    def close(self):
        # An observer cannot reconnect a bridge after final shutdown. Transient
        # client.close remains reconnectable; only backend.close ends lifecycle.
        with self.session.lock:
            if getattr(self.session,'stopped',False):return
            result=self.session.request('shutdown',timeout=5)
            confirmed=result.get('closed',False)
            self.session.stopped=True
            self.session.close('Session stopped')
            self.status='stopped' if confirmed else 'cleanup_pending'
