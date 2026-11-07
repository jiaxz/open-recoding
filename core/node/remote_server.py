"""Session-bound SSH runtime broker with one controller and a bounded lease."""
import fcntl,json,os,secrets,signal,socket,struct,sys,threading,time
from pathlib import Path
from .backends.sandbox import Sandbox
from .backends.host import HostExecution
from .protocol import PROTOCOL_VERSION


def main():
    config=json.loads(sys.stdin.buffer.readline(32*1024*1024))
    folder=Path(config['workspace'])/'.recoding/runtime/nodes'/config['session_id']/config['node_id']
    lock=os.open(folder/'runtime.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    fields=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
    (folder/'runtime.json').write_text(json.dumps({'pid':os.getpid(),'start':fields[19],'runtime_id':config['runtime_id']}))
    packet=config['deployment'];sources=packet['sources'];code=Path(config['code'])
    namespace={'__name__':'node_tool_registry'};exec(compile(sources['registry'],'<tool-registry>','exec'),namespace,namespace)
    definitions=[{**item,'root':str(code/packet['roots'][item['root']])} for item in packet['definitions']]
    mode=config['execution_mode'];root='/workspace' if mode=='sandbox' else config['workspace']
    from .build import pool_source
    service=pool_source(sources)
    backend=(Sandbox if mode=='sandbox' else HostExecution)(config['workspace'],config['tool_timeout'],config['max_output'],
        network=config['network'],gpu=config['gpu'],execution_owner=config['session_id'],service_source=service,
        registry=namespace['ToolRegistry'](definitions,isolated=mode=='sandbox'),image_source=sources['image'])
    backend.node_id=config['node_id'];backend.session.runtime_id=config['runtime_id']
    result=backend.session.request('check')
    if result.get('stdout','').strip()!='sandbox-ok':raise RuntimeError(result.get('error','Node check failed'))
    server=socket.socket(socket.AF_UNIX);server.bind('\0'+config['address']);server.listen(4);server.settimeout(.5)
    stopped=threading.Event();current=[None];guard=threading.Lock();last_seen=[time.monotonic()]
    identity={'protocol':PROTOCOL_VERSION,**{key:config[key] for key in ('session_id','node_id','runtime_id')}}
    ready={'type':'ready','pid':backend.session.supervisor_pid,'identity':identity,
        'facts':{'workspace':config['workspace'],'network':backend.network,'gpu':backend.gpu_info,'execution_mode':mode,
                 'paths':backend.paths,'tool_workspace':backend.tool_workspace,'resources':backend.capabilities},
        'commands':{name:entry.get('command') for name,entry in backend.registry.entries.items() if entry.get('command')}}
    def handle(channel):
        reader=None
        try:
            channel.settimeout(5)
            uid=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
            if uid!=os.getuid():return
            reader=channel.makefile('rb');auth=json.loads(reader.readline(65536))
            if not secrets.compare_digest(str(auth.get('token','')),config['token']) or any(auth.get(k)!=v for k,v in identity.items()):return
            with guard:
                old=current[0];current[0]=channel
                if old and old is not channel:
                    try:old.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                last_seen[0]=time.monotonic()
            channel.sendall(json.dumps(ready).encode()+b'\n');channel.settimeout(None)
            for line in iter(lambda:reader.readline(16*1024*1024+1),b''):
                if len(line)>16*1024*1024:raise ValueError('Node request too large')
                request=json.loads(line);last_seen[0]=time.monotonic()
                kind=request.pop('kind');identifier=request.pop('id')
                if kind=='shutdown':
                    backend.close();result={'closed':True};stopped.set()
                elif kind=='ping':result={'identity':identity}
                else:result=backend.session.request(kind,**request)
                channel.sendall(json.dumps({'type':'result','id':identifier,'result':result}).encode()+b'\n')
                if kind=='shutdown':break
        except (OSError,ValueError,KeyError):pass
        finally:
            if reader:reader.close()
            with guard:
                if current[0] is channel:current[0]=None
            channel.close()
    def stop(*_):stopped.set()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopped.is_set():
            if time.monotonic()-last_seen[0]>config['lease_seconds']:break
            try:channel,_=server.accept()
            except socket.timeout:continue
            threading.Thread(target=handle,args=(channel,),daemon=True,name='node-controller').start()
    finally:
        stopped.set();server.close()
        with guard:
            if current[0]:
                try:current[0].shutdown(socket.SHUT_RDWR)
                except OSError:pass
        backend.close();os.close(lock)


if __name__=='__main__':main()
