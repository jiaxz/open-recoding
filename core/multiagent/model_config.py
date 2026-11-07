"""Model profiles and environment configuration."""

import os
from dataclasses import dataclass, field


DEFAULT_MODEL = "grok-4.7"
DEFAULT_URL = "https://llm-api.cc/api/v1"
UNKNOWN_CONTEXT_WINDOW = 200_000
UNKNOWN_COLORS = ("#d5c39a", "#a5c8c2", "#c7afd0", "#aabed8", "#d3b4ad", "#b8c69f")


@dataclass(frozen=True)
class ModelProfile:
    identifier: str
    label: str = ""
    short_label: str = ""
    family: str = "other"
    color: str = "#a0aaa8"
    tint: str = "#252c2c"
    border: str = "#485352"
    context_window: int | None = None
    protocol: str = "chat_completions"
    foreign_tool_marker: dict | None = None
    request_options: dict = field(default_factory=dict)

    def presentation(self):
        return {"label":self.label or self.identifier,"short_label":self.short_label or self.label or self.identifier,
                "family":self.family,"color":self.color,"tint":self.tint,"border":self.border}


_GEMINI_MARKER = {"google":{"thought_signature":"skip_thought_signature_validator"}}
PROFILES = {
    "grok-4.7": ModelProfile("grok-4.7", "Grok", "Grok", "grok", "#cbb8e4", "#302a38", "#5b4b6b", 500_000),
    "gemini-4-argon": ModelProfile("gemini-4-argon", "Argon", "G4", "gemini", "#a6dec5", "#22332b", "#3a5b4b", 1_048_576, foreign_tool_marker=_GEMINI_MARKER),
    "gemini-3.8-flash": ModelProfile("gemini-3.8-flash", "Flash", "G3.8", "gemini", "#9bc8ed", "#202e3b", "#3b5974", 1_048_576, foreign_tool_marker=_GEMINI_MARKER),
}


def profile_for(identifier):
    known = PROFILES.get(identifier)
    if known is not None:
        return known
    digest = 2166136261
    for byte in str(identifier).encode():
        digest = ((digest ^ byte) * 16777619) & 0xffffffff
    color = UNKNOWN_COLORS[digest % len(UNKNOWN_COLORS)]
    return ModelProfile(identifier, color=color)


def model_ids():
    return tuple(dict.fromkeys((DEFAULT_MODEL,*PROFILES)))


def public_profiles(identifiers=None):
    return {identifier:profile_for(identifier).presentation() for identifier in identifiers or model_ids()}


VARIABLE_GROUPS = {
    "key": ("RECODING_API_KEY", "GEMINI_API_KEY"),
    "base_url": ("RECODING_BASE_URL", "GEMINI_BASE_URL"),
}
MODEL_VARIABLES = frozenset(name for group in VARIABLE_GROUPS.values() for name in group) | {"RECODING_MODEL", "GEMINI_MODEL"}


def _setting(kind, default, environ=None):
    environment = os.environ if environ is None else environ
    for name in VARIABLE_GROUPS[kind]:
        if name in environment:
            return environment[name].strip()
    return default


def api_key(environ=None):
    return _setting("key", "", environ)


def api_base_url(environ=None):
    return _setting("base_url", DEFAULT_URL, environ)


def default_model(environ=None):
    environment = os.environ if environ is None else environ
    # GEMINI_MODEL remains a legacy choice hint, not a new-Session default.
    return environment.get("RECODING_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def model_choices(environ=None):
    environment = os.environ if environ is None else environ
    return tuple(dict.fromkeys(value for value in (*model_ids(),default_model(environment),environment.get("GEMINI_MODEL")) if value))
