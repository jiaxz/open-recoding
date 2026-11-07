"""Interactive terminal entry; TUI on a terminal, line protocol for pipes."""

import sys
import threading
import time

from .client import Client, MessageStream
from .console import Console
from .render import Renderer, plain


def chat(client, renderer, args, *, sessions=None):
    terminal=sys.stdin.isatty() and sys.stdout.isatty() and renderer.format=='text'
    views={}
    while True:
        if terminal:
            try:
                from .tui import TerminalUI
                import rich
            except ImportError:
                raise ValueError('TUI 依赖未安装：.venv/bin/python3 -m pip install -e ".[cli]"') from None
            ui=TerminalUI(client,renderer,args,sessions=sessions)
            if client.directory in views:ui.restore_view(views[client.directory])
            result=ui.run()
            views[client.directory]=ui.view_state()
        else:
            result=line_chat(client,renderer,args,sessions=sessions)
        if not isinstance(result,Client):return result
        client=result
        renderer=Renderer(renderer.format,'always' if renderer.color else 'never',renderer.timestamps)
        from types import SimpleNamespace
        args=SimpleNamespace(**{**vars(args),'to':'master','agent':None})


def line_chat(client,renderer,args,*,sessions=None):
    # Scripted stdin/JSON stays a line-oriented shell interface. It never emits
    # full-screen control sequences or captures the pipe in a terminal layout.
    console=Console(client,args,sessions=sessions)
    stream=MessageStream(client,renderer,scope=console.scope,history=getattr(args,'history',20))
    done=threading.Event()
    lock=threading.RLock()
    def poll():
        failed=False;reviewed=set();last_status=0
        while not done.is_set():
            try:
                with lock:stream.poll()
                if time.monotonic()-last_status>=1:
                    last_status=time.monotonic()
                    for request in client.call('status').get('compaction',[]):
                        if (request['request_id'],request['status']) not in reviewed:
                            reviewed.add((request['request_id'],request['status']))
                            print('Background compression: '+plain(request)+'\nUse /compact cancel [REQUEST_ID]',file=sys.stderr,flush=True)
                failed=False
            except (ValueError,OSError) as error:
                if not failed:print('Connection: '+plain(error),file=sys.stderr,flush=True)
                failed=True
            done.wait(.2)
    thread=threading.Thread(target=poll,daemon=True,name='cli-messages')
    thread.start()
    try:
        for text in sys.stdin:
            try:
                result=console.execute(text.rstrip('\n'))
                if result.get('quit'):break
                if result.get('switch'):return result['switch']
                if result.get('panel') and result.get('data') is not None:renderer.data(result['data'])
                elif result.get('panel')=='help':
                    from .console import COMMANDS
                    renderer.data([{'command':name,'description':description} for name,description in COMMANDS])
                if result.get('notice'):print(plain(result['notice']),file=sys.stderr,flush=True)
                if result.get('history'):
                    for item in result['history']:renderer.message(item)
                stream.scope=console.scope
                stream.agent=console.view
            except (ValueError,OSError,KeyError) as error:
                print('Error: '+plain(error),file=sys.stderr,flush=True)
    except KeyboardInterrupt:
        return 130
    finally:
        done.set();thread.join(timeout=2)
    return 0
