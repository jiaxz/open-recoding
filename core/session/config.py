"""Load local model configuration without shell evaluation or frontend imports."""

import os
from pathlib import Path
import shlex


from multiagent.model_config import MODEL_VARIABLES, VARIABLE_GROUPS


def default_env_file():
    root = next((parent for parent in Path(__file__).resolve().parents
                 if (parent / "pyproject.toml").is_file() and (parent / "core" / "multiagent").is_dir()), None)
    # Source checkouts and editable installs use the project's file even when
    # launched by absolute path from another directory. Wheels use the cwd.
    return (root or Path.cwd()) / ".env"


def load_env_file(path=None, *, environ=None):
    """Load only model settings; existing environment variables take precedence."""
    path = Path(path) if path is not None else default_env_file()
    environment = os.environ if environ is None else environ
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return False
    if len(raw) > 65536:
        raise ValueError(f"{path.name}: configuration exceeds 64 KiB")
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError(f"{path.name}: configuration must be UTF-8") from None
    values = {}
    for number, line in enumerate(source.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if name not in MODEL_VARIABLES:
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            parts = None
        if not separator or parts is None or len(parts) > 1:
            # Never put the offending source line or value in an error message.
            raise ValueError(f"{path.name}:{number}: invalid model configuration")
        values[name] = parts[0] if parts else ""
    # Parse the complete file before applying settings, avoiding partial loads.
    protected = {name for group in VARIABLE_GROUPS.values() if any(name in environment for name in group) for name in group}
    for name, value in values.items():
        if name not in protected:
            environment.setdefault(name, value)
    return True
