"""Persistent multi-agent runtime; importing the loop has no worker side effects."""


from .errors import AgentError
from .loop import AgentLoop

__all__ = ["Agent", "AgentLoop", "AgentError", "AgentStore", "MultiAgentSystem"]


def __getattr__(name):
    if name == "MultiAgentSystem":
        from .system import MultiAgentSystem
        return MultiAgentSystem
    if name == "Agent":
        from .agent import Agent
        return Agent
    if name == "AgentStore":
        from .storage import AgentStore
        return AgentStore
    raise AttributeError(name)
