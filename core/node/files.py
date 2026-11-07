"""Restricted workspace file endpoint, also deployable over an SSH stdin channel."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import sys
import uuid

CHUNK = 256 * 1024
MAX_FRAME = 4 * 1024 * 1024


class WorkspaceFiles:
    def __init__(self, workspace, private_files=()):
        self.workspace = str(Path(workspace).expanduser().resolve(strict=True))
        self.root = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.private = {str(Path(path).resolve()) for path in private_files}
        self.reader = self.writer = None

    def parts(self, path):
        if not isinstance(path, str) or not path or '\0' in path or len(path) > 4096:
            raise ValueError('Invalid workspace path')
        value = PurePosixPath(path)
        if value.is_absolute():
            root = PurePosixPath(self.workspace)
            if value.is_relative_to(root): value = value.relative_to(root)
            elif value.is_relative_to('/workspace'): value = value.relative_to('/workspace')
            else: raise ValueError('Path is outside the node workspace')
        parts = value.parts
        if '..' in parts or any(part == '.env' or part.startswith('.env.') for part in parts):
            raise ValueError('Private or escaping workspace path')
        if '.recoding' in parts and not (parts[:2] == ('.recoding', 'inputs') and '.recoding' not in parts[1:]):
            raise ValueError('Session/runtime data cannot be transferred')
        if str(Path(self.workspace, *parts)) in self.private:
            raise ValueError('Private file cannot be transferred')
        return parts

    def parent(self, path, *, create=False):
        parts = self.parts(path)
        if not parts: raise ValueError('A file path is required')
        fd = os.dup(self.root)
        try:
            for part in parts[:-1]:
                if create:
                    try: os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError: pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd); fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd); raise

    @staticmethod
    def version(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def manifest(self, path):
        parts = self.parts(path)
        entries = []; size = 0
        def visit(parent, name, relative, full):
            nonlocal size
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode): raise ValueError('Symlinks are not transferable')
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and not stat.S_ISREG(info.st_mode): raise ValueError('Only regular files/directories are transferable')
            entry = {'path': relative, 'kind': 'directory' if directory else 'file',
                     'bytes': 0 if directory else info.st_size, 'mode': stat.S_IMODE(info.st_mode) & 0o777,
                     'version': self.version(info)}
            size += len(json.dumps(entry).encode())
            if len(entries) >= 10000 or size > 2 * 1024 * 1024: raise ValueError('Transfer manifest exceeds 10,000 entries or 2 MiB; narrow the source')
            entries.append(entry)
            if directory:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                try:
                    for child in sorted(os.listdir(fd)):
                        child_full = (*full, child)
                        try: self.parts('/'.join(child_full))
                        except ValueError: continue  # Exclude private archives/credentials from directory copies.
                        visit(fd, child, '/'.join(filter(None, (relative, child))), child_full)
                finally: os.close(fd)
        if parts:
            fd, name = self.parent(path)
        else: fd, name = os.dup(self.root), '.'
        try: visit(fd, name, '', parts)
        finally: os.close(fd)
        return {'path': '/'.join(parts), 'entries': entries}

    def request(self, action, **fields):
        if action == 'normalize': return {'path': '/'.join(self.parts(fields['path']))}
        if action == 'manifest': return self.manifest(fields['path'])
        if action == 'mkdir':
            if not self.parts(fields['path']): return {}
            fd, name = self.parent(fields['path'], create=True)
            try:
                try: os.mkdir(name, mode=0o755, dir_fd=fd)
                except FileExistsError: pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(child)
            finally: os.close(fd)
            return {}
        if action == 'open_read':
            if self.reader is not None: raise ValueError('Reader already open')
            parent, name = self.parent(fields['path'])
            try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            finally: os.close(parent)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or self.version(info) != fields['version']:
                os.close(fd); raise ValueError('Source changed since manifest')
            self.reader = [fd, fields['version'], hashlib.sha256(), 0]
            return {}
        if action == 'read':
            fd, version, digest, total = self.reader
            data = os.read(fd, CHUNK)
            digest.update(data); self.reader[3] += len(data)
            return {'data': base64.b64encode(data).decode()}
        if action == 'finish_read':
            fd, version, digest, total = self.reader
            valid = self.version(os.fstat(fd)) == version and total == version[2]
            os.close(fd); self.reader = None
            if not valid: raise ValueError('Source changed during transfer; destination was not published')
            return {'sha256': digest.hexdigest(), 'bytes': total}
        if action == 'open_write':
            if self.writer is not None: raise ValueError('Writer already open')
            parent, name = self.parent(fields['path'], create=True)
            temporary = 'recoding-transfer-' + uuid.uuid4().hex + '.part'
            try:
                self.check_target(parent, name, fields['overwrite'])
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            except BaseException: os.close(parent); raise
            self.writer = [fd, parent, temporary, name, fields['overwrite'], hashlib.sha256(), 0, fields.get('mode', 0o644)]
            return {}
        if action == 'write':
            data = base64.b64decode(fields['data'], validate=True)
            if len(data) > CHUNK: raise ValueError('Oversized transfer chunk')
            view = memoryview(data)
            while view:
                count = os.write(self.writer[0], view); view = view[count:]
            self.writer[5].update(data); self.writer[6] += len(data)
            return {}
        if action == 'finish_write':
            fd, parent, temporary, name, overwrite, digest, total, mode = self.writer
            if digest.hexdigest() != fields['sha256'] or total != fields['bytes']: raise ValueError('Transfer checksum/size mismatch')
            os.fchmod(fd, mode & 0o777); os.fsync(fd)
            self.check_target(parent, name, overwrite)
            if overwrite: os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(temporary, dir_fd=parent)
            os.close(fd); os.close(parent); self.writer = None
            return {'bytes': total, 'sha256': digest.hexdigest()}
        raise ValueError('Unknown file endpoint action')

    @staticmethod
    def check_target(parent, name, overwrite):
        try: info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError: return
        if not stat.S_ISREG(info.st_mode): raise ValueError('Destination is not a regular file')
        if not overwrite: raise FileExistsError('Destination exists; set overwrite=true explicitly')

    def close(self):
        if self.reader is not None:
            os.close(self.reader[0]); self.reader = None
        if self.writer is not None:
            fd, parent, temporary, *_ = self.writer
            try: os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError: pass
            finally: os.close(fd); os.close(parent); self.writer = None
        if self.root is not None: os.close(self.root); self.root = None


def main():
    endpoint = None
    def terminate(*_): raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        config = json.loads(sys.stdin.buffer.readline(MAX_FRAME + 1))
        endpoint = WorkspaceFiles(config['workspace'])
        print(json.dumps({'ready': True, 'workspace': endpoint.workspace}), flush=True)
        while True:
            line = sys.stdin.buffer.readline(MAX_FRAME + 1)
            if not line: break
            if len(line) > MAX_FRAME: raise ValueError('Oversized file frame')
            request = json.loads(line)
            try: result = endpoint.request(request['action'], **request.get('fields', {}))
            except (OSError, ValueError, TypeError, KeyError) as error: result = {'error': str(error)}
            print(json.dumps(result), flush=True)
    finally:
        if endpoint: endpoint.close()


if __name__ == '__main__': main()
