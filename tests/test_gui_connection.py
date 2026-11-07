"""GUI release/takeover fences do not own or stop Session runtimes."""

import unittest
from unittest.mock import patch

from recoding_web.host import GuiSessionHost, GuiBusyError
from workspace_support import temporary_workspace
import test_web as web_fixture


class GuiConnectionTests(unittest.TestCase):
    def setUp(self):
        self.root=temporary_workspace(prefix='gui-connection-')
        self.addCleanup(self.root.cleanup)
        self.host=GuiSessionHost(self.root.name,demo=True,network=False,gpu=False)
        self.addCleanup(self.host.close)

    def test_release_admits_new_page_immediately_without_renewal_by_late_requests(self):
        with patch('recoding_web.host.time.monotonic',return_value=100):
            self.host.connect_gui('a','doc-a')
            self.assertTrue(self.host.release_gui('a','doc-a'))
            self.assertIsNone(self.host.connect_gui('b','doc-b'))
            with self.assertRaises(GuiBusyError):self.host.connect_gui('a','doc-a')
        self.host.require_gui('b','doc-b')

    def test_old_release_cannot_remove_refreshed_document_on_the_same_page(self):
        self.host.connect_gui('a','old-document')
        self.assertEqual(self.host.connect_gui('a','new-document'),'a')
        self.assertFalse(self.host.release_gui('a','old-document'))
        self.host.require_gui('a','new-document')
        with self.assertRaises(GuiBusyError):self.host.connect_gui('a','old-document')

    def test_takeover_revokes_old_document_even_after_timeout(self):
        with patch('recoding_web.host.time.monotonic',return_value=100):
            self.host.connect_gui('a','doc-a')
            with self.assertRaises(GuiBusyError):self.host.connect_gui('b','doc-b')
            self.assertEqual(self.host.connect_gui('b','doc-b',takeover=True),'a')
        with patch('recoding_web.host.time.monotonic',return_value=1000):
            with self.assertRaises(GuiBusyError):self.host.connect_gui('a','doc-a')
        self.assertFalse(self.host.release_gui('a','doc-a'))
        self.host.require_gui('b','doc-b')
        self.assertEqual(self.host.connect_gui('a','explicit-reclaim',takeover=True),'b')


class GuiConnectionHTTPTests(unittest.TestCase):
    setUp=web_fixture.WebTests.setUp
    cleanup=web_fixture.WebTests.cleanup
    request=web_fixture.WebTests.request

    def test_takeover_requires_same_origin_token_and_instance_and_fences_old_api(self):
        a={'X-recoding-Page':'a','X-recoding-Document':'doc-a'}
        b={'X-recoding-Page':'b','X-recoding-Document':'doc-b'}
        self.assertEqual(self.request('/api/config',headers=a)[0],200)
        status,conflict=self.request('/api/config',headers=b)
        self.assertEqual((status,conflict['code']),(409,'gui_busy'))
        body={'instance_id':self.server.instance_id}
        self.assertEqual(self.request('/api/gui/takeover',body,headers=b,token=False)[0],403)
        self.assertEqual(self.request('/api/gui/takeover',body,headers={**b,'Origin':'https://example.invalid'})[0],403)
        self.assertEqual(self.request('/api/gui/takeover',{'instance_id':'stale'},headers=b)[0],400)
        self.app.require_gui('a','doc-a')
        self.assertEqual(self.request('/api/gui/takeover',body,headers={**b,'X-Agent-Token':conflict['takeover_token']})[0],200)
        self.assertEqual(self.request('/api/config',headers=a)[0],409)
        self.assertEqual(self.request('/api/exit',{},headers=a)[0],409)
        self.assertEqual(self.request('/api/config',headers=b)[0],200)

    def test_beacon_release_requires_exact_owner_document_and_cannot_reacquire(self):
        headers={'X-recoding-Page':'a','X-recoding-Document':'doc-new'}
        self.assertEqual(self.request('/api/config',headers=headers)[0],200)
        body={'page':'a','document':'doc-old','csrf':self.server.token,'instance_id':self.server.instance_id}
        status,value=self.request('/api/gui/release',body,token=False)
        self.assertEqual((status,value['released']),(200,False))
        self.assertEqual(self.request('/api/gui/release',{**body,'document':'doc-new','csrf':'bad'},token=False)[0],403)
        status,value=self.request('/api/gui/release',{**body,'document':'doc-new'},token=False)
        self.assertEqual((status,value['released']),(200,True))
        self.assertEqual(self.request('/api/config',headers=headers)[0],409)
        self.assertEqual(self.request('/api/config',headers={'X-recoding-Page':'b','X-recoding-Document':'doc-b'})[0],200)


if __name__=='__main__':unittest.main()
