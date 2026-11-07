"""Loopback-only real OpenSSH fixture with generated disposable keys."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import socket
import subprocess
import time
from types import SimpleNamespace


def descendants(pid):
    pending=[pid];found=set()
    while pending:
        parent=pending.pop()
        try:
            for task in Path('/proc',str(parent),'task').iterdir():
                for child in map(int,(task/'children').read_text().split()):
                    if child not in found:found.add(child);pending.append(child)
        except OSError:pass
    return found


@contextmanager
def local_ssh(directory):
    executable=shutil.which('sshd') or '/usr/sbin/sshd'
    if not Path(executable).is_file():raise RuntimeError('SSH integration requires OpenSSH sshd')
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    remote=root/'remote';remote.mkdir();(remote/'nested').mkdir()
    sessions=root/'remote-sessions';sessions.mkdir()
    for name in ['host-key','client-key']:
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/name)],check=True,capture_output=True)
    (root/'authorized_keys').write_text((root/'client-key.pub').read_text())
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    user=pwd.getpwuid(os.getuid()).pw_name
    force=root/'force.py'
    force.write_text('import json,os\nfrom pathlib import Path\n'
        +'pid=os.getpid(); fields=Path("/proc/self/stat").read_text().rsplit(")",1)[1].split()\n'
        +'Path('+repr(str(sessions)) +',str(pid)).write_text(json.dumps({"pid":pid,"start":fields[19]}))\n'
        +'os.environ["RECODING_HOME"]='+repr(str(root/'.recoding'))+'\n'
        +'os.chdir('+repr(str(remote))+')\n'
        +'os.execv("/bin/bash",["/bin/bash","-c",os.environ["SSH_ORIGINAL_COMMAND"]])\n')
    import sys
    config=root/'sshd.conf'
    config.write_text('\n'.join([
        'Port '+str(port),'ListenAddress 127.0.0.1','HostKey '+str(root/'host-key'),
        'PidFile '+str(root/'sshd.pid'),'AuthorizedKeysFile '+str(root/'authorized_keys'),
        'PasswordAuthentication no','KbdInteractiveAuthentication no','UsePAM no','StrictModes no',
        'UseDNS no','AllowUsers '+user,'LogLevel VERBOSE',
        'ForceCommand '+shlex.quote(sys.executable)+' '+shlex.quote(str(force)), '']))
    pub=(root/'host-key.pub').read_text().split()
    (root/'known_hosts').write_text('[127.0.0.1]:'+str(port)+' '+pub[0]+' '+pub[1]+'\n')
    log=open(root/'sshd.log','wb',buffering=0)
    server=subprocess.Popen([executable,'-D','-e','-f',str(config)],stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,stderr=log,start_new_session=True,close_fds=True)
    ssh_config=root/'ssh_config'
    ssh_config.write_text('\n'.join(['Host recoding-test-node','  HostName 127.0.0.1','  User '+user,
        '  Port '+str(port),'  IdentityFile '+str(root/'client-key'),'  IdentitiesOnly yes',
        '  StrictHostKeyChecking yes','  UserKnownHostsFile '+str(root/'known_hosts'),'']))
    fixture=SimpleNamespace(root=root,remote=remote,port=port,user=user,log=root/'sshd.log',ssh_config=ssh_config,alias='recoding-test-node')
    try:
        deadline=time.monotonic()+5
        while True:
            if server.poll() is not None:raise RuntimeError('Test sshd exited: '+fixture.log.read_text())
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1):break
            except OSError:
                if time.monotonic()>deadline:raise RuntimeError('Test sshd did not start')
                time.sleep(.02)
        yield fixture
    finally:
        tracked=descendants(server.pid)
        records=[*sessions.iterdir(),*(remote/'.recoding/runtime/nodes').rglob('runtime.json')]
        for entry in records:
            try:
                record=json.loads(entry.read_text());pid=record['pid']
                fields=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
                if fields[19]==record['start']:tracked.add(pid);tracked.update(descendants(pid))
            except (OSError,ValueError,KeyError):pass
        for pid in tracked:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if server.poll() is None:server.terminate()
        try:server.wait(timeout=2)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        log.close()
