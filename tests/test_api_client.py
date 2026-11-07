from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import time
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch
import urllib.request

from multiagent.loop import AgentLoop as Agent
from endless_support import drive
from multiagent.client import Client
from multiagent.prompts import SYSTEM, TOOLS
from multiagent.errors import AgentError
from multiagent.client import NoRedirect
from multiagent.storage import ContextHistory
from multiagent.multimodal import to_api_messages


class MetadataTests(unittest.TestCase):
    def test_no_tools_omits_tool_choice_instead_of_sending_none_to_gateway(self):
        client = Client("fixture-key",tools=[])
        client.tool_choice = "none"
        request, metadata = client.prepare_request([{"role":"user","content":"summary"}])
        body = json.loads(request.data)
        self.assertNotIn("tools",body)
        self.assertNotIn("tool_choice",body)
        self.assertEqual(metadata["tool_names"],[])

    def test_preparation_frames_user_instruction_without_changing_history_or_network(self):
        client = Client('test-secret')
        client.opener = Mock()
        messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'hi'}]
        request, metadata = client.prepare_request(messages)
        expected_messages = [messages[0], {'role': 'user', 'content': '<user_query>hi</user_query>'}]
        expected = json.dumps({'model': client.model, 'messages': expected_messages,
                               'stream': True, 'stream_options': {'include_usage': True}}, ensure_ascii=False).encode()
        self.assertEqual(request.data, expected)
        self.assertEqual(metadata['request_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(metadata['request_bytes'], len(expected))
        self.assertEqual(messages[1]['content'], 'hi')
        client.opener.open.assert_not_called()
        self.assertNotIn('test-secret', json.dumps(metadata))

    def test_reasoning_budget_is_optional_and_can_be_selected_for_diagnostics(self):
        for effort in (None, 'low', 'medium', 'high'):
            with self.subTest(effort=effort):
                client = Client('test-secret', reasoning_effort=effort)
                request, _ = client.prepare_request([{'role': 'user', 'content': 'hi'}])
                body = json.loads(request.data)
                if effort is None:
                    self.assertNotIn('reasoning_effort', body)
                else:
                    self.assertEqual(body['reasoning_effort'], effort)
                self.assertEqual(client.configuration()['reasoning_effort'], effort)

    def test_framing_keeps_old_prefix_code_and_tool_signatures_across_turns(self):
        text = '请执行这段代码：\nprint("</user_query>")\n  # 保留缩进和中文'
        assistant = {'role': 'assistant', 'tool_calls': [{
            'id': 'one', 'type': 'function', 'function': {'name': 'python', 'arguments': '{"code":"print(42)"}'},
            'extra_content': {'google': {'thought_signature': 'retain'}}}]}
        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': text},
                    assistant, {'role': 'tool', 'tool_call_id': 'one', 'content': '42'}]
        original = deepcopy(messages)
        first = to_api_messages(messages)
        later = to_api_messages(messages + [{'role': 'assistant', 'content': '42'},
                                             {'role': 'user', 'content': '继续'}])
        self.assertEqual(first[1]['content'], '<user_query>' + text + '</user_query>')
        self.assertEqual(later[:len(first)], first)
        self.assertEqual(first[2:], messages[2:])
        self.assertEqual(messages, original)

    def test_runtime_records_and_structured_image_messages_keep_their_protocol(self):
        record = '[Agent停止记录]\n{"location":{"phase":"model","step":1}}'
        image = {'role': 'user', 'content': [
            {'type': 'text', 'text': '[工具图像附件]'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}]}
        messages = [{'role': 'user', 'content': record}, image]
        self.assertEqual(to_api_messages(messages), messages)
        self.assertEqual(json.loads(to_api_messages(messages)[0]['content'].split('\n', 1)[1])['location']['step'], 1)

    def test_configuration_redacts_proxy_credentials_and_exposes_only_key_fingerprint(self):
        client = Client('test-secret')
        with patch('multiagent.client.urllib.request.getproxies', return_value={
                'https': 'http://proxy-user:proxy-password@proxy.example:8080'}),\
             patch('multiagent.client.urllib.request.proxy_bypass', return_value=False):
            config = client.configuration()
        self.assertEqual(config['proxy_mode'], 'environment')
        self.assertEqual(config['proxy'], 'http://proxy.example:8080')
        self.assertEqual(config['automatic_retries'], 0)
        self.assertEqual(config['key_fingerprint'], hashlib.sha256(b'test-secret').hexdigest()[:12])
        for secret in ('test-secret', 'proxy-user', 'proxy-password'):
            self.assertNotIn(secret, json.dumps(config))

    def test_cancel_before_send_records_phase_without_sending_or_losing_user_prompt(self):
        client = Client('test-secret')
        client.opener = Mock()
        events = []
        def observer(event):
            events.append(event)
            if event['type'] == 'model_transport' and event['phase'] == 'waiting_headers':
                raise KeyboardInterrupt()
        agent = Agent(client, Mock(), on_event=observer)
        with self.assertRaises(KeyboardInterrupt):
            drive(agent, 'hi')
        client.opener.open.assert_not_called()
        self.assertEqual(client.last_call['phase'], 'cancelled')
        self.assertEqual(client.last_call['interrupted_phase'], 'waiting_headers')
        self.assertEqual(agent.messages[1]['content'], 'hi')
        self.assertTrue(agent.messages[-1]['content'].startswith('[Agent停止记录]'))



@unittest.skipUnless(shutil.which('openssl'), 'openssl needed for ephemeral localhost TLS fixture')
class LocalHTTPSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = temporary_workspace()
        cls.root = Path(cls.temporary.name)
        cert, key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
                        '-keyout', str(key), '-out', str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cert, key)
        cls.client_context = ssl.create_default_context(cafile=str(cert))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def server(self, mode):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if mode == 'timeout':
                    time.sleep(0.5)
                    self.close_connection = True
                    return
                if mode == 'delayed_success':
                    time.sleep(0.12)
                if mode in ('stream_success', 'stream_idle'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    def chunk(data):
                        self.wfile.write(f'{len(data):x}\r\n'.encode()+data+b'\r\n')
                        self.wfile.flush()
                    try:
                        if mode == 'stream_idle':
                            for _ in range(30):
                                chunk(b': keepalive\n\n');time.sleep(0.02)
                        else:
                            chunk(b': keepalive\n\n')
                            time.sleep(0.05)
                            payload=('data: '+json.dumps({'choices': [{'index': 0, 'delta': {'content': '你好'}, 'finish_reason': 'stop'}]},ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode()
                            for part in (payload[:25],payload[25:60],payload[60:]): chunk(part)
                            # Do not terminate HTTP chunking until the client has returned.
                            hold.wait(1)
                        self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                    except OSError: pass
                    self.close_connection=True
                    return
                status = 503 if mode == 'upstream' else 200
                body = (json.dumps({'error': {'type': 'upstream_unavailable', 'code': 'credential_unavailable',
                         'message': 'credential expired; request dfr_fixture; test-secret'}}).encode()
                        if status == 503 else json.dumps({'model': 'fixture-model', 'choices': [{
                            'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'hello'}}],
                            'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}}).encode())
                self.send_response(status)
                self.send_header('X-Request-ID', 'dfr_fixture')
                self.send_header('Connection', 'close')
                if mode == 'truncated':
                    self.send_header('Transfer-Encoding', 'chunked')
                else:
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write((f'{len(body) + 43:x}\r\n'.encode() + body) if mode == 'truncated' else body)
                self.wfile.flush()
                self.close_connection = True
        hold = threading.Event()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.socket = self.server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            hold.set()
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        client = Client('test-secret', f'https://localhost:{server.server_port}/v1', timeout=3)
        client.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                   urllib.request.HTTPSHandler(context=self.client_context))
        client.test_stream_hold = hold
        return client, seen

    def test_actual_tls_stream_returns_at_done_and_records_first_output(self):
        client, seen = self.server('stream_success')
        client.timeout=0.3
        try:
            self.assertEqual(client.complete([{'role': 'user', 'content': '你好'}])['content'], '你好')
            self.assertEqual(len(seen),1)
            self.assertEqual(client.last_call['http_status'],200)
            self.assertGreaterEqual(client.last_call['first_output_seconds'],0.04)
            self.assertEqual(client.last_call['heartbeat_count'],1)
        finally: client.test_stream_hold.set()

    def test_actual_tls_heartbeats_do_not_hide_missing_model_output_timeout(self):
        client, seen = self.server('stream_idle')
        client.timeout=0.1
        started=time.monotonic()
        with self.assertRaisesRegex(AgentError,'no model output'):
            client.complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(len(seen),1)
        self.assertEqual(client.last_call['http_status'],200)
        self.assertEqual(client.last_call['error_category'],'timeout')
        self.assertLess(time.monotonic()-started,0.5)

    def test_actual_tls_success_has_ordered_timings_and_archive_without_secrets(self):
        client, seen = self.server('delayed_success')
        with temporary_workspace() as folder:
            history = ContextHistory(folder, system=SYSTEM, tools=TOOLS, model=client.model)
            self.addCleanup(history.close)
            events = []
            agent = Agent(client, Mock(), history=history, on_event=events.append)
            self.assertEqual(drive(agent, 'hi'), 'hello')
            archived = history.path.read_text()
            records = [json.loads(line) for line in archived.splitlines()]
        observations = [event for event in events if event['type'] == 'model_transport']
        self.assertEqual([e['phase'] for e in observations],
                         ['waiting_headers', 'headers_received', 'reading_body', 'parsing', 'complete'])
        self.assertEqual(client.last_call['http_status'], 200)
        self.assertEqual(client.last_call['request_id'], 'dfr_fixture')
        self.assertGreaterEqual(client.last_call['headers_seconds'], 0.1)
        self.assertGreater(client.last_call['response_bytes'], 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['messages'][1]['content'], '<user_query>hi</user_query>')
        self.assertEqual(agent.messages[1]['content'], 'hi')
        self.assertTrue(any(record['type'] == 'api_configuration' for record in records))
        self.assertTrue(any(record['type'] == 'model_transport' for record in records))
        self.assertNotIn('test-secret', archived)
        self.assertNotIn('request_sha256', json.dumps(agent.messages))

    def test_actual_503_is_classified_with_id_and_never_automatically_retried(self):
        client, seen = self.server('upstream')
        events = []
        agent = Agent(client, Mock(), on_event=events.append)
        with self.assertRaisesRegex(AgentError, 'API HTTP 503') as caught:
            drive(agent, 'hi')
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.last_call['error_category'], 'upstream_credentials')
        self.assertEqual(client.last_call['upstream_code'], 'credential_unavailable')
        self.assertEqual(client.last_call['request_id'], 'dfr_fixture')
        self.assertEqual(client.last_call['phase'], 'error')
        failed = next(event for event in events if event['type'] == 'model_transport' and event['phase'] == 'error')
        self.assertEqual(failed['http_status'], 503)
        self.assertEqual(failed['upstream_code'], 'credential_unavailable')
        self.assertEqual(failed['request_id'], 'dfr_fixture')
        self.assertNotIn('test-secret', str(caught.exception) + json.dumps(client.last_call))
        self.assertEqual(agent.messages[1]["role"], "user")
        self.assertTrue(agent.messages[-1]["content"].startswith("[Agent停止记录]"))

    def test_actual_truncated_chunked_body_keeps_received_status_and_phase(self):
        client, seen = self.server('truncated')
        events = []
        client.set_observer(events.append)
        with self.assertRaisesRegex(AgentError, 'IncompleteRead'):
            client.complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.last_call['http_status'], 200)
        self.assertEqual(client.last_call['error_category'], 'incomplete_response')
        self.assertEqual(client.last_call['interrupted_phase'], 'reading_body')
        self.assertEqual(events[-1]['phase'], 'error')

    def test_actual_timeout_before_headers_is_distinct_from_received_http_error(self):
        client, seen = self.server('timeout')
        client.timeout = 0.15
        with self.assertRaisesRegex(AgentError, 'timed out'):
            client.complete([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.last_call['error_category'], 'timeout')
        self.assertEqual(client.last_call['interrupted_phase'], 'waiting_headers')
        self.assertIsNone(client.last_call.get('http_status'))
        self.assertIsNone(client.last_call.get('request_id'))


if __name__ == '__main__':
    unittest.main()
