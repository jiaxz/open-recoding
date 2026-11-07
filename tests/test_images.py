import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch
import zlib

from multiagent.loop import AgentLoop as Agent
from endless_support import drive
from multiagent.client import Client
from multiagent.prompts import SYSTEM, TOOLS
from multiagent.errors import AgentError
from multiagent.storage import ContextHistory
from multiagent.image_reader import MAX_IMAGE_BYTES, image_info, image_attachment
from multiagent.multimodal import context_stats, make_tool_message, make_user_message, to_api_messages
from sandbox_support import tool_sandbox
from sandbox_support import Sandbox


def png(width=2, height=2, *, noise=False):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    row = lambda: os.urandom(width * 3) if noise else b'\xff\x00\x00' * width
    pixels = b''.join(b'\0' + row() for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def image_result(data=None, path='/workspace/chart.png'):
    data = png() if data is None else data
    mime, width, height = image_info(data)
    return {'image': {'path': path, 'mime_type': mime, 'width': width, 'height': height,
                      'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
            '_image_data': 'data:' + mime + ';base64,' + base64.b64encode(data).decode()}


def call(identifier, name='view_image', **arguments):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments or {'path': 'chart.png'})},
        'extra_content': {'google': {'thought_signature': 'preserve-this-signature'}}}


class ImageProtocolTests(unittest.TestCase):
    def test_user_attachments_are_real_images_retained_in_subsequent_requests(self):
        picture = image_attachment(png(), '/workspace/.recoding/inputs/input.png')
        client, sandbox = Mock(), Mock()
        client.last_usage = {}
        client.complete.return_value = {'role': 'assistant', 'content': '红色图片'}
        agent = Agent(client, sandbox, system='test')
        drive(agent, '这是什么？', attachments=[picture])
        wire = to_api_messages(agent.messages)
        self.assertEqual(wire[1]['content'][0]['text'], '<user_query>这是什么？</user_query>')
        self.assertEqual(wire[1]['content'][2]['image_url']['url'], picture['data_url'])
        self.assertNotIn('_image_attachments', wire[1])
        drive(agent, '比较刚才的图片')
        self.assertEqual(to_api_messages(agent.messages)[:len(wire)], wire)
        self.assertEqual(context_stats(agent.messages)['images'], 1)
        self.assertEqual(picture['path'], '/workspace/.recoding/inputs/input.png')
        sandbox.execute.assert_not_called()

    def test_user_image_validation_and_budget_fail_before_mutating_context(self):
        picture = image_attachment(png(), '/workspace/input.png')
        messages = [make_user_message([], '', [picture])]
        with patch('multiagent.multimodal.MAX_CONTEXT_IMAGE_BYTES', picture['bytes']):
            with self.assertRaisesRegex(ValueError, '10 MiB'):
                make_user_message(messages, '', [picture])
        client = Mock()
        agent = Agent(client, Mock(), system='system')
        with self.assertRaises(ValueError):
            drive(agent, '', attachments=[{**picture, 'data_url': 'data:image/png;base64,bm90LWFuLWltYWdl'}])
        self.assertEqual(len(agent.messages), 1)
        self.assertEqual(agent.model_step, 0)
        client.complete.assert_not_called()

    def test_image_is_inserted_after_all_tool_results_and_prefix_is_preserved(self):
        calls = [call('image'), call('shell', 'bash', command='pwd')]
        assistant = {'role': 'assistant', 'tool_calls': calls, '_model': 'gemini-4-argon'}
        picture, public = make_tool_message([], 'image', image_result())
        messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': '检查图表'},
                    assistant, picture, {'role': 'tool', 'tool_call_id': 'shell', 'content': '{"exit_code":0}'}]
        original = deepcopy(messages)
        client = Client('test-key', model=assistant['_model'])
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps({'choices': [{'message': {
            'role': 'assistant', 'content': 'checked'}}]}).encode())
        client.complete(messages)
        body = json.loads(client.opener.open.call_args.args[0].data)
        api = body['messages']
        self.assertEqual([m['role'] for m in api], ['system', 'user', 'assistant', 'tool', 'tool', 'user'])
        self.assertEqual(api[2], {key: value for key, value in assistant.items() if key != '_model'})
        self.assertNotIn('_image_attachment', api[3])
        self.assertNotIn('_image_data', public)
        self.assertEqual(api[5]['content'][1]['image_url']['url'], image_result()['_image_data'])
        self.assertEqual(messages, original)
        extended = messages + [{'role': 'assistant', 'content': 'checked'}, {'role': 'user', 'content': '继续'}]
        self.assertEqual(to_api_messages(extended)[:len(api)], api)
        self.assertEqual(ImageScopeTests.images(to_api_messages(extended)), [image_result()['_image_data']])
        self.assertEqual(context_stats(extended)['images'], 1)

    def test_multiple_images_share_one_attachment_message_and_unpaired_batch_fails(self):
        picture, _ = make_tool_message([], 'one', image_result())
        other, _ = make_tool_message([], 'two', image_result(path='/workspace/other.png'))
        messages = [{'role': 'assistant', 'tool_calls': [call('one'), call('two')]}]
        with self.assertRaisesRegex(ValueError, 'complete batch'):
            to_api_messages(messages + [picture])
        api = to_api_messages(messages + [picture, other])
        self.assertEqual(len(api), 4)
        self.assertEqual(len(api[-1]['content']), 4)
        self.assertIn('one', api[-1]['content'][0]['text'])
        self.assertIn('two', api[-1]['content'][2]['text'])

    def test_image_budget_failure_is_a_tool_error_and_request_limit_prevents_post(self):
        result = image_result()
        original, _ = make_tool_message([], 'first', result)
        repeated, metadata = make_tool_message([original], 'repeat', result)
        self.assertTrue(metadata['image_reused'])
        self.assertNotIn('_image_attachment', repeated)
        self.assertEqual(context_stats([original, repeated])['images'], 1)
        with patch('multiagent.multimodal.MAX_CONTEXT_IMAGE_BYTES', result['image']['bytes'] - 1):
            message, public = make_tool_message([], 'image', result)
        self.assertNotIn('_image_attachment', message)
        self.assertIn('10 MiB', public['error'])
        client = Client('test-key')
        client.opener = Mock()
        with patch('multiagent.client.MAX_REQUEST_BYTES', 1), self.assertRaisesRegex(AgentError, '20 MiB'):
            client.complete([{'role': 'user', 'content': 'hello'}])
        client.opener.open.assert_not_called()

    def test_cancelled_later_tool_keeps_image_in_context_and_archive(self):
        with temporary_workspace() as directory:
            history = ContextHistory(directory, system=SYSTEM, tools=TOOLS, model='test')
            self.addCleanup(history.close)
            client, sandbox = Mock(), Mock()
            client.complete.side_effect = [
                {'role': 'assistant', 'tool_calls': [call('image'), call('later', 'bash', command='sleep 30')]},
                {'role': 'assistant', 'content': '继续看图'}]
            sandbox.execute.side_effect = [image_result(), KeyboardInterrupt()]
            events = []
            agent = Agent(client, sandbox, history=history, on_event=events.append)
            with self.assertRaises(KeyboardInterrupt):
                drive(agent, '分析图片')
            api = to_api_messages(agent.messages)
            self.assertEqual([m['role'] for m in api], ['system', 'user', 'assistant', 'tool', 'tool', 'user', 'user'])
            self.assertEqual(ImageScopeTests.images(api), [image_result()['_image_data']])
            self.assertIn('_image_attachment', json.dumps(agent.messages))
            self.assertEqual(json.loads(api[3]['content'])['image']['path'], '/workspace/chart.png')
            record = json.loads(api[-1]['content'].split('\n', 1)[1])
            self.assertEqual(json.loads(api[3]['content'])['image']['path'], '/workspace/chart.png')
            self.assertEqual(record['interrupted_tool_calls'], ['later'])
            saved = [json.loads(line) for line in history.path.read_text().splitlines()]
            self.assertEqual(saved[0]['format_version'], 5)
            self.assertEqual(to_api_messages(next(e for e in reversed(saved) if e['type']=='loop_error')['messages']), api)
            self.assertNotIn('_image_data', json.dumps(events))
            before = deepcopy(agent.messages)
            drive(agent, '继续')
            self.assertEqual(agent.messages[:len(before)], before)
            self.assertIn('_image_attachment', history.path.read_text())

    def test_image_result_boundary_cancellation_retains_image_without_duplicate_call(self):
        client, sandbox = Mock(), Mock()
        client.complete.return_value = {'role': 'assistant', 'tool_calls': [call('image'), call('skipped')]}
        sandbox.execute.return_value = image_result()
        def cancel(event):
            if event['type'] == 'tool_end':
                raise KeyboardInterrupt()
        agent = Agent(client, sandbox, on_event=cancel)
        with self.assertRaises(KeyboardInterrupt):
            drive(agent, '查看图片')
        api = to_api_messages(agent.messages)
        self.assertEqual(ImageScopeTests.images(api), [image_result()['_image_data']])
        self.assertIn('_image_attachment', json.dumps(agent.messages))
        self.assertEqual(json.loads(api[4]['content'])['status'], 'not_started')
        self.assertEqual(sandbox.execute.call_count, 1)





class ImageScopeTests(unittest.TestCase):
    @staticmethod
    def images(messages):
        return [part['image_url']['url']
                for message in messages if isinstance(message.get('content'), list)
                for part in message['content'] if part.get('type') == 'image_url']

    def test_wire_requests_keep_old_images_and_comparison_only_loads_new_image(self):
        old = image_result(png(100, 100, noise=True))
        new = image_result(png(101, 100, noise=True), '/workspace/new.png')
        responses = [
            {'role': 'assistant', 'tool_calls': [call('old')]},
            {'role': 'assistant', 'tool_calls': [call('text-only', 'python', code='print(42)')]},
            {'role': 'assistant', 'content': '已记录旧图的文字分析'},
            {'role': 'assistant', 'tool_calls': [
                call('new', path='new.png')]},
            {'role': 'assistant', 'content': '两张图对比完成'},
        ]
        client = Client('test-key')
        client.opener = Mock()
        client.opener.open.side_effect = [
            io.BytesIO(json.dumps({'choices': [{'message': response}]}).encode())
            for response in responses]
        sandbox = Mock()
        sandbox.execute.side_effect = [old, {'stdout': '42', 'exit_code': 0}, new]
        events = []
        agent = Agent(client, sandbox, on_event=events.append)
        drive(agent, '分析旧图后执行计算')
        self.assertIn('_image_attachment', json.dumps(agent.messages))
        drive(agent, '比较旧图与新图')
        bodies = [json.loads(item.args[0].data) for item in client.opener.open.call_args_list]
        self.assertEqual([len(self.images(body['messages'])) for body in bodies], [0, 1, 1, 1, 2])
        self.assertEqual(bodies[2]['messages'][:len(bodies[1]['messages'])], bodies[1]['messages'])
        self.assertEqual(self.images(bodies[1]['messages']), [old['_image_data']])
        self.assertEqual(self.images(bodies[4]['messages']), [old['_image_data'], new['_image_data']])
        self.assertEqual([event['images'] for event in events if event['type'] == 'model_start'],
                         [0, 1, 1, 1, 2])
        self.assertEqual(bodies[1]['messages'][2]['tool_calls'][0]['extra_content'],
                         responses[0]['tool_calls'][0]['extra_content'])
        self.assertEqual(context_stats(agent.messages)['images'], 2)
        self.assertIn(old['_image_data'], json.dumps(agent.messages))
        self.assertIn(new['_image_data'], json.dumps(agent.messages))

    def test_image_budget_includes_history_and_does_not_evict_old_images(self):
        result = image_result()
        first_assistant = {'role': 'assistant', 'tool_calls': [call('first')]}
        first, _ = make_tool_message([first_assistant], 'first', result)
        messages = [first_assistant, first, {'role': 'assistant', 'content': '旧分析'},
                    {'role': 'assistant', 'tool_calls': [call('again'), call('extra')]}]
        with patch('multiagent.multimodal.MAX_CONTEXT_IMAGE_BYTES', result['image']['bytes']):
            again, public = make_tool_message(messages, 'again', result)
            self.assertNotIn('_image_attachment', again)
            self.assertIn('images retained in context', public['error'])
            self.assertNotIn('image_reused', public)
            extra, failure = make_tool_message(messages + [again], 'extra', image_result(png(3, 2)))
            self.assertIn('images retained in context', failure['error'])
            self.assertNotIn('_image_attachment', extra)
        messages += [again, extra]
        self.assertEqual(self.images(to_api_messages(messages)), [result['_image_data']])
        self.assertEqual(context_stats(messages)['image_bytes'], result['image']['bytes'])

    def test_duplicates_are_deduplicated_within_one_batch_only(self):
        result = image_result()
        assistant = {'role': 'assistant', 'tool_calls': [call('one'), call('two')]}
        one, _ = make_tool_message([assistant], 'one', result)
        two, public = make_tool_message([assistant, one], 'two', result)
        self.assertTrue(public['image_reused'])
        self.assertNotIn('_image_attachment', two)
        self.assertEqual(self.images(to_api_messages([assistant, one, two])), [result['_image_data']])
        new_assistant = {'role': 'assistant', 'tool_calls': [call('three')]}
        three, public = make_tool_message([assistant, one, two, new_assistant], 'three', result)
        self.assertNotIn('image_reused', public)
        self.assertIn('_image_attachment', three)
        self.assertEqual(self.images(to_api_messages([assistant, one, two, new_assistant, three])),
                         [result['_image_data'], result['_image_data']])

    def test_retained_snapshots_are_sent_when_the_current_batch_has_no_image(self):
        result = image_result(png(100, 100, noise=True))
        picture, _ = make_tool_message([], 'old', result)
        messages = [{'role': 'assistant', 'tool_calls': [call('old')]}, picture,
                    {'role': 'assistant', 'content': '旧图分析'},
                    {'role': 'user', 'content': '只计算，不看图'},
                    {'role': 'assistant', 'tool_calls': [call('compute', 'python', code='print(42)')]},
                    {'role': 'tool', 'tool_call_id': 'compute', 'content': '{"stdout":"42"}'}]
        api = to_api_messages(messages)
        self.assertEqual(self.images(api), [result['_image_data']])
        self.assertIn(result['_image_data'], json.dumps(api))
        self.assertEqual(json.loads(api[1]['content'])['image']['sha256'], result['image']['sha256'])
        self.assertEqual(context_stats(messages)['images'], 1)

    def test_cancelled_image_request_keeps_image_in_the_next_turn(self):
        with temporary_workspace() as directory:
            history = ContextHistory(directory, system=SYSTEM, tools=TOOLS, model='test')
            self.addCleanup(history.close)
            client, sandbox = Mock(), Mock()
            requests = []
            def complete(messages):
                requests.append(deepcopy(to_api_messages(messages)))
                if len(requests) == 1:
                    return {'role': 'assistant', 'tool_calls': [call('image')]}
                if len(requests) == 2:
                    raise KeyboardInterrupt()
                return {'role': 'assistant', 'content': '新任务完成'}
            client.complete.side_effect = complete
            sandbox.execute.return_value = image_result()
            agent = Agent(client, sandbox, history=history)
            with self.assertRaises(KeyboardInterrupt):
                drive(agent, '查看图片')
            self.assertIn('_image_attachment', json.dumps(agent.messages))
            drive(agent, '新的纯文字任务')
            self.assertEqual([len(self.images(request)) for request in requests], [0, 1, 1])
            records = [json.loads(line) for line in history.path.read_text().splitlines()]
            released = [entry for entry in records if entry['type'] == 'image_attachments_released']
            self.assertEqual(released, [])
            self.assertIn('_image_attachment', history.path.read_text())

    def test_model_failure_preserves_its_image_and_original_snapshot(self):
        with temporary_workspace() as directory:
            history = ContextHistory(directory, system=SYSTEM, tools=TOOLS, model='test')
            self.addCleanup(history.close)
            client, sandbox = Mock(), Mock()
            client.complete.side_effect = [
                {'role': 'assistant', 'tool_calls': [call('image')]}, AgentError('gateway failed')]
            sandbox.execute.return_value = image_result()
            agent = Agent(client, sandbox, history=history)
            with self.assertRaisesRegex(AgentError, 'gateway failed'):
                drive(agent, '看图')
            self.assertEqual(ImageScopeTests.images(to_api_messages(agent.messages)), [image_result()['_image_data']])
            records = [json.loads(line) for line in history.path.read_text().splitlines()]
            self.assertNotIn('image_attachments_released', [entry['type'] for entry in records])
            self.assertIn('_image_attachment', history.path.read_text())

class ImageSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'session'
        self.root.mkdir()
        self.sandbox = tool_sandbox(self.root, timeout=3, network=False, max_output=20)
        self.addCleanup(self.sandbox.close)

    def view(self, path):
        return self.sandbox.execute('view_image', json.dumps({'path': path}))

    def test_png_large_payload_bypasses_text_budget_without_starting_pool(self):
        data = png(100, 100, noise=True)
        (self.root / '图.png').write_bytes(data)
        result = self.view('/workspace/图.png')
        self.assertEqual(result['image']['width'], 100, result)
        self.assertGreater(len(result['_image_data']), 16000)
        self.assertEqual(base64.b64decode(result['_image_data'].split(',', 1)[1]), data)
        self.assertEqual(result['image']['sha256'], hashlib.sha256(data).hexdigest())
        tool, _ = make_tool_message([], 'image', result)
        (self.root / '图.png').write_bytes(png())
        self.assertEqual(base64.b64decode(tool['_image_attachment']['data_url'].split(',', 1)[1]), data)

    def test_jpeg_and_content_detection_without_filename_extension(self):
        data = (Path(__file__).parent / 'fixtures' / 'small.jpg').read_bytes()
        (self.root / 'picture.bin').write_bytes(data)
        result = self.view('picture.bin')
        self.assertEqual((result['image']['mime_type'], result['image']['width'], result['image']['height']),
                         ('image/jpeg', 7, 5), result)
        self.assertEqual(self.sandbox.session_state['execution']['status'], 'ready')

    def test_outside_paths_symlinks_and_special_files_are_rejected(self):
        outside = Path(self.temp.name) / 'outside.png'
        outside.write_bytes(png())
        (self.root / 'inside.png').write_bytes(png())
        (self.root / 'escape').symlink_to(outside)
        (self.root / 'link').symlink_to(self.root / 'inside.png')
        (self.root / 'parent').symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / 'pipe')
        for path in ('../outside.png', str(outside), '/usr/picture.png', 'escape', 'link',
                     'parent/inside.png', 'pipe', '/workspace', '', 'missing.png'):
            with self.subTest(path=path):
                self.assertIn('error', self.view(path))
        self.assertEqual(outside.read_bytes(), png())

    def test_invalid_corrupt_oversized_and_huge_dimension_files(self):
        for data in (b'not an image', png()[:-1], png()[:40] + b'corrupt' + png()[47:], b'\xff\xd8bad'):
            (self.root / 'bad').write_bytes(data)
            self.assertIn('error', self.view('bad'))
        with (self.root / 'huge').open('wb') as stream:
            stream.truncate(MAX_IMAGE_BYTES + 1)
        self.assertIn('5 MiB', self.view('huge')['error'])
        raw = bytearray(png())
        raw[16:24] = struct.pack('>II', 100000, 100000)
        raw[29:33] = struct.pack('>I', zlib.crc32(raw[12:29]) & 0xffffffff)
        (self.root / 'huge-pixels').write_bytes(raw)
        self.assertIn('25 million', self.view('huge-pixels')['error'])
        for arguments in ('{}', '{"path":42}', '{"path":"a","extra":true}', '[]'):
            self.assertIn('error', self.sandbox.execute('view_image', arguments))

    def test_real_agent_loop_keeps_image_across_other_tools(self):
        data = png()
        (self.root / 'chart.png').write_bytes(data)
        client = Mock()
        responses = [
            {'role': 'assistant', 'tool_calls': [call('image')]},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'next-tool', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{"command":"true"}'}}]},
            {'role': 'assistant', 'content': '已分析图片'}]
        requests = []
        def complete(messages):
            requests.append(deepcopy(to_api_messages(messages)))
            return responses.pop(0)
        client.complete.side_effect = complete
        agent = Agent(client, self.sandbox)
        self.assertEqual(drive(agent, '分析 chart.png'), '已分析图片')
        api = requests[1]
        images = [part for message in api if isinstance(message.get('content'), list)
                  for part in message['content'] if part['type'] == 'image_url']
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0]['image_url']['url'].split(',', 1)[1]), data)
        self.assertEqual([len(ImageScopeTests.images(request)) for request in requests], [0, 1, 1])
        self.assertEqual(context_stats(agent.messages)['images'], 1)
        self.assertIn('_image_attachment', json.dumps(agent.messages))
        self.assertEqual(self.sandbox.session_state['execution']['status'], 'ready')

    def test_overwritten_original_does_not_change_retained_historical_image(self):
        old, new = png(), png(3, 2)
        (self.root / 'scene.png').write_bytes(old)
        responses = [
            {'role': 'assistant', 'tool_calls': [call('old', path='scene.png')]},
            {'role': 'assistant', 'tool_calls': [call('change', 'bash', command='/usr/bin/python3 -c ' + __import__('shlex').quote(
                f"from pathlib import Path; Path('/workspace/scene.png').write_bytes({new!r})"))]},
            {'role': 'assistant', 'tool_calls': [call('current', path='scene.png')]},
            {'role': 'assistant', 'content': '已比较历史图片与当前图片'}]
        requests, client = [], Mock()
        def complete(messages):
            requests.append(deepcopy(to_api_messages(messages)))
            return responses.pop(0)
        client.complete.side_effect = complete
        agent = Agent(client, self.sandbox)
        self.assertEqual(drive(agent, '查看并比较'), '已比较历史图片与当前图片')
        self.assertEqual([len(ImageScopeTests.images(request)) for request in requests], [0, 1, 1, 2])
        decoded = [base64.b64decode(url.split(',', 1)[1]) for url in ImageScopeTests.images(requests[-1])]
        self.assertEqual(decoded, [old, new])
        self.assertEqual((self.root / 'scene.png').read_bytes(), new)
        self.assertEqual(context_stats(agent.messages)['images'], 2)


if __name__ == '__main__':
    unittest.main()
