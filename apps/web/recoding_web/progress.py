"""Web request-scoped progress registry."""
from collections import OrderedDict
import re
import threading
from multiagent.events import Progress


class ProgressRegistry:
    """Bounded, request-scoped records; never store prompts, keys or history."""
    def __init__(self):
        self.lock = threading.Lock()
        self.operations = OrderedDict()

    def begin(self, identifier):
        progress = Progress()
        if identifier:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier):
                raise ValueError("Invalid progress operation ID.")
            with self.lock:
                self.operations[identifier] = progress
                while len(self.operations) > 128:
                    self.operations.popitem(last=False)
        return progress

    def get(self, identifier):
        with self.lock:
            progress = self.operations.get(identifier)
        return progress.snapshot() if progress else None
