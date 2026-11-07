"""Automatic Session maintenance: background snapshots, no Agent/runtime interruption."""

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from multiagent.context_window import prefix_hash, blocks, ContextCompactor, MAX_SUMMARY_CHARS
from multiagent.runtime_build import RUNTIME_REVISION, require_runtime_revision
from multiagent.storage import atomic_json, read_json
from multiagent.storage import MemberStore

ACTIVE = {"queued", "running", "cancelling"}


class SessionCompaction:
    """Serialize summary work across a Session, independently of its live loops."""
    def __init__(self, session):
        self.session = session
        self.lock = threading.RLock()
        self.operations = {}
        self.thread = self.process = None
        self.closed = False
        self.path = session.store.folder / "context-maintenance.json"
        try:
            saved = read_json(self.path, 262144) if self.path.exists() else {}
            if not isinstance(saved, dict) or not isinstance(saved.get("agents", []), list):
                saved = {}
        except (OSError, ValueError):
            saved = {}  # A progress cache must never prevent opening the archive.
        for item in saved.get("agents", []):
            if isinstance(item, dict) and isinstance(item.get("agent_id"), str) and isinstance(item.get("request_id"), str):
                if item.get("status") in ACTIVE:
                    item.update(status="cancelled", error="Host exited before compression completed")
                self.operations[item["agent_id"]] = item

    def snapshot(self):
        with self.lock:
            return [{key:deepcopy(value) for key,value in operation.items() if not key.startswith("_")} for operation in self.operations.values()]

    @property
    def active(self):
        with self.lock:
            return any(operation["status"] in ACTIVE for operation in self.operations.values())

    def _publish(self, operation, status, **fields):
        operation.update(status=status, updated_at=time.time(), **fields)
        atomic_json(self.path, {"version":1, "agents":self.snapshot()})
        self.session.publish({"type":"context_compaction_session", "operation":next(
            item for item in self.snapshot() if item["agent_id"] == operation["agent_id"])})

    def request_from_agent(self, source, fields, *, cancelled=None):
        team = self.session.team
        required = {"model", "estimated_input_tokens", "trigger_tokens", "context_window_tokens", "output_reserve_tokens",
                    "estimate_method", "profile_source", "last_reported_input_tokens", "messages", "images", "step",
                    "snapshot_count", "snapshot_sha256"}
        allowed = required | {"calibrated", "local_estimated_tokens", "image_estimated_tokens", "tools_estimated_tokens"}
        if (not required <= set(fields) <= allowed or not isinstance(fields["model"], str)
                or not isinstance(fields["snapshot_sha256"], str) or len(fields["snapshot_sha256"]) != 64
                or any(type(fields[key]) is not int or fields[key] < 0 for key in
                       ("estimated_input_tokens", "trigger_tokens", "context_window_tokens", "output_reserve_tokens",
                        "messages", "images", "step", "snapshot_count"))):
            raise ValueError("Invalid context budget request")
        with team.condition:
            team.assert_accepting(source)
            runtime = team.agents.get(source)
            request = {**deepcopy(fields), "agent_id": source,
                       "name": team.record(source)["name"], "run_id": runtime.run_id if runtime else None}
        if cancelled and cancelled():
            raise ValueError("Context maintenance request cancelled by Stop")
        return self.request(request)

    def request(self, request):
        with self.lock:
            if self.closed or self.session.closed:
                return {"status":"unavailable"}
            previous = self.operations.get(request["agent_id"])
            if previous:
                if previous["status"] in ACTIVE:
                    return {"request_id":previous["request_id"], "status":previous["status"]}
                if previous.get("model") == request["model"]:
                    if previous["status"] == "failed" and previous.get("retryable"):
                        if time.time() < previous.get("retry_at", 0):
                            return {"status":"cooldown", "retry_at":previous["retry_at"]}
                    # A successful summary changes the budget baseline. Its
                    # pre-summary (possibly uncalibrated) estimate cannot gate
                    # the next above-threshold request from the live projection.
                    elif previous["status"] != "succeeded" and request["estimated_input_tokens"] < previous.get("retry_tokens", 0):
                        return {"status":"cooldown"}
            require_runtime_revision(RUNTIME_REVISION)
            operation = {**deepcopy(request), "request_id":uuid.uuid4().hex, "created_at":time.time(),
                   "retry_tokens":request["estimated_input_tokens"]+max(1024,request["context_window_tokens"]//20),
                   "failure_count":previous.get("failure_count",0) if previous and previous.get("status")=="failed" else 0,
                   "_cancel":threading.Event()}
            self.operations[request["agent_id"]] = operation
            self._publish(operation, "queued", error=None)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True, name="session-context-maintenance")
                self.thread.start()
            return {"request_id":operation["request_id"], "status":operation["status"]}

    def cancel(self, request_id=None):
        with self.lock:
            changed = False
            for operation in self.operations.values():
                if operation["status"] not in ACTIVE or request_id is not None and operation["request_id"] != request_id:
                    continue
                changed = True
                operation["_cancel"].set()
                running = operation["status"] in {"running", "cancelling"}
                self._publish(operation,"cancelling" if running else "cancelled")
                if running and self.process and self.process.poll() is None:
                    try:os.kill(self.process.pid, signal.SIGINT)
                    except ProcessLookupError:pass
            return changed

    def cancel_request(self, fields):
        if not isinstance(fields,dict) or set(fields)!={"request_id"} or not isinstance(fields["request_id"],str):
            raise ValueError("A compression request_id is required")
        return {"request_id":fields["request_id"], "cancelled":self.cancel(fields["request_id"])}

    def _work(self):
        while True:
            with self.lock:
                operation = next((operation for operation in self.operations.values() if operation["status"] == "queued"), None)
                if not operation or self.closed:
                    self.thread = None
                    return
                self._publish(operation,"running")
            self._run(operation)

    def _run(self, operation):
        session, team = self.session, self.session.team
        process = None
        outcome = None
        identifier = operation["agent_id"]
        try:
            store = session.store if identifier == "master" else MemberStore(session.store, identifier)
            runtime = team.agents.get(identifier)
            if not runtime:
                raise ValueError("Agent runtime is no longer available")
            with self.lock:
                if operation["_cancel"].is_set() or self.closed:
                    raise InterruptedError("Compression cancelled")
                require_runtime_revision(RUNTIME_REVISION)
                process = self.process = subprocess.Popen([sys.executable,"-u","-m","multiagent.context_maintenance"],
                    cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", start_new_session=True)
                process.stdin.write(json.dumps({**session.config, "runtime_revision":RUNTIME_REVISION,
                    "session_directory":str(store.folder), "directory":str(store.workspace), "store_metadata":store.metadata,
                    "tool_schemas":self.session.team.agents[identifier].tool_schemas, "agent_id":identifier, "model":operation["model"],
                    "run_id":operation.get("run_id") or "context-"+operation["request_id"], "snapshot_count":operation["snapshot_count"],
                    "snapshot_sha256":operation["snapshot_sha256"], "step":operation["step"]})+"\n")
                process.stdin.flush()
            outcome = None
            for line in process.stdout:
                packet = json.loads(line)
                if packet["kind"] == "event":
                    current_runtime = team.agents.get(identifier)
                    if current_runtime is not runtime or runtime.closed:
                        raise InterruptedError("Agent runtime changed; stale maintenance events discarded")
                    runtime.publish(packet["event"])
                elif packet["kind"] == "finished":
                    outcome = packet
            process.wait(timeout=5)
            with self.lock:
                if operation["_cancel"].is_set() or self.closed or outcome and outcome.get("cancelled"):
                    raise InterruptedError("Compression cancelled; Agent continues with its previous context")
                if not outcome or not outcome.get("success"):
                    raise ValueError((outcome or {}).get("error", "Compression worker exited without a result"))
                memory = outcome["memory"]
                current = store.load_state()["messages"]
                cut = memory["prefix_count"]
                if (cut > operation["snapshot_count"] or not blocks(current[:cut])
                        or prefix_hash(current, cut) != memory["prefix_sha256"]):
                    raise ValueError("Historical prefix changed; compression candidate discarded")
                # Append-only live work is intentionally accepted. The Agent
                # reloads this atomic projection before its next Model call.
                atomic_json(store.folder/"context-window.json", memory)
                self._publish(operation,"succeeded",context=outcome["context"],generation=memory["generation"],failure_count=0)
        except Exception as error:
            with self.lock:
                retryable = bool(outcome and outcome.get("retryable")) and not operation["_cancel"].is_set()
                failures = operation.get("failure_count",0)+1
                self._publish(operation,"cancelled" if operation["_cancel"].is_set() else "failed",error=str(error),
                    retryable=retryable, failure_count=failures,
                    retry_at=time.time()+min(30*2**min(failures-1,4),300) if retryable else None)
        finally:
            if process:
                if process.poll() is None:
                    process.kill();process.wait(timeout=5)
                for stream in (process.stdin,process.stdout):stream.close()
            with self.lock:
                self.process = None

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel()
            thread = self.thread
        if thread:
            thread.join(timeout=6)
            if thread.is_alive() and self.process and self.process.poll() is None:
                self.process.kill()
                thread.join(timeout=6)


def recover_summary_checkpoint(store, journal, operation):
    """Recover a paid successful prefix from a prior run after verifying its snapshot."""
    journal = Path(journal).resolve(strict=True)
    if not journal.is_relative_to((store.folder/"runs").resolve()) or journal.suffix != ".jsonl":
        raise ValueError("Recovery journal must belong to the selected Agent")
    messages = store.load_state()["messages"]
    count = operation["snapshot_count"]
    if count > len(messages) or prefix_hash(messages, count) != operation["snapshot_sha256"]:
        raise ValueError("Original compression snapshot changed; journal cannot be reused")
    found = None
    with journal.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (record.get("type") == "context_compaction_end" and record.get("run_id") == operation.get("run_id")
                    and record.get("model") == operation["model"] and record.get("step") == operation["step"]):
                end, summary = record.get("prefix_count"), record.get("summary")
                if (type(end) is int and 1 < end <= count and end in {stop for _, stop in blocks(messages)}
                        and isinstance(summary,str) and 0 < len(summary) <= MAX_SUMMARY_CHARS):
                    found = record
    if not found:
        raise ValueError("No verified successful summary exists in this journal")
    compact = ContextCompactor(store.folder/"context-window.json",None,model=lambda:operation["model"],checkpoint=True)
    end = found["prefix_count"]
    memory = {"version":1, "prefix_count":end, "prefix_sha256":prefix_hash(messages,end),
              "generation":found["generation"], "summary":found["summary"], "model":found["model"], "created_at":time.time()}
    current = compact.state
    if current and current["prefix_count"] >= end:
        return {"prefix_count":current["prefix_count"], "already_committed":True}
    compact.resume_checkpoint(messages, len(messages))
    if compact.state is not current and compact.state["prefix_count"] >= end:
        return {"prefix_count":compact.state["prefix_count"], "generation":compact.state["generation"], "reused_checkpoint":True}
    compact.state = memory
    projection = compact.view(messages)
    from multiagent.context_window import token_units
    memory["saved_tokens_estimate"] = max(0,token_units(messages)-token_units(projection))
    atomic_json(compact.checkpoint_path,{"version":1,"base":compact.memory_identity(current),"memory":memory})
    return {"prefix_count":end,"generation":memory["generation"],"recovered":True}
