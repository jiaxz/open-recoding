"""Visual inspection adapter; embedded in the existing execution environment."""
from pathlib import Path

class Runtime:
    def __init__(self, context, ports):
        self.read_image = ports['read_image']

    def request(self, name, request):
        path = request['arguments']['path']
        if not path.startswith('/'):
            path = '/workspace/' + path
        return self.read_image(path)
