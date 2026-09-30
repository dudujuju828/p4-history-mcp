import marshal
import subprocess

import pytest

from p4_history.errors import HistoryError
from p4_history.p4 import Perforce


def test_adapter_pins_connection_and_does_not_use_a_shell(tmp_path, monkeypatch):
    commands = []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, marshal.dumps({b"code": b"stat", b"example": b"value"}), b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    adapter = Perforce(tmp_path, "chosen-client", "localhost:1666", "user")
    assert adapter.run("info") == [{"code": "stat", "example": "value"}]
    assert commands[0][0] == [
        "p4",
        "-G",
        "-c",
        "chosen-client",
        "-p",
        "localhost:1666",
        "-u",
        "user",
        "info",
    ]
    assert not commands[0][1].get("shell", False)


@pytest.mark.parametrize("generic,allowed", [(17, True), (6, False), (3, False)])
def test_only_structured_empty_result_is_ignored(tmp_path, monkeypatch, generic, allowed):
    output = marshal.dumps(
        {b"code": b"error", b"generic": generic, b"severity": 3, b"data": b"synthetic error"}
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, output, b"")
    )
    adapter = Perforce(tmp_path, "client")
    if allowed:
        assert adapter.run("opened", "-c", "default", allow_empty=True) == []
    else:
        with pytest.raises(HistoryError, match="synthetic error"):
            adapter.run("opened", "-c", "default", allow_empty=True)


def test_default_changelist_is_scoped_to_the_current_user(tmp_path, monkeypatch):
    adapter = Perforce(tmp_path, "client")
    calls = []
    monkeypatch.setattr(adapter, "identity", lambda: {"user": "selected-user"})

    def run(*args, **kwargs):
        calls.append(args)
        return [{"client": "client", "change": "default", "user": "another-user"}]

    monkeypatch.setattr(adapter, "run", run)
    with pytest.raises(HistoryError, match="outside the selected changelist"):
        adapter.opened("default")
    assert calls == [("opened", "-c", "default", "-u", "selected-user", "-m", "2001")]


@pytest.mark.parametrize(
    "output", [b"invalid marshal", marshal.dumps(None), marshal.dumps({b"code": b"unexpected"})]
)
def test_invalid_perforce_response_is_rejected(tmp_path, monkeypatch, output):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, output, b"")
    )
    with pytest.raises(HistoryError, match="Perforce returned"):
        Perforce(tmp_path, "client").run("info")


def test_file_limit_is_enforced_before_path_mapping(tmp_path, monkeypatch):
    adapter = Perforce(tmp_path, "client")
    monkeypatch.setattr(adapter, "identity", lambda: {"user": "tester"})
    calls = []

    def run(*args, **kwargs):
        calls.append(args[0])
        return [{}] * 2001

    monkeypatch.setattr(adapter, "run", run)
    with pytest.raises(HistoryError, match="2000-file"):
        adapter.opened("123")
    assert calls == ["opened"]
