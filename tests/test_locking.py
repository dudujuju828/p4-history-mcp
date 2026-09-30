import subprocess
import sys

import pytest

from p4_history.errors import HistoryError


def test_another_process_cannot_bypass_the_store_lock(history):
    script = """
import sys
from filelock import FileLock
with FileLock(sys.argv[1], timeout=5):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(history.state.directory / "workspace.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout.readline().strip() == "locked"
        history.state.lock.timeout = 0.05
        with pytest.raises(HistoryError, match="Another operation"):
            history.commit("123", "must wait")
        assert history.store.head("123") is None
    finally:
        worker.communicate("release\n", timeout=10)
    assert history.commit("123", "lock released")["created"]
