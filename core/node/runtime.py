"""Shared execution supervisor, inside Bubblewrap or an explicit Host subprocess.

SERVICE_SOURCE, TOOL_SOURCES, IMAGE_SOURCE and INVENTORY_SOURCE are supplied by the host.
"""

import ctypes
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time


SPECIAL_ENV = {"PWD", "OLDPWD", "SHLVL", "_", "SHELLOPTS", "BASHOPTS", "BASHPID", "UID", "EUID", "PPID"}


SPAWN_LOCK=threading.RLock()
CHILDREN={}

def spawn(*args,**kwargs):
    with SPAWN_LOCK:
        process=subprocess.Popen(*args,**kwargs)
        CHILDREN[process.pid]=process
        return process

def descendants(pid):
    pending, found = [pid], set()
    while pending:
        current = pending.pop()
        try:
            children = []
            for task in Path(f"/proc/{current}/task").iterdir():
                children.extend((task / "children").read_text().split())
        except OSError:
            continue
        for child in map(int, children):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def kill_tree(process):
    if process is None:
        return
    for pid in descendants(process.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def inspect_tools(context, deadline):
    process = spawn(["/usr/bin/python3", "-I", "-S", "-c", INVENTORY_SOURCE],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=context["cwd"], env={**context["env"], "PYTHONDONTWRITEBYTECODE": "1"},
        start_new_session=True, close_fds=True)
    try:
        output, _ = process.communicate(timeout=max(.01, deadline-time.monotonic()))
        if process.returncode or len(output)>256*1024:
            raise RuntimeError("Environment discovery failed or exceeded its output budget")
        return {"inventory": json.loads(output), "exit_code": 0}
    except subprocess.TimeoutExpired:
        raise TimeoutError("Environment discovery timed out") from None
    finally:
        if process.poll() is None:
            kill_tree(process)
        for stream in (process.stdout, process.stderr):
            stream.close()


def command(request, context, deadline):
    """Run a registered argv without eval, a new sandbox or persistent-shell mutation."""
    process = spawn(request["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=context["cwd"], env=context["env"], start_new_session=True, close_fds=True)
    output = Output(process)
    output.begin(request["max_output"])
    remaining = memoryview(request.get("input", "").encode())
    os.set_blocking(process.stdin.fileno(), False)
    timed_out = cancelled = False
    try:
        while process.poll() is None:
            if request.get("_cancelled",lambda:False)():raise InterruptedError("tool cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("tool timed out")
            if remaining:
                if select.select([], [process.stdin], [], .01)[1]:
                    try:
                        count = os.write(process.stdin.fileno(), remaining[:65536])
                        remaining = remaining[count:]
                    except BrokenPipeError:
                        remaining = memoryview(b"")
            else:
                if not process.stdin.closed:
                    process.stdin.close()
                time.sleep(.01)
    except (KeyboardInterrupt, TimeoutError, InterruptedError) as exc:
        timed_out, cancelled = isinstance(exc, TimeoutError), isinstance(exc, (KeyboardInterrupt,InterruptedError))
    finally:
        # Also reclaim background descendants; command tools do not own services.
        kill_tree(process)
        result = output.finish()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
        for thread in output.threads:
            thread.join(timeout=.2)
    return {**result, "exit_code": process.returncode, "timed_out": timed_out, "cancelled": cancelled}


class JsonPipe:
    def __init__(self, stream):
        self.stream, self.buffer = stream, bytearray()
        os.set_blocking(stream.fileno(), False)

    def read(self, deadline, limit=1024 * 1024, cancelled=None):
        while b"\n" not in self.buffer:
            if cancelled and cancelled():
                raise KeyboardInterrupt()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("tool timed out")
            if not select.select([self.stream], [], [], min(remaining, 0.05))[0]:
                continue
            data = os.read(self.stream.fileno(), 65536)
            if not data:
                raise RuntimeError("executor exited before returning a complete result")
            self.buffer.extend(data)
            if len(self.buffer) > limit:
                raise RuntimeError("executor response exceeds output budget")
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return json.loads(line)


class Output:
    """Drain bounded command output independently of supervision/control streams."""
    def __init__(self, process):
        self.lock, self.active = threading.Lock(), None
        self.streams = {"stdout": process.stdout, "stderr": process.stderr}
        self.threads = []
        for name, stream in self.streams.items():
            os.set_blocking(stream.fileno(), False)
            thread = threading.Thread(target=self.drain, args=(name, stream), daemon=True)
            thread.start()
            self.threads.append(thread)

    def drain(self, name, stream):
        try:
            while True:
                if not select.select([stream], [], [], 0.05)[0]:
                    continue
                with self.lock:
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        break
                    self.accept(name, data)
        except (OSError, ValueError):
            pass

    def accept(self, name, data):
        if self.active is not None:
            parts, remaining, truncated = self.active
            keep = min(len(data), remaining)
            parts[name].extend(data[:keep])
            self.active = parts, remaining - keep, truncated or keep < len(data)

    def begin(self, limit):
        with self.lock:
            self.active = {"stdout": bytearray(), "stderr": bytearray()}, limit, False

    def finish(self):
        with self.lock:
            # The foreground command completed before its metadata was sent.
            # Read any remaining pipe bytes under the same lock as the pump,
            # avoiding a race between a final read and the result snapshot.
            for name, stream in self.streams.items():
                for _ in range(16):
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not data:
                        break
                    self.accept(name, data)
            parts, _, truncated = self.active
            self.active = None
        return {**{key: value.decode("utf-8", errors="replace") for key, value in parts.items()},
                "truncated": truncated}


class Operations:
    def __init__(self,invoke):
        self.invoke=invoke;self.lock=threading.RLock();self.items={};self.closed=False
    def submit(self,request):
        identifier=request['operation_id']
        with self.lock:
            if identifier in self.items:return {'operation_id':identifier,'accepted':True}
            if self.closed or sum(not i['done'].is_set() for i in self.items.values())>=64:raise ValueError('Execution control queue is full')
            item={'done':threading.Event(),'cancel':threading.Event(),'result':None,'abandoned':False,'created':time.monotonic()}
            self.items[identifier]=item
            def run():
                try:item['result']=self.invoke({**request['request'],'_cancelled':item['cancel'].is_set})
                except BaseException as error:item['result']={'error':str(error) or type(error).__name__,'exit_code':-1}
                finally:
                    with self.lock:
                        item['done'].set()
                        if item['abandoned']:self.items.pop(identifier,None)
            item['thread']=threading.Thread(target=run,daemon=True,name='tool-operation-'+identifier[:8]);item['thread'].start()
            return {'operation_id':identifier,'accepted':True}
    def request(self,kind,request):
        if kind=='operation_submit':return self.submit(request)
        with self.lock:
            item=self.items.get(request['operation_id'])
            if item is None:raise ValueError('Unknown operation; request was not replayed')
            if kind=='operation_cancel':
                item['abandoned']=not request.get('wait_for_result',False)
                item['cancel'].set()
                if item['abandoned'] and item['done'].is_set():self.items.pop(request['operation_id'],None)
            if kind=='operation_forget':
                if item['done'].is_set():self.items.pop(request['operation_id'],None)
                return {}
            return {'done':item['done'].is_set(),'result':item['result'] if item['done'].is_set() else None}
    def close(self):
        self.closed=True
        for item in list(self.items.values()):item['cancel'].set()
        for item in list(self.items.values()):item['thread'].join(timeout=.5)

class NodeRuntime:
    """One node-side supervisor, shared by every Agent in this Session."""
    def run(self):
        libc = ctypes.CDLL(None)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("cannot enable sandbox child reaping")
        # Losing the Host also stops this supervisor in unsandboxed mode. This
        # complements EOF cleanup; it is not a PID-namespace replacement.
        parent=os.getppid()
        if os.environ.get('RECODING_EXECUTION_MODE')=='host':
            if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid()!=parent:
                raise RuntimeError('Execution parent exited during startup')
        context = {"cwd": os.getcwd(), "env": {k: v for k, v in os.environ.items() if k not in SPECIAL_ENV}}
        file_namespace={'__name__':'node_files'}
        exec(compile(FILE_SOURCE,'<node-files>','exec'),file_namespace,file_namespace)
        log_namespace={'__name__':'node_logs'}
        exec(compile(LOG_SOURCE,'<node-logs>','exec'),log_namespace,log_namespace)
        file_endpoints={}
        images = {"__name__": "sandbox_image_reader"}
        exec(compile(IMAGE_SOURCE, "<image-reader>", "exec"), images, images)
        runtimes, tools = [], {}
        image_root=os.environ.get('RECODING_WORKSPACE','/workspace')
        ports = {'read_image': lambda path:images['read_image'](path,root=image_root,label_root=image_root), 'kill_tree': kill_tree, 'descendants': descendants,'spawn':spawn}
        pool=None
        if SERVICE_SOURCE:
            namespace={'__name__':'node_process_pool'}
            exec(compile(SERVICE_SOURCE,'<node-process-pool>','exec'),namespace,namespace)
            drivers={}
            for language,source in namespace['DRIVER_SOURCES'].items():
                driver_namespace={'__name__':'process_driver_'+language}
                exec(compile(source,'<process-driver-'+language+'>','exec'),driver_namespace,driver_namespace)
                drivers[language]=driver_namespace['Driver']
            ports['json_pipe']=JsonPipe
            pool=namespace['ProcessPool'](context,ports,drivers)
        for descriptor in TOOL_SOURCES:
            namespace = {'__name__': 'sandbox_tool_runtime'}
            exec(compile(descriptor['source'], '<sandbox-tool>', 'exec'), namespace, namespace)
            runtime = namespace['Runtime'](context, ports)
            runtimes.append(runtime)
            for name in descriptor['names']: tools[name] = runtime
        def invoke(request):
            kind=request['kind']
            timeout=request.get('timeout',30)
            deadline=time.monotonic()+timeout if timeout else float('inf')
            if kind=='tool':return tools[request['name']].request(request['name'],request)
            if kind=='command':return command(request,context,deadline)
            if kind=='tool_inventory':return inspect_tools(context,deadline)
            if kind=='pool':
                if pool is None:raise ValueError('Node Process Pool not installed')
                return pool.request(request)
            raise ValueError('Unknown asynchronous operation')
        operations=Operations(invoke)
        active = False

        def interrupt(signum, frame):
            if active:
                raise KeyboardInterrupt()

        def send(message):
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        signal.signal(signal.SIGINT, interrupt)
        def terminate(signum, frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminate)
        send({"type": "ready", "pid": os.getpid(), "identity":IDENTITY})
        try:
            for line in sys.stdin.buffer:
                request = json.loads(line)
                active = True
                result = {}
                try:
                    deadline = time.monotonic() + request["timeout"]
                    if not os.path.isdir(context["cwd"]):
                        context["cwd"] = os.environ.get("RECODING_WORKSPACE","/workspace")
                    kind = request["kind"]
                    if kind.startswith('operation_'):
                        result=operations.request(kind,request)
                    elif kind == 'files':
                        key=request['endpoint_id'];action=request['action']
                        if action=='close':
                            endpoint=file_endpoints.pop(key,None)
                            if endpoint:endpoint.close()
                            result={}
                        else:
                            if key not in file_endpoints:
                                if len(file_endpoints)>=16:raise ValueError('Too many file endpoints')
                                file_endpoints[key]=file_namespace['WorkspaceFiles'](image_root)
                            result=file_endpoints[key].request(action,**request.get('fields',{}))
                    elif kind == 'logs':
                        result=log_namespace['read_output'](image_root,os.environ['RECODING_EXECUTION_OWNER'],request['language'],request['record'],**request.get('options',{}))
                    elif kind == 'tool':
                        result = tools[request['name']].request(request['name'], request)
                    elif kind == 'runtime_updates':
                        result = {'updates': [record for runtime in runtimes for record in getattr(runtime, 'updates', lambda: [])()]}
                    elif kind == 'pool':
                        if pool is None:raise ValueError('Node Process Pool not installed')
                        result = pool.request(request)
                    elif kind == "command":
                        result = command(request, context, deadline)
                    elif kind == "check":
                        result = {"stdout": "sandbox-ok\n", "stderr": "", "exit_code": 0,
                                  "timed_out": False, "truncated": False}
                    elif kind == "tool_inventory":
                        result = inspect_tools(context, deadline)
                    else:
                        raise ValueError("unknown sandbox request")
                except KeyboardInterrupt:
                    result = {"cancelled": True}
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    result = {"error": str(exc), "exit_code": -1, "stdout": "", "stderr": "",
                              "timed_out": isinstance(exc, TimeoutError), "truncated": False}
                finally:
                    active = False
                result["session_state"] = {"pid": os.getpid(), "cwd": context["cwd"], "execution": {"status": "ready"}}
                send({"type": "result", "id": request["id"], "result": result})
                with SPAWN_LOCK:
                    direct=set(CHILDREN)
                    for pid in descendants(os.getpid())-direct:
                        try:os.waitpid(pid,os.WNOHANG)
                        except ChildProcessError:pass
                    for pid,process in list(CHILDREN.items()):
                        if process.returncode is not None:CHILDREN.pop(pid,None)
        finally:
            for endpoint in file_endpoints.values():endpoint.close()
            operations.close()
            if pool:pool.close()
            for runtime in reversed(runtimes): getattr(runtime, 'close', lambda: None)()
            for pid in descendants(os.getpid()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    NodeRuntime().run()
