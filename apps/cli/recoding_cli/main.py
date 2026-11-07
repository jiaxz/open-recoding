"""Shell commands and interactive entry point, built on the public Session API."""

import argparse
from multiagent.model_config import api_key
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sys
import time

from session import SessionStore
from session.config import default_env_file, load_env_file
from multiagent.config import configuration
from multiagent.models import validate_model
from .catalog import discover, select
from .client import Client, NoSessionClient, MessageStream, detached
from .control import request
from .host import CliSessionHost, serve
from .render import Renderer


def parser():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument('-C', '--workspace', help='server-side workspace/search root (default: cwd)')
    common.add_argument('-s', '--session', help='Session directory, ID/prefix, or unique name')
    common.add_argument('--env-file', help='dotenv model credentials; environment takes precedence')
    common.add_argument('--format', choices=['text', 'json', 'jsonl'], help='output format; streams emit one JSON object per line')
    common.add_argument('--json', dest='format', action='store_const', const='json')
    common.add_argument('--color', choices=['auto', 'always', 'never'])
    common.add_argument('--no-color', dest='color', action='store_const', const='never')
    common.add_argument('--no-timestamps', dest='timestamps', action='store_false')
    common.add_argument('--model', help='model ID (new Session default: configured model)')
    common.add_argument('--base-url', help='OpenAI-compatible gateway URL')
    common.add_argument('--api-timeout', type=float, help='model request timeout in seconds')
    common.add_argument('--tool-timeout', type=float, help='tool execution timeout in seconds')
    common.add_argument('--max-output', type=int, help='maximum tool output characters')
    common.add_argument('--max-agents', type=int, help='sub-Agent concurrency, 0–99 (default: 16)')
    common.add_argument('--idle-sleep', type=float, help='fallback sleep, 0.1–3600 seconds (default: 10)')
    common.add_argument('--execution-mode',choices=['sandbox','host'],help='tool execution: sandbox (default), or unrestricted Host subprocesses; change only when stopped')
    common.add_argument('--network', action=argparse.BooleanOptionalAction, help='allow sandbox network (default: on)')
    common.add_argument('--gpu', action=argparse.BooleanOptionalAction, help='allow sandbox GPU (default: on)')
    common.add_argument('--demo', action='store_true', help='offline scripted model with real sandbox tools')
    common.add_argument('-p', '--prompt', action='append', help='user message text (repeatable)')
    common.add_argument('--prompt-file', help='read user text from UTF-8 file; - reads stdin')
    common.add_argument('--image', action='append', help='attach a local PNG/JPEG (repeatable, up to 8)')
    common.add_argument('--to', help='recipient Agent ID/name, master, all, or subs')
    common.add_argument('--agent', help='filter displayed messages by Agent ID/name')
    common.add_argument('--messages', choices=['human', 'all'], help='human or all messages (chat default: all; run default: human)')
    common.add_argument('--history', type=int, help='initial message history, 0–200 (default: 20)')
    common.add_argument('--timeout', type=float, help='viewer time limit in seconds; expiry returns 124')
    common.add_argument('--until', help='exit viewer after an Agent-to-human message contains this text')
    common.add_argument('--max-messages', type=int, help='exit after this many new Agent-to-human messages')
    common.add_argument('--name', help='new Session name (default: session-YYYYMMDD-HHMMSS)')
    common.add_argument('--detach', action='store_true', help='run a CLI host in the background')

    root = argparse.ArgumentParser(prog='recoding', parents=[common],
        description='recoding... — message-first CLI for endless multi-agent Sessions',
        epilog='No command: interactive chat. Options work before or after a command. See docs/cli.md.')
    commands = root.add_subparsers(dest='command', metavar='COMMAND')
    descriptions = {
        'create': 'create and host a Session, waking Master',
        'run': 'start/resume Master and stream messages (endless unless explicitly stopped)',
        'chat': 'interactive console; creates on first message, or resumes with -s',
        'attach': 'interactive console for an existing CLI host',
        'list': 'discover current-format Sessions below the workspace',
        'status': 'show the running CLI host status',
        'agents': 'list Agent metadata (no context)',
        'speed': 'read whole-Session Output speed samples (no model calls)',
        'processes': 'show running/ready Bash/Python resources across nodes',
        'send': 'enqueue a human message; stopped direct recipients stay stopped',
        'messages': 'read correspondence without consuming inboxes',
        'stop': 'stop and close the hosted Session, releasing ownership',
        'resume': 'restore a saved Session, starting only Master',
        'wake': 'wake only Master in an already open CLI Session',
        'close': 'stop Agents and release the CLI host/owner lock',
        'model': 'show or change the shared Session model',
        'config': 'show or change model/concurrency/default sleep',
        'rename': 'rename the Session', 'pin': 'pin/unpin a saved or running Session',
        'export': 'export messages or copy the complete Session archive',
        'compact': 'inspect or cancel automatic background context compression',
        'delete': 'delete this Session archive, retaining workspace files (requires --yes)',
    }
    for name, description in descriptions.items():
        sub = commands.add_parser(name, parents=[common], help=description, description=description)
        if name == 'messages':
            sub.add_argument('--follow', action='store_true')
            sub.add_argument('--before')
            sub.add_argument('--after')
            sub.add_argument('--limit', type=int, default=100)
        if name == 'send': sub.add_argument('text', nargs='*', help='message text; use --prompt-file - for stdin')
        if name == 'model': sub.add_argument('value', nargs='?')
        if name == 'config': sub.add_argument('values', nargs='*', help='KEY=VALUE; model/max_subagents/idle_sleep_seconds')
        if name == 'rename': sub.add_argument('value')
        if name == 'pin': sub.add_argument('--off', action='store_true')
        if name == 'delete': sub.add_argument('--yes', action='store_true')
        if name == 'export':
            sub.add_argument('output'); sub.add_argument('--archive', action='store_true')
        if name == 'compact':
            sub.add_argument('--request-id', help='compression ID to cancel'); sub.add_argument('--cancel', action='store_true')
    return root


def options(args):
    mapping = {'max_agents': 'max_subagents', 'idle_sleep': 'idle_sleep_seconds'}
    result = {mapping.get(key, key): value for key, value in vars(args).items() if key in {
        'model', 'base_url', 'api_timeout', 'tool_timeout', 'max_output', 'max_agents', 'idle_sleep', 'network', 'gpu', 'demo', 'execution_mode'}}
    if 'model' in result: validate_model(result['model'])
    if 'max_subagents' in result and not 0 <= result['max_subagents'] <= 99: raise ValueError('--max-agents must be 0–99')
    if 'idle_sleep_seconds' in result and not .1 <= result['idle_sleep_seconds'] <= 3600: raise ValueError('--idle-sleep must be 0.1–3600')
    for key in ('api_timeout', 'tool_timeout', 'max_output'):
        if key in result and not 0 < result[key] < float('inf'): raise ValueError(f'{key} must be positive and finite')
    for key in ('timeout', 'max_messages'):
        value = getattr(args, key, None)
        if value is not None and not 0 < value < float('inf'): raise ValueError(f'--{key.replace("_", "-")} must be positive')
    if not 0 <= getattr(args, 'history', 20) <= 200: raise ValueError('--history must be 0–200')
    return result


def input_text(args):
    parts = list(getattr(args, 'prompt', []))
    if getattr(args, 'text', None): parts.append(' '.join(args.text))
    if hasattr(args, 'prompt_file'):
        parts.append(sys.stdin.read() if args.prompt_file == '-' else Path(args.prompt_file).read_text(encoding='utf-8'))
    return '\n\n'.join(parts)


def settings_input(args):
    values = {}
    for item in getattr(args, 'values', []):
        key, sep, value = item.partition('=')
        if not sep or key not in {'model', 'max_subagents', 'idle_sleep_seconds'}:
            raise ValueError('Use model=ID, max_subagents=0..99, idle_sleep_seconds=0.1..3600')
        values[key] = value if key == 'model' else int(value) if key == 'max_subagents' else float(value)
    for arg, key in [('model', 'model'), ('max_agents', 'max_subagents'), ('idle_sleep', 'idle_sleep_seconds')]:
        if hasattr(args, arg): values[key] = getattr(args, arg)
    return values


def follow(client, renderer, args):
    from .host import resolve_agent
    agent = resolve_agent(client.call('agents'), args.agent) if hasattr(args, 'agent') else None
    stream = MessageStream(client, renderer, scope=getattr(args, 'messages', 'human'), agent=agent,
                           history=getattr(args, 'history', 20))
    if hasattr(args, '_after') and not getattr(args, 'history', 20):
        stream.ready, stream.cursor = True, args._after
    first = stream.poll()
    since = getattr(args, '_started_at', time.time())
    started, count = time.monotonic(), 0
    pending = set()
    while True:
        records, first = first or stream.poll(), []
        for item in records:
            if item['kind'] != 'human_output' or item['timestamp'] < since: continue
            count += 1
            if hasattr(args, 'until') and args.until in item['content']: return 0
            if hasattr(args, 'max_messages') and count >= args.max_messages: return 0
        status = client.call('status')
        for item in status.get('compaction', []):
            key=(item['request_id'],item['status'])
            if key not in pending:
                pending.add(key)
                print(f"Context compression · {item['name']} · {item['status']} · "
                      f"cancel: compact -s {client.directory} --cancel --request-id {item['request_id']}",file=sys.stderr,flush=True)
        active = status['status']=='run' or any(
            item['status'] in {'starting', 'running', 'sleeping', 'stopping'} for item in status.get('agents', []))
        if not active:
            if status.get('error'): print(status['error'], file=sys.stderr)
            return 1 if status.get('error') else 0
        if hasattr(args, 'timeout') and time.monotonic()-started >= args.timeout: return 124
        time.sleep(.15)


def runtime_config(args, directory, config):
    effective = configuration(SessionStore(directory).settings() if directory is not None else {}, config)
    effective['private_files'] = sorted({str(default_env_file().resolve()),
        str(Path(getattr(args, 'env_file', default_env_file())).expanduser().resolve())})
    return effective


def launch_config(args,config,directory=None):
    if not config.get('demo'):load_env_file(getattr(args,'env_file',None))
    effective=runtime_config(args,directory,config)
    if not effective.get('demo') and not api_key():
        raise ValueError('RECODING_API_KEY is missing; use --env-file FILE or --demo')
    return effective


def navigation(args,config,*,initial=None,client=None):
    from .sessions import CliSessionNavigator
    restore={key:value for key,value in config.items() if key not in {'model','max_subagents','idle_sleep_seconds'}}
    return CliSessionNavigator(args.workspace,lambda folder:launch_config(args,restore,folder),
        initial=initial,initial_client=client,new_name=getattr(args,'name',None),
        new_config_factory=lambda _:launch_config(args,config))


def run(args, renderer, config):
    if args.command in {'resume','attach'} and not hasattr(args,'session'):raise ValueError('请用 -s 指定 Session；TUI 中 /resume 可打开目录选择器')
    if args.command=='create' and hasattr(args,'session'):raise ValueError('create 不接受 -s；恢复已有存档请使用 resume 或 chat -s')
    text=input_text(args)
    if not hasattr(args,'session') and not text.strip() and not getattr(args,'image',[]) and args.command!='create':
        if args.command!='chat' or getattr(args,'detach',False):
            raise ValueError('新 Session 需要第一条用户消息；请用 -p 提供任务，或用 chat 进入输入界面')
        from .interactive import chat
        client=NoSessionClient(args.workspace)
        navigator=navigation(args,config,client=client)
        try:return chat(client,renderer,args,sessions=navigator)
        finally:navigator.close()
    # Explicit create, or a supplied first message, may create immediately.
    fresh=launch_config(args,config) if not hasattr(args,'session') else None
    directory = select(args.session, args.workspace) if hasattr(args, 'session') else SessionStore.create(
        args.workspace, getattr(args, 'name', datetime.now().strftime('session-%Y%m%d-%H%M%S')), fresh).folder
    client = Client(directory)
    live = request(directory, 'status')
    owned = None
    if args.command!='attach' and live is not None:
        raise ValueError('此 Session 已由 CLI Host 独占；使用 attach 连接其控制界面，或先 stop')
    if live is None:
        if args.command=='attach':raise ValueError('No live CLI host; use chat -s PATH to resume')
        effective=fresh or launch_config(args,config,directory)
        if getattr(args,'detach',False):detached(directory,effective)
        else:owned=CliSessionHost(directory,effective)
    elif config:
        raise ValueError('attach 不能更改启动配置；连接后使用 /settings')
    navigator=None
    try:
        print(f'Session: {directory}', file=sys.stderr, flush=True)
        args._after = client.call('messages', limit=1)['after'] or 'start'
        args._started_at = time.time()
        if text.strip() or getattr(args, 'image', []): client.send(text, getattr(args, 'to', 'master'), getattr(args, 'image', []))
        if getattr(args, 'detach', False):
            status = client.call('status')['status']
            renderer.data({'session_directory': str(directory), 'status': status, 'detached': True})
            return 1 if status == 'failed' else 0
        if args.command in {'chat','attach','create','resume'}:
            from .interactive import chat
            navigator=navigation(args,config,initial=owned,client=client)
            return chat(client,renderer,args,sessions=navigator)
        return follow(client, renderer, args)
    finally:
        if navigator:navigator.close()
        elif owned:owned.close()
        elif args.command=='attach':client.call('stop')


def execute(args, renderer, config):
    command = args.command
    if 'execution_mode' in config and command not in {'run','chat','create','resume','attach'}:
        raise ValueError('--execution-mode is a creation/resume option; stop the Session before changing it')
    if command == 'list':
        renderer.data(discover(args.workspace)); return 0
    if command in {'run','chat','create','resume'}:return run(args,renderer,config)
    directory = select(getattr(args, 'session', None), args.workspace)
    client = Client(directory)
    renderer.session_directory=directory
    if command == 'attach':
        if request(directory, 'status') is None: raise ValueError('No live CLI host; use `chat` to start/resume.')
        return run(args, renderer, config)
    if command == 'wake': renderer.data(client.call('wake'))
    elif command == 'processes':
        from .render import active_processes
        renderer.data(active_processes(client.call('status')))
    elif command in {'status', 'agents', 'speed'}: renderer.data(client.call(command))
    elif command in {'stop', 'close'}:
        result = client.call('shutdown' if command == 'close' else 'stop')
        if command == 'close':
            from session import SessionHostProtocol
            deadline = time.monotonic()+30
            while True:
                try:
                    if SessionHostProtocol.available(directory):break
                    raise ValueError('Host 仍在释放资源')
                except ValueError:
                    if time.monotonic() >= deadline: raise TimeoutError('CLI host is still releasing resources; check status.')
                    time.sleep(.05)
            result['status'] = 'closed'
        renderer.data(result)
    elif command == 'send': renderer.data(client.send(input_text(args), getattr(args, 'to', 'master'), getattr(args, 'image', [])))
    elif command == 'messages':
        if args.follow:
            if args.before or args.after: raise ValueError('Use --before/--after with paged reads, not --follow')
            return follow(client, renderer, args)
        page = client.call('messages', limit=args.limit, **{key: getattr(args, key) for key in ('before', 'after') if getattr(args, key)})
        renderer.agents = {item['id']: item for item in client.call('agents')}
        from .host import resolve_agent
        agent = resolve_agent(list(renderer.agents.values()), args.agent) if hasattr(args, 'agent') else None
        page['messages'] = [item for item in page['messages'] if
            (getattr(args, 'messages', 'human') == 'all' or item['kind'] != 'peer') and
            (not agent or agent in {item['source'], item['target']})]
        if renderer.format == 'text':
            for item in page['messages']: renderer.message(item)
        else: renderer.data(page)
    elif command in {'model', 'config'}:
        values = settings_input(args)
        if command == 'model' and args.value: values['model'] = args.value
        renderer.data(client.call('config', values=values) if values else client.call('status')['settings'])
    elif command == 'rename': renderer.data(client.call('rename', name=args.value))
    elif command == 'pin': renderer.data(client.call('rename', pinned=not args.off))
    elif command == 'compact':
        jobs=client.call('status').get('compaction',[])
        if args.cancel:
            selected=[operation for operation in jobs if operation['status'] in {'queued','running','cancelling'} and (not args.request_id or operation['request_id']==args.request_id)]
            renderer.data([client.call('compact',request_id=operation['request_id']) for operation in selected])
        else:renderer.data(jobs)
    elif command == 'export':
        output = Path(args.output).expanduser().resolve()
        if output == directory or output.is_relative_to(directory): raise ValueError('Export destination must be outside the source Session')
        if args.archive:
            raise ValueError('完整存档复制请先关闭 Session 后在文件系统进行；CLI 不提供离线浏览')
        else:
            records, cursor = [], 'start'
            while True:
                page = client.call('messages', after=cursor, limit=200); records += page['messages']
                if not page['has_more']: break
                cursor = page['after']
            with output.open('x', encoding='utf-8') as file: json.dump(records, file, ensure_ascii=False, indent=2)
        renderer.data({'exported': str(output)})
    elif command == 'delete':
        if not args.yes: raise ValueError('Deletion is irreversible; inspect --session and pass --yes.')
        renderer.data(client.call('delete'))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == '_host':
        packet = json.loads(sys.stdin.buffer.readline(65537))
        serve(packet['directory'], packet['config']); return 0
    args = parser().parse_args(argv)
    if args.command is None: args.command = 'chat'
    if not hasattr(args, 'workspace'): args.workspace = '.'
    renderer = Renderer(getattr(args, 'format', 'text'), getattr(args, 'color', 'auto'), getattr(args, 'timestamps', True))
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return execute(args, renderer, options(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except (ValueError, OSError, KeyError, TimeoutError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
