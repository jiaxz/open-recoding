"""Paid summary checkpoints and time-based maintenance retry, offline only."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from multiagent.context_window import ContextCompactor, prefix_hash
from multiagent.storage import atomic_json
from session.compaction import SessionCompaction
from test_context_window import history
from workspace_support import temporary_workspace


class CheckpointTests(unittest.TestCase):
    def make(self,path,summary):
        return ContextCompactor(path,summary,model=lambda:'grok-4.7',threshold=8000,recent_blocks=3,checkpoint=True)

    def test_later_failure_keeps_checkpoint_and_retry_skips_successful_prefix(self):
        with temporary_workspace() as directory:
            path=Path(directory)/'context-window.json';messages=history(65,3000);calls=[]
            def fail_later(records,model):
                calls.append(records)
                if len(calls)==2:raise OSError('Temporary quota failure')
                return 'Checkpoint: first paid source chunk.',{}
            compact=self.make(path,fail_later)
            self.assertEqual(compact(messages),messages)
            self.assertTrue(compact.failed);self.assertFalse(path.exists())
            checkpoint=json.loads(compact.checkpoint_path.read_text())
            first=checkpoint['memory']['prefix_count']
            self.assertGreater(first,1)
            self.assertIsNone(compact.state)
            retried=[]
            def resume(records,model):
                retried.append(records)
                return 'Checkpoint: updated continuation memory.',{}
            compact=self.make(path,resume)
            projected=compact(messages,force=True)
            self.assertTrue(path.exists());self.assertFalse(compact.failed)
            self.assertIn('Checkpoint: first paid source chunk.',retried[0][1]['content'])
            self.assertIn('record-'+str((first-2)//2),retried[0][1]['content'])
            self.assertNotEqual(retried[0][1]['content'],calls[0][1]['content'])
            self.assertEqual(compact.state['prefix_sha256'],prefix_hash(messages,compact.state['prefix_count']))
            self.assertLess(len(projected),len(messages))

    def test_changed_prefix_or_base_does_not_reuse_checkpoint(self):
        with temporary_workspace() as directory:
            messages=history(65,3000);path=Path(directory)/'context-window.json';calls=[]
            def summarize(records,model):
                calls.append(records)
                if len(calls)==2:raise OSError('temporary')
                return 'A paid successful summary.',{}
            compact=self.make(path,summarize);compact(messages)
            changed=deepcopy(messages);changed[3]['content']='changed source body'
            retry=[]
            compact=self.make(path,lambda records,model:(retry.append(records) or 'Fresh validated summary.',{}))
            compact(changed,force=True)
            self.assertNotIn('A paid successful summary.',retry[0][1]['content'])

    def test_cancel_retains_staging_and_never_publishes_it_as_active(self):
        with temporary_workspace() as directory:
            calls=[]
            def summarize(*_):
                calls.append(1)
                if len(calls)==2:raise KeyboardInterrupt()
                return 'Successful prefix before cancellation.',{}
            compact=self.make(Path(directory)/'context-window.json',summarize)
            with self.assertRaises(KeyboardInterrupt):compact(history(65,3000))
            self.assertIsNone(compact.state);self.assertFalse(compact.path.exists())
            self.assertTrue(compact.checkpoint_path.exists())

    def test_changed_active_summary_invalidates_its_partial_checkpoint(self):
        with temporary_workspace() as directory:
            path=Path(directory)/'context-window.json'
            compact=self.make(path,lambda *_:('Initial active memory.',{}));compact(history(20,3000))
            calls=[]
            def fail_second(*_):
                calls.append(1)
                if len(calls)==2:raise OSError('temporary')
                return 'Tentative memory from an older baseline.',{}
            compact.summarize=fail_second;compact(history(65,3000),force=True)
            active=json.loads(path.read_text());active['summary']='Updated active memory, same generation.'
            atomic_json(path,active)
            seen=[]
            fresh=self.make(path,lambda records,model:(seen.append(records) or 'New continuation.',{}))
            fresh(history(65,3000),force=True)
            self.assertIn('Updated active memory, same generation.',seen[0][1]['content'])
            self.assertNotIn('Tentative memory from an older baseline.',seen[0][1]['content'])

    def test_old_journal_cannot_overwrite_a_newer_paid_checkpoint(self):
        from session import SessionStore
        from session.compaction import recover_summary_checkpoint
        from multiagent.context_window import blocks
        with temporary_workspace() as directory:
            store=SessionStore.create(Path(directory)/'workspace','recovery',{})
            messages=history(65,3000);store.save_state({'messages':messages})
            groups=blocks(messages);older,newer=groups[10][1],groups[20][1]
            memory={'version':1,'prefix_count':newer,'prefix_sha256':prefix_hash(messages,newer),
                    'generation':2,'summary':'Newer paid memory.','model':'grok-4.7'}
            checkpoint=store.folder/'context-checkpoint.json';atomic_json(checkpoint,{'version':1,'base':None,'memory':memory})
            original=checkpoint.read_bytes()
            journal=store.folder/'runs/legacy.jsonl'
            journal.write_text(json.dumps({'type':'context_compaction_end','run_id':'old-run','step':10,'model':'grok-4.7',
                'prefix_count':older,'generation':1,'summary':'Older paid memory.'})+'\n')
            operation={'run_id':'old-run','step':10,'model':'grok-4.7','snapshot_count':len(messages),
                       'snapshot_sha256':prefix_hash(messages,len(messages))}
            result=recover_summary_checkpoint(store,journal,operation)
            self.assertTrue(result['reused_checkpoint']);self.assertEqual(result['prefix_count'],newer)
            self.assertEqual(checkpoint.read_bytes(),original)


class RetryScheduleTests(unittest.TestCase):
    def test_successful_compaction_does_not_reuse_pre_summary_estimate_cooldown(self):
        with temporary_workspace() as directory:
            session=SimpleNamespace(store=SimpleNamespace(folder=Path(directory)),closed=False,publish=lambda event:None)
            maintenance=SessionCompaction(session)
            request={'agent_id':'master','model':'gemini-4-argon','estimated_input_tokens':900000,
                     'context_window_tokens':1048576}
            maintenance.operations['master']={**request,'status':'succeeded','retry_tokens':1995557,
                                               'estimated_input_tokens':1943129,'request_id':'previous'}
            try:
                with patch('session.compaction.threading.Thread'):
                    self.assertEqual(maintenance.request(request)['status'],'queued')
                self.assertEqual(maintenance.operations['master']['estimated_input_tokens'],900000)
            finally:
                maintenance.thread=None;maintenance.close()

    def test_transient_failure_retries_after_time_without_context_growth(self):
        with temporary_workspace() as directory:
            store=SimpleNamespace(folder=Path(directory))
            session=SimpleNamespace(store=store,closed=False,publish=lambda event:None)
            maintenance=SessionCompaction(session)
            request={'agent_id':'master','model':'grok-4.7','estimated_input_tokens':400001,'context_window_tokens':500000}
            maintenance.operations['master']={**request,'status':'failed','retry_tokens':425001,
                                               'retryable':True,'retry_at':100,'failure_count':1,'request_id':'previous'}
            with patch('session.compaction.time.time',return_value=99):
                self.assertEqual(maintenance.request(request)['status'],'cooldown')
            with patch('session.compaction.time.time',return_value=101),patch('session.compaction.threading.Thread'):
                self.assertEqual(maintenance.request(request)['status'],'queued')
                self.assertEqual(maintenance.operations['master']['estimated_input_tokens'],400001)
            maintenance.thread=None;maintenance.close()


class MaintenanceRecoveryTests(unittest.TestCase):
    def test_completed_maintenance_is_not_recovered_as_a_cancelled_agent_loop(self):
        from multiagent.agent import Agent
        from unittest.mock import Mock
        agent=object.__new__(Agent)
        agent.record = {}
        agent.events=[{'type':'context_compaction_start','run_id':'maintenance','timestamp':1},
                      {'type':'context_compaction_end','run_id':'maintenance','timestamp':2}]
        agent.publish=Mock()
        agent._recover_interrupted_preview()
        agent.publish.assert_not_called()


class MaintenanceCommandTests(unittest.TestCase):
    def test_explicit_offline_maintenance_and_probe_use_shared_driver(self):
        import os, subprocess, sys
        from model_support import local_model
        from session import SessionStore
        from multiagent.context_window import SUMMARY_SYSTEM
        with temporary_workspace(prefix='context-maintenance-command-') as directory:
            root=Path(directory)
            def respond(body,number):
                if body['messages'][0]['content']==SUMMARY_SYSTEM:
                    self.assertNotIn('tools',body);self.assertNotIn('tool_choice',body)
                    return {'role':'assistant','content':'Verified historical work; continue the latest user request.'}
                names=[item['function']['name'] for item in body['tools']]
                self.assertEqual(len(names),len(set(names)))
                return {'role':'assistant','content':'VALIDATION_OK'}
            with local_model(root/'model',respond) as model:
                store=SessionStore.create(root/'workspace','maintenance',{'model':'grok-4.7','base_url':model.url,'api_timeout':5})
                store.save_state({'messages':history(30,3000),'iteration':40,'tool_count':30})
                source=(store.folder/'state.json').read_bytes()
                envfile=root/'model.env';envfile.write_text('RECODING_API_KEY=local-only\nRECODING_BASE_URL='+model.url+'\n')
                env={k:v for k,v in os.environ.items() if not k.startswith(('RECODING_API','RECODING_BASE','GEMINI_'))}
                env.update(SSL_CERT_FILE=str(model.cert),NO_PROXY='127.0.0.1',no_proxy='127.0.0.1')
                command=[sys.executable,'maintenance/recoding-context.py','-s',str(store.folder),'--live','--probe',
                    '--env-file',str(envfile),'--report-dir',str(root/'report')]
                reply=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(reply.returncode,0,reply.stderr+reply.stdout)
                result=json.loads((root/'report/result.json').read_text())
                self.assertTrue(result['probe']['success']);self.assertTrue(result['raw_state_unchanged'])
                self.assertEqual((store.folder/'state.json').read_bytes(),source)
                self.assertTrue((store.folder/'context-window.json').exists())
