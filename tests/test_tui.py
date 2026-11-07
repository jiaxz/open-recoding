"""Focused TUI rendering, interaction and fixed-composer layout checks."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

from prompt_toolkit.formatted_text import ANSI,to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input, DummyInput
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size

from recoding_cli.console import Console
from recoding_cli.render import Renderer
from recoding_cli.tui import TerminalUI
from recoding_cli.tui_messages import Conversation
from recoding_cli.client import NoSessionClient
from workspace_support import temporary_workspace


class ModelClient:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.agents=[{'id':'master','name':'Master','icon':'🧭','status':'running'},
                     {'id':'a'*32,'name':'闲聊伙伴','icon':'🌱','status':'sleeping'},
                     {'id':'b'*32,'name':'已停止','icon':'📚','status':'stopped'}]
        self.items=[]
        self.calls=[]
        self.settings={'model':'grok-4.7','max_subagents':6,'idle_sleep_seconds':10}
    def call(self,action,**fields):
        self.calls.append((action,fields))
        if action=='agents':return self.agents
        if action=='status':return {'name':'TUI 本机测试','status':'run','directory':str(self.directory),
                                   'settings':self.settings,'compaction':[]}
        if action=='messages':
            rows=list(self.items)
            if fields.get('after') not in (None,'start'):
                index=next(i for i,row in enumerate(rows) if row['id']==fields['after'])
                rows=rows[index+1:]
            limit=fields.get('limit',100)
            rows=rows[:limit] if fields.get('after') else rows[-limit:]
            return {'messages':rows,'after':rows[-1]['id'] if rows else None,'has_more':False}
        if action=='config':self.settings.update(fields['values']);return self.settings
        if action in {'stop','resume','wake','rename'}:return {'status':'stopped' if action=='stop' else 'running'}
        raise ValueError(action)
    def send(self,content,target='master',images=()):
        self.calls.append(('send',{'content':content,'target':target,'images':list(images)}))


def message(index=0,peer=False):
    return {'id':str(index),'timestamp':time.time()+index,'source':'a'*32,
            'target':'master' if peer else 'user','kind':'peer' if peer else 'human_output',
            'content':'## 春天的计划\n\n**先检查数据**，再开始。\n\n> 有出处的引用\n\n```python\nvalue = 42\nprint(value)\n```\n\n| 项目 | 结果 |\n|---|---|\n| 检查 | 通过 |','images':[]}


class TUITests(unittest.TestCase):
    def test_python_pool_panel_shows_owner_status_and_sharing_without_execution(self):
        with temporary_workspace(prefix='tui-pool-') as root:
            client=ModelClient(root);original=client.call
            def call(action,**fields):
                if action=='python_pool':return {'processes':{'p1':{'id':'p1','name':'shared state','owner':'master','status':'ready','participants':['a'*32],'created_at':1}},'executions':{}}
                return original(action,**fields)
            client.call=call
            ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput())
            ui.buffer.text='keep draft';ui.apply(ui.console.execute('/python'))
            rows,kind,_=ui.options();self.assertEqual(kind,'python_pool')
            self.assertIn('shared state',rows[0][0]);self.assertIn('ready',rows[0][1]);self.assertIn('全员可用',rows[0][1])
            self.assertEqual(ui.buffer.text,'keep draft')

    def test_jobs_panel_reads_current_agent_logs_without_stopping_or_changing_draft(self):
        with temporary_workspace(prefix='tui-jobs-') as root:
            client=ModelClient(root);original=client.call;identifier='c'*32
            def call(action,**fields):
                if action=='executions':
                    client.calls.append((action,fields))
                    return {'agent_id':fields['agent_id'],'executions':[{'id':identifier,'kind':'python','name':'Build','status':'running'}]}
                if action=='execution_output':
                    client.calls.append((action,fields))
                    return {'execution':{'id':identifier,'name':'Build','status':'running'},'stdout':'line one\nline two\n','stderr':'','stdout_offset':18,'stderr_offset':0}
                return original(action,**fields)
            client.call=call
            ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput())
            ui.console.view='a'*32;ui.buffer.text='keep draft'
            ui.apply(ui.console.execute('/executions'))
            rows,kind,_=ui.options();self.assertEqual(kind,'executions');self.assertEqual(rows[0][0],'cccccccc · Build')
            self.assertEqual(client.calls[-1][1]['agent_id'],'a'*32)
            ui.apply(ui.console.execute('/executions '+identifier))
            rows,kind,_=ui.options();self.assertEqual(kind,'execution_output')
            self.assertIn('line two',[row[0] for row in rows])
            self.assertEqual(ui.buffer.text,'keep draft')
            self.assertFalse(any(action in {'stop','wake'} for action,_ in client.calls))

    def test_background_compaction_is_visible_and_cancellable_without_focus_or_draft_changes(self):
        with temporary_workspace(prefix='tui-background-context-') as root:
            client=ModelClient(root)
            jobs=[{'request_id':'context-test','agent_id':'master','name':'Master','status':'running'}]
            original=client.call
            def call(action,**fields):
                if action=='status':return {**original(action,**fields),'compaction':jobs}
                if action=='compact':client.calls.append((action,fields));return {'cancelled':True}
                return original(action,**fields)
            client.call=call
            ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput())
            try:
                ui.status=client.call('status');ui.buffer.text='正在编辑，不打断'
                self.assertIn('Master',fragment_list_to_text(ui.maintenance_status()))
                self.assertIn('/compact',fragment_list_to_text(ui.maintenance_status()))
                self.assertIsNone(ui.manual_panel)
                result=ui.console.execute('/compact');ui.apply(result)
                self.assertEqual(ui.options()[1],'compact')
                ui.dismiss_panel()
                self.assertFalse(any(action=='compact' for action,_ in client.calls),'Esc closes the panel without cancelling')
                ui.console.execute('/compact cancel context-test')
                self.assertIn(('compact',{'request_id':'context-test'}),client.calls)
                self.assertEqual(ui.buffer.text,'正在编辑，不打断')
            finally:ui.close()

    def test_blank_start_and_footer_only_show_applicable_controls(self):
        async def scenario(root):
            with create_pipe_input() as keyboard:
                ui=TerminalUI(NoSessionClient(root),Renderer(),SimpleNamespace(),input=keyboard,output=DummyOutput())
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                try:
                    await asyncio.sleep(.15)
                    footer=fragment_list_to_text(ui.footer())
                    self.assertIn('/resume',footer)
                    self.assertNotIn('Esc',footer)
                    self.assertNotIn('/stop',footer)
                    self.assertFalse(ui.error,ui.notice)
                    ui.open_panel('help')
                    self.assertIn('Esc 关闭',fragment_list_to_text(ui.footer()))
                    ui.buffer.text='保留草稿'
                    footer=fragment_list_to_text(ui.footer())
                    self.assertIn('Tab 确认',footer)
                    self.assertIn('Enter 发送',footer)
                    self.assertNotIn('Tab/Enter 确认',footer)
                    ui.dismiss_panel()
                    self.assertIn('Enter 发送',fragment_list_to_text(ui.footer()))
                    self.assertNotIn('Esc',fragment_list_to_text(ui.footer()))
                    keyboard.send_text('\x1b');await asyncio.sleep(.6)
                    self.assertEqual(ui.buffer.text,'保留草稿')
                    self.assertFalse((Path(root)/'.recoding').exists())
                    keyboard.send_text('\x04')
                    await asyncio.wait_for(asyncio.shield(task),2)
                    self.assertFalse((Path(root)/'.recoding').exists())
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
        with temporary_workspace(prefix='tui-empty-start-') as root:asyncio.run(scenario(root))

    def test_markdown_has_real_styles_and_json_stays_a_clean_protocol(self):
        renderer=Renderer(color='always')
        renderer.agents={'a'*32:{'name':'闲聊伙伴','icon':'🌱'}}
        output=renderer.card_text(message(),60)
        fragments=to_formatted_text(ANSI(output))
        text=fragment_list_to_text(fragments)
        self.assertIn('春天的计划',text)
        self.assertIn('value = 42',text)
        self.assertNotIn('**先检查数据**',text)
        self.assertNotIn('```python',text)
        self.assertNotIn('\x1b',text)
        self.assertGreater(len({style for style,_,*rest in fragments if style}),5)
        peer=fragment_list_to_text(to_formatted_text(ANSI(renderer.card_text(message(peer=True),60))))
        self.assertIn('Agent 对话',peer)
        stream=StringIO()
        with redirect_stdout(stream):Renderer(format='jsonl',color='always').message(message())
        self.assertNotIn('\x1b',stream.getvalue())
        import json
        self.assertEqual(json.loads(stream.getvalue())['kind'],'human_output')

    def test_personal_and_peer_cards_are_distinct_without_color(self):
        renderer=Renderer(color='never')
        renderer.agents={'a'*32:{'name':'闲聊伙伴','icon':'🌱'},'master':{'name':'Master','icon':'🧭'}}
        personal=renderer.card_text(message(),64)
        peer=renderer.card_text(message(peer=True),64)
        outgoing={**message(),'source':'user','target':'master','kind':'human_input'}
        user=renderer.card_text(outgoing,64)
        self.assertIn('给你的消息',personal)
        self.assertIn('╔',personal)
        self.assertIn('你发出的消息',user)
        self.assertTrue(user.splitlines()[0].startswith('    ╭'))
        self.assertIn('Agent 对话',peer)
        self.assertIn('内部通信',peer)
        self.assertTrue(peer.splitlines()[0].startswith('  ┌'))
        for rendered in (personal,peer,user):self.assertNotIn('\x1b',rendered)

    def test_messages_between_agents_are_visible_by_default_and_scrolling_keeps_anchor(self):
        renderer=Renderer(color='always')
        conversation=Conversation(renderer,lambda:None)
        conversation.scope='all'
        for i in range(8):conversation.add(message(i,peer=i%2==0))
        conversation.create_content(76,12)
        conversation.scroll(-20)
        anchor=conversation.anchor()
        conversation.add(message(9,peer=True))
        conversation.create_content(76,12)
        self.assertEqual(conversation.anchor()[0],anchor[0])
        self.assertEqual(conversation.unread,1)
        conversation.create_content(44,12)
        self.assertEqual(conversation.anchor()[0],anchor[0])
        conversation.latest();conversation.create_content(44,12)
        self.assertEqual(conversation.unread,0)
        self.assertEqual(conversation.offset,max(0,len(conversation.rows)-12))

    def test_at_routing_configuration_and_failed_draft(self):
        with temporary_workspace(prefix='tui-controller-') as root:
            client=ModelClient(root);console=Console(client,SimpleNamespace())
            self.assertEqual(console.scope,'all')
            console.execute('@闲聊伙伴 你的建议呢？')
            self.assertEqual(client.calls[-1],('send',{'content':'你的建议呢？','target':'a'*32,'images':[]}))
            console.execute('/to all')
            self.assertEqual(console.target,'all')
            console.execute('/model gemini-4-argon')
            self.assertEqual(client.settings['model'],'gemini-4-argon')
            with self.assertRaises(ValueError):console.execute('@不存在的Agent 请保留这句话')
            self.assertEqual(console.execute('/retry')['draft'],'@不存在的Agent 请保留这句话')
            console.execute('/view master')
            self.assertEqual(console.view,'master')
            def fail(*_):raise ValueError('暂时断线')
            client.send=fail
            with self.assertRaises(ValueError):console.execute('保留这条草稿')
            recovered=console.execute('/retry')
            self.assertEqual(recovered['draft'],'保留这条草稿')

    def test_agent_selection_and_settings_do_not_discard_the_draft(self):
        with temporary_workspace(prefix='tui-draft-') as root:
            client=ModelClient(root)
            ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput())
            ui.agents=client.agents
            ui.renderer.agents={item['id']:item for item in client.agents}
            ui.buffer.text='不要覆盖这条正在写的消息'
            ui.open_panel('agents');ui.activate(3)
            self.assertEqual(ui.console.target,'a'*32)
            self.assertEqual(ui.buffer.text,'不要覆盖这条正在写的消息')
            ui.open_panel('settings');ui.activate(0)
            self.assertEqual(ui.buffer.text,'/model ')
            ui.buffer.text=''
            ui.apply({'panel':'settings','data':client.call('status'),'notice':'设置已保存'})
            self.assertEqual(ui.buffer.text,'不要覆盖这条正在写的消息')
            ui.renderer.message_sink=None

    def test_resume_selects_a_session_and_wake_only_restarts_master(self):
        from recoding_cli.client import Client
        with temporary_workspace(prefix='tui-resume-') as root:
            client=ModelClient(root)
            class Navigator:
                chosen=[]
                def listing(self):return {'directory':root,'parent':None,'directories':[],
                    'sessions':[{'name':'Session Two','id':'c'*32,'directory':root,'session_directory':str(Path(root)/'session two')}]}
                def wake(self,client):return client.call('wake')
                def restore(self,value):self.chosen.append(value);return Client(Path(root)/'session two')
            navigation=Navigator()
            console=Console(client,SimpleNamespace(),sessions=navigation)
            result=console.execute('/resume')
            self.assertEqual(result['panel'],'sessions')
            self.assertEqual([action for action,_ in client.calls if action=='resume'],[])
            result=console.execute('/resume "Session Two"')
            self.assertEqual(navigation.chosen,['Session Two'])
            self.assertEqual(result['switch'].directory,Path(root)/'session two')
            console.execute('/wake')
            self.assertEqual([action for action,_ in client.calls if action=='wake'],['wake'])
            self.assertEqual([action for action,_ in client.calls if action=='resume'],[])
            self.assertEqual(navigation.chosen,['Session Two'])
            with self.assertRaises(ValueError):console.execute('/wake other-agent')
            ui=TerminalUI(client,Renderer(),SimpleNamespace(),sessions=navigation,input=DummyInput(),output=DummyOutput())
            ui.apply(console.execute('/resume'))
            self.assertEqual(ui.options()[1],'sessions')
            self.assertIn('Session Two',str(ui.panel_fragments()))
            ui.activate(0)
            import shlex
            self.assertEqual(shlex.split(ui.tasks.get_nowait()[0]),['/resume',str(Path(root)/'session two')])
            ui.buffer.text='切换 Session 前的草稿'
            state=ui.view_state()
            restored=TerminalUI(client,Renderer(),SimpleNamespace(),sessions=navigation,input=DummyInput(),output=DummyOutput())
            restored.restore_view(state)
            self.assertEqual(restored.buffer.text,'切换 Session 前的草稿')
            ui.close();restored.close()

    def test_only_slash_stop_stops_and_function_keys_have_no_actions(self):
        async def scenario(root):
            client=ModelClient(root)
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=keyboard,output=DummyOutput())
                ui.app.timeoutlen=.02;ui.app.ttimeoutlen=.02
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                try:
                    await asyncio.sleep(.1)
                    ui.buffer.text='正在编辑的消息';ui.buffer.cursor_position=len(ui.buffer.text)
                    for keys in ['\x1b','\x03','\x1bOP','\x1bOQ','\x1bOR','\x1bOS','\x1b[21~']:
                        keyboard.send_text(keys);await asyncio.sleep(.04)
                    self.assertFalse(task.done())
                    self.assertEqual(ui.buffer.text,'正在编辑的消息')
                    self.assertEqual([action for action,_ in client.calls if action=='stop'],[])
                    self.assertNotIn('[Stop]',str(ui.header()))
                    self.assertNotIn('F1',str(ui.footer()))
                    ui.buffer.text='/';await asyncio.sleep(.04)
                    self.assertTrue(ui.options()[0])
                    keyboard.send_text('\x1b');await asyncio.sleep(.06)
                    self.assertFalse(ui.options()[0])
                    self.assertEqual(ui.buffer.text,'/')
                    ui.buffer.text='/agents';keyboard.send_text('\r');await asyncio.sleep(.1)
                    self.assertEqual(ui.manual_panel,'agents')
                    ui.buffer.text='/stop';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertEqual([action for action,_ in client.calls if action=='stop'],['stop'])
                    self.assertFalse(task.done())
                    ui.app.exit();await task
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
        with temporary_workspace(prefix='tui-stop-') as root:asyncio.run(scenario(root))

    def test_ctrl_d_exits_with_empty_input_draft_or_open_panel(self):
        async def scenario(root,draft,panel):
            client=ModelClient(root)
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=keyboard,output=DummyOutput())
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                try:
                    await asyncio.sleep(.05)
                    ui.buffer.text=draft
                    if panel:ui.open_panel(panel)
                    keyboard.send_text('\x04')
                    result=await asyncio.wait_for(asyncio.shield(task),2)
                    self.assertIsNone(result)
                    self.assertEqual(ui.buffer.text,draft)
                    self.assertFalse(any(action in {'send','stop','shutdown','resume','wake'} for action,_ in client.calls))
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
                self.assertTrue(ui.done.is_set())
                self.assertFalse(any(thread.is_alive() for thread in ui.workers))
        with temporary_workspace(prefix='tui-ctrl-d-') as root:
            for draft,panel in [('',None),('未发送的草稿',None),('保留输入','agents')]:
                with self.subTest(draft=draft,panel=panel):asyncio.run(scenario(root,draft,panel))

    def test_fullscreen_layout_menu_and_resize_with_background_messages(self):
        async def scenario(root):
            client=ModelClient(root);client.items=[message(0),message(1,peer=True)]
            class Output(DummyOutput):
                size=Size(rows=32,columns=100)
                def get_size(self):return self.size
            output=Output()
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(color='always'),SimpleNamespace(),input=keyboard,output=output)
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                try:
                    await asyncio.sleep(.1)
                    ui.renderer.agents={item['id']:item for item in client.agents}
                    ui.agents=client.agents;ui.status=client.call('status')
                    for row in client.items:ui.conversation.add(row)
                    ui.invalidate();await asyncio.sleep(.1)
                    self.assertTrue(ui.app.full_screen)
                    self.assertEqual(ui.conversation.scope,'all')
                    self.assertGreater(ui.input_window.render_info._y_offset,ui.message_window.render_info._y_offset)
                    keyboard.send_text('@');await asyncio.sleep(.1)
                    self.assertIn('闲聊伙伴',str(ui.panel_fragments()))
                    self.assertGreater(ui.panel_window.render_info._y_offset,ui.input_window.render_info._y_offset)
                    keyboard.send_text('\x1b[B\x1b[B\x1b[B\t');await asyncio.sleep(.1)
                    self.assertEqual(ui.console.target,'a'*32)
                    ui.buffer.text='我在编辑，不要抢焦点'
                    ui.conversation.add(message(2,peer=True));ui.invalidate();await asyncio.sleep(.1)
                    self.assertEqual(ui.buffer.text,'我在编辑，不要抢焦点')
                    self.assertIs(ui.app.layout.current_control,ui.input_control)
                    output.size=Size(rows=22,columns=48);ui.invalidate();await asyncio.sleep(.1)
                    self.assertEqual(ui.conversation.width,47)
                    ui.open_panel('settings');ui.invalidate();await asyncio.sleep(.1)
                    self.assertGreater(ui.panel_window.render_info._y_offset,ui.input_window.render_info._y_offset)
                    screen=ui.app.renderer._last_screen
                    lines=[''.join(screen.data_buffer[y][x].char for x in range(output.size.columns)) for y in range(output.size.rows)]
                    Path(root,'screen.txt').write_text('\n'.join(lines))
                    self.assertNotIn('?[32m','\n'.join(lines))
                    self.assertIn('设置','\n'.join(lines))
                    self.assertEqual(ui.input_window.render_info.window_height,1)
                    ui.manual_panel=None;ui.buffer.text='/max-agents 8';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertEqual(client.settings['max_subagents'],8)
                    ui.buffer.text='/messages human';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertEqual(ui.conversation.scope,'human')
                    ui.buffer.text='/messages all';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertEqual(ui.conversation.scope,'all')
                    import json
                    Path(root,'test-report.json').write_text(json.dumps({'passed':True,'external_model_api_calls':0,
                        'checks':['fixed bottom composer','menus below input','@ keyboard selection','peer messages by default',
                                  'draft/focus preservation','terminal resize','settings execution','message scope toggle'],
                        'screen':'screen.txt'},ensure_ascii=False,indent=2)+'\n')
                    ui.app.exit();await task
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
        with temporary_workspace(prefix='tui-layout-') as root:asyncio.run(scenario(root))


if __name__=='__main__':unittest.main()
