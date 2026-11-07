"""Small, thread-safe terminal status log for the Web listener and SSE streams."""

from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from zoneinfo import ZoneInfo

from session.store import SESSION_HOME, protect_directory


class WebStatus:
    def __init__(self, root, instance):
        self.lock = threading.Lock()
        self.streams = {}
        self.seen = OrderedDict()
        self.counter = 0
        self.listening = False
        self.instance = instance
        self.records = queue.Queue(maxsize=8192)
        self.record_lock = threading.Lock()
        self.record_number = 0
        self.dropped = 0
        self.closed = False
        self.connections = {}
        container = Path(root) / SESSION_HOME
        protect_directory(container)
        directory = container / "server-logs"
        protect_directory(directory)
        suffix = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S_%f")
        self.log_path = directory / f"server_{suffix}_{os.getpid()}_{instance}.jsonl"
        fd = os.open(self.log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self.writer = threading.Thread(target=self._write_records, daemon=True, name="web-server-log")
        self.writer.start()

    def _write_records(self):
        try:
            while True:
                try:
                    record = self.records.get(timeout=.5)
                except queue.Empty:
                    if self.closed:
                        break
                    continue
                if record is None:
                    break
                self.file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as error:
            print(f"Server log write failed: {type(error).__name__}", flush=True)
        finally:
            self.file.close()

    def record(self, event, **fields):
        # No disk writes or waiting on a full queue in HTTP worker threads.
        with self.record_lock:
            if self.closed:
                return
            self.record_number += 1
            value = {"timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds"),
                     "sequence": self.record_number, "event": event, "pid": os.getpid(),
                     "instance": self.instance, **fields}
            if self.dropped:
                value["dropped_before"] = self.dropped
            try:
                self.records.put_nowait(value)
            except queue.Full:
                self.dropped += 1
            else:
                self.dropped = 0

    def accepted(self, connection, peer):
        identifier = os.urandom(6).hex()
        with self.lock:
            self.connections[connection.fileno()] = (identifier, time.monotonic())
        self.record("TCP_ACCEPT", connection=identifier, peer=peer)
        return identifier

    def connection_info(self, connection):
        with self.lock:
            return self.connections.get(connection.fileno(), (None, time.monotonic()))

    def connection_closed(self, connection):
        with self.lock:
            value = self.connections.pop(connection.fileno(), None)
        if value:
            self.record("TCP_CLOSE", connection=value[0], elapsed_ms=round((time.monotonic()-value[1])*1000, 3))

    def _log(self, event, **fields):
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
        values = " · ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        print(f"[{stamp}] {event} · {values}", flush=True)
        self.record(event, **fields)

    def listen(self, address, instance, root):
        with self.lock:
            self.listening = True
            self._log("LISTENING", url=f"http://{address[0]}:{address[1]}", pid=os.getpid(),
                      instance=instance, workspace=str(root), streams=0, log=str(self.log_path))

    def page_loaded(self, peer):
        with self.lock:
            self._log("PAGE LOAD", peer=peer, streams=len(self.streams))

    def connect(self, page, session, peer):
        # Page IDs come from the browser; validate before using them in logs.
        page = page if isinstance(page, str) and re.fullmatch(r"[a-f0-9-]{36}", page) else None
        with self.lock:
            self.counter += 1
            token = self.counter
            previous = self.seen.get(page) if page else None
            event = "WEB SWITCH" if previous and previous != session else "WEB RECONNECT" if previous else "WEB CONNECT"
            if page:
                self.seen[page] = session
                self.seen.move_to_end(page)
                while len(self.seen) > 256:
                    self.seen.popitem(last=False)
            self.streams[token] = (page, session, time.monotonic())
            self._log(event, page=page[:8] if page else "unknown", session=str(session)[:12],
                      peer=peer, streams=len(self.streams),page_streams=sum(item[0]==page for item in self.streams.values()),stream_scope='all_pages')
            return token

    def disconnect(self, token, reason):
        with self.lock:
            connection = self.streams.pop(token, None)
            if connection:
                page, session, started = connection
                self._log("WEB DISCONNECT", page=page[:8] if page else "unknown", session=str(session)[:12],
                          reason=reason, seconds=round(time.monotonic()-started, 2), streams=len(self.streams),
                          page_streams=sum(item[0]==page for item in self.streams.values()),stream_scope='all_pages')

    def stopped(self):
        with self.lock:
            if self.listening:
                self.listening = False
                self._log("LISTENER STOPPED", streams=len(self.streams))
        with self.record_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.records.put(None, timeout=.5)
        except queue.Full:
            pass
        self.writer.join(timeout=2)
