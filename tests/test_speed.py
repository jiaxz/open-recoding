"""Focused speed arithmetic, public data projection and terminal dashboard checks."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size

from session import Session,SavedSession,SessionStore
from multiagent.storage import atomic_json
from multiagent.storage import MemberStore
from recoding_cli.render import Renderer
from recoding_cli.speed import SessionRate,SpeedPlot,TOTAL
from multiagent.model_config import profile_for
from recoding_cli.tui import TerminalUI
from test_tui import ModelClient
from workspace_support import temporary_workspace


def sample(tokens=100,stamp=100,elapsed=10,model='grok-4.7',**fields):
    return {'run_id':'test','step':1,'timestamp':stamp,'elapsed':elapsed,'output_tokens':tokens,
            'rate':tokens/elapsed,'model':model,**fields}


class SpeedTests(unittest.TestCase):
    def test_model_calls_are_dots_and_only_the_session_aggregate_is_a_line(self):
        plot=SpeedPlot()
        plot.update({'started_at':80,'as_of':150,'samples':[sample(100,100),sample(200,140)]})
        fragments=plot.render(80,12,now=150)
        model_styles={'fg:'+profile_for(point['model']).color for point in plot.samples}
        dots=[text for style,text,*_ in fragments if style in model_styles and text=='•']
        self.assertEqual(len(dots),2)
        self.assertFalse(any(style in model_styles and any(0x2800<=ord(char)<=0x28ff for char in text)
                             for style,text,*_ in fragments),'Calls must not be joined by model-colored lines')
        self.assertTrue(any(style==TOTAL and any(0x2800<=ord(char)<=0x28ff for char in text)
                            for style,text,*_ in fragments),'The additive Session estimate keeps its curve')

    def test_adjustable_recent_window_remains_anchored_to_now(self):
        plot=SpeedPlot();plot.update({'started_at':0,'as_of':20000,'samples':[sample(100,19000)]})
        with patch.object(plot,'now',return_value=20000):
            plot.select('wider')
            self.assertEqual(plot.view['preset'],'recent')
            self.assertEqual(plot.bounds(20000),(12800,20000))
            self.assertEqual(plot.bounds(20100),(12900,20100))
            plot.select('narrower');self.assertEqual(plot.bounds(20000),(16400,20000))
            self.assertIn('最近1小时',fragment_list_to_text(plot.render(80,12,now=20000)))
            plot.select('in');fixed=plot.bounds(20000)
            self.assertEqual(plot.bounds(20100),fixed)
            plot.select('wider');self.assertEqual(plot.bounds(20100)[1],20100)
            plot.select('reset');self.assertEqual(plot.view['preset'],'1h')

    def test_concurrent_agents_add_and_thinking_is_not_added_again(self):
        points=[sample(100,reasoning_tokens=999),sample(200,agent_id='sub')]
        curve=SessionRate(points)
        self.assertEqual(curve(100),30)
        self.assertEqual(curve(105),15)
        self.assertEqual(curve(110),0)
        self.assertEqual(curve(80),0)
        plot=SpeedPlot();plot.update({'started_at':80,'as_of':105,'samples':points})
        text=fragment_list_to_text(plot.render(70,12,now=105))
        self.assertIn('最近 20.0',text)
        self.assertIn('合计10s 15.0',text)
        self.assertIn('NOW',text)
        self.assertTrue(any(0x2800<=ord(c)<=0x28ff for c in text))

    def test_visible_window_rescales_and_manual_window_does_not_follow_clock(self):
        plot=SpeedPlot()
        points=[sample(100000,10),sample(100,50),sample(100,3690,model='gemini-4-argon')]
        plot.update({'started_at':0,'as_of':3700,'samples':points})
        plot.render(70,12,now=3700)
        self.assertLess(plot.y_range[1],30)
        plot.select('in')
        self.assertIsNone(plot.view['preset'])
        bounds=plot.bounds(3700)
        self.assertEqual(plot.bounds(3800),bounds)
        plot.select('reset');self.assertEqual(plot.view['preset'],'1h')
        self.assertGreater(plot.bounds(3800)[1],bounds[1])
        plot.select('all');plot.render(70,12,now=3700)
        self.assertGreater(plot.y_range[1],1000)

    def test_empty_or_invalid_usage_is_not_fabricated_and_models_keep_colors(self):
        plot=SpeedPlot()
        plot.update({'started_at':80,'as_of':100,'samples':[sample(elapsed=1,output_tokens=float('nan'))]})
        text=fragment_list_to_text(plot.render(60,10,now=100))
        self.assertIn('等待 API 回报 Output',text)
        self.assertIn('合计10s —',text)
        plot.update({'started_at':80,'as_of':105,'samples':[sample(),sample(200,101,model='gemini-4-argon'),sample(150,102,model='gemini-3.8-flash')]})
        fragments=plot.render(70,12,now=105)
        styles={style for style,_,*rest in fragments}
        for model in ('grok-4.7','gemini-4-argon','gemini-3.8-flash'):
            self.assertIn('fg:'+profile_for(model).color,styles)
        compact=fragment_list_to_text(plot.render(30,3,now=105))
        self.assertIn('增大终端',compact)

    def test_live_and_saved_metrics_never_return_context_or_tool_bodies(self):
        raw=sample(content='private-body',reasoning_content='private-reasoning')
        peer=sample(200,agent_id='a'*32,context='private-context')
        session=Session.__new__(Session)
        session.master=SimpleNamespace(condition=threading.Condition(),model_rates={('test',1):raw})
        session.team=SimpleNamespace(rates=lambda:[peer], event_stream=SimpleNamespace(condition=threading.Condition()))
        session.store=SimpleNamespace(metadata={'created_at':'2026-10-25T00:00:00+00:00'})
        live=session.output_speed()
        self.assertEqual(len(live['samples']),2)
        self.assertTrue(live['live'])
        self.assertNotIn('private-',str(live))
        with temporary_workspace(prefix='speed-archive-') as root:
            store=SessionStore.create(root,'speed-test',{})
            store.append_event({'sequence':1,'type':'model_end','timestamp':100,'run_id':'test','step':1,
                                'elapsed':10,'model':'grok-4.7','usage':{'completion_tokens':100}})
            MemberStore.create_agent(store,'a'*32,'speed-peer',{})
            atomic_json(store.folder/'agents/index.json',{'version':5,'messages':[],'agents':[{'id':'a'*32,'rates':[peer]}]})
            saved=SavedSession(store.folder)
            with patch.object(saved.store,'load_state',side_effect=AssertionError('must not load context')):
                result=saved.output_speed()
            self.assertEqual(len(result['samples']),2)
            self.assertFalse(result['live'])
            self.assertNotIn('private-',str(result))

    def test_speed_dashboard_stays_below_input_and_refreshes_only_when_open(self):
        async def scenario(root):
            class Client(ModelClient):
                def call(self,action,**fields):
                    if action=='speed':
                        self.calls.append((action,fields));now=time.time()
                        return {'as_of':now,'started_at':now-60,'live':True,
                                'samples':[sample(100,now-8),sample(200,now-2,model='gemini-4-argon')]}
                    return super().call(action,**fields)
            class Output(DummyOutput):
                def get_size(self):return Size(rows=32,columns=90)
            client=Client(root)
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(color='always'),SimpleNamespace(),input=keyboard,output=Output())
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                try:
                    await asyncio.sleep(.1)
                    self.assertFalse(any(action=='speed' for action,_ in client.calls))
                    ui.buffer.text='/speed';keyboard.send_text('\r');await asyncio.sleep(.2)
                    self.assertEqual(ui.manual_panel,'speed')
                    deadline=time.monotonic()+2
                    while ui.speed_window.render_info is None and time.monotonic()<deadline:await asyncio.sleep(.02)
                    self.assertIsNotNone(ui.speed_window.render_info)
                    self.assertGreater(ui.speed_window.render_info._y_offset,ui.input_window.render_info._y_offset)
                    self.assertIs(ui.app.layout.current_control,ui.input_control)
                    self.assertIn('← 扩大时间窗口',fragment_list_to_text(ui.footer()))
                    keyboard.send_text('\x1b[D');await asyncio.sleep(.1)
                    self.assertEqual(ui.speed_plot.view['preset'],'recent')
                    self.assertEqual(ui.speed_plot.view['seconds'],7200)
                    keyboard.send_text('\x1b[C');await asyncio.sleep(.1)
                    self.assertEqual(ui.speed_plot.view['seconds'],3600)
                    ui.buffer.text='正在编辑';ui.buffer.cursor_position=len(ui.buffer.text)
                    keyboard.send_text('\x1b[D');await asyncio.sleep(.1)
                    self.assertEqual(ui.buffer.cursor_position,len(ui.buffer.text)-1)
                    self.assertEqual(ui.speed_plot.view['seconds'],3600)
                    ui.buffer.text=''
                    count=sum(action=='speed' for action,_ in client.calls)
                    await asyncio.sleep(1.1)
                    self.assertGreater(sum(action=='speed' for action,_ in client.calls),count)
                    ui.buffer.text='/speed 10m';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertEqual(ui.speed_plot.view['preset'],'10m')
                    ui.buffer.text='/speed in';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertIsNone(ui.speed_plot.view['preset'])
                    screen=ui.app.renderer._last_screen
                    Path(root,'screen.txt').write_text('\n'.join(''.join(screen.data_buffer[y][x].char for x in range(90)) for y in range(32)))
                    ui.buffer.text='/speed off';keyboard.send_text('\r');await asyncio.sleep(.15)
                    self.assertIsNone(ui.manual_panel)
                    count=sum(action=='speed' for action,_ in client.calls)
                    await asyncio.sleep(.5)
                    self.assertEqual(sum(action=='speed' for action,_ in client.calls),count)
                    self.assertFalse(any(action in {'resume','wake','send'} for action,_ in client.calls))
                    import json
                    Path(root,'test-report.json').write_text(json.dumps({'passed':True,'external_model_api_calls':0,
                        'checks':['curve below fixed composer','visible-only refresh','time presets','zoom preserves manual window',
                                  'no model or tool action'],'screen':'screen.txt'},ensure_ascii=False,indent=2)+'\n')
                    ui.app.exit();await task
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close()
        with temporary_workspace(prefix='speed-tui-') as root:asyncio.run(scenario(root))


if __name__=='__main__':unittest.main()
