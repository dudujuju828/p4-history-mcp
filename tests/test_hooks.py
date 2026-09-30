import json

import pytest

from p4_history.errors import HistoryError
from p4_history.hook_install import install_hooks, uninstall_hooks
from p4_history.hooks import denial, run_hook, select_task


def test_hook_blocks_known_edits_without_a_selected_task(history):
    history.p4.add("one.txt")
    with pytest.raises(HistoryError, match="Select a task"):
        run_hook(
            history,
            {
                "cwd": str(history.p4.root),
                "toolName": "edit",
                "toolArgs": json.dumps({"path": "one.txt"}),
            },
            "pre",
        )


def test_hooks_checkpoint_shell_and_handoff_without_executing_input(history):
    path = history.p4.add("one.txt", b"before")
    history.begin("123")
    select_task(history, "123")
    path.write_bytes(b"after")
    run_hook(
        history,
        {
            "cwd": str(history.p4.root),
            "tool_name": "Bash",
            "tool_input": {"command": "this command must never run"},
        },
        "pre",
    )
    assert not history.status("123")["dirty"]
    path.write_bytes(b"final")
    run_hook(history, {"cwd": str(history.p4.root)}, "stop")
    assert not history.status("123")["dirty"]


def test_hooks_skip_other_workspaces_and_recovery_tools(history, tmp_path):
    history.p4.add("one.txt")
    run_hook(
        history,
        {
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "elsewhere.txt")},
        },
        "pre",
    )
    run_hook(history, {"tool_name": "mcp__p4-history__p4_history_recover_restore"}, "pre")
    assert history.store.head("123") is None


def test_disabling_hooks_is_explicit_and_persistent(history):
    select_task(history, None)
    run_hook(
        history,
        {"cwd": str(history.p4.root), "toolName": "create", "toolArgs": {"path": "new.txt"}},
        "pre",
    )
    assert history.store.head("123") is None


def test_missing_old_workspace_does_not_block_global_hooks_in_other_projects(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "p4_history.cli",
            "hook",
            "--host",
            "copilot",
            "--event",
            "pre",
            "--workspace",
            str(tmp_path / "removed-workspace"),
            "--client",
            "removed-client",
        ],
        input=json.dumps(
            {"cwd": str(tmp_path), "toolName": "edit", "toolArgs": {"path": "unrelated.txt"}}
        ).encode(),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


@pytest.mark.parametrize("host", ["copilot", "claude-code"])
def test_hook_registration_preserves_custom_config_and_refuses_customised_owned_entries(
    history, tmp_path, monkeypatch, host
):
    profile = tmp_path / host
    profile.mkdir()
    monkeypatch.setenv("COPILOT_HOME", str(profile))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    custom = {"type": "command", "command": "custom-hook"}
    if host == "claude-code":
        (profile / "settings.json").write_text(
            json.dumps(
                {"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [{"hooks": [custom]}]}}
            )
        )
    program = ["C:/Tools/p4-history.exe"]
    connection = ["--workspace", str(history.p4.root), "--client", "test"]
    first = install_hooks(history, host, program, connection)
    assert first["changed"]
    from pathlib import Path

    config = Path(first["config"])
    document = json.loads(config.read_bytes())
    if host == "claude-code":
        assert document["permissions"] == {"allow": ["Read"]}
        assert document["hooks"]["PreToolUse"][0] == {"hooks": [custom]}
    assert not install_hooks(history, host, program, connection)["changed"]
    updated = install_hooks(history, host, ["C:/Tools/new-runtime.exe"], connection)
    assert updated["changed"]
    document = json.loads(config.read_bytes())
    event = "preToolUse" if host == "copilot" else "PreToolUse"
    document["hooks"][event][-1]["customised"] = True
    config.write_text(json.dumps(document))
    before = config.read_bytes()
    with pytest.raises(HistoryError, match="customised"):
        install_hooks(history, host, program, connection)
    assert config.read_bytes() == before


def test_denials_do_not_grant_any_other_host_permissions():
    assert denial("copilot", "failure")["permissionDecision"] == "deny"
    assert denial("claude-code", "failure")["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("host", ["copilot", "claude-code"])
def test_uninstall_hooks_preserves_other_policies_and_history(history, tmp_path, monkeypatch, host):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("COPILOT_HOME", str(profile))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    if host == "claude-code":
        (profile / "settings.json").write_text('{"permissions":{"deny":["Bash"]}}')
    history.p4.add("one.txt")
    baseline = history.begin("123")
    install_hooks(history, host, ["test-program"], [])
    result = uninstall_hooks(history.p4.root, history.state.directory, host)
    assert result["removed"]
    assert history.store.head("123") == baseline["commit"]
    if host == "claude-code":
        assert json.loads((profile / "settings.json").read_bytes())["permissions"] == {
            "deny": ["Bash"]
        }
