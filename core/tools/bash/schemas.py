"""Non-builtin Bash execution schemas and behavioral guidance."""
from multiagent.tools import tool

SCHEMAS = [
    tool("bash_process", "Create/list/rename/transfer/delete Node Bash processes. Every Agent may use/read every process without sharing. Only Owner/Master may rename/transfer/delete. Persistent Shells keep variables/functions/cwd/env; optional ssh_argv holds one managed SSH connection. Owner Stop cancels queued work and retires after the current other submitter finishes, within a bounded grace. Transfer before Stop if state is valuable. Session Stop destroys all memory. No PTY or automatic SSH reconnect/replay.", {
        "action":{"type":"string","enum":["create","list","rename","transfer","delete"]},
        "process_id":{"type":"string"},"new_owner":{"type":"string"},"name":{"type":"string","minLength":1,"maxLength":100},
        "cwd":{"type":"string","maxLength":4096},"env":{"type":"object","additionalProperties":True},
        "ssh_argv":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"string","maxLength":4096}},
        "timeout":{"type":"number","minimum":0.1,"maximum":120}}, ("action",)),
    tool("bash", "Run Bash code on a Node. Omit process_id for a temporary Shell, or supply an explicitly created process ID to retain Shell state. Every Agent may use it. sync waits; async returns execution.id and one completion notice to the SUBMITTER. Same process FIFO, different processes/nodes parallel. cwd/env are temporary/creation options. timeout defaults to tool limit for sync and unlimited for async; 0 is unlimited. Temporary async keep_stdin_open permits piped stdin. Persistent stdin is reserved; use heredocs/files. Do not exit/exec the managed Shell.", {
        "process_id":{"type":"string"},
        "command":{"type":"string","minLength":1,"maxLength":1048576},
        "mode":{"type":"string","enum":["sync","async"]},
        "cwd":{"type":"string","maxLength":4096},
        "env":{"type":"object","additionalProperties":True},
        "timeout":{"type":"number","minimum":0},
        "name":{"type":"string","minLength":1,"maxLength":100},
        "keep_stdin_open":{"type":"boolean"}}, ("command",)),
    tool("bash_execution", "List/status/read all Bash executions; only submitter, process Owner or Master may cancel/write. Logs use independent byte cursors. write requires temporary async keep_stdin_open. Persistent cancellation tries to preserve Shell state; SSH interruption can lose its connection and does not guarantee remote detached children exited. Never replay interrupted commands automatically.", {
        "action":{"type":"string","enum":["list","status","read","cancel","write"]},
        "execution_id":{"type":"string"},"process_id":{"type":"string"},
        "stdout_offset":{"type":"integer","minimum":0},
        "stderr_offset":{"type":"integer","minimum":0},
        "max_bytes":{"type":"integer","minimum":128,"maximum":64000},
        "text":{"type":"string","maxLength":65536},
        "close_stdin":{"type":"boolean"}}, ("action",))]

PROMPT = """BashDriver retains variables/functions/cwd/env/settings only in explicitly created
processes. Persistent stdin is reserved for framing: use heredocs/files. For SSH use ssh_argv
with accessible key/known_hosts paths; one connection is retained, no PTY or automatic replay.
Do not exit/exec the managed Shell, change __recoding_* control names or daemonize work.
Temporary async keep_stdin_open supports bash_execution write; other calls have stdin EOF.
"""
