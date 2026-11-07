"""Common model-facing contract for Node processes; injected once per Agent."""
PROMPT = """All language processes are resources in the selected Node's common Process Pool.
All live Agents with the execution tool can list/use/read every process: no share/revoke step.
Use stable process IDs; names are display labels, chosen and renamed by Owner or Master.
Owner starts as creator; creator never changes. Only Owner/Master rename/transfer/delete.
Every Execution has its own submitted_by, timeout, logs and ID. Async results go to the
submitter even after ownership transfer; sync results return to the original caller.
Same process is FIFO, different processes/nodes can execute concurrently. Async frees
the caller, not the process: it does not create parallel execution in one namespace.
Use shared persistent state for SHORT cooperation; return before another caller can run.
Do not wait inside one process for queued code in that same process. Messages wake Agents,
dict/file changes do not. Keep heavy computations in separate processes and bound budgets.
Agent Stop cancels all its running/queued submissions, including on others' processes.
Its owned processes stop accepting work, cancel queued calls, and close after the current
other submitter finishes; close grace is bounded. Transfer ownership BEFORE Stop to keep
valuable state. There is no automatic handoff to Master. Only Session close destroys all.
Cancellation is not rollback: partial object/file/external effects remain. Persistent code
that cannot be interrupted becomes unresponsive; Owner/Master may delete it explicitly.
Resume never restores memory or replays old code. Inspect process lists before reuse.
Bash persistent processes keep Shell variables/functions/cwd/settings; Python keeps objects,
imports/functions/threads. States are per process, files/packages are shared per node.
Temporary calls preserve files, not interpreter state. New imports may need a fresh process.
Persistent Bash stdin is reserved. Temporary async keep_stdin_open accepts write calls.
For persistent SSH use bash_process ssh_argv with explicit accessible key/known_hosts paths;
no Host credentials are implicitly mounted. One managed connection is reused. No PTY.
SSH cancellation may close the connection and lose Shell state; manage remote side effects.
Do not daemonize or detach child processes; join/reclaim them. Use async foreground work.
"""
