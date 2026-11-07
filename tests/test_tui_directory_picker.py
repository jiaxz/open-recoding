"""Navigate the resume picker without recursion, runtime creation or lost input."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style
from session import SessionStore
from recoding_cli.render import Renderer
from recoding_cli.sessions import CliSessionNavigator
from recoding_cli.tui import TerminalUI, STYLES
from test_tui import ModelClient
from workspace_support import temporary_workspace


class DirectoryPickerTests(unittest.TestCase):
    def test_enter_child_and_parent_preserves_draft_without_resuming(self):
        async def scenario(root):
            here=SessionStore.create(root,'here',{})
            nested=SessionStore.create(Path(root)/'child'/'nested','there',{})
            for index in range(9):(Path(root)/f'folder-{index}').mkdir()
            nav=CliSessionNavigator(root,lambda _:self.fail('Browsing directories must not create a Host'))
            client=ModelClient(root)
            with create_pipe_input() as keyboard:
                ui=TerminalUI(client,Renderer(),SimpleNamespace(),sessions=nav,input=keyboard,output=DummyOutput())
                task=asyncio.create_task(ui.app.run_async(pre_run=ui.start))
                async def until(predicate):
                    deadline=asyncio.get_running_loop().time()+3
                    while not predicate():
                        self.assertLess(asyncio.get_running_loop().time(),deadline)
                        await asyncio.sleep(.02)
                def choose(directory):
                    options=ui.options()[0]
                    index=next(index for index,row in enumerate(options) if row[3]=='/resume --directory '+str(directory))
                    ui.activate(index)
                try:
                    ui.buffer.text='/resume';keyboard.send_text('\r')
                    await until(lambda:ui.manual_panel=='sessions')
                    self.assertEqual([item['id'] for item in ui.panel_data['sessions']],[here.id])
                    rows=ui.options()[0]
                    self.assertEqual(rows[0][3],'/resume '+str(here.folder))
                    self.assertTrue(rows[1][0].startswith('↰'))
                    self.assertTrue(all(row[0].startswith('▱') and row[1]=='' for row in rows[2:]))
                    fragments=ui.panel_fragments()
                    selected_style=next(style for style,label,*_ in fragments if '◈ here' in label)
                    directory_style=next(style for style,label,*_ in fragments if '▱ child' in label)
                    self.assertIn('class:session-entry',selected_style)
                    self.assertIn('class:directory',directory_style)
                    theme=Style.from_dict(STYLES)
                    self.assertEqual(theme.get_attrs_for_style_str(selected_style).bgcolor,'335163','Category colors must retain selected-row highlighting')
                    self.assertNotEqual(theme.get_attrs_for_style_str(selected_style).color,theme.get_attrs_for_style_str(directory_style).color)
                    keyboard.send_text('\x1b[A\x1b[A')
                    await asyncio.sleep(.06)
                    self.assertEqual(ui.selection,0,'Up at the first row must not wrap')
                    ui.selection=len(rows)-1
                    keyboard.send_text('\x1b[B\x1b[B')
                    await asyncio.sleep(.06)
                    self.assertEqual(ui.selection,len(rows)-1,'Down at the last row must not wrap')
                    self.assertIn('▱ child',str(ui.panel_fragments()))
                    self.assertNotIn('Ctrl+R',str(ui.panel_title()))
                    ui.buffer.text='正在写的草稿'
                    choose(Path(root)/'child')
                    await until(lambda:ui.panel_data['directory']==str(Path(root)/'child'))
                    self.assertEqual(ui.panel_data['sessions'],[])
                    choose(nested.workspace)
                    await until(lambda:ui.panel_data['directory']==str(nested.workspace))
                    self.assertEqual([item['id'] for item in ui.panel_data['sessions']],[nested.id])
                    self.assertEqual(ui.options()[0][0][3],'/resume '+str(nested.folder))
                    self.assertTrue(ui.options()[0][1][0].startswith('↰'))
                    choose(nested.workspace.parent)
                    await until(lambda:ui.panel_data['directory']==str(nested.workspace.parent))
                    self.assertEqual(ui.buffer.text,'正在写的草稿')
                    self.assertIs(ui.app.layout.current_control,ui.input_control)
                    self.assertFalse(any(action in {'resume','wake','send','stop'} for action,_ in client.calls))
                    with self.assertRaises(ValueError):ui.console.execute('/resume --recursive')
                    keyboard.send_text('\x04');await asyncio.wait_for(asyncio.shield(task),2)
                finally:
                    if not task.done():ui.app.exit();await task
                    ui.close();nav.close()
        with temporary_workspace(prefix='tui-directory-picker-') as root:asyncio.run(scenario(root))


if __name__=='__main__':unittest.main()
