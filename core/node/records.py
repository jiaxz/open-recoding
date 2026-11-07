"""Detached process/execution metadata, never Agent context."""
from .storage import read_json
import re
TERMINAL={"succeeded","failed","cancelled","timed_out","interrupted"}

def browse_pool(store, kind):
    """Read detached pool metadata without creating processes or writing state."""
    if not isinstance(kind,str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}',kind):
        raise ValueError('Unknown execution pool')
    path = store.folder / (kind + '-pool.json')
    data = read_json(path, 8 * 1024 * 1024) if path.exists() else {'version': 1, 'processes': {}, 'executions': {}}
    if data.get('version') != 1:
        raise ValueError('Unsupported pool records')
    for record in data['processes'].values():
        record.update(status='closed', active_execution=None, queued=0)
    for record in data['executions'].values():
        if record['status'] not in TERMINAL:
            record.update(status='interrupted', reason='Previous pool runtime ended; not replayed')
    return data
