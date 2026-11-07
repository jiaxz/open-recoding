"""Static non-builtin definitions, validated and executed through the same execution environment."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


TOOL_PATH = "/opt/recoding/tools"

BASE_NAMES = {"sleep", "send_user_message", "recall_context"}


from node.protocol import validate_arguments


def validate_schema(schema):
    supported={"type","properties","required","additionalProperties","items","minItems","maxItems","uniqueItems",
               "minLength","maxLength","minimum","maximum","enum","description","title","default","examples"}
    if not isinstance(schema,dict) or set(schema)-supported:
        raise ValueError("Unsupported non-builtin parameter schema keyword")
    if schema.get("type") not in {"object","array","string","boolean","integer","number","null"}:
        raise ValueError("Unsupported non-builtin parameter type")
    if schema["type"]=="object":
        properties=schema.get("properties",{})
        if not isinstance(properties,dict) or not isinstance(schema.get("required",[]),list) or set(schema.get("required",[]))-properties.keys():
            raise ValueError("Invalid non-builtin object schema")
        if type(schema.get("additionalProperties",True)) is not bool:
            raise ValueError("Only boolean additionalProperties is supported")
        for value in properties.values():validate_schema(value)
    if schema["type"]=="array":validate_schema(schema.get("items"))


from node.protocol import tool_schemas, combine_schemas


class ToolRegistry:
    def __init__(self, definitions=(), *, isolated=True):
        self.entries, self.mounts, self.runtimes = {}, {}, {}
        for original in definitions:
            item = deepcopy(original)
            schema = item["schema"]
            name = schema["function"]["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name) or name in BASE_NAMES or name in self.entries:
                raise ValueError("Invalid, duplicate or reserved non-builtin tool name")
            parameters = schema["function"]["parameters"]
            if parameters.get("type") != "object" or "oneOf" in parameters or "anyOf" in parameters:
                raise ValueError("Tool parameter root must be a plain object")
            validate_schema(parameters)
            root = Path(item["root"])
            if root.is_symlink() or not root.is_dir():
                raise ValueError("Non-builtin tool root must be a real directory")
            root = root.resolve()
            item['root']=root
            if item.get('runtime'):
                unresolved = root / item['runtime']
                path = unresolved.resolve(strict=True)
                if not path.is_relative_to(root) or unresolved.is_symlink() or not path.is_file():
                    raise ValueError('Invalid sandbox tool runtime')
                key = str(path)
                if key not in self.runtimes:
                    sources=[]
                    for relative in item.get('runtime_includes',[]):
                        dependency=root / relative
                        if dependency.is_symlink() or not dependency.resolve(strict=True).is_relative_to(root):
                            raise ValueError('Invalid sandbox runtime dependency')
                        sources.append(dependency.read_text(encoding='utf-8'))
                    self.runtimes[key] = {'source': '\n'.join([*sources,path.read_text(encoding='utf-8')]), 'names': []}
                self.runtimes[key]['names'].append(name)
                self.entries[name] = item
                continue
            if item.get('service'):
                if not isinstance(item['service'],str):raise ValueError('Invalid tool service')
                self.entries[name]=item
                continue
            if root not in self.mounts:
                self.mounts[root] = TOOL_PATH + "/" + str(len(self.mounts)) if isolated else str(root)
            command = item["command"]
            if not isinstance(command, list) or not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
                raise ValueError("Non-builtin command must be a nonempty argv list")
            if 'max_output' in item and (type(item['max_output']) is not int or not 128 <= item['max_output'] <= 1024*1024):
                raise ValueError('Non-builtin output budget must be 128–1048576 bytes')
            item["root"] = root
            item["command"] = [arg.replace("{tool_root}", self.mounts[root]) for arg in command]
            self.entries[name] = item

        # Runtime code executes during sandbox startup, before the first tool call.
        for name, item in self.entries.items():
            if item.get('runtime'): self.verify_source(name)

    @property
    def schemas(self):
        return [deepcopy(item["schema"]) for item in self.entries.values()]

    def verify_source(self,name):
        item=self.entries[name]
        for relative, expected in item.get("files", {}).items():
            path = item["root"] / relative
            if path.is_symlink() or not path.resolve().is_relative_to(item["root"]) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Non-builtin tool source changed; restart Host to load the new build")

    def execute(self, session, name, arguments, *, cancelled=None):
        item = self.entries[name]
        fields = json.loads(arguments)
        validate_arguments(fields, item["schema"]["function"]["parameters"])
        self.verify_source(name)
        if item.get('runtime'):
            timeout = fields.get('timeout') if item.get('execution') and fields.get('mode','sync') == 'sync' else None
            return session.operation('tool', name=name, arguments=fields, timeout=timeout,cancelled=cancelled)
        encoded = json.dumps(fields, ensure_ascii=False)
        if len(encoded.encode()) > 1024*1024:
            raise ValueError("Non-builtin arguments exceed 1 MiB")
        output_budget = {'max_output':item['max_output']} if 'max_output' in item else {}
        result = session.operation("command", argv=item["command"], input=encoded + "\n",cancelled=cancelled, **output_budget)
        if item.get("output") == "json" and not result.get("truncated"):
            try:
                result["data"] = json.loads(result["stdout"])
            except ValueError:
                result.update(exit_code=1, error="Tool returned invalid JSON; see stdout/stderr")
        return result
