"""One active event subscription per browser page, independent of Session lifetime."""

from dataclasses import dataclass, field
import socket
import threading


@dataclass(eq=False)
class Subscription:
    page: str
    connection: object
    token: int
    cancelled: threading.Event = field(default_factory=threading.Event)


class PageStreams:
    def __init__(self, status):
        self.status = status
        self.lock = threading.Lock()
        self.pages = {}

    def _stop(self, item, reason):
        item.cancelled.set()
        self.status.disconnect(item.token, reason)
        # Closing EventSource does not guarantee a reverse proxy closes its
        # upstream socket. Actively unblock writes to the previous subscription.
        try:
            item.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def open(self, page, session, peer, connection):
        with self.lock:
            previous = self.pages.pop(page, None)
            if previous:
                self._stop(previous, "page subscription replaced")
            item = Subscription(page, connection, self.status.connect(page, session, peer))
            self.pages[page] = item
            return item

    def close(self, item, reason):
        with self.lock:
            if self.pages.get(item.page) is item:
                self.pages.pop(item.page)
            if not item.cancelled.is_set():
                self._stop(item, reason)

    def close_page(self, page, reason):
        with self.lock:
            item=self.pages.pop(page,None)
            if item:self._stop(item,reason)

    def close_all(self):
        with self.lock:
            for item in self.pages.values():
                self._stop(item, "server stopped")
            self.pages.clear()
