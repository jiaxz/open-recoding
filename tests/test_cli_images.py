"""CLI images show usable host paths, including immutable sub-Agent snapshots."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from multiagent.storage import MemberStore
from multiagent import MultiAgentSystem
from session import SavedSession, SessionStore
from recoding_cli.client import MessageStream
from recoding_cli.render import Renderer
from workspace_support import PROJECT, temporary_workspace


class CLIImageTests(unittest.TestCase):
    def test_human_and_peer_cards_show_the_correct_immutable_host_file(self):
        with temporary_workspace(prefix='cli-image-paths-') as root:
            store=SessionStore.create(root,'image-paths',{})
            sub=MemberStore.create_agent(store,'a'*32,'artist',{})
            data=(PROJECT/'tests/fixtures/small.jpg').read_bytes()
            renderer=Renderer(color='never')
            MessageStream(SimpleNamespace(directory=store.folder),renderer)
            for owner,agent in [('master',store),('a'*32,sub)]:
                image=agent.add_upload(data,'figure.jpg');image['source']='/workspace/figure.jpg'
                expected=str(agent.folder/image['asset'])
                self.assertEqual(Path(expected).read_bytes(),data)
                for kind,source,target in [('human_output',owner,'user'),('human_input','user',owner),('peer','master',owner)]:
                    item={'id':'message','kind':kind,'source':source,'target':target,'image_owner':owner,
                          'timestamp':100,'content':'![图](/workspace/figure.jpg)','images':[image]}
                    self.assertEqual(renderer.image_paths(item),[expected],'Do not duplicate the same Markdown/attachment image')
                    self.assertIn(expected,renderer.card_text(item,480))
                    output=StringIO()
                    with redirect_stdout(output):renderer.message(item)
                    self.assertIn('[image] '+expected,output.getvalue())
            output=StringIO()
            with redirect_stdout(output):Renderer(format='jsonl').message(item)
            self.assertEqual(json.loads(output.getvalue()),item,'JSON protocol preserves original metadata')

    def test_markdown_paths_are_mapped_to_workspace_without_fetching_images(self):
        with temporary_workspace(prefix='cli-markdown-images-') as root:
            store=SessionStore.create(root,'markdown-images',{})
            renderer=Renderer(color='never');renderer.session_directory=store.folder
            item={'kind':'human_output','source':'master','target':'user','timestamp':100,'images':[],
                  'content':'![结果](<sandbox:/workspace/plots/a%20b.png>)\n\n![外链](https://example.invalid/plot.png)\n\n'
                            '```markdown\n![示例](/workspace/not-an-image.png)\n```'}
            self.assertEqual(renderer.image_paths(item),[str(store.workspace/'plots/a b.png'),'https://example.invalid/plot.png'])

    def test_shell_messages_rejects_unhosted_archive_without_starting_an_agent(self):
        with temporary_workspace(prefix='cli-image-shell-') as root:
            store=SessionStore.create(root,'shell-image',{})
            data=(PROJECT/'tests/fixtures/small.jpg').read_bytes()
            image=store.add_upload(data,'example.jpg')
            team=MultiAgentSystem(store,{'demo':True,'network':False,'gpu':False})
            try:team.queue_user('master','附图',[image['id']])
            finally:team.close()
            saved=SavedSession(store.folder)
            image=saved.messages()['messages'][0]['images'][0]
            expected=str(store.folder/image['asset'])
            env={key:value for key,value in os.environ.items() if not key.startswith('GEMINI_')}
            child=subprocess.run([sys.executable,str(PROJECT/'recoding-cli.py'),'messages','-s',str(store.folder),'--color','never'],
                                 cwd=PROJECT,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,1,child.stderr)
            self.assertIn('没有 CLI Host',child.stderr)
            self.assertEqual(Path(expected).read_bytes(),data)
            self.assertFalse((store.folder/'cli-runtime.json').exists())


if __name__=='__main__':unittest.main()
