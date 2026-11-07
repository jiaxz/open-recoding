"""One transfer cursor into an existing NodeRuntime, with no second SSH service."""
import uuid
from pathlib import PurePosixPath


class NodeFiles:
    def __init__(self,environment):
        self.environment=environment;self.backend=environment.ensure();self.client=self.backend.session
        self.identifier=uuid.uuid4().hex;self.closed=False

    def request(self,action,**fields):
        if self.closed:raise ValueError('File endpoint closed')
        if 'path' in fields:
            path=PurePosixPath(fields['path']);root=PurePosixPath(self.environment.store.workspace)
            if path.is_absolute() and path.is_relative_to(root):fields['path']=self.backend.tool_workspace+'/'+str(path.relative_to(root))
        result=self.client.request('files',endpoint_id=self.identifier,action=action,fields=fields,timeout=10)
        if result.get('error'):raise ValueError(result['error'])
        return result

    def close(self):
        if not self.closed:
            try:self.client.request('files',endpoint_id=self.identifier,action='close',timeout=3)
            finally:self.closed=True
