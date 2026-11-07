"""Python interpreter protocol only; lifecycle, ownership and queues belong to Node."""
import json
import os
import signal
import subprocess
import threading
import time

class Driver:
    def __init__(self,context,options,temporary,ports):
        if options:raise ValueError('Python process has no startup options')
        self.context,self.ports=context,ports;self.process=None;self.reader=None
        self.lifecycle=threading.RLock();self.closed=False

    def start(self,deadline,cancelled):
        with self.lifecycle:
            if self.closed:raise RuntimeError('Python process closed during creation')
            self.process=self.ports['spawn'](['/usr/bin/python3','-I','-u','-c',WORKER_SOURCE],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=self.context['cwd'],env=self.context['env'],start_new_session=True,close_fds=True)
        def drain():
            try:
                while os.read(self.process.stderr.fileno(),65536):pass
            except (OSError,ValueError,AttributeError):pass
        threading.Thread(target=drain,daemon=True).start()
        self.reader=self.ports['json_pipe'](self.process.stdout)
        ready=self.reader.read(deadline,cancelled=cancelled)
        if ready.get('type')!='ready':raise RuntimeError('Python worker did not become ready')
        self.context.update(ready['_context'])
        return {'pid':self.process.pid,'cwd':self.context['cwd'],'remote':False}

    def execute(self,code,identifier,folder,cancelled):
        process=self.process;reader=self.reader;cancel_path=folder/'cancel'
        payload={'id':identifier,'code':code,'max_output':16000,'cancel_path':str(cancel_path),
                 'context':self.context,'log_paths':{name:str(folder/(name+'.log')) for name in ('stdout','stderr')}}
        process.stdin.write(json.dumps(payload,ensure_ascii=False).encode()+b'\n');process.stdin.flush()
        started=sent=False
        while process.poll() is None:
            if cancelled.is_set():
                cancel_path.touch()
                if started and not sent:
                    sent=True
                    try:os.kill(process.pid,signal.SIGINT)
                    except ProcessLookupError:pass
            try:message=reader.read(time.monotonic()+.05,2*1024*1024)
            except TimeoutError:continue
            if message.get('id')!=identifier:raise RuntimeError('Python response execution ID mismatch')
            if message.get('type')=='started':started=True;continue
            if message.get('type')!='result':raise RuntimeError('Invalid Python worker response')
            result=message['result'];context=result.pop('_context',None)
            if context:self.context.update(context)
            return {'exit_code':result.get('exit_code'),'interrupted':bool(result.get('interrupted'))}
        raise RuntimeError('Python interpreter exited; memory is lost')

    def close(self):
        with self.lifecycle:
            self.closed=True;process,self.process=self.process,None
        if process is not None:
            self.ports['kill_tree'](process)
            for stream in (process.stdin,process.stdout,process.stderr):
                if not stream.closed:stream.close()
