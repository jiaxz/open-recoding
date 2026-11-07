"""Execution waiting mode survives bounded display projections without changing history."""
from copy import deepcopy
import json
import unittest
from multiagent.agent import preview

class ExecutionDisplayTests(unittest.TestCase):
    def test_large_tool_calls_retain_explicit_and_default_waiting_mode(self):
        for name,field in [('python','code'),('bash','command')]:
            for mode in (None,'sync','async'):
                with self.subTest(name=name,mode=mode):
                    args={field:'print(42)\n'*4000}
                    if mode is not None:args['mode']=mode
                    event={'type':'tool_start','name':name,'arguments':json.dumps(args)}
                    original=deepcopy(event);shown=preview(event)
                    self.assertEqual(shown['execution_mode'],mode or 'sync')
                    self.assertTrue(shown['source_truncated'])
                    self.assertEqual(event,original)
                    with self.assertRaises(ValueError):json.loads(shown['arguments'])
                    self.assertEqual(preview(shown),shown)

if __name__=='__main__':unittest.main()
