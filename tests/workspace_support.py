"""Isolated test workspaces; outer test commands remove all data on exit."""

from pathlib import Path
import atexit
import os
import sys
import tempfile

from workspace_cleanup import PROJECT, TEST_SESSION_ROOT, cleanup_test_data


_test_home = None
_owner_pid = int(os.environ.setdefault("RECODING_TEST_OWNER_PID", str(os.getpid())))


def _cleanup_at_exit():
    if os.getpid() != _owner_pid:
        return
    try:
        cleanup_test_data()
    except (OSError, ValueError, RuntimeError) as error:
        print("Test data cleanup failed: " + str(error), file=sys.stderr)


atexit.register(_cleanup_at_exit)


class TestWorkspace:
    def __init__(self, *, prefix, suffix=None):
        global _test_home
        TEST_SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        if _test_home is None:
            _test_home = tempfile.mkdtemp(prefix="recoding-test-home-", dir=TEST_SESSION_ROOT)
            os.environ["RECODING_HOME"] = str(Path(_test_home) / ".recoding")
            for key in ("RECODING_API_KEY", "RECODING_BASE_URL", "RECODING_MODEL"):
                os.environ.pop(key, None)
        self.name = tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=TEST_SESSION_ROOT)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()

    def cleanup(self):
        """Run-level cleanup preserves shared fixtures until the command exits."""


def temporary_workspace(*, prefix="recoding-test-", suffix=None):
    return TestWorkspace(prefix=prefix, suffix=suffix)
