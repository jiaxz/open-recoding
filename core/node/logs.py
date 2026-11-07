"""Bounded node execution log access."""
from pathlib import Path
import re,os,stat,codecs,json,base64
TERMINAL={"succeeded","failed","cancelled","timed_out","interrupted"}

def execution_directory(workspace, owner):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', owner):
        raise ValueError('Invalid execution owner')
    return Path(workspace) / '.recoding' / 'runtime' / 'executions' / owner

def open_execution_file(workspace, owner, identifier, filename):
    execution_directory(workspace, owner)
    if not re.fullmatch('[a-f0-9]{32}', identifier) or filename not in {'metadata.json','stdout.log','stderr.log'}:
        raise ValueError('Invalid execution file')
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in ('.recoding','runtime','executions',owner,identifier):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd);fd=child
        result = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result);raise ValueError('Expected a regular execution file')
        return result
    finally:
        os.close(fd)

def read_saved_execution(workspace, owner, identifier, *, stdout_offset=0, stderr_offset=0, max_bytes=16000, runtime_active=False, include_raw=False):
    if type(max_bytes) is not int or not 128 <= max_bytes <= 64000:
        raise ValueError('Invalid log read limit')
    with os.fdopen(open_execution_file(workspace,owner,identifier,'metadata.json')) as stream:
        execution=json.loads(stream.read(65536))
    if not isinstance(execution,dict) or execution.get('id') != identifier:raise ValueError('Invalid execution metadata')
    if not runtime_active and execution.get('status') not in TERMINAL:
        execution={**execution,'status':'interrupted','reason':'Runtime unavailable; command not restarted automatically'}
    result={'execution':execution}
    for name,offset in [('stdout',stdout_offset),('stderr',stderr_offset)]:
        if type(offset) is not int or offset<0:raise ValueError('Invalid log cursor')
        with os.fdopen(open_execution_file(workspace,owner,identifier,name+'.log'),'rb') as stream:
            stream.seek(offset);data=stream.read(max_bytes);more=stream.read(1)!=b''
        if include_raw:result.setdefault('_raw',{})[name]=base64.b64encode(data).decode()
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        result[name]=decoder.decode(data,final=not more)
        result[name+'_offset']=offset+len(data)-len(decoder.getstate()[0])
        result[name+'_has_more']=more
    return result


def read_output(workspace,owner,language,record,*,stdout_offset=0,stderr_offset=0,max_bytes=16000):
    if language!='python':return read_saved_execution(workspace,owner,record['id'],stdout_offset=stdout_offset,stderr_offset=stderr_offset,max_bytes=max_bytes,runtime_active=True,include_raw=True)
    if type(max_bytes) is not int or not 128<=max_bytes<=64000:raise ValueError('Invalid log request')
    for value in (owner,record['id'],record['process_id']):
        if not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('Invalid log identity')
    parent=os.open(workspace,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    result={'execution':record}
    try:
        for part in ('.recoding','runtime','python-pool',owner,record['process_id'],record['id']):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent);os.close(parent);parent=child
        for name,offset in [('stdout',stdout_offset),('stderr',stderr_offset)]:
            if type(offset) is not int or offset<0:raise ValueError('Invalid log cursor')
            fd=os.open(name+'.log',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
            with os.fdopen(fd,'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('Invalid log file')
                stream.seek(offset);data=stream.read(max_bytes);more=bool(stream.read(1))
            result.setdefault('_raw',{})[name]=base64.b64encode(data).decode()
            decoder=codecs.getincrementaldecoder('utf-8')('replace');result[name]=decoder.decode(data,final=not more)
            result[name+'_offset']=offset+len(data)-len(decoder.getstate()[0]);result[name+'_has_more']=more
        return result
    finally:os.close(parent)
