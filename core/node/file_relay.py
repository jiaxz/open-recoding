"""Bounded Host relay between two workspace endpoints; no Agent context data path."""

def copy_files(source, destination, source_path, destination_path, *, same_node=False,
               overwrite=False, cancelled=lambda: False, progress=lambda **_: None):
    def check():
        if cancelled(): raise InterruptedError('File transfer cancelled')
    check()
    manifest = source.request('manifest', path=source_path)
    target = destination.request('normalize', path=destination_path)['path']
    if same_node and (target == manifest['path'] or target.startswith(manifest['path'].rstrip('/') + '/') or not manifest['path']):
        raise ValueError('Cannot copy a source onto or inside itself')
    entries = manifest['entries']
    total_bytes = sum(item['bytes'] for item in entries)
    total_files = sum(item['kind'] == 'file' for item in entries)
    completed = transferred = 0
    progress(total_bytes=total_bytes, total_files=total_files)
    for item in entries:
        check()
        suffix = item['path']
        src = '/'.join(filter(None, (manifest['path'], suffix))) or '.'
        dst = '/'.join(filter(None, (target, suffix))) or '.'
        if item['kind'] == 'directory':
            destination.request('mkdir', path=dst); continue
        source.request('open_read', path=src, version=item['version'])
        destination.request('open_write', path=dst, overwrite=overwrite, mode=item['mode'])
        while True:
            check()
            chunk = source.request('read')
            if not chunk['data']: break
            destination.request('write', data=chunk['data'])
            # Base64 stays inside the bounded transport, never in model messages.
            count = len(chunk['data']) // 4 * 3 - (len(chunk['data']) - len(chunk['data'].rstrip('=')))
            transferred += count
            progress(bytes_transferred=transferred, current_path=src)
        verified = source.request('finish_read')
        destination.request('finish_write', **verified)
        completed += 1
        progress(files_completed=completed)
    return {'files_completed': completed, 'bytes_transferred': transferred,
            'total_files': total_files, 'total_bytes': total_bytes, 'via': 'host'}
