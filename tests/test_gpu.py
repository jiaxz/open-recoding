from python_pool_support import run_python,fresh_python,close_pools
"""GPU exposure boundaries and real CUDA execution, without a model API."""

import json
import os
from pathlib import Path
import unittest

from workspace_support import temporary_workspace
from unittest.mock import patch

from multiagent.prompts import system_prompt
from node.gpu import GPUConfig, discover_gpu, gpu_description, usable_device
from sandbox_support import tool_sandbox
from sandbox_support import Sandbox


CUDA_SETUP = r'''
import ctypes as ct
import json
cuda = ct.CDLL('libcuda.so.1')
def bind(name, arguments):
    function = getattr(cuda, name)
    function.argtypes = arguments
    function.restype = ct.c_int
    return function
def checked(function, *arguments):
    status = function(*arguments)
    assert status == 0, (function.__name__, status)
bind('cuInit', [ct.c_uint])
bind('cuDeviceGetCount', [ct.POINTER(ct.c_int)])
bind('cuCtxCreate_v2', [ct.POINTER(ct.c_void_p), ct.c_uint, ct.c_int])
bind('cuMemAlloc_v2', [ct.POINTER(ct.c_uint64), ct.c_size_t])
bind('cuModuleLoadData', [ct.POINTER(ct.c_void_p), ct.c_void_p])
bind('cuModuleGetFunction', [ct.POINTER(ct.c_void_p), ct.c_void_p, ct.c_char_p])
bind('cuLaunchKernel', [ct.c_void_p, ct.c_uint, ct.c_uint, ct.c_uint,
    ct.c_uint, ct.c_uint, ct.c_uint, ct.c_uint, ct.c_void_p,
    ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_void_p)])
bind('cuCtxSynchronize', [])
bind('cuMemcpyDtoH_v2', [ct.c_void_p, ct.c_uint64, ct.c_size_t])
checked(cuda.cuInit, 0)
count = ct.c_int()
checked(cuda.cuDeviceGetCount, ct.byref(count))
assert count.value > 0
context = ct.c_void_p()
# Shared hosts can have the first device full. Stay within CUDA's visible set
# and try another ordinal only when context creation reports out of memory.
for ordinal in range(count.value):
    status = cuda.cuCtxCreate_v2(ct.byref(context), 0, ordinal)
    if status == 0:
        break
    assert status == 2, ('cuCtxCreate_v2', status)
else:
    raise AssertionError('No visible GPU has memory for a small CUDA test context')
pointer = ct.c_uint64()
checked(cuda.cuMemAlloc_v2, ct.byref(pointer), 32 * 4)
ptx = ct.create_string_buffer(b"""
.version 6.0
.target sm_52
.address_size 64
.visible .entry twice(.param .u64 output) {
    .reg .u64 %base, %offset;
    .reg .u32 %index, %value;
    ld.param.u64 %base, [output];
    mov.u32 %index, %tid.x;
    mul.lo.u32 %value, %index, 2;
    mul.wide.u32 %offset, %index, 4;
    add.u64 %base, %base, %offset;
    st.global.u32 [%base], %value;
    ret;
}
""")
module, kernel = ct.c_void_p(), ct.c_void_p()
checked(cuda.cuModuleLoadData, ct.byref(module), ct.cast(ptx, ct.c_void_p))
checked(cuda.cuModuleGetFunction, ct.byref(kernel), module, b'twice')
arguments = (ct.c_void_p * 1)(ct.cast(ct.byref(pointer), ct.c_void_p))
checked(cuda.cuLaunchKernel, kernel, 1, 1, 1, 32, 1, 1, 0, None, arguments, None)
checked(cuda.cuCtxSynchronize)
values = (ct.c_uint32 * 32)()
checked(cuda.cuMemcpyDtoH_v2, values, pointer, ct.sizeof(values))
assert list(values) == [index * 2 for index in range(32)]
print(json.dumps({'devices': count.value, 'selected_ordinal': ordinal, 'values': list(values)}))
'''


class GPUConfigurationTests(unittest.TestCase):
    def test_discovery_rejects_regular_files_and_symlinks(self):
        with temporary_workspace() as folder:
            file = Path(folder, 'nvidia0')
            file.write_text('not a device')
            link = Path(folder, 'nvidia1')
            link.symlink_to('/dev/null')
            self.assertFalse(usable_device(file))
            self.assertFalse(usable_device(link))
        self.assertTrue(usable_device(Path('/dev/null')))

    def test_disabled_gpu_does_not_probe_devices_or_inherit_host_environment(self):
        with patch('node.gpu.usable_device', side_effect=AssertionError('must not probe')):
            configuration = discover_gpu(enabled=False)
        self.assertEqual(configuration.mount_arguments(), [])
        self.assertEqual(configuration.environment(), {'CUDA_VISIBLE_DEVICES': '-1'})
        self.assertFalse(configuration.metadata()['enabled'])

    def test_no_gpu_is_available_without_accessible_compute_nodes(self):
        with patch('node.gpu.usable_device', return_value=False):
            configuration = discover_gpu()
        self.assertEqual(configuration.status, 'unavailable')
        self.assertEqual(configuration.mount_arguments(), [])
        self.assertIn('unavailable', gpu_description(configuration.metadata()))

    def test_visible_devices_filter_and_order_survive_sanitized_environment(self):
        allowed = {'/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-uvm'}
        with patch('node.gpu.Path.glob', return_value=[Path('/dev/nvidia0')]),\
             patch('node.gpu.usable_device', side_effect=lambda path: str(path) in allowed),\
             patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': 'GPU-test',
                                     'CUDA_DEVICE_ORDER': 'PCI_BUS_ID',
                                     'LD_LIBRARY_PATH': '/private/host-libraries',
                                     'GEMINI_API_KEY': 'must-not-forward'}):
            configuration = discover_gpu()
        environment = configuration.environment()
        self.assertEqual(environment['CUDA_VISIBLE_DEVICES'], 'GPU-test')
        self.assertEqual(environment['CUDA_DEVICE_ORDER'], 'PCI_BUS_ID')
        self.assertNotIn('GEMINI_API_KEY', environment)
        self.assertNotIn('/private/host-libraries', str(environment))
        self.assertEqual(configuration.gpu_devices, ('/dev/nvidia0',))


    def test_prompt_includes_runtime_gpu_availability_without_gpu_imports(self):
        self.assertIn('GPU device access is unavailable', system_prompt(gpu=GPUConfig('unavailable').metadata()))
        self.assertIn('fresh Python Pool process', system_prompt(gpu=GPUConfig('enabled').metadata()))


class GPUSandboxBoundaryTests(unittest.TestCase):
    def test_gpu_disabled_hides_host_devices_from_both_tools(self):
        with temporary_workspace() as folder:
            sandbox = tool_sandbox(folder, gpu=False)
            self.addCleanup(sandbox.close)
            sandbox.check()
            code = "import glob, os; assert not glob.glob('/dev/nvidia*'); assert os.environ['CUDA_VISIBLE_DEVICES']=='-1'; assert not os.path.exists('/sys/devices'); print('gpu-hidden')"
            python = run_python(sandbox,code)
            bash = sandbox.execute('bash', json.dumps({'command': "python3 -c \"import glob; assert not glob.glob('/dev/nvidia*'); print('gpu-hidden')\""}))
            for result in (python, bash):
                self.assertEqual(result['exit_code'], 0, result)
                self.assertEqual(result['stdout'], 'gpu-hidden\n')


@unittest.skipUnless(discover_gpu().status == 'enabled', 'accessible NVIDIA GPU required')
class RealGPUTests(unittest.TestCase):
    def test_bash_gpu_mask_is_explicit_and_does_not_persist(self):
        with temporary_workspace() as folder:
            sandbox = tool_sandbox(folder, timeout=20)
            self.addCleanup(sandbox.close)
            code = "import ctypes,os; assert os.environ['CUDA_VISIBLE_DEVICES']=='-1'; cuda=ctypes.CDLL('libcuda.so.1'); assert cuda.cuInit(0)==100; print('no-visible-cuda-device')"
            import shlex
            result=sandbox.execute('bash',json.dumps({'command':'python3 -c '+shlex.quote(code),'env':{'CUDA_VISIBLE_DEVICES':'-1'}}))
            self.assertEqual(result['stdout'],'no-visible-cuda-device\n',result)
            next_call=sandbox.execute('bash',json.dumps({'command':'printf %s "${CUDA_VISIBLE_DEVICES-unset}"'}))
            self.assertNotEqual(next_call['stdout'],'-1')

    def test_real_kernel_persists_across_calls_and_worker_restart_recovers_cuda(self):
        with temporary_workspace() as folder:
            sandbox = tool_sandbox(folder, timeout=20)
            self.addCleanup(sandbox.close)
            sandbox.check()
            probe = sandbox.execute('bash', json.dumps({'command': 'nvidia-smi --query-gpu=name --format=csv,noheader'}))
            self.assertEqual(probe['exit_code'], 0, probe)
            result = run_python(sandbox,CUDA_SETUP)
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual(json.loads(result['stdout'])['values'], list(range(0, 64, 2)))
            again = run_python(sandbox,'checked(cuda.cuMemcpyDtoH_v2, values, pointer, ct.sizeof(values)); print(list(values))')
            self.assertEqual(again['exit_code'], 0, again)
            self.assertEqual(json.loads(again['stdout']), list(range(0, 64, 2)))
            before = sandbox.session_state
            restarted = fresh_python(sandbox)
            self.assertEqual(restarted['process']['status'], 'ready', restarted)
            self.assertEqual(sandbox.session_state['pid'], before['pid'])
            fresh = run_python(sandbox,"assert 'pointer' not in globals(); " + CUDA_SETUP)
            self.assertEqual(fresh['exit_code'], 0, fresh)
            Path(folder, 'gpu_kernel.py').write_text(CUDA_SETUP)
            child = sandbox.execute('bash', json.dumps({'command': 'python3 gpu_kernel.py'}))
            self.assertEqual(child['exit_code'], 0, child)
            self.assertEqual(json.loads(child['stdout'])['values'], list(range(0, 64, 2)))
            # GPU access must not widen the user-file write/read boundary.
            protected = run_python(sandbox,"from pathlib import Path; Path('/sys/module/nvidia/version').write_text('bad')")
            self.assertNotEqual(protected['exit_code'], 0, protected)


if __name__ == '__main__':
    unittest.main()
