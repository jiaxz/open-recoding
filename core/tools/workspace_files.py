"""Version-checked workspace reads/edits; executed inside the existing sandbox."""

from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import uuid

ROOT = Path(os.environ.get('RECODING_WORKSPACE','/workspace'))
MAX_FILE = 16 * 1024 * 1024
ABSENT = 'absent'


def relative_path(value):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('Expected a nonempty workspace path')
    p = PurePosixPath(value)
    if p.is_absolute():
        try: p = p.relative_to(ROOT) if p.is_relative_to(ROOT) else p.relative_to('/workspace')
        except ValueError: raise ValueError('Path must be inside /workspace') from None
    else:
        cwd = Path.cwd()
        p = PurePosixPath(cwd.relative_to(ROOT).as_posix()) / p
    if '..' in p.parts or not p.parts:
        raise ValueError('Use a file path without ..')
    return p.as_posix()


@contextmanager
def parent_fd(relative, create=False):
    parts = PurePosixPath(relative).parts
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            if create:
                try: os.mkdir(part, mode=0o755, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd); fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def digest(data):
    return ABSENT if data is None else hashlib.sha256(data).hexdigest()


def snapshot(relative):
    try:
        with parent_fd(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode): raise ValueError('Expected a regular file: ' + relative)
                if info.st_size > MAX_FILE: raise ValueError('File exceeds 16 MiB; use Python/Bash explicitly')
                data = stream.read(MAX_FILE + 1)
                if len(data) > MAX_FILE: raise ValueError('File grew beyond 16 MiB')
                return data, stat.S_IMODE(info.st_mode)
    except FileNotFoundError:
        return None, 0o644


def text(data):
    if b'\0' in data: raise ValueError('Binary file; use Python/Bash explicitly')
    return data.decode('utf-8')


def read_file(fields):
    relative = relative_path(fields['path']); data, mode = snapshot(relative)
    if data is None: raise FileNotFoundError(relative)
    source = text(data); lines = source.splitlines(keepends=True)
    start, end, maximum = fields.get('start_line', 1), fields.get('end_line', len(lines)), fields.get('max_chars', 16000)
    if type(start) is not int or type(end) is not int or start < 1 or end < start-1:
        raise ValueError('Invalid one-based line range')
    if type(maximum) is not int or not 128 <= maximum <= 64000: raise ValueError('max_chars must be 128–64000')
    chosen = ''.join(lines[start-1:end]); content = chosen[:maximum]
    returned = content.count('\n') + bool(content and not content.endswith('\n'))
    return {'ok': True, 'path': str(ROOT/relative), 'sha256': digest(data), 'bytes': len(data),
            'mode': mode, 'total_lines': len(lines), 'start_line': start, 'end_line': start+returned-1,
            'newline': 'crlf' if b'\r\n' in data and b'\n' not in data.replace(b'\r\n', b'') else 'lf',
            'content': content, 'truncated': len(chosen)>maximum,
            'next_line': start+returned-(not content.endswith('\n')) if len(chosen)>maximum else None,
            'partial_last_line': len(chosen)>maximum and not content.endswith('\n')}


def patch_text(source, patch):
    """Apply context-bearing unified hunks, without Git or fuzzy matching."""
    chunks, old, new, previous = [], None, None, None
    for line in patch.splitlines(keepends=True):
        if line.startswith('@@ '):
            if old is not None: chunks.append((''.join(old), ''.join(new)))
            if not re.match(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@', line): raise ValueError('Invalid hunk header')
            old, new, previous = [], [], None
        elif old is None:
            if not (line.startswith(('--- ', '+++ ', 'diff ', 'index ')) or not line.strip()):
                raise ValueError('Expected unified diff with @@ hunks')
        elif line.startswith('\\ No newline at end of file'):
            if previous in (' ', '-') and old: old[-1] = old[-1].rstrip('\n')
            if previous in (' ', '+') and new: new[-1] = new[-1].rstrip('\n')
        elif line and line[0] in ' +-':
            previous = line[0]
            if previous in ' -': old.append(line[1:])
            if previous in ' +': new.append(line[1:])
        else:
            raise ValueError('Unexpected line in unified hunk')
    if old is not None: chunks.append((''.join(old), ''.join(new)))
    if not chunks: raise ValueError('Patch contains no hunks')
    edits = []
    for before, after in chunks:
        if not before: raise ValueError('A hunk needs old context; use content for an empty/new file')
        if source.count(before) != 1: raise ValueError('Patch context must match exactly once; read current code and add context')
        pos = source.index(before); edits.append((pos, pos+len(before), after))
    edits.sort()
    if any(a[1] > b[0] for a,b in zip(edits, edits[1:])): raise ValueError('Patch hunks overlap')
    for start,end,after in reversed(edits): source = source[:start]+after+source[end:]
    return source


def replacement_bytes(data, change):
    source = text(data)
    crlf = b'\r\n' in data and b'\n' not in data.replace(b'\r\n', b'')
    if crlf: source = source.replace('\r\n', '\n')
    choices = [name for name in ('content', 'replacements', 'patch') if name in change]
    if len(choices) != 1: raise ValueError('Specify exactly one of content/replacements/patch')
    if choices[0] == 'content': result = change['content']
    elif choices[0] == 'patch': result = patch_text(source, change['patch'].replace('\r\n','\n'))
    else:
        result = source
        if not change['replacements']: raise ValueError('No replacements supplied')
        for item in change['replacements']:
            before,after,count = item['old'],item['new'],item.get('count',1)
            if not before or type(count) is not int or count < 1 or result.count(before) != count:
                raise ValueError('Old text occurrence count differs; read current code and use unique context')
            result = result.replace(before, after, count)
    if not isinstance(result,str): raise ValueError('New content must be text')
    if crlf: result = result.replace('\r\n','\n').replace('\n','\r\n')
    encoded = result.encode('utf-8')
    if len(encoded)>MAX_FILE: raise ValueError('New file exceeds 16 MiB')
    return encoded


def runtime_directory(name):
    relative = '.recoding/runtime/' + name + '/placeholder'
    with parent_fd(relative, create=True): pass
    return ROOT / '.recoding' / 'runtime' / name


def publish(relative, data, mode):
    with parent_fd(relative, create=data is not None) as (parent,name):
        if data is None:
            os.unlink(name, dir_fd=parent)
        else:
            fd, temporary = tempfile.mkstemp(prefix='.recoding-edit-', dir='/proc/self/fd/'+str(parent))
            try:
                with os.fdopen(fd,'wb') as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.replace(Path(temporary).name, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try: os.unlink(Path(temporary).name, dir_fd=parent)
                except FileNotFoundError: pass
        os.fsync(parent)


def edit_files(fields):
    journal_root = runtime_directory('edits')
    lock = os.open(journal_root.parent/'edits.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if fields.get('undo_id'):
            if fields.get('changes'): raise ValueError('Undo and changes are mutually exclusive')
            identifier = fields['undo_id']
            if not re.fullmatch('[a-f0-9]{32}',identifier): raise ValueError('Invalid journal ID')
            raw,_ = snapshot('.recoding/runtime/edits/'+identifier+'/manifest.json')
            previous = json.loads(raw)
            originals,targets = {},{}
            for item in previous['files']:
                relative = relative_path('/workspace/'+item['path'])
                current = snapshot(relative)
                expected = item['after'] if relative in previous['committed'] else item['before']
                if previous.get('pending') == relative and digest(current[0]) == item['after']:
                    expected = item['after']
                if digest(current[0]) != expected:
                    return {'ok':False,'error':'undo_conflict','path':relative,'expected':expected,'actual':digest(current[0])}
                data = None
                if item['before'] != ABSENT:
                    data,_=snapshot('.recoding/runtime/edits/'+identifier+'/'+item['backup'])
                    if digest(data)!=item['before']:raise ValueError('Edit backup is missing or corrupt')
                originals[relative]=current;targets[relative]=(data,item['mode'])
        else:
            changes=fields.get('changes',[])
            if not isinstance(changes,list) or not 1<=len(changes)<=32:raise ValueError('Supply 1–32 changes')
            originals,targets={},{}
            for change in changes:
                relative=relative_path(change['path'])
                if relative in targets: raise ValueError('Combine changes to a path in one entry')
                current=snapshot(relative);actual=digest(current[0]);expected=change.get('expected_sha256')
                if expected != actual:
                    return {'ok':False,'error':'edit_conflict','path':relative,'expected':expected,'actual':actual,'committed':[]}
                originals[relative]=current
                if sum(len(value[0] or b'') for value in originals.values())>32*1024*1024:
                    raise ValueError('Original edit batch exceeds 32 MiB')
                operation=change.get('operation','modify')
                if operation=='create':
                    if current[0] is not None:raise ValueError('Create requires expected_sha256=absent')
                    targets[relative]=(change['content'].encode('utf-8'),change.get('mode',0o644))
                    if len(targets[relative][0])>MAX_FILE:raise ValueError('New file exceeds 16 MiB')
                elif current[0] is None:raise FileNotFoundError(relative)
                elif operation=='modify':targets[relative]=(replacement_bytes(current[0],change),current[1])
                elif operation=='delete':targets[relative]=(None,current[1])
                elif operation=='rename':
                    destination=relative_path(change['new_path'])
                    if destination in targets or destination==relative:raise ValueError('Rename destination is repeated')
                    dest=snapshot(destination)
                    if dest[0] is not None:return {'ok':False,'error':'destination_exists','path':destination,'committed':[]}
                    originals[destination]=dest;targets[destination]=current;targets[relative]=(None,current[1])
                else:raise ValueError('Unknown edit operation')
        if sum(len(v[0] or b'') for v in targets.values())>32*1024*1024:raise ValueError('Edit batch exceeds 32 MiB')
        identifier=uuid.uuid4().hex;folder=journal_root/identifier;folder.mkdir(mode=0o700)
        manifest={'id':identifier,'created_at':time.time(),'complete':False,'committed':[],'files':[]}
        previews=[]
        for i,(relative,(data,mode)) in enumerate(targets.items()):
            before,before_mode=originals[relative];backup=str(i)+'.before'
            if before is not None:(folder/backup).write_bytes(before)
            manifest['files'].append({'path':relative,'before':digest(before),'after':digest(data),'mode':before_mode,'backup':backup})
            previews.extend(difflib.unified_diff(text(before or b'').splitlines(keepends=True),text(data or b'').splitlines(keepends=True),fromfile=relative,tofile=relative))
        def checkpoint():
            temp=folder/'manifest.tmp';temp.write_text(json.dumps(manifest,ensure_ascii=False));os.replace(temp,folder/'manifest.json')
        checkpoint()
        try:
            for relative,(data,mode) in targets.items():
                if digest(snapshot(relative)[0])!=digest(originals[relative][0]):raise ValueError('File changed during commit: '+relative)
                if data is None and originals[relative][0] is None:continue
                manifest['pending']=relative;checkpoint()
                publish(relative,data,mode);manifest['committed'].append(relative);manifest.pop('pending',None);checkpoint()
            manifest['complete']=True;checkpoint()
        except (OSError,ValueError) as error:
            manifest['error']=str(error);checkpoint()
            return {'ok':False,'error':str(error),'journal_id':identifier,'committed':manifest['committed'],'partial':True}
        preview=''.join(previews)
        return {'ok':True,'journal_id':identifier,'committed':manifest['committed'],
                'revisions':{p:digest(v[0]) for p,v in targets.items()},'diff':preview[:12000],'truncated':len(preview)>12000}
    finally:
        os.close(lock)


def emit(result):
    # Keep a complete JSON envelope inside the trusted 512 KiB transport budget.
    budget = 480*1024
    result_offset = result.pop('_result_offset',0)
    encoded = json.dumps(result,ensure_ascii=False)
    if len(encoded.encode()) > budget and 'results' in result:
        original = len(result['results']);end = result.get('next_offset')
        start = end-original if end is not None else result_offset
        while len(encoded.encode()) > budget and len(result['results']) > 1:
            count=max(1,int(len(result['results'])*budget/len(encoded.encode())*.9))
            result['results']=result['results'][:count]
            result.update(has_more=True,next_offset=start+count,response_limited=True)
            encoded=json.dumps(result,ensure_ascii=False)
    if len(encoded.encode()) > budget:
        result={key:result[key] for key in ('ok','journal_id','partial','error') if key in result}
        result.update(response_limited=True,note='Structured response exceeds budget; inspect its edit journal or narrow the requested range.')
        encoded=json.dumps(result,ensure_ascii=False)
    print(encoded)


def main():
    try:
        fields=json.loads(sys.stdin.read(2*1024*1024+1))
        result={'read_file':read_file,'edit_files':edit_files}[sys.argv[1]](fields)
    except (OSError,ValueError,KeyError,TypeError,UnicodeError) as error:
        result={'ok':False,'error':str(error)}
    emit(result)
    return 0 if result.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
