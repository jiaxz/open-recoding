"""Minimal versioned node bundle; only trusted runtime and selected tool sources."""
import base64,hashlib,io,json
from pathlib import Path
import zipfile


def deployment(definitions,sources):
    root=Path(__file__).parent;buffer=io.BytesIO();roots={}
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*.py')):archive.writestr('node/'+path.relative_to(root).as_posix(),path.read_bytes())
        for record in definitions:
            folder=Path(record['root'])
            if str(folder) in roots:continue
            target='plugins/'+str(len(roots));roots[str(folder)]=target
            for relative,digest in record.get('files',{}).items():
                path=folder/relative
                if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('Tool source changed; restart Host')
                archive.writestr(target+'/'+relative,path.read_bytes())
    data=buffer.getvalue()
    return {'archive':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),
            'roots':roots,'definitions':definitions,'sources':sources}


# Executed by the configured remote Python; no local project installation is needed.
BOOTSTRAP=r'''
import base64,hashlib,io,json,os,socket,subprocess,sys,time,zipfile
from pathlib import Path
value=json.loads(sys.stdin.buffer.readline(32*1024*1024))
packet=value['deployment'];config=value['config']
workspace=Path(config['workspace']).expanduser().resolve(strict=True)
if not workspace.is_dir():raise ValueError('Remote workspace is not a directory')
container=workspace/'.recoding';container.mkdir(mode=0o700,exist_ok=True)
if container.is_symlink():raise ValueError('Unsafe workspace metadata directory')
data=base64.b64decode(packet.pop('archive'),validate=True)
if hashlib.sha256(data).hexdigest()!=packet['sha256']:raise ValueError('Node bundle checksum mismatch')
root=container/'runtime'/'node-code'/packet['sha256']
root.mkdir(mode=0o700,parents=True,exist_ok=True)
if any(p.is_symlink() for p in (root,*root.parents) if p!=workspace.parent):raise ValueError('Node code path contains symlinks')
with zipfile.ZipFile(io.BytesIO(data)) as archive:
 for item in archive.infolist():
  path=Path(item.filename)
  if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe node bundle member')
  target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
  if target.is_symlink() or any(p.is_symlink() for p in target.parents if p.is_relative_to(root)):raise ValueError('Unsafe node bundle target')
  content=archive.read(item)
  if target.exists() and hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(content).digest():raise ValueError('Existing node build was modified; remove the damaged build before reconnecting')
  if not target.exists():target.write_bytes(content);target.chmod(0o444)
config.update(workspace=str(workspace),code=str(root),deployment=packet)
folder=container/'runtime'/'nodes'/config['session_id']/config['node_id'];folder.mkdir(mode=0o700,parents=True,exist_ok=True)
log=open(folder/'runtime.log','ab',buffering=0)
command=[sys.executable,'-I','-u','-c',"import sys;sys.path.insert(0,"+repr(str(root))+");from node.remote_server import main;main()"]
process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=log,start_new_session=True,close_fds=True,cwd=workspace)
process.stdin.write(json.dumps(config).encode()+b'\n');process.stdin.close();log.close()
deadline=time.monotonic()+15
while True:
 if process.poll() is not None:raise RuntimeError('Remote runtime failed; inspect its workspace runtime.log')
 try:
  channel=socket.socket(socket.AF_UNIX);channel.connect('\0'+config['address']);channel.close();break
 except OSError:
  if time.monotonic()>deadline:raise TimeoutError('Remote runtime startup timed out')
  time.sleep(.05)
print(json.dumps({'code':str(root),'runtime_id':config['runtime_id']}),flush=True)
'''
