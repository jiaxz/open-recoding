"""Inject prebuilt mock Agents through the production team's single factory."""

from types import SimpleNamespace
from unittest.mock import Mock
import threading

from multiagent import MultiAgentSystem
from multiagent.agent import RuntimeState


def fixture_team(store, config, factory, publish, master):
    master = master or SimpleNamespace(status="stopped", closed=False, cancel_requested=False)
    def build(s, c, owner, identifier):
        runtime = master if identifier == "master" else factory(s, c, owner, identifier)
        state = {key:getattr(runtime,key,None) for key in ("status","run_id","error","sleep")}
        runtime.record = owner.record(identifier)
        runtime.record.update({key:value for key,value in state.items() if value is not None})
        runtime.start_worker = getattr(runtime,"start_worker",lambda:None)
        runtime.stop_worker = getattr(runtime,"stop_worker",lambda:None)
        runtime.request_stop = getattr(runtime,"request_stop",lambda:None)
        if not hasattr(runtime,"condition"):runtime.condition = threading.Condition()
        if not hasattr(runtime,"run_id"):runtime.run_id = None
        return runtime
    builder = Mock(side_effect=build)
    bound_to_master = getattr(publish, "__self__", None) is master
    team = MultiAgentSystem(store, config, builder, publish=None if bound_to_master else publish)
    master.event_stream = team.event_stream
    master.events = team.event_stream.events
    master.condition = team.event_stream.condition
    master.team = team
    master.publish = team.publish
    builder.reset_mock()
    return team
