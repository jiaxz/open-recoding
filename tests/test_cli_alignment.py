"""Current team metadata, Node resources and real CLI multi-agent message flow."""

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
import time
from unittest.mock import patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input, DummyInput
from prompt_toolkit.output import DummyOutput

from recoding_cli.console import Console
from recoding_cli.render import Renderer, active_processes, execution_details, token_count
from recoding_cli.tui import TerminalUI
from test_tui import ModelClient
from workspace_support import temporary_workspace


def resources(now=100):
    return {'python_pool':{'processes':{
        'state':{'id':'state','name':'Shared memory','owner':'master','status':'ready','lifetime':'persistent','created_at':now-99},
        'old':{'id':'old','name':'Closed history','owner':'master','status':'closed','created_at':now-10},
        'temporary':{'id':'temporary','name':'Unused temporary','owner':'master','status':'ready','lifetime':'temporary','created_at':now-1}},'executions':{}},
        'bash_pool':{'processes':{'build':{'id':'build','name':'Compile','owner':'master','status':'running',
            'lifetime':'temporary','created_at':now-99,'active_execution':'current','node_id':'remote','node_name':'Build node'}},
            'executions':{'current':{'id':'current','process_id':'build','kind':'bash','status':'running',
                'mode':'async','lifetime':'temporary','created_at':now-10,'started_at':now-2}}}}


class CLIAlignmentTests(unittest.TestCase):
    def ui(self, root, **kwargs):
        client=ModelClient(root)
        ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput(),**kwargs)
        self.addCleanup(ui.close)
        ui.agents=client.agents
        ui.renderer.agents={item['id']:item for item in client.agents}
        return client,ui

    def test_agent_tokens_use_display_projection_and_unknown_stays_unknown(self):
        with temporary_workspace(prefix='tui-agent-metrics-') as root:
            client,ui=self.ui(root)
            ui.status={'agent_token_usage':{'master':{'input_tokens':12345,'compaction_saved_tokens':67890}}}
            rows=ui.agent_options()
            master=next(row for row in rows if row[3]=='master')
            self.assertIn('Input 12.3k',master[1]);self.assertIn('已压缩 ≈67.9k',master[1])
            child=next(row for row in rows if row[3]=='a'*32)
            self.assertIn('Input —',child[1]);self.assertIn('已压缩 —',child[1])
            self.assertNotIn('created_at',str(rows))
            self.assertFalse(client.calls,'Rendering must use cached display metadata')
            for value in (None,True,-1):self.assertEqual(token_count(value),'—')

    def test_process_projection_is_current_and_elapsed_is_per_call(self):
        snapshot=resources();original=deepcopy(snapshot)
        rows=active_processes(snapshot)
        self.assertEqual([(row['kind'],row['phase']) for row in rows],[('bash','run'),('python','ready')])
        call={**rows[0]['process'],**rows[0]['execution']}
        self.assertEqual(execution_details(call,now=100),'Build node · async · 无状态 · 2.0s')
        self.assertEqual(snapshot,original)
        self.assertEqual(len(active_processes(snapshot,'python')),1)
        self.assertEqual(execution_details({'status':'queued','created_at':1,'mode':'sync','lifetime':'persistent'},now=100),
                         'sync · 有状态 · 等待执行')
        snapshot['bash_pool']['processes'].clear()
        snapshot['bash_pool']['executions']['current'].update(status='unknown',node_id='remote',node_name='Build node')
        self.assertEqual(active_processes(snapshot)[0]['phase'],'run','Lost acknowledgements must remain visible')

    def test_process_panel_reads_host_cache_and_keeps_draft(self):
        with temporary_workspace(prefix='tui-process-panel-') as root:
            client,ui=self.ui(root)
            original=client.call
            client.call=lambda action,**fields:{**original(action,**fields),**resources()} if action=='status' else original(action,**fields)
            ui.buffer.text='Keep editing'
            ui.apply(ui.console.execute('/processes'))
            rows,kind,_=ui.options()
            self.assertEqual(kind,'processes')
            self.assertIn('bash · Compile',rows[0][0]);self.assertIn('async',rows[0][1]);self.assertIn('Build node',rows[0][1])
            self.assertIn('空闲',rows[1][1]);self.assertNotIn('sync',rows[1][1])
            self.assertIn('run 1 · ready 1',fragment_list_to_text(ui.panel_title()))
            self.assertEqual(ui.buffer.text,'Keep editing')
            self.assertFalse(any(action in {'send','stop','wake'} for action,_ in client.calls))

    def test_real_screen_refresh_and_resize_keep_focus_and_input(self):
        async def scenario(root):
            class Output(DummyOutput):
                size=Size(rows=32,columns=110)
                def get_size(self):return self.size
            output=Output();client=ModelClient(root);original=client.call
            def call(action,**fields):
                result=original(action,**fields)
                if action=='status':result.update(resources(time.time()),agent_token_usage={'master':{'input_tokens':12345,'compaction_saved_tokens':67890}})
                return result
            client.call=call
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(color='always'),SimpleNamespace(),input=keyboard,output=output)
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                async def wait(predicate):
                    for _ in range(100):
                        if predicate():return
                        await asyncio.sleep(.02)
                    self.fail('TUI did not update')
                try:
                    await wait(lambda:bool(ui.status.get('agent_token_usage')))
                    ui.buffer.text='Keep this draft';ui.open_panel('agents')
                    ui.invalidate();await asyncio.sleep(.1)
                    self.assertIn('已压缩',str(ui.panel_fragments()))
                    screen=ui.app.renderer._last_screen
                    text='\n'.join(''.join(screen.data_buffer[y][x].char for x in range(output.size.columns)) for y in range(output.size.rows))
                    Path(root,'agents-110.txt').write_text(text)
                    self.assertIn('Input 12.3k',text);self.assertIn('67.9k',text)
                    ui.apply(ui.console.execute('/processes'));ui.invalidate();await asyncio.sleep(.1)
                    for columns,lines in [(110,32),(48,22)]:
                        output.size=Size(rows=lines,columns=columns);ui.invalidate();await asyncio.sleep(.1)
                        screen=ui.app.renderer._last_screen
                        text='\n'.join(''.join(screen.data_buffer[y][x].char for x in range(columns)) for y in range(lines))
                        Path(root,f'processes-{columns}.txt').write_text(text)
                        self.assertIn('Processes',text);self.assertIn('Keep this draft',text)
                        self.assertNotIn('?[32m',text)
                        self.assertGreater(ui.panel_window.render_info._y_offset,ui.input_window.render_info._y_offset)
                    self.assertIs(ui.app.layout.current_control,ui.input_control)
                    self.assertEqual(ui.buffer.text,'Keep this draft')
                    keyboard.send_text('\x04');await asyncio.wait_for(task,2)
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
        with temporary_workspace(prefix='tui-current-screen-') as root:asyncio.run(scenario(root))
