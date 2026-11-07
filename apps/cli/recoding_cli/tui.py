"""Full-screen terminal shell: scrolling correspondence above a fixed composer.

Only this application layer knows terminal layout. Backend work is dispatched
outside the UI thread; no model/tool observations are inserted into the chat.
"""

import asyncio
import shlex
from queue import Empty, Queue
import threading
import time

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIControl, UIContent
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame

from multiagent.model_config import default_model, model_choices
from .client import MessageStream,NoSessionClient
from .console import COMMANDS, Console
from .host import resolve_agent
from .render import plain, token_count, active_processes, execution_details
from .tui_messages import Conversation
from .speed import ACTIONS as SPEED_ACTIONS, SpeedPlot

LIVE = {'starting','running','sleeping'}
STYLES = {
    '': '#d8e2e5 bg:#10171b', 'brand':'bold #a8ddc6', 'muted':'#85979f',
    'header':'bg:#19262d #c4d1d7', 'input':'bg:#1c303c #eef3f5',
    'input.command':'bold #eac37a', 'input.mention':'bold #a8d6ed',
    'frame.border':'#598ba0', 'frame.label':'bold #a9d8ed',
    'panel':'bg:#18252d #d2e0e5', 'panel.title':'bold #aacddd bg:#21343f',
    'directory':'bold #90c7ea', 'session-entry':'#e7c681',
    'selected':'bg:#335163 #ffffff bold', 'active':'#9cd5b5', 'stopped':'#b7a28c',
    'error':'#f0a4a4', 'notice':'#a5d6b5', 'button':'bold #a6d2e5',
    'footer':'bg:#19262d #a1b4be',
}


def fit(text, width):
    result=''
    for char in plain(text).replace('\n',' '):
        if get_cwidth(result+char)>max(0,width-1): return result+'…'
        result+=char
    return result


class InputLexer(Lexer):
    def lex_document(self, document):
        def line(index):
            text=document.lines[index]
            if index==0 and text.startswith(('@','/')):
                token, separator, rest=text.partition(' ')
                return [('class:input.mention' if text.startswith('@') else 'class:input.command',token),
                        ('class:input',separator+rest)]
            return [('class:input',text)]
        return line


class SpeedControl(UIControl):
    def __init__(self,plot):self.plot=plot
    def create_content(self,width,height):
        from prompt_toolkit.formatted_text.utils import split_lines
        lines=list(split_lines(self.plot.render(width,height)))
        return UIContent(get_line=lambda index:lines[index],line_count=len(lines),show_cursor=False)


class TerminalUI:
    def __init__(self, client, renderer, args, *, sessions=None, input=None, output=None):
        self.client, self.renderer, self.args=client, renderer, args
        self.console=Console(client,args,sessions=sessions)
        self.status, self.agents={},[]
        self.manual_panel, self.panel_data=None,None
        self.selection=0
        self.dismissed=None
        self.notice, self.error='',False
        self.loop=None
        self.done=threading.Event()
        self.tasks=Queue(maxsize=16)
        self.stopping=False
        self.last_error=None
        self.saved_drafts=[]
        self.speed_plot=SpeedPlot()
        self.conversation=Conversation(renderer,self.invalidate)
        self.conversation.scope=self.console.scope
        self.buffer=Buffer(multiline=True,history=InMemoryHistory(),on_text_changed=self.changed)
        self.input_control=BufferControl(buffer=self.buffer,lexer=InputLexer())
        self.input_window=Window(self.input_control,height=self.composer_height,wrap_lines=True,style='class:input',
            get_line_prefix=lambda line,wrap:[('class:input.mention',' › ' if line==0 and wrap==0 else '   ')])
        self.message_window=Window(self.conversation,wrap_lines=False,
            get_vertical_scroll=lambda _:self.conversation.offset,
            right_margins=[ScrollbarMargin(display_arrows=False)],height=Dimension(min=2,weight=1))
        self.panel_control=FormattedTextControl(self.panel_fragments,show_cursor=False,
            get_cursor_position=lambda:Point(0,self.selection))
        self.panel_window=Window(self.panel_control,height=self.panel_height,wrap_lines=False,
            get_vertical_scroll=lambda _:max(0,self.selection-self.panel_rows()+1),style='class:panel')
        self.speed_window=Window(SpeedControl(self.speed_plot),height=self.speed_height,style='class:panel')
        speed=ConditionalContainer(Frame(self.speed_window,title=' Session · Output speed · All agents '),
            filter=Condition(lambda:self.manual_panel=='speed' and not self.options()[0]))
        composer=Frame(self.input_window,title=self.composer_title,style='class:input')
        panel=ConditionalContainer(HSplit([
            Window(FormattedTextControl(self.panel_title),height=1,style='class:panel.title'),self.panel_window]),
            filter=Condition(lambda:bool(self.options()[0])))
        self.container=HSplit([
            Window(FormattedTextControl(self.header),height=1,style='class:header'),
            ConditionalContainer(Window(FormattedTextControl(self.maintenance_status),height=1),
                                 filter=Condition(lambda:any(operation['status'] in {'queued','running','cancelling'} for operation in self.status.get('compaction',[])))),
            self.message_window,
            ConditionalContainer(Window(FormattedTextControl(self.unread_bar),height=1),
                                 filter=Condition(lambda:not self.conversation.follow)),
            composer,
            ConditionalContainer(Window(FormattedTextControl(self.notification),height=1),
                                 filter=Condition(lambda:bool(self.notice))),
            panel,speed,
            Window(FormattedTextControl(self.footer),height=1,style='class:footer'),
        ])
        self.app=Application(layout=Layout(self.container,focused_element=self.input_control),
            key_bindings=self.bindings(),full_screen=True,mouse_support=True,
            style=Style.from_dict(STYLES if renderer.color else {}),
            color_depth=None if renderer.color else ColorDepth.DEPTH_1_BIT,
            refresh_interval=.5,min_redraw_interval=.03,input=input,output=output)
        self.stream=MessageStream(client,renderer,scope='all',history=getattr(args,'history',20))
        self.previous_sink=self.renderer.message_sink
        self.renderer.message_sink=lambda item:self.post(lambda:self.conversation.add(item))

    def invalidate(self):
        if hasattr(self,'app'): self.app.invalidate()

    def post(self, callback):
        if self.loop and not self.loop.is_closed() and not self.done.is_set():
            def update():
                if not self.done.is_set(): callback(); self.invalidate()
            self.loop.call_soon_threadsafe(update)

    def changed(self, _):
        self.dismissed=None
        self.selection=0
        self.invalidate()

    def label(self, identifier):
        if identifier=='all': return '@all · 全部存活 Agent'
        if identifier in {'subs','all_except_master'}: return '@subs · 存活 sub'
        return self.renderer.label(identifier)

    def click(self, callback):
        def handle(event):
            if event.event_type==MouseEventType.MOUSE_UP: callback()
            return None
        return handle

    def header(self):
        size=self.app.output.get_size().columns
        name=self.status.get('name','Session')
        left=fit(' recoding...'+(' · '+name if size>=80 else ''),max(12,size//2))
        state='待开始' if isinstance(self.client,NoSessionClient) else self.status.get('status','connecting')
        model=self.status.get('settings',{}).get('model',self.status.get('model',''))
        mode='Host 无隔离' if self.status.get('settings',{}).get('execution_mode')=='host' else 'Sandbox'
        middle=fit(f' · {state} · {mode} · {model} ',max(0,size-get_cwidth(left)-1))
        return [('class:brand',left),('class:muted',middle)]

    def maintenance_status(self):
        active=[operation for operation in self.status.get('compaction',[]) if operation['status'] in {'queued','running','cancelling'}]
        label=' · '.join(f"{operation['name']} ({operation['status']})" for operation in active)
        return [('class:notice',' '+fit('后台压缩 · '+label+' · /compact cancel 取消 · Agent 继续工作',self.app.output.get_size().columns-2))]

    def composer_height(self):
        width=max(1,(self.app.output.get_size().columns if hasattr(self,'app') else 80)-5)
        lines=sum(max(1,(get_cwidth(line)+width-1)//width) for line in self.buffer.document.lines)
        return Dimension.exact(min(5,max(1,lines)))

    def speed_height(self):
        available=self.app.output.get_size().rows-self.composer_height().preferred-9-int(bool(self.notice))-int(not self.conversation.follow)
        return Dimension.exact(max(2,min(14,available)))

    def composer_title(self):
        width=max(10,self.app.output.get_size().columns-9)
        with self.console.lock:
            title=' 发给 '+self.label(self.console.target)
            if self.console.attachments: title+=f' · {len(self.console.attachments)} 张图片'
        return [('class:frame.label',fit(title,width)+' ')]

    def unread_bar(self):
        count=self.conversation.unread
        return [('class:button',f' ↓ 最新{f" · {count} 条新消息" if count else ""}  [Ctrl+End]',
                 self.click(self.conversation.latest))]

    def notification(self):
        return [('class:error' if self.error else 'class:notice',
                 ' '+fit(self.notice,self.app.output.get_size().columns-2))]

    def footer(self):
        options,_,automatic=self.options()
        enter='Enter 执行命令' if self.buffer.text.startswith('/') else 'Enter 发送'
        if options:
            selectable=any(row[2]!='none' for row in options)
            if automatic or not self.buffer.text:confirm=' · Tab/Enter 确认' if selectable else ''
            else:confirm=(' · Tab 确认' if selectable else '')+' · '+enter
            hints=f'↑↓ 选择{confirm} · '+'Esc 关闭'
        elif self.speed_controls_active():
            hints='← 扩大时间窗口 · → 缩短时间窗口 · Esc 关闭'
        elif self.manual_panel=='speed':
            hints=enter+' · ←→ 编辑 · /speed off 关闭曲线 · Esc 关闭'
        elif self.console.failed:
            hints='/retry 恢复草稿 · /help'
        elif self.buffer.text.startswith('/'):
            hints='Enter 执行命令 · /help'
        elif self.buffer.text.strip() or self.console.attachments:
            hints='Enter 发送 · Alt+Enter 换行 · / 命令'
        elif isinstance(self.client,NoSessionClient):
            hints='@ 收件人 · 输入第一条消息创建 Session · /resume 恢复 · /help'
        else:
            hints='@ 收件人 · /agents · /speed · /settings · /stop · /help'
        if not self.conversation.follow and not options:hints='Ctrl+End 最新 · '+hints
        return [('class:footer',fit(' '+hints+' · Ctrl+D 退出',self.app.output.get_size().columns))]

    def speed_controls_active(self):
        return self.manual_panel=='speed' and not self.options()[0] and not self.buffer.text

    def sorted_agents(self):
        members=sorted(self.agents,key=lambda item:str(item.get('created_at','')),reverse=True)
        return sorted(members,key=lambda item:(item['id']!='master',item.get('status') not in LIVE))

    def agent_options(self,query='',kind='target'):
        options=[]
        if isinstance(self.client,NoSessionClient):
            return [('Master','首条消息发送时创建 Session','target','master')] if kind=='target' and (not query or query.casefold() in 'master') else []
        if kind=='target':
            options.extend([('@all · 全部存活 Agent','广播','target','all'),
                            ('@subs · 存活 sub','广播，不含 Master','target','all_except_master')])
        elif kind=='view': options.append(('全部 Agent','消息汇总','view',None))
        for item in self.sorted_agents():
            label=f"{item.get('icon','')} {item['name']}"
            usage=self.status.get('agent_token_usage',{}).get(item['id'],{})
            saved=token_count(usage.get('compaction_saved_tokens'))
            detail=f"{item.get('status','saved')} · Input {token_count(usage.get('input_tokens'))} · 已压缩 {'≈' if saved!='—' else ''}{saved} · {item['id'][:8]}"
            options.append((label,detail,kind,item['id']))
        if query:
            lowered=query.casefold()
            options=[row for row in options if lowered in f'{row[0]} {row[3]}'.casefold()]
        return options

    def options(self):
        text=self.buffer.text
        automatic=text!=self.dismissed
        if automatic and text.startswith('@') and not any(char.isspace() for char in text):
            return self.agent_options(text[1:]),'recipients',True
        for prefix,kind in [('/to ','target'),('/view ','view')]:
            if automatic and text.startswith(prefix): return self.agent_options(text[len(prefix):],kind),kind,True
        if automatic and text.startswith('/speed '):
            query=text[len('/speed '):]
            return [(key,label,'execute','/speed '+key) for key,label in SPEED_ACTIONS.items() if query in key],'speed_options',True
        if automatic and text.startswith('/model '):
            query=text[len('/model '):].casefold()
            choices=list(dict.fromkeys([*model_choices(),self.status.get('settings',{}).get('model','')]))
            return [(name,'切换全 Session 模型','execute','/model '+name) for name in choices if name and query in name.casefold()],'models',True
        if automatic and text.startswith('/messages '):
            query=text[len('/messages '):]
            return [(name,label,'execute','/messages '+name) for name,label in
                    [('all','人机 + Agent 之间的全部消息'),('human','只看人机消息')] if query in name],'messages',True
        if automatic and text.startswith('/') and ' ' not in text:
            return [(command,description,'insert' if command.endswith(' ') else 'execute',command)
                    for command,description in COMMANDS if command.startswith(text)],'commands',True
        if self.manual_panel=='sessions':
            listing=self.panel_data or {};records=listing.get('sessions',[])
            query=text[len('/resume '):].casefold() if text.startswith('/resume ') else ''
            rows=[('◈ '+item['name'],'Session · '+item['id'][:8],
                   'execute','/resume '+shlex.quote(item['session_directory'])) for item in records
                  if not query or query in (item['name']+' '+item['id']+' '+item['session_directory']).casefold()]
            if listing.get('parent'):rows.append(('↰ ..','返回上一级','execute','/resume --directory '+shlex.quote(listing['parent'])))
            rows += [('▱ '+directory.rsplit('/',1)[-1],'','execute','/resume --directory '+shlex.quote(directory)) for directory in listing.get('directories',[])]
            return rows or [('此目录没有 Session 或子目录','可直接输入 /resume 存档路径','none',None)],'sessions',bool(rows) and text.startswith('/resume ')
        if self.manual_panel=='agents': return self.agent_options(),'agents',False
        if self.manual_panel=='help':
            return [('Enter / Alt+Enter','发送 / 换行','none',None),('PgUp / PgDn · Ctrl+End','滚动 / 最新','none',None),
                    ('@ / /agents','选择收件人 / 查看 Agent','none',None),
                    ('/messages all|human','全部消息 / 人机消息','none',None),
                    ('/settings','设置','execute','/settings'),
                    ('/stop','停止全部 Agent','execute','/stop'),
                    ('Esc / Ctrl+C','关闭面板，保留草稿，不停止 Agent','none',None),
                    ('Ctrl+D · /quit · /exit','离开 TUI','execute','/quit'),
                    *[(command,description,'insert' if command.endswith(' ') else 'execute',command) for command,description in COMMANDS]],'help',False
        if self.manual_panel=='settings':
            settings=self.status.get('settings',{})
            return [('模型',str(settings.get('model',default_model())),'insert','/model '),
                    ('sub 并发上限',str(settings.get('max_subagents',16)),'insert','/max-agents '),
                    ('兜底休眠',str(settings.get('idle_sleep_seconds',10))+' 秒','insert','/sleep '),
                    ('消息显示','全部消息' if self.console.scope=='all' else '只看人机','insert','/messages '),
                    ('查看 Agent',self.label(self.console.view) if self.console.view else '全部','insert','/view '),
                    ('Session 名字',self.status.get('name',''),'insert','/rename '),
                    ('查看工作目录和状态','','execute','/status'),
                    ('Session 输出速度曲线','','execute','/speed'),
                    ('运行 / ready 进程','','execute','/processes'),
                    ('唤醒 Master','','execute','/wake'),
                    ('恢复/切换 Session','','execute','/resume')],'settings',False
        if self.manual_panel=='status':
            rows=[('Workspace',self.status.get('directory',str(self.client.directory.parent.parent)),'none',None),
                  ('Session',str(self.client.directory),'none',None),
                  ('状态',self.status.get('status','unknown'),'none',None),
                  ('Agent 数',str(len(self.agents)),'execute','/agents')]
            return rows,'status',False
        if self.manual_panel in {'processes','python_pool'}:
            records=active_processes(self.status,'python' if self.manual_panel=='python_pool' else None)
            rows=[]
            for item in records:
                process,call=item['process'],item['execution']
                node=process.get('node_name') or process.get('node_id')
                prefix=str(node)+' · ' if node and node!='local' else ''
                info=execution_details({**process,**call}) if call else prefix+('空闲 · 有状态' if item['phase']=='ready' else '等待调用状态')
                rows.append((item['kind']+' · '+process['name'],item['phase']+' · '+info+' · '+self.label(process.get('owner','master'))+' · 全员可用',
                    'execute' if call else 'none','/executions '+call['id'] if call else None))
            return rows or [('没有运行或 ready 进程','由 Agent 按需创建；历史执行使用 /executions 查看','none',None)],self.manual_panel,False
        if self.manual_panel=='executions':
            data=self.panel_data or {}
            rows=[(item['id'][:8]+' · '+item.get('name',item['kind']),
                item['kind']+' · '+item['status']+' · '+execution_details(item),'execute','/executions '+item['id']) for item in data.get('executions',[])]
            return rows or [('没有后台任务','使用 bash(mode=async) 或 Python Pool 异步执行','none',None)],'executions',False
        if self.manual_panel=='execution_output':
            data=self.panel_data or {};execution=data.get('execution',{})
            rows=[(execution.get('name','Execution'),execution.get('status',''),'none',None)]
            for stream in ('stdout','stderr'):
                rows.append((stream,'字节游标 '+str(data.get(stream+'_offset',0)),'none',None))
                rows.extend((line,'','none',None) for line in data.get(stream,'').splitlines())
            if execution.get('error'):rows.append(('错误',execution['error'],'none',None))
            if execution.get('status') in {'starting','running','stopping'} or any(data.get(key+'_has_more') for key in ('stdout','stderr')):
                rows.append(('读取后续输出','','execute',f"/executions {execution['id']} {data['stdout_offset']} {data['stderr_offset']}"))
            rows.append(('返回任务列表','','execute','/executions'))
            return rows,'execution_output',False
        if self.manual_panel=='compact':
            jobs=self.status.get('compaction',[])
            if not jobs:return [('没有后台压缩任务','','none',None)],'compact',False
            rows=[]
            for item in jobs:
                rows.append((f"{item.get('name','Agent')} · {item['status']}",item['request_id'][:8],'none',None))
                if item['status'] in {'queued','running','cancelling'}:
                    rows.append(('取消后台压缩','Agent 继续运行','execute',f"/compact cancel {item['request_id']}"))
                if item.get('error'):rows.append(('未采用新摘要',item['error'],'none',None))
            return rows,'compact',False
        return [],'',False

    def panel_rows(self):
        return max(1,min(7,self.app.output.get_size().rows-10))

    def panel_height(self):
        return Dimension(min=1,max=self.panel_rows(),preferred=min(len(self.options()[0]),self.panel_rows()))

    def panel_title(self):
        options,kind,_=self.options()
        if kind=='sessions':
            listing=self.panel_data or {};count=len(listing.get('sessions',[]))
            return [('class:panel.title',fit(f" {listing.get('directory','')} · {count} Sessions · ↑↓/Enter 选择 · Esc 关闭",self.app.output.get_size().columns))]
        if kind in {'processes','python_pool'}:
            rows=active_processes(self.status,'python' if kind=='python_pool' else None)
            counts={phase:sum(row['phase']==phase for row in rows) for phase in ('run','ready')}
            return [('class:panel.title',f" {'Python' if kind=='python_pool' else 'Processes'} · run {counts['run']} · ready {counts['ready']} · Esc 关闭 " )]
        title={'recipients':'@ 收件人','target':'收件人','view':'查看范围','agents':'Agent 列表',
               'commands':'命令','models':'Session 模型','messages':'消息显示','settings':'设置',
               'help':'帮助','status':'Session 信息','compact':'后台上下文压缩','sessions':'恢复 Session','speed_options':'速度曲线','python_pool':'Session Python Pool','executions':'执行记录','execution_output':'执行日志'}.get(kind,kind)
        return [('class:panel.title',f' {title} · {len(options)} 项 · ↑↓ 选择 · Tab/Enter 确定 · Esc 关闭 ')]

    def panel_fragments(self):
        options,kind,_=self.options()
        if not options: return []
        self.selection=min(self.selection,len(options)-1)
        width=self.app.output.get_size().columns
        fragments=[]
        for index,row in enumerate(options):
            chosen=index==self.selection
            label,detail,_,value=row
            left=fit(label,max(8,min(32,width//2)))
            line=fit(f' {"›" if chosen else " "} {left}  {detail}',width)
            style='class:selected' if chosen else 'class:panel'
            if not chosen and detail.startswith(('failed','error')):style+=' class:error'
            elif not chosen and detail.startswith(('running','sleeping','starting')):style+=' class:active'
            elif not chosen and detail.startswith(('stopped','stopping')):style+=' class:stopped'
            if kind=='sessions' and value:
                style+=' class:directory' if value.startswith('/resume --directory ') else ' class:session-entry'
            fragments.append((style,line,self.click(lambda index=index:self.activate(index))))
            fragments.append(('', '\n'))
        # Keep the selected row visible without moving keyboard focus away from input.
        if fragments: fragments.pop()
        return fragments

    def activate(self,index=None):
        options,_,_=self.options()
        if not options:return
        row=options[self.selection if index is None else index]
        _,_,action,value=row
        if action=='target':
            self.console.select(value)
            if self.buffer.text.startswith(('@','/to ')):self.buffer.text=''
            self.manual_panel=None
            self.notice=''
        elif action=='view':
            self.console.view=value; self.conversation.agent=value
            self.conversation.dirty=True
            if self.buffer.text.startswith('/view '):self.buffer.text=''
            self.manual_panel=None
        elif action=='insert':
            if self.buffer.text and not self.buffer.text.startswith(('/','@')):self.saved_drafts.append(self.buffer.text)
            self.buffer.text=value;self.buffer.cursor_position=len(value)
            self.manual_panel=None
        elif action=='execute':
            if self.buffer.text.startswith(('/','@')):self.buffer.text=''
            self.submit(value)
        self.selection=0
        self.invalidate()

    def open_panel(self,name):
        self.manual_panel=None if self.manual_panel==name else name
        self.dismissed=self.buffer.text
        self.selection=0;self.notice='';self.invalidate()

    def apply(self,result):
        if result.get('panel')=='speed':
            self.speed_plot.update(result['data'])
            self.speed_plot.select(result.get('speed_action'))
        if 'panel' in result:
            self.manual_panel=result['panel'];self.panel_data=result.get('data');self.selection=0
        if result.get('data') and result.get('panel') in {'settings','status','processes'}: self.status=result['data']
        if result.get('panel')=='python_pool':self.status['python_pool']=result['data']
        if 'scope' in result:self.conversation.scope=result['scope'];self.conversation.dirty=True
        if 'view' in result:self.conversation.agent=result['view'];self.conversation.dirty=True
        if result.get('history'):self.conversation.follow=False
        for item in result.get('history',[]):self.conversation.add(item,new=False)
        if 'draft' in result:
            if not self.buffer.text:self.buffer.text=result['draft'];self.buffer.cursor_position=len(result['draft'])
            else:
                with self.console.lock:self.console.failed.append({'text':result['draft'],'target':self.console.target,'images':[]})
        if self.saved_drafts and not self.buffer.text and not result.get('sent'):
            self.buffer.text=self.saved_drafts.pop();self.buffer.cursor_position=len(self.buffer.text)
        self.notice=result.get('notice','');self.error=False
        if result.get('switch'):self.app.exit(result=result['switch'])
        elif result.get('quit'):self.app.exit()

    def submit(self,text):
        if text.strip()=='/draft':
            if self.saved_drafts and not self.buffer.text:
                self.buffer.text=self.saved_drafts.pop();self.buffer.cursor_position=len(self.buffer.text)
                self.notice='原草稿已恢复';self.error=False
            else:self.notice='请先清空当前输入，或当前没有暂存草稿'
            self.invalidate();return
        if text.strip() in {'/quit','/exit'}:self.app.exit();return
        if text.strip()=='/stop':self._stop_from_command();return
        with self.console.lock:target=self.console.target
        try:self.tasks.put_nowait((text,target))
        except Exception:
            if not self.buffer.text:self.buffer.text=text;self.buffer.cursor_position=len(text)
            self.notice='操作队列已满，草稿已保留';self.error=True
        self.invalidate()

    def _stop_from_command(self):
        if self.stopping:return
        self.stopping=True;self.notice='正在停止全部 Agent…';self.error=False;self.invalidate()
        def work():
            try:result=self.console.execute('/stop')
            except Exception as error:result={'error':str(error)}
            def finish():
                self.stopping=False
                if 'error' in result:self.notice=plain(result['error']);self.error=True
                else:self.apply(result)
            self.post(finish)
        threading.Thread(target=work,daemon=True,name='tui-stop').start()

    def bindings(self):
        bindings=KeyBindings()
        @bindings.add('enter')
        def enter(event):
            options,_,automatic=self.options()
            if options and (automatic or not self.buffer.text):self.activate();return
            text=self.buffer.text
            self.buffer.append_to_history();self.buffer.text=''
            self.submit(text)
        @bindings.add('escape','enter')
        def newline(event):self.buffer.insert_text('\n')
        @bindings.add('tab')
        def select(event):
            if self.options()[0]:self.activate()
            else:self.buffer.insert_text('    ')
        @bindings.add('down',filter=Condition(lambda:bool(self.options()[0])))
        def down(event):self.selection=min(self.selection+1,len(self.options()[0])-1);self.invalidate()
        @bindings.add('up',filter=Condition(lambda:bool(self.options()[0])))
        def up(event):self.selection=max(0,self.selection-1);self.invalidate()
        @bindings.add('left',filter=Condition(self.speed_controls_active))
        def wider(event):self.speed_plot.select('wider');self.invalidate()
        @bindings.add('right',filter=Condition(self.speed_controls_active))
        def narrower(event):self.speed_plot.select('narrower');self.invalidate()
        @bindings.add('pageup')
        def pageup(event):self.conversation.scroll(-max(1,self.conversation.height-2))
        @bindings.add('pagedown')
        def pagedown(event):self.conversation.scroll(max(1,self.conversation.height-2))
        @bindings.add('c-home')
        def first(event):self.conversation.offset=0;self.conversation.follow=False
        @bindings.add('c-end')
        def latest(event):self.conversation.latest()
        @bindings.add('c-c')
        def cancel(event):
            self.dismiss_panel()
            self.notice='草稿已保留；停止 Agent 请使用 /stop'
            self.error=False
        @bindings.add('escape')
        def escape(event):
            self.dismiss_panel()
        @bindings.add('c-d')
        def exit_tui(event):
            self.submit('/quit')
        return bindings

    def dismiss_panel(self):
        self.manual_panel=None
        self.dismissed=self.buffer.text
        self.notice=''
        self.invalidate()

    def command_worker(self):
        while not self.done.is_set():
            try:text,target=self.tasks.get(timeout=.2)
            except Empty:continue
            try:
                result=self.console.execute(text,target=target)
                self.post(lambda result=result:self.apply(result))
            except Exception as error:
                message=plain(error)+(' · /retry 恢复草稿' if not text.startswith('/') else '')
                def failed(message=message):
                    if self.saved_drafts and not self.buffer.text:
                        self.buffer.text=self.saved_drafts.pop();self.buffer.cursor_position=len(self.buffer.text)
                    self.notice=message;self.error=True
                self.post(failed)

    def poll(self):
        last_status=0;last_speed=0;failed=False;view_resolved=False
        while not self.done.is_set():
            try:
                self.stream.poll()
                agents=list(self.renderer.agents.values())
                if agents and not view_resolved:
                    view=resolve_agent(agents,self.console.view) if self.console.view else None
                    target=resolve_agent(agents,self.console.target)
                    self.console.select(target)
                    view_resolved=True
                    def set_view(view=view):
                        self.console.view=view;self.conversation.agent=view;self.conversation.dirty=True
                    self.post(set_view)
                status=self.client.call('status') if time.monotonic()-last_status>=1 else None
                if status:last_status=time.monotonic()
                speed=None
                if self.manual_panel=='speed' and time.monotonic()-last_speed>=1:
                    if self.speed_plot.data is None or self.speed_plot.data.get('live',True):
                        speed=self.client.call('speed')
                    last_speed=time.monotonic()
                def update(agents=agents,status=status,speed=speed):
                    self.agents=agents
                    if speed:self.speed_plot.update(speed)
                    if status:
                        self.status=status
                        if status.get('error') and status['error']!=self.last_error:
                            self.last_error=status['error'];self.notice='Agent 运行失败：'+plain(status['error']);self.error=True
                self.post(update);failed=False
            except Exception as error:
                if not failed:
                    def failure(message=plain(error)):
                        self.notice='连接异常：'+message;self.error=True
                        if '独占' in message or '另一个运行环境' in message:
                            self.conversation.records.clear();self.conversation.cache.clear();self.conversation.dirty=True
                            self.manual_panel=None
                    self.post(failure)
                failed=True
            self.done.wait(.35)

    def view_state(self):
        return {'console':self.console,'records':dict(self.conversation.records),
                'anchor':self.conversation.anchor(),'follow':self.conversation.follow,
                'unread':self.conversation.unread,'draft':self.buffer.text,
                'saved_drafts':list(self.saved_drafts),'cursor':self.stream.cursor,'ready':self.stream.ready,
                'speed_view':dict(self.speed_plot.view)}

    def restore_view(self,state):
        self.console=state['console'];self.console.client=self.client
        self.conversation.scope=self.console.scope;self.conversation.agent=self.console.view
        self.conversation.records=state['records'];self.conversation.follow=state['follow']
        self.conversation.unread=state['unread']
        self.conversation.restore_anchor=state['anchor']
        self.buffer.text=state['draft'];self.buffer.cursor_position=len(self.buffer.text)
        self.saved_drafts=state['saved_drafts']
        self.stream.cursor=state['cursor'];self.stream.ready=state['ready']
        self.speed_plot.view=dict(state.get('speed_view',self.speed_plot.view))

    def start(self):
        self.loop=asyncio.get_running_loop()
        self.workers=[threading.Thread(target=method,daemon=True,name=name) for method,name in
                      [(self.poll,'tui-mail'),(self.command_worker,'tui-commands')]]
        for thread in self.workers:thread.start()

    def close(self):
        self.done.set();self.renderer.message_sink=lambda item:None
        workers=getattr(self,'workers',[])
        for thread in workers:thread.join(timeout=.5)
        if not any(thread.is_alive() for thread in workers):self.renderer.message_sink=self.previous_sink

    def run(self):
        try:
            return self.app.run(pre_run=self.start) or 0
        finally:
            self.close()
