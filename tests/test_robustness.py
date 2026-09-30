import json
import marshal
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from p4_history.errors import HistoryError
from p4_history.history import History
from p4_history.p4 import Perforce


def test_partial_perforce_failure_is_not_a_success(tmp_path, monkeypatch):
    output = marshal.dumps({b"code": b"stat", b"example": b"partial"})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, output, b"connection interrupted"),
    )
    with pytest.raises(HistoryError, match="Perforce failed"):
        Perforce(tmp_path, "client").run("opened", allow_empty=True)


def test_corrupt_blob_is_rejected_before_restore_writes(history):
    path = history.p4.add("file.txt", b"original")
    baseline = history.begin("123")
    oid = history.store.manifest("123")["files"][0]["blob"]
    path.write_bytes(b"latest")
    plan = history.plan_restore("123", baseline["commit"])
    loose = history.store.repo / "objects" / oid[:2] / oid[2:]
    loose.chmod(0o600)
    loose.write_bytes(zlib.compress(b"blob 8\0tampered"))
    with pytest.raises(HistoryError, match="integrity|corrupt|mismatch"):
        history.apply_restore(plan["id"])
    assert path.read_bytes() == b"latest"
    assert not history.state.journal.exists()


def test_commit_cannot_claim_to_repair_a_corrupt_existing_blob(history):
    history.p4.add("file.txt", b"original")
    baseline = history.begin("123")
    oid = history.store.manifest("123")["files"][0]["blob"]
    loose = history.store.repo / "objects" / oid[:2] / oid[2:]
    loose.chmod(0o600)
    loose.write_bytes(zlib.compress(b"blob 8\0tampered"))
    with pytest.raises(HistoryError, match="integrity|corrupt|mismatch"):
        history.commit("123", "Do not claim success")
    assert history.store.head("123") == baseline["commit"]


def test_incomplete_git_repository_is_not_reinitialized(history):
    history.begin("123")
    (history.store.repo / "HEAD").unlink()
    with pytest.raises(HistoryError, match="incomplete|corrupt"):
        History(history.p4, history.state.directory)


def test_malformed_plan_is_an_actionable_error(history):
    history.p4.add("file.txt")
    history.begin("123")
    plan = history.plan_restore("123", "HEAD")
    (history.state.directory / f"plan-{plan['id']}.json").write_text('{"change": []}')
    with pytest.raises(HistoryError, match="Invalid|invalid|Corrupt|corrupt"):
        history.apply_restore(plan["id"])


def test_recovery_verifies_files_already_written(history, monkeypatch):
    one = history.p4.add("one.txt", b"one original")
    two = history.p4.add("two.txt", b"two original")
    baseline = history.begin("123")
    one.write_bytes(b"one latest")
    two.write_bytes(b"two latest")
    plan = history.plan_restore("123", baseline["commit"])
    original = history.files.write

    def interrupted(path, data):
        original(path, data)
        if path == "two.txt":
            raise OSError("interrupted")

    monkeypatch.setattr(history.files, "write", interrupted)
    with pytest.raises(HistoryError):
        history.apply_restore(plan["id"])

    def late_edit(path, data):
        original(path, data)
        if path == "two.txt":
            one.write_bytes(b"later human edit")

    monkeypatch.setattr(history.files, "write", late_edit)
    with pytest.raises(HistoryError, match="changed during recovery"):
        history.recover_restore()
    assert one.read_bytes() == b"later human edit"
    assert history.state.journal.exists()


def test_recovery_rejects_incomplete_journal_file_list(history, monkeypatch):
    path = history.p4.add("file.txt", b"original")
    baseline = history.begin("123")
    path.write_bytes(b"latest")
    plan = history.plan_restore("123", baseline["commit"])
    monkeypatch.setattr(
        history.files, "write", lambda *args: (_ for _ in ()).throw(OSError("stop"))
    )
    with pytest.raises(HistoryError):
        history.apply_restore(plan["id"])
    journal = json.loads(history.state.journal.read_bytes())
    journal["files"] = []
    history.state.journal.write_text(json.dumps(journal))
    with pytest.raises(HistoryError, match="journal|Journal"):
        history.recover_restore()
    assert history.state.journal.exists()


def test_completed_restore_cleanup_does_not_undo_work(history, monkeypatch):
    path = history.p4.add("file.txt", b"original")
    baseline = history.begin("123")
    path.write_bytes(b"latest")
    plan = history.plan_restore("123", baseline["commit"])
    unlink = Path.unlink

    def fail_journal_cleanup(target, *args, **kwargs):
        if target == history.state.journal:
            raise OSError("Simulated interruption after completion")
        return unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_cleanup)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    assert path.read_bytes() == b"original"
    assert history.state.load_journal()["phase"] == "complete"
    path.write_bytes(b"new human work after completion")
    monkeypatch.setattr(Path, "unlink", unlink)

    def offline():
        pytest.fail("Completed journal cleanup must not require a live or pending changelist.")

    monkeypatch.setattr(history.p4, "identity", offline)
    result = history.recover_restore()
    assert result["cleanup_only"]
    assert path.read_bytes() == b"new human work after completion"
    assert not history.state.journal.exists()


def test_actual_process_exit_during_restore_is_recoverable(history):
    one = history.p4.add("one.txt", b"one original")
    two = history.p4.add("two.txt", b"two original")
    baseline = history.begin("123")
    one.write_bytes(b"one latest")
    two.write_bytes(b"two latest")
    plan = history.plan_restore("123", baseline["commit"])
    script = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from conftest import FakePerforce
from p4_history.history import History
p4 = FakePerforce(Path(sys.argv[2]))
p4.files = json.loads(sys.argv[3])
history = History(p4, Path(sys.argv[4]))
original = history.files.write
def crash(relative, data):
    original(relative, data)
    os._exit(73)
history.files.write = crash
history.apply_restore(sys.argv[5])
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).parent),
            str(history.p4.root),
            json.dumps(history.p4.files),
            str(history.state.directory),
            plan["id"],
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    assert one.read_bytes() == b"one original"
    assert two.read_bytes() == b"two latest"
    restarted = History(history.p4, history.state.directory)
    assert restarted.recover_restore()["recovered"]
    assert one.read_bytes() == b"one latest"
    assert two.read_bytes() == b"two latest"


def test_readonly_file_is_found_before_any_restore_write(history):
    one = history.p4.add("one.txt", b"one original")
    two = history.p4.add("two.txt", b"two original")
    baseline = history.begin("123")
    one.write_bytes(b"one latest")
    two.write_bytes(b"two latest")
    plan = history.plan_restore("123", baseline["commit"])
    two.chmod(0o444)
    try:
        with pytest.raises(HistoryError, match="read-only"):
            history.apply_restore(plan["id"])
        assert one.read_bytes() == b"one latest"
        assert not history.state.journal.exists()
        assert history.store.head("123") == baseline["commit"]
    finally:
        two.chmod(0o644)


def test_missing_binding_cannot_attach_a_different_workspace(history):
    history.begin("123")
    history.state.binding.unlink()
    with pytest.raises(HistoryError, match="binding is missing"):
        History(history.p4, history.state.directory)


def test_invalid_commit_request_does_not_create_binding_or_history(history):
    history.p4.add("file.txt")
    with pytest.raises(HistoryError, match="commit message"):
        history.commit("123", " ")
    assert not history.state.binding.exists()
    assert not history.store.head("123")


def test_diff_preserves_missing_final_newline_information(history):
    path = history.p4.add("file.txt", b"original")
    history.begin("123")
    path.write_bytes(b"changed")
    patch = history.diff("123")["files"][0]["patch"]
    assert "-original\n\\ No newline at end of file\n" in patch
    assert "+changed\n\\ No newline at end of file\n" in patch


def test_diff_does_not_load_large_historical_blobs(history, monkeypatch):
    path = history.p4.add("file.txt", b"a" * 70_000)
    baseline = history.begin("123")
    path.write_bytes(b"b" * 70_000)
    head = history.commit("123", "next")
    read = history.store.read_blob

    def metadata_only(oid, **kwargs):
        if not kwargs:
            pytest.fail("Large file blob should not be loaded to render a summary.")
        return read(oid, **kwargs)

    monkeypatch.setattr(history.store, "read_blob", metadata_only)
    result = history.diff("123", baseline["commit"], head["commit"])
    assert "Large file" in result["files"][0]["summary"]
