"""Trusted Session node bindings; models only receive the public projection."""
import re,os
from pathlib import PurePosixPath


def binding(identifier,value):
    if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identifier) or identifier=='local':raise ValueError('Invalid remote node ID')
    allowed={'name','ssh_target','workspace','python','execution_mode','ssh_config','lease_seconds'}
    if not isinstance(value,dict) or set(value)-allowed:raise ValueError('Unknown remote node configuration field')
    result={'name':identifier,'python':'/usr/bin/python3','execution_mode':'sandbox','lease_seconds':60,**value}
    if not isinstance(result['name'],str) or not 1<=len(result['name'])<=64:raise ValueError('Node name must be 1–64 characters')
    target=result.get('ssh_target')
    if not isinstance(target,str) or not target or target.startswith('-') or len(target)>255 or any(c.isspace() or ord(c)<32 for c in target):raise ValueError('Invalid SSH alias/destination')
    for key in ('workspace','python','ssh_config'):
        path=result.get(key)
        if path is None and key=='ssh_config':continue
        if not isinstance(path,str) or not path.startswith('/') or '\0' in path or '\n' in path or '..' in PurePosixPath(path).parts:raise ValueError(key+' must be an absolute path without ..')
    if result['execution_mode'] not in {'sandbox','host'}:raise ValueError('execution_mode must be sandbox or host')
    if type(result['lease_seconds']) not in (int,float) or not 10<=result['lease_seconds']<=300:raise ValueError('lease_seconds must be 10–300')
    return result


def ssh_arguments(config):
    args=['ssh','-T','-oBatchMode=yes','-oConnectTimeout=10']
    if config.get('ssh_config'):args+=['-F',config['ssh_config']]
    return [*args,'--',config['ssh_target']]


def ssh_environment():
    # Authentication/config references are needed; model credentials are not.
    keys={'PATH','HOME','USER','LOGNAME','LANG','SSH_AUTH_SOCK','SSH_AGENT_PID','XDG_RUNTIME_DIR','KRB5CCNAME'}
    return {key:value for key,value in os.environ.items() if key in keys or key.startswith('LC_')}
