"""Local credentials: safe parsing, precedence and frontend startup wiring."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from session.config import default_env_file, load_env_file
from recoding_web.server import main,GuiHttpServer


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.env'

    def test_quotes_comments_export_and_only_model_settings(self):
        self.path.write_text("\ufeff# local configuration\nexport GEMINI_API_KEY='test # key'\n"
                             'GEMINI_BASE_URL=https://example.test/v1 # comment\n'
                             'GEMINI_MODEL="test-model"\nPATH=/untrusted\n')
        environment = {}
        self.assertTrue(load_env_file(self.path, environ=environment))
        self.assertEqual(environment, {'GEMINI_API_KEY': 'test # key',
                                      'GEMINI_BASE_URL': 'https://example.test/v1',
                                      'GEMINI_MODEL': 'test-model'})

    def test_existing_environment_including_empty_values_has_priority(self):
        self.path.write_text('GEMINI_API_KEY=file-key\nGEMINI_MODEL=file-model\n')
        environment = {'GEMINI_API_KEY': 'environment-key', 'GEMINI_MODEL': ''}
        load_env_file(self.path, environ=environment)
        self.assertEqual(environment, {'GEMINI_API_KEY': 'environment-key', 'GEMINI_MODEL': ''})

    def test_no_shell_execution_or_variable_interpolation(self):
        marker = Path(self.temp.name) / 'must-not-exist'
        self.path.write_text(f'GEMINI_API_KEY="$(touch {marker})"\nGEMINI_MODEL="$HOME"\n')
        environment = {}
        load_env_file(self.path, environ=environment)
        self.assertEqual(environment['GEMINI_API_KEY'], f'$(touch {marker})')
        self.assertEqual(environment['GEMINI_MODEL'], '$HOME')
        self.assertFalse(marker.exists())

    def test_invalid_file_does_not_partially_apply_or_leak_values(self):
        self.path.write_text('GEMINI_API_KEY=private-test-key\nGEMINI_MODEL="private-broken-value\n')
        environment = {}
        with self.assertRaises(ValueError) as raised:
            load_env_file(self.path, environ=environment)
        self.assertEqual(environment, {})
        self.assertNotIn('private', str(raised.exception))
        self.assertIn('.env:2:', str(raised.exception))

    def test_missing_file_and_size_limit(self):
        self.assertFalse(load_env_file(self.path, environ={}))
        self.path.write_text('GEMINI_API_KEY=' + 'x' * 65536)
        with self.assertRaises(ValueError):
            load_env_file(self.path, environ={})

    def test_source_default_is_project_file_not_the_session_directory(self):
        self.assertEqual(default_env_file(), Path(__file__).resolve().parents[1] / '.env')

    def test_web_startup_loads_dotenv_without_contacting_model_or_exposing_key(self):
        self.path.write_text('GEMINI_API_KEY=startup-test-key\nGEMINI_MODEL=from-dotenv\n')
        server = Mock(server_port=8765)
        captured = {}
        def factory(address, app):
            server.host=app
            server.config=app.config
            server.token=server.instance_id=server.frontend_revision='local-test'
            captured.update(GuiHttpServer.configuration(server))
            return server
        output = StringIO()
        with patch.dict(os.environ, {}, clear=True),\
             patch('recoding_web.server.GuiHttpServer', side_effect=factory), redirect_stdout(output):
            self.assertEqual(main(['-C', str(Path(self.temp.name) / 'sessions'),
                                   '--env-file', str(self.path)]), 0)
        self.assertTrue(captured['key_configured'])
        self.assertEqual(captured['model'], 'grok-4.7')  # dotenv must not override new-Session default
        self.assertNotIn('startup-test-key', str(captured) + output.getvalue())
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_demo_does_not_read_credentials(self):
        with patch('recoding_web.server.load_env_file', side_effect=AssertionError('must not read key')),\
             patch('recoding_web.server.GuiHttpServer', return_value=Mock(server_port=8765)),\
             redirect_stdout(StringIO()):
            self.assertEqual(main(['--demo', '-C', str(Path(self.temp.name) / 'sessions')]), 0)

    def test_startup_directory_can_be_positional_flag_or_current_directory(self):
        chosen = []
        def server(address, app):
            chosen.append(app.catalog.root)
            return Mock(server_port=8765)
        path = Path(self.temp.name)/'chosen'
        with patch('recoding_web.server.GuiHttpServer', side_effect=server), redirect_stdout(StringIO()):
            self.assertEqual(main(['--demo', str(path)]), 0)
            self.assertEqual(main(['--demo', '-C', str(path)]), 0)
            self.assertEqual(main(['--demo']), 0)
        self.assertEqual(chosen, [path, path, Path.cwd()])


if __name__ == '__main__':
    unittest.main()
