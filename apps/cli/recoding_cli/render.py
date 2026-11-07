"""Message rendering for shell streams and TUI; model text never supplies ANSI."""

from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote

from multiagent.media import CODE, IMAGE_LINK


def plain(value):
    text = str(value)
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(char for char in text if char in '\n\t' or ord(char) >= 32 and not 127 <= ord(char) < 160)


ACTIVE_EXECUTIONS = {'submitting','starting','queued','running','stopping','cancelling','unresponsive','unknown'}


def token_count(value):
    if type(value) is not int or value < 0:return '—'
    for size,suffix in ((1000000,'M'),(1000,'k')):
        if value >= size:return f'{value/size:.1f}'.rstrip('0').rstrip('.')+suffix
    return str(value)


def execution_details(record, *, now=None):
    mode=record.get('mode','—')
    lifetime={'persistent':'有状态','temporary':'无状态'}.get(record.get('lifetime'),'—')
    started,finished=record.get('started_at'),record.get('finished_at')
    if type(started) in (int,float):
        end=finished if type(finished) in (int,float) else time.time() if now is None else now
        duration=f'{max(0,end-started):.1f}s'
    else:duration='等待执行' if record.get('status') in {'queued','starting','submitting'} else '耗时未知'
    node=record.get('node_name') or record.get('node_id')
    return ' · '.join(([str(node)] if node and node!='local' else [])+[mode,lifetime,duration])


def active_processes(snapshot, kind=None):
    """Project current resources from Host data; never execute or inspect the node."""
    rows=[]
    for language in ('python','bash'):
        if kind and language!=kind:continue
        pool=snapshot.get(language+'_pool') or {}
        processes=dict(pool.get('processes',{}))
        executions={item['id']:item for item in snapshot.get('executions',[]) if item.get('kind')==language}
        executions.update(pool.get('executions',{}))
        by_process={}
        for record in executions.values():
            if record['status'] not in ACTIVE_EXECUTIONS:continue
            identifier=record['process_id']
            by_process.setdefault(identifier,[]).append(record)
            processes.setdefault(identifier,{'id':identifier,'name':record.get('name',language),
                'owner':record.get('owner',record.get('source')),'status':record['status'],
                'lifetime':record.get('lifetime','temporary'),'created_at':record.get('created_at',0),
                'node_id':record.get('node_id'),'node_name':record.get('node_name')})
        for process in processes.values():
            if process['status'] in {'closed','failed'}:continue
            calls=sorted(by_process.get(process['id'],[]),key=lambda item:item.get('created_at',0))
            call=next((item for item in calls if item['id']==process.get('active_execution')),None)
            call=call or next((item for item in calls if item['status']!='queued'),None) or (calls[0] if calls else None)
            running=call or process.get('active_execution') or process.get('queued',0) or process['status'] in ACTIVE_EXECUTIONS | {'closing'}
            phase='run' if running else 'ready' if process['status']=='ready' and process.get('lifetime')!='temporary' else None
            if phase:rows.append({'kind':language,'process':process,'execution':call,'phase':phase})
    return sorted(rows,key=lambda item:(item['phase']!='run',-item['process'].get('created_at',0)))


class Renderer:
    def __init__(self, format='text', color='auto', timestamps=True):
        self.format, self.timestamps = format, timestamps
        self.color = format == 'text' and (color == 'always' or color == 'auto' and sys.stdout.isatty())
        self.agents = {}
        self.message_sink = None
        self.session_directory = None

    def image_paths(self, item):
        """Display immutable host files where possible; never open or fetch images."""
        folder=Path(self.session_directory) if self.session_directory else None
        workspace=folder.parent.parent if folder else None
        owner=item.get('image_owner') or (item['target'] if item['source']=='user' or item['kind']=='peer' else item['source'])
        assets=folder if owner=='master' else folder/'agents'/owner if folder and re.fullmatch(r'[a-f0-9]{32}',owner) else None
        paths,seen=[],set()
        def source(value):
            if not isinstance(value,str) or not value or value.lower().startswith('data:'):return None
            return unquote(re.sub(r'\\([\\() ])',r'\1',value)).removeprefix('sandbox:')
        def host_path(value):
            if workspace and value.startswith('/workspace/'):
                return str(workspace/value.removeprefix('/workspace/'))
            if workspace and not Path(value).is_absolute() and not re.match(r'[a-zA-Z][\w+.-]*:',value):
                return str(workspace/value)
            return value
        for image in item.get('images',[]):
            for key in ('source','path'):
                if value:=source(image.get(key)):seen.add(value)
            asset=image.get('asset','')
            if assets and isinstance(asset,str) and re.fullmatch(r'images/[a-f0-9]{64}\.(png|jpg)',asset):
                paths.append(str(assets/asset))
            elif value:=source(image.get('path')) or source(image.get('source')):
                paths.append(host_path(value))
            else:
                paths.append(str(image.get('asset') or image.get('name') or '图片')+'（未提供完整路径）')
        for match in IMAGE_LINK.finditer(CODE.sub('',item.get('content',''))):
            value=source(match[1] or match[2])
            if value and value not in seen:paths.append(host_path(value));seen.add(value)
        return list(dict.fromkeys(plain(path) for path in paths))

    def data(self, value):
        if self.format != 'text':
            print(json.dumps(value, ensure_ascii=False), flush=True)
        elif isinstance(value, list):
            for item in value: print(plain(json.dumps(item, ensure_ascii=False)), flush=True)
        else:
            print(plain(json.dumps(value, ensure_ascii=False, indent=2)), flush=True)

    def label(self, value):
        if value == 'user': return 'you'
        item = self.agents.get(value, {})
        return plain(f"{item.get('icon', '')} {item.get('name', value)}".strip())

    def route(self,item):
        copied=item.get('delivery')=='cc'
        targets=item.get('to',[item['target']]) if copied else [item['target']]
        route=f"{self.label(item['source'])} → {', '.join(self.label(target) for target in targets)}"
        if copied:route+=f" · CC → {self.label(item['target'])}（知情副本）"
        elif item.get('cc'):route+=' · CC: '+', '.join(self.label(target) for target in item['cc'])
        return route

    def card(self, item, *, width=None):
        from rich import box
        from rich.align import Align
        from rich.console import Group
        from rich.markdown import Markdown
        from rich.padding import Padding
        from rich.panel import Panel
        from rich.text import Text
        human = item['source'] == 'user'
        peer = item['kind'] == 'peer'
        if peer:
            accent, background, foreground = '#a997bd', '#1b1822', '#bcb5c6'
            category, border = 'Agent 对话 · 内部通信'+(' · 抄送' if item.get('delivery')=='cc' else ''), box.SQUARE
        elif human:
            accent, background, foreground = '#85c9f2', '#122c40', '#e1eff9'
            category, border = '你发出的消息', box.ROUNDED
        else:
            accent, background, foreground = '#efd08a', '#000000', '#eff4ed'
            category, border = '✉ 给你的消息', box.DOUBLE
        route = Text(self.route(item),style='bold '+accent)
        title = Text(category,style='bold '+accent)
        stamp = datetime.fromtimestamp(item['timestamp']).astimezone().strftime('%H:%M:%S') if self.timestamps else None
        parts = [route,Markdown(plain(item['content']),code_theme='monokai',hyperlinks=False)]
        for path in self.image_paths(item):
            parts.append(Text('▧ '+path,style='#8eb4cf'))
        inset = (2 if width>=40 else 1 if width>=20 else 0) if width and peer else 0
        right_indent = (4 if width>=60 else 2 if width>=32 else 0) if width and human else 0
        card_width = width-2*inset-right_indent if width else None
        card = Panel(Group(*parts),title=title,title_align='left',subtitle=stamp,subtitle_align='right',
                     border_style=accent,style=foreground+' on '+background,box=border,
                     padding=(0,1),width=card_width)
        if inset:return Padding(card,(0,inset))
        if right_indent:return Align.right(card,width=width)
        return card

    def card_text(self, item, width):
        """Rich produces trusted styles; the TUI parses them as structured fragments."""
        from rich.console import Console
        from rich.theme import Theme
        buffer = StringIO()
        console = Console(file=buffer, width=max(12, width), force_terminal=self.color,
            color_system='256' if self.color else None, no_color=not self.color,
            theme=Theme({'markdown.h1':'bold #a7dec6', 'markdown.h2':'bold #9fcbe8',
                         'markdown.code':'bold #edc579 on #29323a', 'markdown.link':'underline #83bfe3'}))
        console.print(self.card(item,width=max(12,width)))
        return buffer.getvalue()

    def message(self, item):
        if self.message_sink:
            self.message_sink(item)
            return
        if self.format != 'text':
            self.data(item)
            return
        if self.color:
            try:
                from rich.console import Console
                console=Console(force_terminal=True)
                console.print(self.card(item,width=console.width))
                return
            except ImportError:
                pass
        stamp = datetime.fromtimestamp(item['timestamp']).astimezone().strftime('%H:%M:%S')+'  ' if self.timestamps else ''
        print(f"{stamp}{self.route(item)}\n{plain(item['content'])}", flush=True)
        for path in self.image_paths(item):
            print('  [image] '+path, flush=True)
        print(flush=True)
