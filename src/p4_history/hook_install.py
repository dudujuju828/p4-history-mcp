"""User-scoped hook registration with explicit ownership and config backups."""

import json
import os
import shlex
import shutil
import uuid
from pathlib import Path

from filelock import FileLock

from .codec import fingerprint
from .errors import HistoryError
from .limits import MAX_METADATA
from .state import atomic_json


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise HistoryError("Linked hook configuration paths are not supported.")
    if path.stat().st_size > MAX_METADATA:
        raise HistoryError("Hook configuration exceeds the size limit.")
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise HistoryError("Hook configuration is not valid UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise HistoryError("Hook configuration must be a JSON object.")
    return result


def registration_paths(root: Path, directory: Path, host: str) -> tuple[Path, Path]:
    if host not in {"copilot", "claude-code"}:
        raise HistoryError("Automatic hooks currently support Copilot CLI and Claude Code.")
    if directory.is_relative_to(root):
        raise HistoryError("Hook ownership state must remain outside the Perforce workspace.")
    key = fingerprint({"root": str(root), "state": str(directory)})[:20]
    if host == "copilot":
        profile = Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot")))
        config = profile / "hooks" / f"p4-history-{key}.json"
    else:
        profile = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        config = profile / "settings.json"
    if config.resolve().is_relative_to(root):
        raise HistoryError("User hook configuration must be outside the Perforce workspace.")
    for part in (config, *config.parents):
        if part.is_symlink() or part.is_junction():
            raise HistoryError("Linked hook configuration paths are not supported.")
    return config, directory / f"hooks-{host}-{fingerprint(str(config))[:20]}.json"


def install_hooks(history, host: str, program: list[str], connection: list[str]) -> dict:
    config, receipt_path = registration_paths(history.p4.root, history.state.directory, host)
    environment = {
        name: os.environ[name]
        for name in ("P4CONFIG", "P4ENVIRO", "P4TICKETS", "P4TRUST", "P4CHARSET")
        if os.environ.get(name)
    }
    groups = {}
    events = (("preToolUse", "pre"), ("postToolUse", "post"), ("agentStop", "stop"))
    if host == "claude-code":
        events = (("PreToolUse", "pre"), ("PostToolUse", "post"), ("Stop", "stop"))
    for event, phase in events:
        command = [*program, "hook", "--host", host, "--event", phase, *connection]
        if host == "copilot":
            groups[event] = [
                {
                    "type": "command",
                    "exec": command[0],
                    "args": command[1:],
                    "env": environment,
                    "timeoutSec": 120,
                }
            ]
        else:
            # Claude command hooks use the configured command shell. Forward slashes and
            # POSIX quoting also handle Git Bash paths containing spaces or apostrophes.
            quoted = " ".join(
                shlex.quote(arg) for arg in [command[0].replace("\\", "/"), *command[1:]]
            )
            if environment:
                quoted = (
                    "env "
                    + " ".join(shlex.quote(f"{k}={v}") for k, v in environment.items())
                    + " "
                    + quoted
                )
            groups[event] = [{"hooks": [{"type": "command", "command": quoted, "timeout": 120}]}]
    config.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(config) + ".p4-history.lock", timeout=5):
        document = _read(config)
        receipt = _read(receipt_path)
        previous = json.loads(json.dumps(document))
        if host == "copilot":
            if document and (not receipt or fingerprint(document) != receipt.get("digest")):
                raise HistoryError("Existing hook file is unowned or customised; it was preserved.")
            document = {"version": 1, "hooks": groups}
        else:
            hooks = document.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise HistoryError("Existing hooks configuration is invalid.")
            for event, owned in receipt.get("groups", {}).items():
                entries = hooks.get(event, [])
                if not isinstance(entries, list) or any(entries.count(item) != 1 for item in owned):
                    raise HistoryError("Previously installed hooks were customised; preserved.")
                hooks[event] = [item for item in entries if item not in owned]
            for event, entries in groups.items():
                existing = hooks.setdefault(event, [])
                if not isinstance(existing, list) or any(item in existing for item in entries):
                    raise HistoryError("Conflicting hook configuration was preserved.")
                existing.extend(entries)
        if previous == document and receipt:
            return {"configured": True, "changed": False, "config": str(config)}
        backup = None
        if config.exists():
            backup = config.with_name(config.name + f".p4-history-{uuid.uuid4().hex}.bak")
            shutil.copyfile(config, backup)
        try:
            atomic_json(config, document)
            atomic_json(receipt_path, {"digest": fingerprint(document), "groups": groups})
        except Exception:
            if backup:
                shutil.copyfile(backup, config)
            else:
                config.unlink(missing_ok=True)
            raise
        return {
            "configured": True,
            "changed": True,
            "config": str(config),
            "backup": str(backup) if backup else None,
        }


def uninstall_hooks(root: Path, directory: Path, host: str) -> dict:
    config, receipt_path = registration_paths(root, directory, host)
    if not receipt_path.exists():
        return {"removed": False, "reason": "No owned hook registration."}
    if not config.exists():
        receipt_path.unlink()
        return {"removed": True, "config": str(config)}
    with FileLock(str(config) + ".p4-history.lock", timeout=5):
        document, receipt = _read(config), _read(receipt_path)
        if host == "copilot":
            if fingerprint(document) != receipt.get("digest"):
                raise HistoryError("Hook file was customised; it was preserved.")
        else:
            hooks = document.get("hooks", {})
            for event, owned in receipt.get("groups", {}).items():
                entries = hooks.get(event, [])
                if not isinstance(entries, list) or any(entries.count(item) != 1 for item in owned):
                    raise HistoryError("Installed hooks were customised; they were preserved.")
                remaining = [item for item in entries if item not in owned]
                if remaining:
                    hooks[event] = remaining
                else:
                    hooks.pop(event, None)
        backup = config.with_name(config.name + f".p4-history-{uuid.uuid4().hex}.bak")
        shutil.copyfile(config, backup)
        if host == "copilot":
            config.unlink()
        else:
            atomic_json(config, document)
        receipt_path.unlink()
        return {"removed": True, "config": str(config), "backup": str(backup)}
