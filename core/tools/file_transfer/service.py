"""Bounded transfer lifecycle on the Host; only trusted file plumbing runs here."""
from contextlib import ExitStack
from copy import deepcopy
import threading
import time
import uuid

from multiagent.storage import atomic_json, read_json
from node.file_relay import copy_files

TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}


from node.spec import binding


class FileTransfers:
    def __init__(self, store, config, *, notify, observe,endpoint_factory):
        self.store, self.config, self.notify, self.observe = store, config, notify, observe
        self.endpoint_factory=endpoint_factory
        self.lock = threading.RLock(); self.closed = False
        self.cancellations = {}; self.threads = {}; self.finished = {}
        path = store.folder / 'remote-nodes.json'
        raw = read_json(path, 1024 * 1024) if path.exists() else config.get('remote_nodes', {})
        self.nodes = {key: binding(key, value) for key, value in raw.items()}
        if self.nodes: atomic_json(path, self.nodes)
        path = store.folder / 'file-transfers.json'
        self.records = read_json(path, 2 * 1024 * 1024) if path.exists() else {}
        for record in self.records.values():
            if record['status'] not in TERMINAL: record.update(status='interrupted', error='Previous Host ended; transfer was not replayed')
        self._save()

    def _save(self):
        retired = sorted((r for r in self.records.values() if r['status'] in TERMINAL), key=lambda r: r['created_at'])
        for record in retired[:-100]: self.records.pop(record['id'], None)
        atomic_json(self.store.folder / 'file-transfers.json', self.records)

    def configure(self, identifier, value):
        value = binding(identifier, value)
        with self.lock:
            if self.closed: raise ValueError('Session must Resume before configuring nodes')
            if any(r['status'] not in TERMINAL and identifier in (r['source_node'], r['destination_node']) for r in self.records.values()):
                raise ValueError('Node is in use by a transfer')
            self.nodes[identifier] = value
            atomic_json(self.store.folder / 'remote-nodes.json', self.nodes)
        return self.targets()

    def remove_node(self,identifier):
        with self.lock:
            if any(r['status'] not in TERMINAL and identifier in (r['source_node'],r['destination_node']) for r in self.records.values()):raise ValueError('Node has an active transfer')
            self.nodes.pop(identifier,None);atomic_json(self.store.folder/'remote-nodes.json',self.nodes)

    def targets(self):
        with self.lock:
            return {'nodes': [{'id': 'local', 'workspace': str(self.store.workspace), 'capabilities': ['files']},
                *[{'id': key, 'name': value['name'], 'workspace': value['workspace'], 'capabilities': ['files']}
                  for key, value in self.nodes.items()]]}

    def _record(self, source, identifier):
        value = self.records.get(identifier)
        if value is None: raise ValueError('Unknown transfer ID')
        if source != 'master' and value['source'] != source: raise ValueError('Transfer is not owned by this Agent')
        return value

    def request(self, source, fields, *, cancelled=None, network=True):
        action = fields.get('action', 'copy')
        with self.lock:
            if self.closed: raise ValueError('Session file transfers stopped')
            if action == 'nodes': return self.targets()
            if action == 'list': return {'transfers': [deepcopy(r) for r in reversed(list(self.records.values())) if source == 'master' or r['source'] == source]}
            if action in {'status', 'cancel'}:
                record = self._record(source, fields.get('transfer_id'))
                if action == 'cancel' and record['status'] not in TERMINAL:
                    self.cancellations[record['id']].set(); record['status'] = 'cancelling'; self._save()
                return {'transfer': deepcopy(record)}
            if action != 'copy': raise ValueError('Unknown transfer action')
            src = fields.get('source_node', 'local'); dst = fields.get('destination_node', 'local')
            if any(key != 'local' and key not in self.nodes for key in (src, dst)): raise ValueError('Unknown node; inspect transfer_files action=nodes')
            if not network and (src != 'local' or dst != 'local'): raise ValueError('Remote file transfer requires delegated network access')
            if not fields.get('source_path') or not fields.get('destination_path'): raise ValueError('source_path and destination_path are required')
            if sum(r['status'] not in TERMINAL for r in self.records.values()) >= 4: raise ValueError('Maximum 4 concurrent file transfers')
            identifier = uuid.uuid4().hex; stop = threading.Event()
            record = {'id': identifier, 'source': source, 'source_node': src, 'destination_node': dst,
                'source_path': fields['source_path'], 'destination_path': fields['destination_path'],
                'mode': fields.get('mode', 'sync'), 'status': 'running', 'created_at': time.time(),
                'bytes_transferred': 0, 'files_completed': 0, 'via': 'host'}
            self.records[identifier] = record; self.cancellations[identifier] = stop
            self.finished[identifier] = threading.Event(); self._save()
        args = (record, fields.get('overwrite', False), stop, cancelled)
        with self.lock:
            if self.closed: stop.set()
            if record['mode'] == 'async':
                thread = threading.Thread(target=self._run, args=args, daemon=True, name='file-transfer-' + identifier[:8])
                self.threads[identifier] = thread; thread.start()
            else: self.threads[identifier] = threading.current_thread()
        if record['mode'] != 'async': self._run(*args)
        with self.lock: result = deepcopy(record)
        return {'transfer': result, 'exit_code': 0 if result['status'] in {'running', 'succeeded'} else 130 if result['status'] == 'cancelled' else 1}

    def _run(self, record, overwrite, stop, cancelled):
        last_update = 0
        def is_cancelled(): return stop.is_set() or bool(cancelled and cancelled())
        def update(**values):
            nonlocal last_update
            with self.lock:
                record.update(values)
                if time.monotonic() - last_update < .25: return
                last_update = time.monotonic(); self._save(); snapshot = deepcopy(record)
            self.observe('transfer', snapshot)
        def endpoint(identifier):
            return self.endpoint_factory(identifier)
        try:
            if is_cancelled(): raise InterruptedError('File transfer cancelled')
            with ExitStack() as stack:
                src = endpoint(record['source_node']); stack.callback(src.close)
                if is_cancelled():raise InterruptedError('File transfer cancelled')
                dst = endpoint(record['destination_node']); stack.callback(dst.close)
                result = copy_files(src, dst, record['source_path'], record['destination_path'],
                    same_node=record['source_node'] == record['destination_node'], overwrite=overwrite,
                    cancelled=is_cancelled, progress=update)
            update(**result, status='succeeded')
        except (InterruptedError, KeyboardInterrupt): update(status='cancelled', error='File transfer cancelled; previously published files are retained')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error: update(status='cancelled' if is_cancelled() else 'failed', error=str(error))
        finally:
            with self.lock:
                record['finished_at'] = time.time(); snapshot = deepcopy(record)
                try: self._save()
                finally:
                    self.cancellations.pop(record['id'], None); self.threads.pop(record['id'], None)
                    self.finished.pop(record['id']).set()
            self.observe('transfer', snapshot)
            if record['mode'] == 'async':
                self.notify(record['source'], 'File transfer ' + record['id'] + ' ' + record['status'] + ': '
                    + record['source_node'] + ':' + record['source_path'] + ' → ' + record['destination_node'] + ':' + record['destination_path']
                    + f"; files={record['files_completed']}, bytes={record['bytes_transferred']}"
                    + ('. ' + record['error'] if record.get('error') else ''), {'event': 'file_transfer_finished', 'transfer_id': record['id']})

    def stop_owner(self, source):
        with self.lock:
            for key, stop in self.cancellations.items():
                if self.records[key]['source'] == source: stop.set()

    def reopen(self): self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            for stop in self.cancellations.values(): stop.set()
            finished = [event for key, event in self.finished.items() if self.threads.get(key) is not threading.current_thread()]
        for event in finished: event.wait()
