"""Read-only Pool metadata/log projection; no runtime or process startup."""
import codecs,json,os,re,stat

def read_output(store,execution,*,stdout_offset=0,stderr_offset=0,max_bytes=16000):
    if type(max_bytes) is not int or not 128<=max_bytes<=64000:raise ValueError('max_bytes must be 128–64000')
    for value in (execution['process_id'],execution['id'],store.id):
        if not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('Invalid Pool record ID')
    result={'execution':dict(execution)}
    parent=os.open(store.workspace,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        for part in ('.recoding','runtime','python-pool',store.id,execution['process_id'],execution['id']):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent);os.close(parent);parent=child
        for name,offset in [('stdout',stdout_offset),('stderr',stderr_offset)]:
            if type(offset) is not int or offset<0:raise ValueError('Invalid log cursor')
            fd=os.open(name+'.log',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
            with os.fdopen(fd,'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('Expected a regular log')
                stream.seek(offset);data=stream.read(max_bytes);more=stream.read(1)!=b''
            decoder=codecs.getincrementaldecoder('utf-8')('replace')
            result[name]=decoder.decode(data,final=not more)
            result[name+'_offset']=offset+len(data)-len(decoder.getstate()[0]);result[name+'_has_more']=more
    finally:os.close(parent)
    return result
