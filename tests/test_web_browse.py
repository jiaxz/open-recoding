"""Archive browsing has no runtime or mutating operations."""
import unittest
from unittest.mock import patch
from session import SessionStore,SavedSession
from recoding_web.host import GuiSessionHost
from test_session_ownership import fake_start
from multiagent.agent import Agent
from workspace_support import temporary_workspace

class WebBrowseTests(unittest.TestCase):
    def test_browse_never_launches_workers_and_does_not_consume_capacity(self):
        with temporary_workspace(prefix='web-browse-') as root:
            stores=[SessionStore.create(root,f'archive-{i}',{}) for i in range(10)]
            host=GuiSessionHost(root,demo=True,network=False,gpu=False);self.addCleanup(host.close)
            with patch('multiagent.agent.subprocess.Popen',side_effect=AssertionError('Browse launched worker')):
                for store in stores:
                    view=host.open(identifier=store.id,action='browse')
                    self.assertIsInstance(view,SavedSession)
                    self.assertEqual(view.snapshot()['status'],'stop')
                self.assertEqual(host.sessions,{})
                with self.assertRaises(PermissionError):host.delete(stores[-1].id)
                self.assertTrue(stores[-1].folder.exists())

    def test_navigation_counts_live_agents_without_loading_context(self):
        with temporary_workspace(prefix='web-browse-count-') as root,patch.object(Agent,'start_worker',fake_start):
            store=SessionStore.create(root,'navigation',{})
            host=GuiSessionHost(root,demo=True,network=False,gpu=False);self.addCleanup(host.close)
            session=host.open(identifier=store.id,action='resume')
            with patch.object(session.team,'listing',return_value=[{'status':'sleeping'},{'status':'failed'}]),\
                 patch.object(store,'load_events',side_effect=AssertionError('Navigation loaded history')):
                self.assertEqual(session.navigation(store)['active_agents'],2)
                session.master.status='stopped'
                self.assertEqual(session.navigation(store)['active_agents'],1)

if __name__=='__main__':unittest.main()
