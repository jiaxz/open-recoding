"""Thin model-tool binding to the Node's generic Process Pool projection."""
from node.pool_client import PoolClient
from .schemas import SCHEMAS

class PythonTools(PoolClient):
    def __init__(self,store,config,*,environment=None,actor_info,notify,observe):
        self.owns_environment=environment is None
        if environment is None:
            from node.environment import ExecutionEnvironment
            from tools import execution_sources
            from tools.registry import ToolRegistry
            environment=ExecutionEnvironment(store,config,sources=execution_sources(),registry_factory=ToolRegistry)
        super().__init__(store,config,language='python',schemas=SCHEMAS,environment=environment,actor_info=actor_info,notify=notify,observe=observe)

    def reopen(self):
        super().reopen()
        if self.owns_environment:self.environment.reopen()

    def close(self):
        super().close()
        if self.owns_environment:
            self.environment.close();self._lost('NodeRuntime closed',closing=True)
