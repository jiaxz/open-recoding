"""Agent configuration and display metadata validation."""

import unicodedata

from .model_config import api_base_url, default_model


MASTER_ICON = "🧭"
AGENT_ICON = "🤖"


def validate_icon(value):
    # Emoji may include skin tones, variation selectors, flags or ZWJ sequences.
    # Limit the whole mark, rather than incorrectly requiring len(icon) == 1.
    if (not isinstance(value, str) or not 1 <= len(value) <= 16
            or not any(unicodedata.category(char).startswith("S") or char == "\u20e3" for char in value)
            or any(not (unicodedata.category(char)[0] in {"S", "M"}
                        or char in "\u200d0123456789#*") for char in value)):
        raise ValueError("icon must be a short Unicode emoji or symbol (1–16 code points), without text, whitespace, paths or URLs")
    return value


def validate_execution_config(config):
    mode=config.get("execution_mode","sandbox")
    if mode not in ("sandbox","host"):
        raise ValueError("execution_mode must be sandbox or host")
    if mode=="host" and (not config.get("network",True) or not config.get("gpu",True)):
        raise ValueError("Host execution cannot enforce --no-network or --no-gpu; use sandbox mode")
    return config


def configuration(settings=None, overrides=None):
    return validate_execution_config({"execution_mode":"sandbox", "network": True, "gpu": True, "tool_timeout": 30, "max_output": 16000,
            "api_timeout": 120, "demo": False, "model": default_model(),
            "base_url": api_base_url(), "idle_sleep_seconds": 10,
            **(settings or {}), **(overrides or {})})
