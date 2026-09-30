"""Opt-in tests against a disposable p4d; never connect to an existing server."""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from p4_history.errors import HistoryError
from p4_history.history import History
from p4_history.p4 import Perforce


@pytest.fixture
def live_p4(tmp_path, monkeypatch):
    executable = os.environ.get("P4_HISTORY_TEST_P4D")
    if not executable or not Path(executable).is_file() or not shutil.which("p4"):
        pytest.skip("Set P4_HISTORY_TEST_P4D to run disposable Perforce integration tests.")
    server_root, workspace = tmp_path / "server", tmp_path / "workspace"
    server_root.mkdir()
    workspace.mkdir()
    for name in list(os.environ):
        if name.upper().startswith("P4") and not name.startswith("P4_HISTORY_TEST_"):
            monkeypatch.delenv(name)
    for name, value in {
        "P4CONFIG": ".p4-history-isolated-test-config",
        "P4ENVIRO": str(tmp_path / "environment"),
        "P4TICKETS": str(tmp_path / "tickets"),
        "P4TRUST": str(tmp_path / "trust"),
    }.items():
        monkeypatch.setenv(name, value)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = f"127.0.0.1:{reservation.getsockname()[1]}"
    process = subprocess.Popen(
        [
            executable,
            "-r",
            str(server_root),
            "-p",
            port,
            "-J",
            "off",
            "-L",
            str(tmp_path / "server.log"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    def run(*args, data=None, check=True):
        return subprocess.run(
            ["p4", "-p", port, "-u", "tester", "-c", "test-client", *args],
            cwd=workspace,
            input=data,
            capture_output=True,
            timeout=15,
            check=check,
        )

    try:
        deadline = time.monotonic() + 10
        while run("info", check=False).returncode:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Disposable Perforce server failed to start.")
            time.sleep(0.05)
        spec = (
            f"Client: test-client\nOwner: tester\nRoot: {workspace}\n"
            "Options: noallwrite noclobber nocompress unlocked nomodtime rmdir\n"
            "LineEnd: local\nView:\n\t//depot/... //test-client/...\n"
        )
        run("client", "-i", data=spec.encode())
        one, two = workspace / "one.txt", workspace / "two.bin"
        one.write_bytes(b"original text\n")
        two.write_bytes(b"\x00\xff\r\n")
        run("add", "-t", "text", "one.txt")
        run("add", "-t", "binary", "two.bin")
        run("submit", "-d", "Synthetic test baseline")
        change_spec = (
            "Change: new\nClient: test-client\nUser: tester\n"
            "Status: pending\nDescription:\n\tSynthetic task\n"
        )
        created = run("change", "-i", data=change_spec.encode()).stdout.decode()
        change = re.search(r"Change (\d+) created", created).group(1)
        run("edit", "-c", change, "one.txt", "two.bin")
        adapter = Perforce(workspace, "test-client", port, "tester")
        yield History(adapter, tmp_path / "history"), change, run
    finally:
        run("admin", "stop", check=False)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_real_perforce_edit_commit_diff_restore(live_p4):
    history, change, run = live_p4
    baseline = history.begin(change)
    assert baseline["files"] == 2
    assert len(history.inspect()["pending_changes"]) == 1
    source = history.p4.root / "one.txt"
    binary = history.p4.root / "two.bin"
    original_source, original_binary = source.read_bytes(), binary.read_bytes()
    source.write_bytes(b"changed text\r\n")
    binary.write_bytes(b"\x00new\xff")
    milestone = history.commit(change, "A real Perforce milestone")
    assert history.diff(change, baseline["commit"], milestone["commit"])["files"]
    opened_before = run("-ztag", "opened").stdout
    plan = history.plan_restore(change, baseline["commit"])
    history.apply_restore(plan["id"])
    assert source.read_bytes() == original_source
    assert binary.read_bytes() == original_binary
    assert run("-ztag", "opened").stdout == opened_before
    assert not history.status(change)["dirty"]


def test_real_perforce_reopen_and_wrong_client_are_rejected(live_p4):
    history, change, run = live_p4
    baseline = history.begin(change)
    run("reopen", "-c", "default", "one.txt")
    with pytest.raises(HistoryError, match="membership"):
        history.plan_restore(change, baseline["commit"])
    wrong = Perforce(history.p4.root, "nonexistent-client", history.p4.port, "tester")
    with pytest.raises(HistoryError, match="mismatch|different client"):
        wrong.identity()


def test_real_perforce_empty_default_and_add_delete(live_p4):
    history, change, run = live_p4
    assert history.begin("default")["files"] == 0
    added = history.p4.root / "new file.txt"
    added.write_bytes(b"new")
    run("add", "-c", change, "new file.txt")
    run("revert", "-c", change, "two.bin")
    run("delete", "-c", change, "two.bin")
    assert history.begin(change)["files"] == 3
    manifest = history.store.manifest(change)
    assert next(f for f in manifest["files"] if f["path"] == "two.bin")["blob"] is None


def test_real_perforce_reserved_characters_in_paths(live_p4):
    history, change, run = live_p4
    filename = "special @#%.txt"
    special = history.p4.root / filename
    special.write_bytes(b"literal reserved characters")
    run("add", "-f", "-c", change, filename)
    history.begin(change)
    assert any(item["path"] == filename for item in history.store.manifest(change)["files"])


def test_real_create_and_prepare_task(live_p4):
    history, original, run = live_p4
    run("revert", "-c", original, "one.txt", "two.bin")
    change = history.create_change("A managed task")["change"]
    result = history.prepare(change, ["one.txt"])
    assert result["opened"] == 1
    assert not history.status(change)["dirty"]
    assert history.prepare(change, ["one.txt"])["opened"] == 0
    new = history.p4.root / "new @#%.txt"
    new.write_bytes(b"new bytes")
    result = history.prepare(change, [new.name])
    assert result["opened"] == 1
    assert new.read_bytes() == b"new bytes"
    assert next(f for f in history.p4.opened(change) if f["path"] == new.name)["type"] == "text"
    assert not history.status(change)["dirty"]


def test_real_prepare_refuses_other_changelist_before_any_open(live_p4):
    history, change, run = live_p4
    run("revert", "-c", change, "two.bin")
    other = history.create_change("Another task")["change"]
    with pytest.raises(HistoryError, match="another changelist"):
        history.prepare(other, ["two.bin", "one.txt"])
    assert history.p4.opened(other) == []


def test_real_changelist_restore_add_delete_and_round_trip(live_p4):
    history, change, run = live_p4
    baseline = history.begin(change)
    original = (history.p4.root / "one.txt").read_bytes()
    run("revert", "-c", change, "one.txt")
    run("delete", "-c", change, "one.txt")
    added = history.p4.root / "new.txt"
    added.write_bytes(b"new content")
    run("add", "-c", change, "new.txt")
    later = history.commit(change, "Replace file")
    plan = history.plan_restore(change, baseline["commit"], mode="changelist")
    history.apply_restore(plan["id"])
    assert (history.p4.root / "one.txt").read_bytes() == original
    assert not added.exists()
    assert {f["action"] for f in history.p4.opened(change)} == {"edit"}
    history.apply_restore(history.plan_restore(change, later["commit"], mode="changelist")["id"])
    assert not (history.p4.root / "one.txt").exists()
    assert added.read_bytes() == b"new content"
    assert not history.status(change)["dirty"]


def test_real_membership_restore_keeps_closed_edit_bytes(live_p4):
    history, change, run = live_p4
    run("revert", "-c", change, "two.bin")
    baseline = history.begin(change)
    run("edit", "-c", change, "two.bin")
    extra = history.p4.root / "two.bin"
    extra.write_bytes(b"unrelated work preserved")
    plan = history.plan_restore(change, baseline["commit"], mode="changelist")
    history.apply_restore(plan["id"])
    assert extra.read_bytes() == b"unrelated work preserved"
    assert [f["path"] for f in history.p4.opened(change)] == ["one.txt"]


def test_real_transition_recovery_preserves_newer_unopened_bytes(live_p4, monkeypatch):
    history, change, run = live_p4
    history.begin(change)
    extra = history.p4.root / "new.txt"
    extra.write_bytes(b"new")
    run("add", "-c", change, "new.txt")
    target = history.commit(change, "Add file")
    run("revert", "-k", "-c", change, "new.txt")
    extra.write_bytes(b"previously unopened work")
    plan = history.plan_restore(change, target["commit"], mode="changelist")
    write = history.files.write

    def interrupt(path, data):
        write(path, data)
        raise OSError("interrupted")

    monkeypatch.setattr(history.files, "write", interrupt)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    extra.write_bytes(b"new external work")
    monkeypatch.setattr(history.files, "write", write)
    with pytest.raises(HistoryError, match="Newer edits"):
        history.recover_restore()
    assert extra.read_bytes() == b"new external work"
    extra.write_bytes(b"new")
    history.recover_restore()
    assert extra.read_bytes() == b"previously unopened work"
    assert "new.txt" not in {f["path"] for f in history.p4.opened(change)}


def test_real_continue_task_keeps_ancestry(live_p4):
    history, change, run = live_p4
    baseline = history.begin(change)
    file = history.p4.root / "one.txt"
    file.write_bytes(b"ongoing work")
    destination = history.create_change("Continue task")["change"]
    result = history.continue_task(change, destination, move_files=True)
    assert result["source_history_retained"]
    assert history.p4.opened(change) == []
    assert len(history.p4.opened(destination)) == 2
    assert history.store.manifest(destination, baseline["commit"])["change"] == destination
    plan = history.plan_restore(destination, baseline["commit"])
    history.apply_restore(plan["id"])
    assert file.read_bytes() != b"ongoing work"


def test_real_branch_switch_restores_add_membership(live_p4):
    history, change, run = live_p4
    history.begin(change)
    history.branch(change, "experiment")
    new = history.p4.root / "main.txt"
    new.write_bytes(b"main only")
    run("add", "-c", change, "main.txt")
    history.commit(change, "Add on main")
    history.apply_restore(history.plan_switch(change, "experiment", "changelist")["id"])
    assert not new.exists()
    history.apply_restore(history.plan_switch(change, "main", "changelist")["id"])
    assert new.read_bytes() == b"main only"


def test_real_hook_prepares_existing_and_tracks_new_files(live_p4):
    from p4_history.hooks import run_hook

    history, change, run = live_p4
    run("revert", "-c", change, "one.txt")
    baseline = history.begin(change)
    event = {
        "cwd": str(history.p4.root),
        "toolName": "edit",
        "sessionId": "test-session",
        "toolArgs": {"path": "one.txt"},
    }
    run_hook(history, event, "pre")
    assert "one.txt" in {f["path"] for f in history.p4.opened(change)}
    new_event = {**event, "toolName": "create", "toolArgs": {"path": "new.txt"}}
    run_hook(history, new_event, "pre")
    new = history.p4.root / "new.txt"
    assert not new.exists()
    new.write_bytes(b"created by host")
    run_hook(history, new_event, "post")
    assert "new.txt" in {f["path"] for f in history.p4.opened(change)}
    history.apply_restore(history.plan_restore(change, baseline["commit"], mode="changelist")["id"])
    assert not new.exists()


def test_real_hook_cli_denies_unselected_task(live_p4):
    history, _, _ = live_p4
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "p4_history.cli",
            "hook",
            "--host",
            "claude-code",
            "--event",
            "pre",
            "--workspace",
            str(history.p4.root),
            "--client",
            "test-client",
            "--port",
            history.p4.port,
            "--user",
            "tester",
            "--state-dir",
            str(history.state.directory),
        ],
        input=json.dumps(
            {
                "cwd": str(history.p4.root),
                "tool_name": "Edit",
                "tool_input": {"file_path": "one.txt"},
            }
        ).encode(),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_real_transition_rejects_stale_unopened_bytes_before_mutations(live_p4):
    history, change, run = live_p4
    target = history.begin(change)
    run("revert", "-k", "-c", change, "one.txt")
    plan = history.plan_restore(change, target["commit"], mode="changelist")
    source = history.p4.root / "one.txt"
    source.chmod(0o600)
    source.write_bytes(b"changed after preview")
    before = run("-ztag", "opened").stdout
    with pytest.raises(HistoryError, match="differs|changed"):
        history.apply_restore(plan["id"])
    assert source.read_bytes() == b"changed after preview"
    assert run("-ztag", "opened").stdout == before
    assert not history.state.transition_journal.exists()


def test_real_transition_recovers_after_metadata_mutation(live_p4, monkeypatch):
    from p4_history.operations import PerforceOperations

    history, change, run = live_p4
    baseline = history.begin(change)
    run("revert", "-c", change, "one.txt")
    run("delete", "-c", change, "one.txt")
    current = history.commit(change, "Delete task file")
    plan = history.plan_restore(change, baseline["commit"], mode="changelist")
    close = PerforceOperations.close_file

    def interrupt(operations, selected, path):
        close(operations, selected, path)
        raise OSError("Lost connection after metadata update")

    monkeypatch.setattr(PerforceOperations, "close_file", interrupt)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    monkeypatch.setattr(PerforceOperations, "close_file", close)
    history.recover_restore()
    assert not (history.p4.root / "one.txt").exists()
    assert (
        history.store.manifest(change)["files"]
        == history.store.manifest(change, current["commit"])["files"]
    )


def test_real_transition_rejects_missing_recovery_operations(live_p4, monkeypatch):
    history, change, run = live_p4
    baseline = history.begin(change)
    new = history.p4.root / "new.txt"
    new.write_bytes(b"added")
    run("add", "-c", change, "new.txt")
    plan = history.plan_restore(change, baseline["commit"], mode="changelist")
    monkeypatch.setattr(
        history.files, "write", lambda *args: (_ for _ in ()).throw(OSError("stop"))
    )
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    journal = json.loads(history.state.transition_journal.read_bytes())
    journal["plan"]["files"] = []
    history.state.transition_journal.write_text(json.dumps(journal))
    with pytest.raises(HistoryError, match="historical snapshots"):
        history.recover_restore()
    assert new.read_bytes() == b"added"


def test_real_completed_transition_cleanup_is_offline_and_preserves_edits(live_p4, monkeypatch):
    history, change, run = live_p4
    baseline = history.begin(change)
    new = history.p4.root / "new.txt"
    new.write_bytes(b"added")
    run("add", "-c", change, "new.txt")
    plan = history.plan_restore(change, baseline["commit"], mode="changelist")
    unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == history.state.transition_journal:
            raise OSError("cleanup interrupted")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(HistoryError, match="interrupted"):
        history.apply_restore(plan["id"])
    new.write_bytes(b"later work")
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(history.p4, "identity", lambda: pytest.fail("must remain offline"))
    assert history.recover_restore()["cleanup_only"]
    assert new.read_bytes() == b"later work"


@pytest.mark.parametrize("runtime", ["source", "windows-bundle"])
def test_real_mcp_stdio_transport(live_p4, runtime):
    history, change, _ = live_p4
    executable = sys.executable
    prefix = ["-m", "p4_history.cli"]
    if runtime == "windows-bundle":
        executable = str(windows_bundle() / "p4-history.exe")
        prefix = []

    async def exercise():
        parameters = StdioServerParameters(
            command=executable,
            args=prefix
            + [
                "serve",
                "--workspace",
                str(history.p4.root),
                "--client",
                "test-client",
                "--port",
                history.p4.port,
                "--user",
                "tester",
                "--state-dir",
                str(history.state.directory),
            ],
            env=dict(os.environ),
        )
        async with Client(parameters) as client:
            result = await client.call_tool("p4_history_begin", {"change": change})
            assert not result.is_error
            assert result.structured_content["files"] == 2
            logged = await client.call_tool("p4_history_log", {"change": change})
            assert len(logged.structured_content["commits"]) == 1
            source = history.p4.root / "one.txt"
            original = source.read_bytes()
            source.write_bytes(b"A milestone through the packaged MCP server\r\n")
            committed = await client.call_tool(
                "p4_history_commit", {"change": change, "message": "Packaged milestone"}
            )
            assert not committed.is_error
            plan = await client.call_tool(
                "p4_history_plan_restore",
                {"change": change, "revision": result.structured_content["commit"]},
            )
            assert not plan.is_error
            restored = await client.call_tool(
                "p4_history_apply_restore", {"plan_id": plan.structured_content["id"]}
            )
            assert not restored.is_error
            assert source.read_bytes() == original

    asyncio.run(exercise())


def windows_bundle():
    directory = os.environ.get("P4_HISTORY_TEST_BUNDLE")
    if os.name != "nt" or not directory:
        pytest.skip("Set P4_HISTORY_TEST_BUNDLE to an extracted Windows bundle.")
    return Path(directory)


@pytest.mark.parametrize("host", ["copilot", "codex", "claude-code"])
@pytest.mark.parametrize("runtime", ["bundle", "source"])
def test_windows_setup_preserves_config_and_is_repeatable(live_p4, tmp_path, host, runtime):
    if os.name != "nt":
        pytest.skip("Windows setup requires PowerShell.")
    bundle = windows_bundle() if runtime == "bundle" else None
    environment = dict(os.environ)
    if directory := os.environ.get("P4_HISTORY_TEST_CLAUDE_BIN"):
        environment["PATH"] = directory + os.pathsep + environment["PATH"]
    host_command = "claude" if host == "claude-code" else host
    host_executable = shutil.which(host_command, path=environment["PATH"])
    if not host_executable:
        pytest.skip(f"{host_command} must be installed to test native MCP registration.")
    history, _, _ = live_p4
    instructions = {
        "AGENTS.md": b"\xef\xbb\xbf# Custom policy\r\nKeep existing rules.\r\n",
        "CLAUDE.md": b"# Custom Claude instructions\nRetain this guidance.\n",
    }
    for filename, contents in instructions.items():
        (history.p4.root / filename).write_bytes(contents)
    profile = tmp_path / "host profile"
    profile.mkdir()
    config = (
        profile
        / {"copilot": "mcp-config.json", "codex": "config.toml", "claude-code": ".claude.json"}[
            host
        ]
    )
    if host != "codex":
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "unrelated": {
                            "type": "local" if host == "copilot" else "stdio",
                            "command": "p4",
                            "args": ["-V"],
                            "tools": ["inspect"],
                        }
                    }
                }
            )
        )
    else:
        config.write_text('[mcp_servers.unrelated]\ncommand = "p4"\nargs = ["-V"]\n')
    environment[
        {"copilot": "COPILOT_HOME", "codex": "CODEX_HOME", "claude-code": "CLAUDE_CONFIG_DIR"}[host]
    ] = str(profile)
    environment["COPILOT_AUTO_UPDATE"] = "false"
    repository = Path(__file__).resolve().parents[1]
    if runtime == "source":
        checkout = tmp_path / "temporary checkout"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(repository), str(checkout)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        # Exercise the same inferred-checkout path used by the skill. Install code under
        # test may be uncommitted; the runtime payload itself is still exported from HEAD.
        shutil.copytree(
            repository / ".agents/skills/setup-p4-history",
            checkout / ".agents/skills/setup-p4-history",
            dirs_exist_ok=True,
        )
        script = checkout / ".agents/skills/setup-p4-history/scripts/install.ps1"
        runtime_arguments = []
    else:
        script = bundle / ".agents/skills/setup-p4-history/scripts/install.ps1"
        runtime_arguments = ["-BundlePath", str(bundle)]
    command = [
        "powershell.exe",
        "-NoProfile",
        "-File",
        str(script),
        "-Workspace",
        str(history.p4.root),
        "-Client",
        "test-client",
        "-Port",
        history.p4.port,
        "-User",
        "tester",
        "-InstallRoot",
        str(tmp_path / "installed runtime"),
        "-StateDirectory",
        str(history.state.directory),
    ] + runtime_arguments
    # Exercise the default Copilot target, including when setup is driven by another host.
    if host != "copilot":
        command += ["-HostClient", host]

    def install(*extra):
        return subprocess.run(
            command + list(extra), env=environment, capture_output=True, text=True, timeout=90
        )

    before = config.read_bytes()
    if runtime == "source" and host == "copilot":
        tracked = checkout / "src/p4_history/cli.py"
        committed = tracked.read_bytes()
        tracked.write_bytes(committed + b"\n# uncommitted runtime edit\n")
        dirty = install("-CheckOnly")
        assert dirty.returncode != 0
        assert "runtime source changes" in dirty.stderr
        assert config.read_bytes() == before
        assert not (tmp_path / "installed runtime").exists()
        tracked.write_bytes(committed)
    checked = install("-CheckOnly")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert config.read_bytes() == before
    assert (tmp_path / "installed runtime").exists() == (runtime == "source")
    first = install()
    assert first.returncode == 0, first.stdout + first.stderr
    installed_config = config.read_bytes()
    repeated = install()
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert config.read_bytes() == installed_config
    # Updating an installer-owned entry must use the native client's replacement path.
    environment["P4TICKETS"] = str(tmp_path / "new tickets")
    updated = install()
    assert updated.returncode == 0, updated.stdout + updated.stderr
    installed_config = config.read_bytes()
    parsed = (
        json.loads(installed_config)
        if host != "codex"
        else tomllib.loads(installed_config.decode("utf-8-sig"))
    )
    servers = parsed["mcpServers" if host != "codex" else "mcp_servers"]
    assert servers["unrelated"]["command"] == "p4"
    name = next(name for name in servers if name.startswith("p4-history-"))
    entry = servers[name]
    assert Path(entry["command"]).is_file()
    assert entry["args"][0] == "serve"
    assert history.p4.port in entry["args"]
    assert entry["env"]["P4TICKETS"] == environment["P4TICKETS"]
    assert "P4PASSWD" not in entry["env"]
    # If registration fails during update (after Copilot removes its old entry),
    # restore the complete previous client config, including unrelated servers.
    shim = tmp_path / "failing host"
    shim.mkdir()
    real_host = host_executable.replace("'", "''")
    (shim / f"{host_command}.ps1").write_text(
        "if ($args[0] -eq 'mcp' -and $args[1] -eq 'add') { exit 23 }\n"
        f"& '{real_host}' @args\nexit $LASTEXITCODE\n"
    )
    old_path = environment["PATH"]
    environment["PATH"] = str(shim) + os.pathsep + old_path
    environment["P4TICKETS"] = str(tmp_path / "another tickets")
    failed = install()
    assert failed.returncode != 0
    assert "restored" in failed.stderr
    assert config.read_bytes() == installed_config
    environment["PATH"] = old_path
    environment["P4TICKETS"] = str(tmp_path / "new tickets")
    # A client policy edited by the user must survive subsequent setup attempts.
    if host == "claude-code":
        entry["env"]["CUSTOM_P4_HISTORY_SETTING"] = "preserve-me"
        config.write_text(json.dumps(parsed))
    elif host == "copilot":
        entry["tools"] = ["p4_history_workspace"]
        config.write_text(json.dumps(parsed))
    else:
        config.write_text(
            config.read_text().replace(
                f"[mcp_servers.{name}]", f"[mcp_servers.{name}]\nenabled = false"
            )
        )
    customised = config.read_bytes()
    rejected = install()
    assert rejected.returncode != 0
    assert "customised" in rejected.stderr
    assert config.read_bytes() == customised
    assert any(path.read_bytes() == before for path in profile.glob("*.bak"))
    for filename, contents in instructions.items():
        assert (history.p4.root / filename).read_bytes() == contents
    if runtime == "source":
        # The installed command must not depend on the checkout used to set it up.
        moved = tmp_path / "moved checkout"
        assert checkout.resolve().is_relative_to(tmp_path.resolve())
        assert moved.resolve().is_relative_to(tmp_path.resolve())
        checkout.rename(moved)

        async def exercise_installed():
            params = StdioServerParameters(
                command=entry["command"], args=entry["args"], env=environment
            )
            async with Client(params) as client:
                result = await client.call_tool("p4_history_workspace")
                assert not result.is_error
                assert result.structured_content["workspace"]["client"] == "test-client"

        asyncio.run(exercise_installed())


def test_windows_setup_rejects_damaged_bundle_before_registration(live_p4, tmp_path):
    original = windows_bundle()
    bundle = tmp_path / "damaged bundle"
    shutil.copytree(original, bundle)
    with (bundle / "p4-history.exe").open("ab") as output:
        output.write(b"damage")
    history, _, _ = live_p4
    script = original / ".agents/skills/setup-p4-history/scripts/install.ps1"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(script),
            "-BundlePath",
            str(bundle),
            "-Workspace",
            str(history.p4.root),
            "-Client",
            "test-client",
            "-Port",
            history.p4.port,
            "-User",
            "tester",
            "-HostClient",
            "copilot",
            "-CheckOnly",
            "-StateDirectory",
            str(history.state.directory),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "hash mismatch" in result.stderr
