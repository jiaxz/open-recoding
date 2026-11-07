"""Internal, cancellable summary process; never starts an Agent or sandbox."""

import json
import os
import signal
import sys
import threading
from copy import deepcopy

from .client import Client
from .context_window import ContextBudget, ContextCompactor, SUMMARY_SYSTEM, blocks, prefix_hash
from .model_config import api_base_url, api_key
from .multimodal import context_stats
from .prompts import TOOLS
from .runtime_build import require_runtime_revision
from .storage import AgentStore, ContextHistory
from .tools import CONTEXT_TOOLS, combine_schemas



class CompressionError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def compress_snapshot(config, *, on_event=None):
    """The same no-tools summary path for background and explicit maintenance."""
    require_runtime_revision(config.get("runtime_revision"))
    model = config["model"]
    store = AgentStore(config["session_directory"], workspace=config["directory"], metadata=config["store_metadata"])
    saved = store.load_state()
    if not saved:
        raise CompressionError("Agent has no saved context to compress")
    count = config["snapshot_count"]
    messages, step = saved["messages"][:count], max(1, config["step"])
    if len(messages) != count or prefix_hash(messages, count) != config["snapshot_sha256"] or not blocks(messages):
        raise CompressionError("Requested historical prefix changed or is incomplete; no compression started")
    history = ContextHistory(store.folder/"runs", system=SUMMARY_SYSTEM, tools=[], model=model)
    current_id, retryable = None, False
    def observe(event):
        nonlocal current_id
        if event["type"] == "context_compaction_start":
            current_id = event["id"]
        value = {**event, "step": step, "run_id": config["run_id"]}
        history.record(value["type"], **{k:v for k,v in value.items() if k!="type"})
        if on_event:
            on_event(value)
    def summarize(records, selected):
        nonlocal retryable
        client = Client(api_key(), config.get("base_url", api_base_url()),
                        selected, config["api_timeout"], tools=[], max_retries=3)
        client.set_observer(lambda value: observe({"type":"context_compaction_transport", "id":current_id, **value}))
        history.record("context_compaction_request", messages=records, model=selected, tools=[])
        try:
            response = client.complete(records)
        except Exception:
            retryable = client._retryable()
            raise
        history.record("context_compaction_response", message=response, usage=client.last_usage, model=selected)
        if response.get("tool_calls"):
            raise CompressionError("Summary returned a tool call; original context retained")
        return response.get("content"), deepcopy(client.last_usage)
    try:
        tools = combine_schemas(TOOLS, CONTEXT_TOOLS, config.get("tool_schemas", []))
        budget = ContextBudget(store.folder/"context-budget.json", model=lambda:model, tools=tools,
                               limits=config.get("context_limits"))
        compactor = ContextCompactor(store.folder/"context-window.json", summarize, model=lambda:model, budget=budget,
                                     on_event=observe, checkpoint=True)
        before = compactor.state
        projection = compactor(messages, force=True, persist=False)
        if compactor.state is before or getattr(compactor,"failed",False):
            raise CompressionError(getattr(compactor,"last_error",None) or "Compression produced no smaller usable context; previous context retained",
                                   retryable=retryable)
        return {"context":context_stats(projection), "memory":compactor.state}
    finally:
        history.close()


def main():
    def interrupt(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, interrupt)
    def send(kind, **fields):
        print(json.dumps({"kind": kind, **fields}, ensure_ascii=False), flush=True)
    try:
        config = json.loads(sys.stdin.readline())
        def disconnected():
            for _ in sys.stdin:
                pass
            os.kill(os.getpid(), signal.SIGINT)
        threading.Thread(target=disconnected, daemon=True).start()
        result = compress_snapshot(config, on_event=lambda event:send("event",event=event))
        send("finished", success=True, **result)
    except KeyboardInterrupt:
        send("finished", success=False, cancelled=True, error="Compression cancelled; checkpoints retained; previous context remains active")
    except Exception as error:
        send("finished", success=False, error=str(error), retryable=bool(getattr(error,"retryable",False)))


if __name__ == "__main__":
    main()
