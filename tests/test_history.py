import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from p4_history.errors import HistoryError
from p4_history.history import History


def test_commits_exact_bytes_and_deduplicates(history):
    payloads = {
        "line endings.txt": b"a\r\nb\n",
        "empty.txt": b"",
        "unicode-é.txt": "Hello 世界".encode(),
        "binary.bin": bytes(range(256)),
    }
    for path, payload in payloads.items():
        history.p4.add(path, payload)
    first = history.begin("123")
    assert first["created"] is True
    assert history.commit("123", "Same state")["created"] is False
    manifest = history.store.manifest("123")
    for item in manifest["files"]:
        assert history.store.read_blob(item["blob"]) == payloads[item["path"]]
    assert len(history.log("123")["commits"]) == 1
    assert history.status("123")["dirty"] is False
    assert history.store.git("remote").strip() == b""
    history.store.git("fsck", "--full")


def test_commit_captures_pending_bytes_without_modifying_workspace(history):
    path = history.p4.add("code.txt", b"human changes before AI\n")
    baseline = history.begin("123")
    path.write_bytes(b"AI changes\n")
    later = history.commit("123", "Handle empty input")
    assert later["commit"] != baseline["commit"]
    assert path.read_bytes() == b"AI changes\n"
    assert [c["message"] for c in history.log("123")["commits"]] == [
        "Handle empty input",
        "Checkpoint before editing",
    ]
    assert (
        "-human changes before AI"
        in history.diff("123", baseline["commit"], later["commit"])["files"][0]["patch"]
    )


def test_empty_baseline_and_newly_opened_files(history):
    history.begin("123")
    history.p4.add("new.txt", b"new", action="add")
    assert history.status("123")["dirty"]
    result = history.begin("123")
    assert result["created"] and result["files"] == 1


def test_changelist_histories_are_separate(history):
    path = history.p4.add("file.txt")
    first = history.begin("123")
    path.write_bytes(b"another task")
    second = history.begin("456")
    assert first["commit"] != second["commit"]
    assert history.store.head("123") == first["commit"]
    with pytest.raises(HistoryError, match="does not belong"):
        history.store.manifest("123", second["commit"])


def test_restore_saves_dirty_work_and_appends_history(history):
    path = history.p4.add("file.txt", b"original\r\n")
    baseline = history.begin("123")
    path.write_bytes(b"unsaved in history\x00\xff")
    plan = history.plan_restore("123", baseline["commit"])
    assert path.read_bytes() == b"unsaved in history\x00\xff"
    result = history.apply_restore(plan["id"])
    assert path.read_bytes() == b"original\r\n"
    assert len(history.log("123")["commits"]) == 3
    backup = history.store.manifest("123", result["safety_commit"])
    assert history.store.read_blob(backup["files"][0]["blob"]) == b"unsaved in history\x00\xff"
    assert not history.status("123")["dirty"]
    assert not history.state.journal.exists()
    history.store.git("fsck", "--full")


def test_restore_keeps_current_description(history):
    path = history.p4.add("file.txt")
    baseline = history.begin("123")
    history.p4.description = "New description"
    path.write_bytes(b"changed")
    plan = history.plan_restore("123", baseline["commit"])
    history.apply_restore(plan["id"])
    assert history.store.manifest("123")["description"] == "New description"


@pytest.mark.parametrize("mutation", ["bytes", "history", "description", "mapping"])
def test_stale_plan_never_writes(history, mutation):
    path = history.p4.add("file.txt")
    baseline = history.begin("123")
    path.write_bytes(b"second")
    plan = history.plan_restore("123", baseline["commit"])
    if mutation == "bytes":
        path.write_bytes(b"third")
    elif mutation == "history":
        history.commit("123", "Another agent committed")
    elif mutation == "description":
        history.p4.description = "Updated by someone else"
    else:
        history.p4.workspace["mapping"]["LineEnd"] = "unix"
    expected = path.read_bytes()
    with pytest.raises(HistoryError, match="changed since the preview"):
        history.apply_restore(plan["id"])
    assert path.read_bytes() == expected
    assert not history.state.journal.exists()


@pytest.mark.parametrize(
    "field,value",
    [("revision", "2"), ("type", "binary"), ("action", "add"), ("path", "renamed.txt")],
)
def test_restore_rejects_changed_perforce_topology(history, field, value):
    path = history.p4.add("file.txt")
    baseline = history.begin("123")
    history.p4.files[0][field] = value
    if field == "path":
        path.rename(path.parent / value)
    with pytest.raises(HistoryError, match="membership, actions"):
        history.plan_restore("123", baseline["commit"])


def test_restore_rejects_changed_file_membership(history):
    history.p4.add("file.txt")
    baseline = history.begin("123")
    history.p4.add("new.txt", b"new", action="add")
    with pytest.raises(HistoryError, match="membership, actions"):
        history.plan_restore("123", baseline["commit"])


@pytest.mark.parametrize("action", ["integrate", "branch", "move/add"])
def test_complex_actions_can_be_saved_but_not_restored(history, action):
    history.p4.add("file.txt", action=action)
    baseline = history.begin("123")
    with pytest.raises(HistoryError, match="integrations is not supported"):
        history.plan_restore("123", baseline["commit"])


def test_deleted_file_and_recreated_bytes_are_preserved(history):
    path = history.p4.add("removed.txt", None, action="delete")
    baseline = history.begin("123")
    path.write_bytes(b"recreated contents")
    plan = history.plan_restore("123", baseline["commit"])
    assert plan["files"] == [{"path": "removed.txt", "operation": "remove"}]
    result = history.apply_restore(plan["id"])
    assert not path.exists()
    reverse = history.plan_restore("123", result["safety_commit"])
    history.apply_restore(reverse["id"])
    assert path.read_bytes() == b"recreated contents"


def test_missing_opened_file_fails_without_committing(history):
    path = history.p4.add("file.txt")
    path.unlink()
    with pytest.raises(HistoryError, match="Opened file is missing"):
        history.begin("123")
    assert history.store.head("123") is None


def test_file_changes_during_capture_abort_without_advancing(history, monkeypatch):
    path = history.p4.add("file.txt")
    baseline = history.begin("123")
    original = history.files.read
    calls = 0

    def read(relative):
        nonlocal calls
        data = original(relative)
        calls += 1
        if calls == 1:
            path.write_bytes(b"concurrent writer")
        return data

    monkeypatch.setattr(history.files, "read", read)
    with pytest.raises(HistoryError, match="changed during capture"):
        history.commit("123", "Should not appear")
    assert history.store.head("123") == baseline["commit"]


def test_interrupted_restore_recovers_after_server_restart(history, monkeypatch):
    one = history.p4.add("one.txt", b"one original")
    two = history.p4.add("two.txt", b"two original")
    baseline = history.begin("123")
    one.write_bytes(b"one latest")
    two.write_bytes(b"two latest")
    plan = history.plan_restore("123", baseline["commit"])
    original = history.files.write

    def fail_second(path, data):
        if path == "two.txt":
            raise OSError("Simulated process interruption")
        original(path, data)

    monkeypatch.setattr(history.files, "write", fail_second)
    with pytest.raises(HistoryError, match="Restore interrupted"):
        history.apply_restore(plan["id"])
    assert one.read_bytes() == b"one original"
    assert two.read_bytes() == b"two latest"
    assert history.state.journal.exists()
    with pytest.raises(HistoryError, match="interrupted restore"):
        history.commit("123", "Blocked")
    restarted = History(history.p4, history.state.directory)
    assert restarted.recover_restore()["recovered"]
    assert one.read_bytes() == b"one latest"
    assert two.read_bytes() == b"two latest"
    assert not restarted.state.journal.exists()


def test_recovery_preserves_newer_external_edits(history, monkeypatch):
    path = history.p4.add("file.txt", b"original")
    baseline = history.begin("123")
    path.write_bytes(b"before restore")
    plan = history.plan_restore("123", baseline["commit"])

    def external_writer(relative, data):
        path.write_bytes(b"external changes")
        raise OSError("external writer")

    monkeypatch.setattr(history.files, "write", external_writer)
    with pytest.raises(HistoryError, match="Restore interrupted"):
        history.apply_restore(plan["id"])
    with pytest.raises(HistoryError, match="preserved a newer edit"):
        history.recover_restore()
    assert path.read_bytes() == b"external changes"
    assert history.state.journal.exists()


def test_concurrent_servers_keep_one_linear_history(history):
    history.p4.add("file.txt")
    peer = History(history.p4, history.state.directory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(service.commit, "123", "concurrent") for service in (history, peer)]
        result = [call.result() for call in calls]
    assert sum(item["created"] for item in result) == 1
    assert len(history.log("123")["commits"]) == 1


def test_storage_inside_workspace_is_rejected(history):
    with pytest.raises(HistoryError, match="outside the Perforce workspace"):
        History(history.p4, history.p4.root / "state")


@pytest.mark.parametrize("field", ["server", "client", "user", "server_id"])
def test_history_is_bound_to_one_workspace_connection(history, field):
    history.p4.add("file.txt")
    baseline = history.begin("123")
    history.p4.workspace[field] = "different"
    with pytest.raises(HistoryError, match="another workspace connection"):
        history.commit("123", "Must not mix histories")
    assert history.store.head("123") == baseline["commit"]


def test_binary_diff_and_unopened_files(history):
    path = history.p4.add("file.bin", b"a\x00b")
    history.begin("123")
    (history.p4.root / "not-opened.txt").write_text("not captured")
    path.write_bytes(b"a\x00c")
    delta = history.diff("123")["files"]
    assert len(delta) == 1 and "summary" in delta[0]
    assert "patch" not in delta[0]


def test_references_and_plan_ids_do_not_accept_command_options(history):
    history.begin("123")
    for bad in ["--all", "HEAD~1", "123;whoami", "../123"]:
        with pytest.raises(HistoryError):
            history.store.resolve("123", bad)
        with pytest.raises(HistoryError):
            history.begin(bad)
        with pytest.raises(HistoryError):
            history.apply_restore(bad)


def test_snapshot_limits_fail_without_publishing_history(history, monkeypatch):
    history.p4.add("large.bin", b"12345")
    monkeypatch.setattr("p4_history.workspace.MAX_FILE", 4)
    with pytest.raises(HistoryError, match="limit"):
        history.begin("123")
    assert not history.log("123")["commits"]


def test_git_objects_remain_reachable_after_garbage_collection(history):
    path = history.p4.add("file.txt", b"one")
    first = history.begin("123")
    path.write_bytes(b"two")
    history.commit("123", "second")
    history.store.git("gc")
    first_manifest = history.store.manifest("123", first["commit"])
    assert history.store.read_blob(first_manifest["files"][0]["blob"]) == b"one"
    assert (
        json.loads(history.store.git("show", f"{first['commit']}:manifest.json"))["change"] == "123"
    )
