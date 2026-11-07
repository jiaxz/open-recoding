"""Control-side bounded log projection; stopped browsing never starts a node."""
import base64,codecs,json,os,re
from pathlib import Path
from .storage import atomic_json


def save(folder,record,result,raw,options):
    identifier=record['id']
    if not re.fullmatch('[a-f0-9]{32}',identifier):raise ValueError('Invalid execution ID')
    target=Path(folder)/'node-logs'/identifier;target.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name in ('stdout','stderr'):
        if name not in raw:continue
        path=target/(name+'.log');offset=options.get(name+'_offset',0)
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        with os.fdopen(fd,'r+b') as stream:
            stream.seek(0,2)
            if offset<=stream.tell():stream.seek(offset);stream.write(base64.b64decode(raw[name],validate=True))
    atomic_json(target/'metadata.json',{'execution':record,'remote_more_available':any(result.get(name+'_has_more') for name in ('stdout','stderr'))})


def read(folder,record,*,stdout_offset=0,stderr_offset=0,max_bytes=16000):
    if type(max_bytes) is not int or not 128<=max_bytes<=64000:raise ValueError('Invalid log limit')
    target=Path(folder)/'node-logs'/record['id']
    result={'execution':record,'cache_only':True,'remote_more_available':True}
    if (target/'metadata.json').exists():
        result['remote_more_available']=json.loads((target/'metadata.json').read_text())['remote_more_available']
    for name,offset in [('stdout',stdout_offset),('stderr',stderr_offset)]:
        if type(offset) is not int or offset<0:raise ValueError('Invalid log cursor')
        path=target/(name+'.log')
        if path.exists():
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            with os.fdopen(fd,'rb') as stream:stream.seek(offset);data=stream.read(max_bytes);more=bool(stream.read(1))
        else:data=b'';more=False
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        result[name]=decoder.decode(data,final=not more and not result['remote_more_available'])
        result[name+'_offset']=offset+len(data)-len(decoder.getstate()[0]);result[name+'_has_more']=more
    return result
