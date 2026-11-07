"""Provider-neutral configuration and unknown models; local fixtures only."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch,Mock

from multiagent.client import Client
from multiagent.context_window import ContextBudget
from multiagent.model_config import ModelProfile, PROFILES, UNKNOWN_COLORS, profile_for, public_profiles
from multiagent.model_config import api_key,api_base_url,default_model
from multiagent.models import model_messages
from session.config import load_env_file
from session import Session
from workspace_support import PROJECT,temporary_workspace
from model_support import local_model


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(temporary_workspace(prefix="recoding-model-catalog-").name)

    def test_neutral_env_alias_precedence_is_environment_then_file(self):
        source=self.root/"model.env"
        source.write_text("RECODING_API_KEY=file-new\nRECODING_BASE_URL=https://new.test/v1\nRECODING_MODEL=vendor/model\n")
        environment={"GEMINI_API_KEY":"existing-old","GEMINI_BASE_URL":"https://old.test/v1"}
        load_env_file(source,environ=environment)
        self.assertEqual(api_key(environment),"existing-old")
        self.assertEqual(api_base_url(environment),"https://old.test/v1")
        self.assertEqual(default_model(environment),"vendor/model")
        environment={"RECODING_API_KEY":"","GEMINI_API_KEY":"must-not-be-used"}
        self.assertEqual(api_key(environment),"")
        environment={};load_env_file(source,environ=environment)
        self.assertEqual(api_key(environment),"file-new")

    def test_unknown_model_uses_portable_history_and_explicit_profile_options(self):
        name="new-vendor/model-9"
        original=[{"role":"assistant","_model":"older-model","content":"previous", "reasoning_content":"readable", "encrypted_content":"private", "tool_calls":[
            {"id":"one","type":"function","function":{"name":"python","arguments":"{}"},"extra_content":{"signature":"private"}}]}]
        kept=deepcopy(original)
        for target in (name,"gemini-unregistered"):
            wire=model_messages(original,target)
            self.assertNotIn("private",json.dumps(wire))
            self.assertNotIn("extra_content",wire[0]["tool_calls"][0])
        self.assertEqual(original,kept)
        profile=ModelProfile(name,context_window=32000,request_options={"stream":False,"temperature":.1})
        with patch.dict(PROFILES,{name:profile}):
            client=Client("fixture","https://fixture.test/compatible",name,1,tools=[])
            request,metadata=client.prepare_request(original)
            body=json.loads(request.data)
            self.assertEqual(request.full_url,"https://fixture.test/compatible/chat/completions")
            self.assertFalse(body["stream"])
            self.assertNotIn("stream_options",body)
            self.assertEqual(body["temperature"],.1)
            self.assertFalse(metadata["stream"])
            response=Mock(status=200,headers={"content-type":"application/json"})
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.return_value=json.dumps({"choices":[{"message":{"role":"assistant","content":"new API text"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}).encode()
            client.opener=Mock();client.opener.open.return_value=response
            self.assertEqual(client.complete([{"role":"user","content":"check"}])["content"],"new API text")
            self.assertEqual(client.last_usage["prompt_tokens"],12)
            budget=ContextBudget(self.root/"context-budget.json",model=lambda:name,tools=[])
            self.assertEqual(budget.measure([{"role":"system","content":"rules"}])["context_window_tokens"],32000)
        unknown=ContextBudget(self.root/"other-budget.json",model=lambda:name,tools=[],limits={name:64000})
        self.assertEqual(unknown.measure([{"role":"system","content":"rules"}])["profile_source"],"session_override")

    @unittest.skipUnless(shutil.which("node"),"Node is used for Web presentation checks")
    def test_web_and_cli_use_the_same_catalog_and_unknown_colors(self):
        identifiers=["grok-4.7","gemini-4-argon","gemini-3.8-flash","vendor/new-model","模型/未来"]
        payload={"profiles":public_profiles(),"style":{"unknown_colors":list(UNKNOWN_COLORS)},
                 "expected":{name:profile_for(name).presentation() for name in identifiers}}
        program=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context={window:{},TextEncoder,document:{querySelectorAll:()=>[]}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const api=context.window.AgentStatistics;api.configureModels(input.profiles,input.style);
for(const [id,profile] of Object.entries(input.expected)){
 const styles={},element={dataset:{},style:{setProperty:(key,value)=>styles[key]=value}};
 api.identifyModel(element,id);assert.equal(styles['--model-accent'],profile.color);
 assert.equal(api.modelFamily(id),profile.family);assert.equal(api.modelPresentation(id).short_label,profile.short_label);
}
"""
        process=subprocess.run(["node","-e",program,str(PROJECT/"apps/web/recoding_web/static/statistics.js")],
            input=json.dumps(payload),text=True,capture_output=True,timeout=5)
        self.assertEqual(process.returncode,0,process.stderr)

    def test_unknown_model_runs_real_session_with_neutral_api_settings(self):
        ready=threading.Event()
        def respond(body,number):
            self.assertEqual(body["model"],"new-vendor/model-9")
            def call(name,**args):return {"role":"assistant","tool_calls":[{"index":0,"id":f"generic-{number}","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}
            if number==1:return call("python",code="print(40+2)")
            if number==2:
                result=json.loads(next(m["content"] for m in reversed(body["messages"]) if m["role"]=="tool"))
                self.assertEqual(result["stdout"],"42\n")
                return call("send_user_message",message="Unknown model fixture verified 42")
            ready.set();return call("sleep",seconds=3600)
        with local_model(self.root/"model",respond) as fixture,patch.dict(os.environ,{
                "RECODING_API_KEY":"neutral-local-fixture","RECODING_BASE_URL":fixture.url,"RECODING_MODEL":"new-vendor/model-9",
                "SSL_CERT_FILE":str(fixture.cert),"NO_PROXY":"127.0.0.1","PYTHONUSERBASE":str(self.root/"empty-site")}):
            session=Session.create(self.root/"workspace","unknown-model",config={"network":False,"gpu":False,"api_timeout":5,"tool_timeout":5})
            try:
                self.assertTrue(ready.wait(8),str(fixture.errors))
                deadline=time.monotonic()+3
                with session.condition:
                    while session.master.status != "sleeping":
                        self.assertLess(time.monotonic(),deadline,str(fixture.errors))
                        session.condition.wait(.02)
                self.assertEqual(session.config["model"],"new-vendor/model-9")
                self.assertFalse(fixture.errors,fixture.errors)
            finally:session.close()
