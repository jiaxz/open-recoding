"""Shipped tools use ordinary Tool calls, real sandboxes and local model fixtures."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from multiagent.tools import HostTools
from sandbox_support import Sandbox
from tools.registry import ToolRegistry, tool_schemas
from tools import definitions
from session import Session
from workspace_support import temporary_workspace
from model_support import local_model


class NonBuiltinTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(temporary_workspace(prefix="recoding-non-builtin-").name)
        self.patcher = patch.dict(os.environ,{"PYTHONUSERBASE":str(self.root/"empty-user-site")})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.sandbox = Sandbox(self.root,timeout=5,network=False,gpu=False,non_builtin_tools=definitions())
        self.addCleanup(self.sandbox.close)

    def test_real_tools_share_existing_sandbox_install_wheel_and_reset_shell(self):
        self.sandbox.execute("bash",json.dumps({"command":"kept_shell=41; cd /tmp"}))
        pid=self.sandbox.session.process.pid
        wheel=self.root/"recoding_addon_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel,"w") as archive:
            archive.writestr("recoding_addon_fixture.py","VALUE=42\n")
            base="recoding_addon_fixture-1.0.dist-info/"
            archive.writestr(base+"METADATA","Metadata-Version: 2.1\nName: recoding-addon-fixture\nVersion: 1.0\n")
            archive.writestr(base+"WHEEL","Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(base+"RECORD","")
        result=self.sandbox.execute("install_packages",json.dumps({"packages":["/workspace/"+wheel.name],"no_index":True,"no_deps":True}))
        self.assertEqual(result["exit_code"],0,result)
        result=self.sandbox.execute("inspect_python_environment","{}")
        self.assertEqual(result["exit_code"],0,result)
        self.assertIn({"name":"recoding-addon-fixture","version":"1.0","scope":"workspace"},result["data"]["packages"])
        self.assertEqual(pid,self.sandbox.session.process.pid)
        result=self.sandbox.execute("bash",json.dumps({"command":"python3 -c 'import recoding_addon_fixture; print(recoding_addon_fixture.VALUE)'"}))
        self.assertEqual(result["stdout"],"42\n",result)
        self.assertEqual(self.sandbox.execute("bash",json.dumps({"command":"printf '%s' ${kept_shell-unset}"}))["stdout"],"unset")
        other=Sandbox(self.root,timeout=5,network=False,gpu=False,non_builtin_tools=definitions())
        self.addCleanup(other.close)
        self.assertEqual(other.execute("bash",json.dumps({"command":"python3 -c 'import recoding_addon_fixture; print(recoding_addon_fixture.VALUE)'"}))["stdout"],"42\n")

    def test_schema_permission_and_source_revision_are_enforced(self):
        host=HostTools(lambda *_:self.fail("not a host RPC"),lambda:[],[])
        self.assertIn("not installed",host.execute("inspect_python_environment","{}")["error"])
        self.assertEqual([item["function"]["name"] for item in tool_schemas(definitions(),{"tools":["python"]})],["python"])
        self.assertIn("error",self.sandbox.execute("inspect_python_environment",'{"unknown":true}'))
        self.assertIn("error",self.sandbox.execute("install_packages",'{"packages":[]}'))
        self.assertIsNone(self.sandbox.session.process)
        duplicate=deepcopy(definitions()[0]);duplicate["schema"]["function"]["name"]="sleep"
        with self.assertRaisesRegex(ValueError,"reserved"):ToolRegistry([duplicate])
        root=self.root/"custom";root.mkdir();source=root/"main.py";source.write_text("print('first')")
        descriptor={"root":str(root),"schema":{"type":"function","function":{"name":"custom","parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}},
            "command":["/usr/bin/python3","-I","-S","{tool_root}/main.py"],"files":{"main.py":hashlib.sha256(source.read_bytes()).hexdigest()}}
        custom=Sandbox(self.root,network=False,gpu=False,non_builtin_tools=[descriptor]);self.addCleanup(custom.close)
        source.write_text("print('changed')")
        self.assertIn("source changed",custom.execute("custom","{}")["error"])

    def test_timeout_reclaims_tool_children_without_restarting_sandbox(self):
        root=self.root/"commands";root.mkdir()
        script=root/"main.py"
        script.write_text("import subprocess,time\nsubprocess.Popen(['/usr/bin/python3','-c',\"import time;from pathlib import Path;time.sleep(.5);Path('/workspace/LATE').touch()\"])\nprint('x'*10000,flush=True)\ntime.sleep(30)\n")
        schema={"type":"function","function":{"name":"slow","parameters":{"type":"object","properties":{},"required":[],"additionalProperties":False}}}
        custom=Sandbox(self.root,timeout=.15,max_output=100,network=False,gpu=False,non_builtin_tools=[{"root":str(root),"schema":schema,"command":["/usr/bin/python3","-I","-S","{tool_root}/main.py"]}])
        self.addCleanup(custom.close)
        result=custom.execute("slow","{}")
        self.assertTrue(result["timed_out"],result)
        self.assertTrue(result["truncated"],result)
        self.assertEqual(len(result["stdout"].encode())+len(result["stderr"].encode()),100)
        pid=custom.session.process.pid
        time.sleep(.6)
        self.assertFalse((self.root/"LATE").exists())
        self.assertEqual(custom.session.request("check")["stdout"],"sandbox-ok\n")
        self.assertEqual(custom.session.process.pid,pid)


class NonBuiltinWorkerTests(unittest.TestCase):
    def test_master_and_sub_use_non_builtin_through_normal_model_tool_loop(self):
        with temporary_workspace(prefix="recoding-non-builtin-worker-") as folder:
            root=Path(folder);stages={"master":0,"sub":0};reports=threading.Event()
            def respond(body,number):
                names={item["function"]["name"] for item in body["tools"]}
                master="create_agent" in names;role="master" if master else "sub"
                assert "inspect_python_environment" in names
                stage=stages[role];stages[role]+=1
                def call(tool_name,**args):return {"role":"assistant","tool_calls":[{"index":0,"id":f"non-builtin-{number}","type":"function","function":{"name":tool_name,"arguments":json.dumps(args)}}]}
                if master and stage==0:return call("create_agent",name="Package inspector",permissions={"tools":["inspect_python_environment"]})
                if (master and stage==1) or (not master and stage==0):return call("inspect_python_environment")
                if (master and stage==2) or (not master and stage==1):
                    previous=json.loads(next(m["content"] for m in reversed(body["messages"]) if m["role"]=="tool"))
                    assert previous["exit_code"]==0 and previous["data"]["cache"]=="/opt/recoding/cache",previous
                    return call("send_user_message",message=role+" inspected packages")
                if stages["master"]>=4 and stages["sub"]>=3:reports.set()
                return call("sleep",seconds=3600)
            with local_model(root/"model",respond) as fixture,patch.dict(os.environ,{"GEMINI_API_KEY":"local-fixture","SSL_CERT_FILE":str(fixture.cert),"NO_PROXY":"127.0.0.1","PYTHONUSERBASE":str(root/"empty-site")}):
                session=Session.create(root/"workspace","non-builtin workers",config={"base_url":fixture.url,"network":False,"gpu":False,"api_timeout":5,"tool_timeout":5})
                try:
                    self.assertTrue(reports.wait(10),str(fixture.errors)+str(stages))
                    self.assertFalse(fixture.errors,fixture.errors)
                finally:session.close()
