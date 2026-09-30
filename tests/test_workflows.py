import pytest

from p4_history.errors import HistoryError


def test_selective_restore_preserves_unrelated_edits_and_new_members(history):
    one = history.p4.add("one.txt", b"original one")
    two = history.p4.add("two.txt", b"original two")
    baseline = history.begin("123")
    one.write_bytes(b"new one")
    two.write_bytes(b"new two")
    extra = history.p4.add("extra.txt", b"new file", action="add")
    plan = history.plan_restore("123", baseline["commit"], ["one.txt"])
    history.apply_restore(plan["id"])
    assert one.read_bytes() == b"original one"
    assert two.read_bytes() == b"new two"
    assert extra.read_bytes() == b"new file"


def test_selective_recovery_preserves_unselected_changes(history, monkeypatch):
    one = history.p4.add("one.txt", b"one")
    two = history.p4.add("two.txt", b"two")
    first = history.begin("123")
    one.write_bytes(b"new one")
    two.write_bytes(b"new two")
    plan = history.plan_restore("123", first["commit"], ["one.txt"])
    write = history.files.write

    def interrupt(path, data):
        write(path, data)
        raise OSError("interrupted")

    monkeypatch.setattr(history.files, "write", interrupt)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    two.write_bytes(b"later unrelated work")
    monkeypatch.setattr(history.files, "write", write)
    history.recover_restore()
    assert one.read_bytes() == b"new one"
    assert two.read_bytes() == b"later unrelated work"


def test_labels_search_pagination_and_compaction(history):
    file = history.p4.add("one.txt", b"one")
    first = history.begin("123")
    history.label("123", "before-refactor")
    file.write_bytes(b"two")
    history.commit("123", "Refactor parser")
    file.write_bytes(b"three")
    history.commit("123", "Validate parser")
    assert history.store.resolve("123", "HEAD~2") == first["commit"]
    assert history.store.resolve("123", "@before-refactor") == first["commit"]
    assert history.log("123", query="REFACTOR")["commits"][0]["message"] == "Refactor parser"
    assert history.log("123", limit=1, offset=2)["commits"][0]["commit"] == first["commit"]
    with pytest.raises(HistoryError, match="already protects"):
        history.label("123", "before-refactor")
    assert history.storage(True)["packed_objects"] > 0
    assert history.store.manifest("123", "@before-refactor")["files"][0]["size"] == 3


@pytest.mark.parametrize("paths", [[], ["one.txt", "one.txt"], ["../one.txt"], ["missing.txt"]])
def test_invalid_selective_restore_never_writes(history, paths):
    file = history.p4.add("one.txt", b"one")
    history.begin("123")
    with pytest.raises(HistoryError):
        history.plan_restore("123", "HEAD", paths)
    assert file.read_bytes() == b"one"


def test_branch_switch_preserves_dirty_work_and_independent_heads(history):
    file = history.p4.add("one.txt", b"base")
    baseline = history.begin("123")
    history.branch("123", "experiment")
    file.write_bytes(b"main work, not yet committed")
    result = history.apply_restore(history.plan_switch("123", "experiment")["id"])
    assert file.read_bytes() == b"base"
    assert history.store.head("123") == baseline["commit"]
    assert history.store.branch_tip("123", "main") == result["safety_commit"]
    file.write_bytes(b"alternative")
    alternative = history.commit("123", "Try another approach")
    history.apply_restore(history.plan_switch("123", "main")["id"])
    assert file.read_bytes() == b"main work, not yet committed"
    assert history.store.branch_tip("123", "experiment") == alternative["commit"]
    history.apply_restore(history.plan_switch("123", "experiment")["id"])
    assert file.read_bytes() == b"alternative"


def test_branch_merge_and_cherry_pick_use_three_way_content(history):
    lines = [f"line {n}\n" for n in range(30)]
    file = history.p4.add("one.txt", "".join(lines).encode())
    history.begin("123")
    history.branch("123", "experiment")
    ours = lines.copy()
    ours[1] = "main improvement\n"
    file.write_bytes("".join(ours).encode())
    main = history.commit("123", "Improve first part")
    history.apply_restore(history.plan_switch("123", "experiment")["id"])
    theirs = lines.copy()
    theirs[28] = "experiment improvement\n"
    file.write_bytes("".join(theirs).encode())
    history.commit("123", "Improve last part")
    planned = history.plan_merge("123", "branch:main", "merge")
    assert planned["conflicts"] == []
    assert b"main improvement" not in file.read_bytes()
    history.apply_restore(planned["plan"]["id"])
    assert b"main improvement" in file.read_bytes()
    assert b"experiment improvement" in file.read_bytes()
    assert (
        history.store.git("merge-base", history.store.head("123"), main["commit"]).decode().strip()
        == main["commit"]
    )
    planned = history.plan_merge("123", main["commit"], "cherry-pick")
    assert planned["plan"]["files"] == []


def test_merge_conflicts_never_write_or_publish_history(history):
    file = history.p4.add("one.txt", b"original\n")
    history.begin("123")
    file.write_bytes(b"incoming\n")
    incoming = history.commit("123", "Incoming change")
    file.write_bytes(b"local\n")
    head = history.store.head("123")
    result = history.plan_merge("123", incoming["commit"])
    assert result["plan"] is None and result["conflicts"]
    assert file.read_bytes() == b"local\n"
    assert history.store.head("123") == head


def test_interrupted_branch_switch_restores_source_branch(history, monkeypatch):
    file = history.p4.add("one.txt", b"base")
    history.begin("123")
    history.branch("123", "experiment")
    file.write_bytes(b"main work")
    plan = history.plan_switch("123", "experiment")
    save = history.restore.save

    def interrupt(change, snapshot, contents, message):
        if message.startswith("Restore"):
            raise OSError("Interrupted after switching branch")
        return save(change, snapshot, contents, message)

    monkeypatch.setattr(history.restore, "save", interrupt)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    assert history.store.active_branch("123") == "experiment"
    monkeypatch.setattr(history.restore, "save", save)
    history.recover_restore()
    assert file.read_bytes() == b"main work"
    assert history.store.active_branch("123") == "main"
