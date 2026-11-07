"""Dependency-free node protocol and validated metadata."""
import json

PROTOCOL_VERSION=1
MAX_IMAGE_BYTES=5*1024*1024

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

def validate_execution_config(config):
    mode=config.get("execution_mode","sandbox")
    if mode not in ("sandbox","host"):
        raise ValueError("execution_mode must be sandbox or host")
    if mode=="host" and (not config.get("network",True) or not config.get("gpu",True)):
        raise ValueError("Host execution cannot enforce --no-network or --no-gpu; use sandbox mode")
    return config
