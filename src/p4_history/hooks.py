"""Scoped host checkpoints. Known file edits are prepared; arbitrary shell writes need scope."""

import json
from pathlib import Path

from pydantic import Field, field_validator

from .errors import HistoryError
from .models import Change, Record, validate
from .operations import PerforceOperations
from .paths import validate_relative, workspace_path
from .prepare import Preparation
from .state import atomic_json


class NewFileIntent(Record):
    path: str
    change: Change
    session: str = Field(max_length=256)

    _path = field_validator("path")(validate_relative)


class HookState(Record):
    change: Change | None = None
    enabled: bool = True
    pending: list[NewFileIntent] = Field(default_factory=list, max_length=2000)


def load_state(history) -> dict:
    path = history.state.directory / "hook-state.json"
    return history.state.read(path, HookState) if path.exists() else HookState().model_dump()


def save_state(history, state: dict) -> None:
    atomic_json(history.state.directory / "hook-state.json", validate(HookState, state))


def select_task(history, change: str | None) -> dict:
    state = load_state(history)
    state.update(change=change, enabled=change is not None)
    save_state(history, state)
    return {"change": change, "automatic_checkpoints": state["enabled"]}


def _relative(history, raw: str, cwd: str) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    # Do not resolve away symlinks or '..' before validating the lexical path.
    try:
        relative = path.relative_to(history.p4.root).as_posix()
    except ValueError:
        return None
    workspace_path(history.p4.root, relative)
    return relative


def relevant(root: Path, payload: dict, event: str) -> bool:
    """Skip other workspaces before constructing a client, even if this root was removed."""
    if not isinstance(payload, dict):
        raise HistoryError("Hook input must be an object.")
    cwd = payload.get("cwd", "")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise HistoryError("Hook input must include an absolute working directory.")
    if Path(cwd).is_relative_to(root):
        return True
    arguments = payload.get("tool_input", payload.get("toolArgs", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return False
    if not isinstance(arguments, dict):
        return False
    raw = arguments.get("file_path", arguments.get("path"))
    return isinstance(raw, str) and Path(raw).is_absolute() and Path(raw).is_relative_to(root)


def run_hook(history, payload: dict, event: str) -> dict:
    if not isinstance(payload, dict):
        raise HistoryError("Hook input must be a JSON object.")
    tool = payload.get("tool_name", payload.get("toolName", ""))
    if not isinstance(tool, str):
        raise HistoryError("Invalid hook tool name.")
    if "p4_history_" in tool:
        return {}  # Never intercept this server's own tools, including recovery.
    arguments = payload.get("tool_input", payload.get("toolArgs", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError as exc:
            raise HistoryError("Invalid hook tool arguments.") from exc
    if not isinstance(arguments, dict):
        raise HistoryError("Hook tool arguments must be an object.")
    cwd = payload.get("cwd", "")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise HistoryError("Hook input must include an absolute working directory.")
    session = payload.get("session_id", payload.get("sessionId", ""))
    if not isinstance(session, str) or len(session) > 256:
        raise HistoryError("Invalid hook session ID.")
    edit = tool.lower() in {"edit", "write", "create", "multiedit", "str_replace_editor"}
    shell = tool.lower() in {"bash", "powershell", "shell", "exec_command"}
    raw = arguments.get("file_path", arguments.get("path")) if edit else None
    relative = _relative(history, raw, cwd) if isinstance(raw, str) else None
    in_workspace = Path(cwd).is_relative_to(history.p4.root)
    if edit and raw is None and in_workspace:
        raise HistoryError(
            "Cannot identify the edited file; prepare its explicit path through MCP."
        )
    if relative is None and (not in_workspace or (not shell and event != "stop")):
        return {}
    with history.state.locked():
        state = load_state(history)
        if not state["enabled"]:
            return {}
        if not state["change"]:
            if edit:
                raise HistoryError("Select a task with p4_history_begin or prepare before editing.")
            return {}
        change = state["change"]
        # Finish additions observed in an earlier pre-edit hook, even if a host omitted
        # its post hook. Only paths whose absence was explicitly observed are considered.
        pending = []
        for intent in state["pending"]:
            if intent["session"] != session or intent["change"] != change:
                pending.append(intent)
                continue
            if history.files.read(intent["path"]) is not None:
                Preparation(history).prepare(change, [intent["path"]])
            elif event != "stop":
                pending.append(intent)
        state["pending"] = pending
        save_state(history, state)
        if event == "pre" and relative is not None:
            if history.files.read(relative) is None:
                inspected = PerforceOperations(history.p4).inspect_file(
                    relative, history.p4.identity()
                )
                if inspected["stat"].get("action") or int(inspected["stat"].get("haveRev", "0")):
                    raise HistoryError(
                        "A tracked file is missing. Restore or reconcile it explicitly."
                    )
                history.commit(change, "Checkpoint before creating a task file")
                intent = {"path": relative, "change": change, "session": session}
                if any(p["path"] == relative and p != intent for p in state["pending"]):
                    raise HistoryError("Another task or session is preparing this new file.")
                if intent not in state["pending"]:
                    state["pending"].append(intent)
                save_state(history, state)
            else:
                Preparation(history).prepare(change, [relative])
        elif event == "pre" and shell:
            history.commit(change, "Checkpoint before a shell tool")
        elif event == "stop":
            history.commit(change, "Checkpoint before agent handoff")
    return {}


def denial(host: str, message: str) -> dict:
    if host == "claude-code":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
    return {"permissionDecision": "deny", "permissionDecisionReason": message}
