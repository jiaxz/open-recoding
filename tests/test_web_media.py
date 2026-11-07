"""Display images remain transferable assets without becoming visual context."""

from pathlib import Path
import shutil
import unittest

from workspace_support import temporary_workspace

from session.store import SESSION_HOME, SessionStore
from multiagent.media import AgentMedia
from multiagent.image_reader import image_attachment
from multiagent.multimodal import image_usage, make_user_message, to_api_messages
from test_images import png


class MediaTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SessionStore.create(self.root/'project', 'media', {})
        self.media = AgentMedia(self.store)

    def test_display_images_are_snapshotted_without_view_image_and_survive_move(self):
        first, second = png(), png(3, 4)
        (self.store.workspace/'first.png').write_bytes(first)
        (self.store.workspace/'second.png').write_bytes(second)
        images, errors = self.media.capture('解释一\n![一](/workspace/first.png)\n解释二\n![二](sandbox:/workspace/second.png)\n总结')
        self.assertEqual(errors, [])
        self.assertEqual([image['path'] for image in images], ['/workspace/first.png','/workspace/second.png'])
        self.assertNotIn('data_url', str(images))
        destination = self.root/'new'/SESSION_HOME/'moved'
        destination.parent.mkdir(parents=True)
        shutil.move(self.store.folder,destination)
        shutil.rmtree(self.store.workspace)
        moved = SessionStore(destination)
        self.assertEqual(moved.image_asset(images[0]['sha256'])[0], first)
        self.assertEqual(moved.image_asset(images[1]['sha256'])[0], second)

    def test_markdown_titles_encoded_paths_and_code_blocks(self):
        (self.store.workspace/'with space.png').write_bytes(png())
        images, errors = self.media.capture('![A](<with space.png> "caption")\n![B](/workspace/with%20space.png)\n'
            '```markdown\n![fake](missing.png)\n```\n`![fake](missing.png)`')
        self.assertEqual(errors, [])
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]['sha256'], images[1]['sha256'])

    def test_model_images_with_or_without_tools_stay_display_only_after_resume(self):
        data = png()
        (self.store.workspace/'chart.png').write_bytes(data)
        original = image_attachment(data, '/workspace/original.png')
        messages = [{'role': 'system', 'content': 'test'}, make_user_message([], '检查', [original])]
        for tool_calls in ([], [{'id': 'pending-tool', 'type': 'function', 'function': {'name': 'sleep', 'arguments': '{"seconds":1}'}}]):
            response = {'role': 'assistant', 'content': '图前文字\n![图](chart.png)\n图后文字', **({'tool_calls': tool_calls} if tool_calls else {})}
            prepared = self.media.prepare_response(response, messages)
            self.assertNotIn('_image_attachments', prepared)
            self.assertNotIn('_image_references', prepared)
            self.assertEqual(prepared['_display_images'][0]['sha256'], original['sha256'])
            self.assertEqual(image_usage([prepared])['images'], 0)
            self.assertNotIn('data_url', str(prepared))
        messages.append(prepared)
        messages.append({'role': 'tool', 'tool_call_id': 'pending-tool', 'content': '{}'})
        wire = to_api_messages(messages)
        self.assertEqual(sum(part.get('type') == 'image_url' for item in wire
                             if isinstance(item.get('content'), list) for part in item['content']), 1)
        self.assertIn('chart.png', wire[-2]['content'])
        self.assertNotIn('_display_images', str(wire))
        self.assertEqual(image_usage(messages)['image_bytes'], len(data))
        self.store.append_event({'type': 'model_end', 'sequence': 1, 'content': prepared['content'],
                                 'images': self.media.display_images})
        self.store.save_state({'messages': messages, 'iteration': 1, 'tool_count': 1})
        (self.store.workspace/'chart.png').unlink()
        restored = self.store.load_state()['messages']
        self.assertEqual(to_api_messages(restored), wire)
        self.assertEqual(image_usage(restored)['images'], 1)

    def test_capture_never_fetches_urls_or_crosses_workspace_and_private_paths(self):
        secret = self.store.workspace/'model.env'
        secret.write_bytes(png())
        (self.store.workspace/'link.png').symlink_to(secret)
        media = AgentMedia(self.store, [secret])
        images, errors = media.capture('![external](https://127.0.0.1:1/private)\n'
            '![escape](../outside.png)\n![secret](/workspace/model.env)\n![link](link.png)')
        self.assertEqual(images, [])
        self.assertEqual(len(errors), 3)
