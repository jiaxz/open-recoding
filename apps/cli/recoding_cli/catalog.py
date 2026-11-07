"""Lightweight Session discovery, independent of the Web catalog."""

from pathlib import Path
import os

from session import SessionStore
from session.store import SESSION_HOME


def discover(root, *, recursive=True):
    root = Path(root).expanduser().resolve()
    results = []
    for directory, folders, _ in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not name.startswith('.') and name not in {'node_modules', 'snapshots', '__pycache__'}
                      and not (Path(directory)/name).is_symlink()] if recursive else []
        container = Path(directory)/SESSION_HOME
        if not container.is_dir() or container.is_symlink():
            continue
        for folder in container.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                results.append(SessionStore(folder, navigation_only=True).navigation_info())
            except (ValueError, OSError):
                continue
    return sorted(results, key=lambda item: (not item['pinned'], -item['updated_at']))


def select(value, root, *, recursive=True):
    if not value:
        raise ValueError('Specify --session PATH, ID or unique name; use `recoding list` to discover Sessions.')
    path = Path(value).expanduser()
    if path.is_dir():
        return SessionStore(path).folder
    records = discover(root, recursive=recursive)
    matches = [item for item in records if value in {item['id'], item['name'], Path(item['session_directory']).name}]
    if not matches:
        matches = [item for item in records if item['id'].startswith(value)]
    if len(matches) != 1:
        raise ValueError('Session not found or ambiguous; use its full path or ID.')
    return Path(matches[0]['session_directory'])
