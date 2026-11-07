"""Model-facing contract; language interface to the Node ProcessPool."""
from multiagent.tools import tool

SCHEMAS = [
    tool("python_process", "Create/list/rename/transfer/delete Python processes on a Node. All live Agents with this tool may use/read every process; no sharing or grants. Only Owner or Master may rename/transfer/delete. Owner Stop closes admission, cancels queued work and deletes after another submitter's current execution finishes, with a bounded grace. Stop also cancels that Agent's submissions in other owners' processes. Transfer before Stop to retain useful memory. Session Stop loses all process memory. Names may repeat; stable IDs route calls.",
         {"action":{"type":"string","enum":["create","list","rename","transfer","delete"]},
          "process_id":{"type":"string"},"new_owner":{"type":"string"},"name":{"type":"string","minLength":1,"maxLength":100},},("action",)),
    tool("python", "Run code in a Node Python process. Omit process_id for a temporary interpreter; create a persistent process first to preserve state. All Agents may use every process. sync waits, async returns execution.id and completion is sent to the SUBMITTER. Each process is FIFO; different processes/nodes run concurrently. timeout defaults to the tool limit for sync, unlimited for async; 0 is unlimited. Cancellation requests KeyboardInterrupt, may leave partial changes and can become unresponsive. Use print for results, check_cancel() for cooperative cancellation.",
         {"process_id":{"type":"string"},"code":{"type":"string","maxLength":1048576},
          "mode":{"type":"string","enum":["sync","async"]},"timeout":{"type":"number","minimum":0}},("code",)),
    tool("python_execution", "List/status/read all Node executions. Only submitter, current process Owner or Master can cancel. Byte cursors read full persisted logs incrementally. Cancellation is not rollback. Unresponsive persistent code is not automatically killed to cancel another Agent's submission; Owner/Master may delete it. Owner Stop retirement has a bounded close grace. Never replay interrupted code automatically.",
         {"action":{"type":"string","enum":["list","status","read","cancel"]},
          "execution_id":{"type":"string"},"process_id":{"type":"string"},
          "stdout_offset":{"type":"integer","minimum":0},"stderr_offset":{"type":"integer","minimum":0},
          "max_bytes":{"type":"integer","minimum":128,"maximum":64000}},("action",)),
]
NAMES={item['function']['name'] for item in SCHEMAS}
PROMPT = """PythonDriver runs each submitted code string in the chosen interpreter namespace.
Use print for bounded results and python_execution read for full logs. check_cancel() supports
cooperative cancellation. Variables/imports/functions persist only with an explicit process_id.
Updating an already imported package may require a fresh process. Python source-defined functions
may need an importable guarded script for multiprocessing children.
"""
