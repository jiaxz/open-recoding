"""Builtin schemas, validation, step notes and Worker tool dispatch."""

import json
from copy import deepcopy


def validate_arguments(value, schema, path="arguments"):
    """Small explicit JSON-Schema subset used by shipped tool definitions."""
    kind = schema.get("type")
    correct = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int, "number": type(value) in (int, float), "null": value is None}
    if kind not in correct or not correct[kind]:
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside enum")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"{path} is missing required fields")
        if schema.get("additionalProperties", True) is False and value.keys() - properties.keys():
            raise ValueError(f"{path} has unknown fields")
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key], path + "." + key)
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError(f"{path} has invalid item count")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ValueError(f"{path} contains duplicates")
        for item in value:
            validate_arguments(item, schema["items"], path + "[]")
    if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
        raise ValueError(f"{path} has invalid length")
    if kind in {"integer", "number"} and not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
        raise ValueError(f"{path} is outside numeric bounds")


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}

TEXT = {"type": "string"}
IMAGES = {"type": "array", "items": TEXT, "maxItems": 8}

SEND_USER_MESSAGE_TOOL = tool("send_user_message", "Deliver a Markdown message and optional workspace images directly to the human user. This is a one-to-one human output channel, separate from Agent messages. It snapshots local images linked inline in the Markdown body automatically. Do not repeat inline images in images; use that field only for extra attachments. It persists a human outbox record and immutable image snapshots. Ordinary assistant prose does not send a human message automatically.",
         {"message": TEXT, "images": IMAGES}, ("message",))

SLEEP_TOOL = tool("sleep", "Sleep for 0–3600 seconds, waking immediately if YOUR inbox already has any unread message or receives one. Other Agents' mail does not wake you. Returns only reason, unread count and elapsed time; message bodies arrive once through the normal pre-model drain. Choose when to work or sleep freely; use longer waits while idle to avoid model-call polling.",
         {"seconds": {"type": "number", "minimum": 0, "maximum": 3600}}, ("seconds",))

def combine_schemas(*groups):
    from copy import deepcopy
    result,names=[],set()
    for group in groups:
        for schema in group:
            name=schema['function']['name']
            if name in names:raise ValueError('Duplicate tool definition: '+name)
            names.add(name);result.append(deepcopy(schema))
    return result


def tool_schemas(definitions,permissions=None):
    from copy import deepcopy
    allowed=(permissions or {}).get('tools')
    return [deepcopy(item['schema']) for item in definitions if allowed is None or item['schema']['function']['name'] in allowed and all(name in allowed for name in item.get('requires',[]))]


class HostTools:
    """A generic injected tool port. It never creates or imports an execution backend."""
    def __init__(self,rpc,messages,schemas=None,permissions=None):
        self.rpc,self.messages=rpc,messages
        self.schemas={item['function']['name']:item['function']['parameters'] for item in (CONTEXT_TOOLS if schemas is None else schemas)}
        self.permissions=permissions or {}

    def execute(self,name,arguments):
        try:
            if name not in self.schemas:raise ValueError('Tool is not installed for this Agent')
            fields=json.loads(arguments)
            if not isinstance(fields,dict):raise ValueError('Tool arguments must be an object')
            fields.pop(FIELD,None)
            validate_arguments(fields,self.schemas[name])
            if name=='recall_context':
                allowed=self.permissions.get('tools')
                if allowed is not None and name not in allowed:raise ValueError('This tool is outside delegated permissions')
                return recall_context(self.messages(),fields)
            if 'seconds' in self.schemas[name].get('properties',{}):fields['seen']=seen_messages(self.messages())
            return self.rpc(name,fields)
        except (ValueError,KeyError,TypeError) as error:return {'error':str(error),'exit_code':1}


def seen_messages(messages):
    return [message["_agent_message_id"] for message in messages if isinstance(message.get("_agent_message_id"), str)]


FIELD = "_step_note"


def with_step_notes(schemas):
    """Decorate only the model-facing copy; executable schemas remain unchanged."""
    result = deepcopy(schemas)
    for schema in result:
        parameters = schema["function"]["parameters"]
        if FIELD in parameters.get("properties", {}):
            raise ValueError(f"{FIELD} is reserved for Model call display metadata")
        parameters["properties"] = {FIELD: {
            "type": "string", "minLength": 1, "maxLength": 240,
            "description": "Brief user-language step note, not private reasoning. Do not claim unexecuted results."},
            **parameters.get("properties", {})}
        parameters["required"] = [FIELD, *parameters.get("required", [])]
        function = schema["function"]
        function["description"] = "Include _step_note describing this step. " + function.get("description", "")
    return result


def attach_step_note(event):
    """Keep provenance explicit; never substitute metadata for original content."""
    notes, messages = [], []
    for call in event.get("calls") or []:
        try:
            fields = json.loads(call["function"]["arguments"])
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(fields, dict):
            continue
        for value, target in ((fields.get(FIELD), notes), (fields.get("message"), messages)):
            if isinstance(value, str) and value.strip() and value.strip() not in target:
                target.append(value.strip())
    selected = notes or messages
    return {**event, "step_note": "\n\n".join(selected),
            "step_note_source": "tool_arguments" if notes else "tool_message"} if selected else event




COMMON_TOOLS = [
    tool("send_agent_message", "Send text and optional workspace image paths to one Agent (agent_id) OR a named group (agent_ids), never both. Recipients must all be starting/running/sleeping Agents; stopped/stopping/failed recipients reject new mail. Human/user/system are never Agent recipients. Image snapshots and accessible copies are delivered as metadata/paths, NOT image pixels in model context: recipients use view_image when needed.",
         {"agent_id": TEXT, "agent_ids": {"type": "array", "items": TEXT, "minItems": 1, "maxItems": 100, "uniqueItems": True}, "message": TEXT, "images": IMAGES}, ("message",)),
    tool("broadcast_agent_message", "Send text and optional workspace images to ALL other accepting Agents, excluding yourself. The recipient snapshot is atomic; no human is included. Image metadata/paths are delivered, and recipients decide whether to view_image. Returns actual recipients and message IDs.",
         {"message": TEXT, "images": IMAGES}, ("message",)),
    SEND_USER_MESSAGE_TOOL,
    tool("list_agents", "List Master and sub-agents using explicit public metadata only. Never returns context, conversation, inboxes, tool results, code, signatures or reasoning.", {}),
    SLEEP_TOOL,
]
PEOPLE = {"type": "array", "items": TEXT, "maxItems": 100, "uniqueItems": True,
          "description": "Optional sender/recipient IDs (including user); matches any selected ID. Both filters must match. Omit for everyone."}
OBSERVATION_TOOLS = [
    tool("read_global_messages", "Master only: optionally inspect this Session's human/Agent correspondence, not context, reasoning, tools or system notices. No inbox is consumed and nothing is forwarded. Omitting after returns recent messages; after=0 reads from the beginning; reuse next_cursor for incremental pages. Broadcasts appear once with all recipients. max_chars bounds total returned body characters; truncated records include next_offset. Use message_id plus offset to read a long message in slices (cannot combine with cursor/filters/limit). Image paths only; view_image explicitly for pixels. Results cost input tokens; read selectively.",
         {"after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50},
          "max_chars": {"type": "integer", "minimum": 256, "maximum": 32000}, "senders": PEOPLE, "recipients": PEOPLE,
          "message_id": TEXT, "offset": {"type": "integer", "minimum": 0}}),
    tool("listen_global_messages", "Master only: wait for new Session correspondence without reading message bodies. after is the last read_global_messages.next_cursor; omitted after watches from now. Returns read_after (use it for the next read), latest_cursor and a signal only; never advance to latest_cursor without reading if you need the intervening messages. Optional sender/recipient filters; seconds defaults to 30 (0–3600). Your own unread inbox also wakes this wait even if filters exclude it. Timeout/Stop cancel waiting; no persistent subscription, no automatic forwarding, no changes to normal sleep.",
         {"after": {"type": "integer", "minimum": 0}, "seconds": {"type": "number", "minimum": 0, "maximum": 3600},
          "senders": PEOPLE, "recipients": PEOPLE}),
]
# Keep the root a plain object across gateways. Recipient XOR is enforced by
# the coordinator, rather than a root oneOf some providers cannot compile.
MANAGEMENT_TOOLS = [
    tool("set_agent_icon", "Master only: choose or change the display icon of yourself (agent_id=master) or any existing sub-agent, including stopped Agents. Use a short Unicode emoji or symbol (up to 16 code points; emoji sequences allowed), not a URL or file. Persists across Resume without changing identity, context, inbox or runtime.",
         {"agent_id": TEXT, "icon": {"type": "string", "minLength": 1, "maxLength": 16}}, ("agent_id", "icon")),
    tool("rename_agent", "Master only: rename an existing sub-agent for a clearer role or assignment. Preserves its ID, history, inbox, files and runtime, including stopped Agents. Send any changed task instructions separately as messages.",
         {"agent_id": TEXT, "name": TEXT}, ("agent_id", "name")),
    tool("create_agent", "Master only: create a named persistent worker with its own System and blank history. Carries NO task: send all required background, goals, inputs, outputs, constraints and file ownership explicitly through messages. Optional permissions may only narrow Session permissions.",
         {"name": TEXT, "icon": {"type": "string", "minLength": 1, "maxLength": 16, "description": "Unicode emoji/symbol matching this Agent's role; editable later with set_agent_icon."}, "permissions": {"type": "object", "properties": {"network": {"type": "boolean"}, "gpu": {"type": "boolean"}, "tools": {"type": "array", "items": TEXT}}, "additionalProperties": False}}, ("name",)),
    tool("stop_agent", "Master only: stop one sub-agent, release its runtime and retain context, accepted inbox, settings and files. Does not invent completion reports.",
         {"agent_id": TEXT, "reason": TEXT}, ("agent_id", "reason")),
    tool("resume_agent", "Master only: restart a stopped/failed Agent with the SAME ID, context and inbox, but fresh Python/Bash memory. Carries NO task or instructions: send new instructions explicitly after it accepts mail.",
         {"agent_id": TEXT}, ("agent_id",)),
]
AGENT_TOOLS = MANAGEMENT_TOOLS + OBSERVATION_TOOLS + COMMON_TOOLS
SUB_AGENT_TOOLS = COMMON_TOOLS
AGENT_TOOL_NAMES = {item["function"]["name"] for item in AGENT_TOOLS}
MANAGEMENT_NAMES = {item["function"]["name"] for item in MANAGEMENT_TOOLS}
OBSERVATION_NAMES = {item["function"]["name"] for item in OBSERVATION_TOOLS}


def agent_tools(identifier):
    return AGENT_TOOLS if identifier == "master" else SUB_AGENT_TOOLS


TOOL_PARAMETERS = {item["function"]["name"]: item["function"]["parameters"] for item in AGENT_TOOLS}


def validate_request(source, action, fields):
    if action not in TOOL_PARAMETERS:
        raise ValueError("Unknown coordination action")
    if source != "master" and action in MANAGEMENT_NAMES | OBSERVATION_NAMES:
        raise ValueError("Only Master may manage Agents or observe global messages")
    # seen is supplied by the trusted worker, never exposed in a model schema.
    host_fields = {"seen"} if action in {"sleep", "listen_global_messages"} else set()
    validate_arguments({key: value for key, value in fields.items() if key not in host_fields}, TOOL_PARAMETERS[action])
    if "seen" in fields and host_fields:
        validate_arguments(fields["seen"], {"type":"array", "items":TEXT})


def model_result_view(name, result):
    """Slim model receipts; full coordinator results remain in events/history."""
    if not isinstance(result, dict) or result.get("error"):
        return None
    if name in {"send_agent_message", "broadcast_agent_message"}:
        view = {"recipients": [{key: item[key] for key in ("agent_id", "message_id") if key in item}
                               for item in result.get("recipients", [])]}
        return view
    if name == "send_user_message":
        view = {key: result[key] for key in ("message_id", "target") if key in result}
        if result.get("images"):
            view["images_sent"] = len(result["images"])
        return view
    if name == "list_agents":
        keys = ("id", "name", "icon", "status", "model", "resumable", "error_available", "permissions")
        return {"agents": [{key: item[key] for key in keys if key in item and item[key] not in (None, {})}
                            for item in result["agents"]], "max_subagents": result["max_subagents"]}
    if name == "sleep":
        return {"reason": result["reason"], "messages_queued": result["messages_queued"],
                "elapsed": round(result["elapsed"], 3)}
    return None


CONTEXT_TOOLS = [{"type": "function", "function": {
    "name": "recall_context", "description": "Read ONLY your own original, uncompressed committed history. Use indices in continuation memory, or query text. Returns bounded text pages and image metadata, never image pixels or another Agent's context. start is a zero-based original message index; offset continues a record's serialized text; next_start/next_offset permit exact paging.",
    "parameters": {"type": "object", "properties": {
        "start": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "query": {"type": "string", "maxLength": 500}, "offset": {"type": "integer", "minimum": 0}},
        "required": [], "additionalProperties": False}}}]


def recall_context(messages, fields):
    from .context_window import portable_record
    start, limit, query, offset = (fields.get(key, default) for key, default in (("start", 0), ("limit", 5), ("query", ""), ("offset", 0)))
    records, budget, next_start, next_offset = [], 16000, None, 0
    for index in range(start, len(messages)):
        record = json.dumps(portable_record(messages[index], index), ensure_ascii=False)
        if query.casefold() not in record.casefold():
            continue
        if not records and offset > len(record):
            raise ValueError("Recall offset is outside this record")
        part = record[offset:offset+budget]
        records.append({"index": index, "offset": offset, "text": part})
        next_offset = offset+len(part) if offset+len(part) < len(record) else 0
        next_start = index if next_offset else index+1
        budget -= len(part);offset = 0
        if next_offset or not budget or len(records) == limit:
            break
    return {"records": records, "next_start": next_start, "next_offset": next_offset, "total_messages": len(messages)}

