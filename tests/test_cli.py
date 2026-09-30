import json
import subprocess
import sys

import pytest


def run_cli(history, operation, arguments):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "p4_history.cli",
            "call",
            "--operation",
            operation,
            "--arguments",
            json.dumps(arguments),
            "--workspace",
            str(history.p4.root),
            "--client",
            "example-client",
            "--state-dir",
            str(history.state.directory),
        ],
        capture_output=True,
        timeout=20,
    )


def test_cli_reads_history_offline(history):
    history.p4.add("one.txt")
    first = history.begin("123")
    result = run_cli(history, "log", {"change": "123"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["commits"][0]["commit"] == first["commit"]


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("_save", {}),
        ("log", {"change": "123", "limit": "not-an-integer"}),
        ("storage", {"compact": "false"}),
    ],
)
def test_cli_rejects_unlisted_operations_and_invalid_argument_types(history, operation, arguments):
    result = run_cli(history, operation, arguments)
    assert result.returncode == 1
    assert b"Traceback" not in result.stderr
    assert b"operation" in result.stderr.lower()
