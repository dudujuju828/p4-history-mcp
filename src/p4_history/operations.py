"""Explicit, bounded Perforce metadata operations. Never submit, sync, or overwrite bytes."""

import re

from .errors import HistoryError
from .models import change_id, commit_message
from .p4 import Perforce
from .paths import workspace_path


class PerforceOperations:
    def __init__(self, reader: Perforce):
        self.reader = reader

    def local_spec(self, relative: str) -> str:
        path = workspace_path(self.reader.root, relative)
        if "..." in relative:
            raise HistoryError("Perforce wildcard sequences are not accepted in explicit paths.")
        value = path.as_posix()
        for char, escape in (("%", "%25"), ("@", "%40"), ("#", "%23"), ("*", "%2A")):
            value = value.replace(char, escape)
        return value

    def inspect_file(self, relative: str, identity: dict) -> dict:
        spec = self.local_spec(relative)
        rows = self.reader.run("where", spec)
        mapped = [r for r in rows if "unmap" not in r]
        if len(mapped) != 1 or not mapped[0].get("depotFile"):
            raise HistoryError(f"Expected one unambiguous mapping: {relative}")
        from pathlib import Path

        if Path(mapped[0]["path"]) != workspace_path(self.reader.root, relative):
            raise HistoryError(f"Perforce returned a different local mapping: {relative}")
        rows = self.reader.run("fstat", spec, allow_empty=True)
        if len(rows) > 1:
            raise HistoryError(f"Perforce returned multiple files for an explicit path: {relative}")
        row = rows[0] if rows else {}
        if rows and row.get("depotFile") != mapped[0]["depotFile"]:
            raise HistoryError(f"Perforce returned a different depot mapping: {relative}")
        if row.get("action"):
            opened = self.reader.run("opened", spec)
            if len(opened) != 1 or opened[0].get("user") != identity["user"]:
                raise HistoryError(f"File is opened by another user: {relative}")
        if row.get("headType", "").startswith("symlink") or row.get("type", "").startswith(
            "symlink"
        ):
            raise HistoryError(f"Symlink file types are not supported: {relative}")
        return {"depot": mapped[0]["depotFile"], "path": relative, "stat": row}

    def create_change(self, description: str) -> str:
        commit_message(description)
        identity = self.reader.identity()
        rows = self.reader.run(
            "change",
            "-i",
            form={
                "Change": "new",
                "Client": identity["client"],
                "User": identity["user"],
                "Description": description,
            },
            include_info=True,
        )
        for row in rows:
            match = re.fullmatch(r"Change ([1-9][0-9]*) created\.", row.get("data", "").strip())
            if match:
                result = match[1]
                self.reader.describe(result, identity)
                return result
        raise HistoryError(
            "Could not identify the created changelist. Inspect pending changes before retrying."
        )

    def open_file(self, change: str, entry: dict) -> None:
        action = entry["action"]
        if action not in {"edit", "add"}:
            raise HistoryError("Preparation only opens files for edit or add.")
        self.reader.run(
            action,
            *(["-f"] if action == "add" else []),
            "-c",
            change_id(change),
            "-t",
            entry["type"],
            (
                workspace_path(self.reader.root, entry["path"]).as_posix()
                if action == "add"
                else self.local_spec(entry["path"])
            ),
        )

    def add_type(self, change: str, path: str) -> str:
        rows = self.reader.run(
            "add",
            "-n",
            "-f",
            "-c",
            change_id(change),
            workspace_path(self.reader.root, path).as_posix(),
        )
        if len(rows) != 1 or not isinstance(rows[0].get("type"), str):
            raise HistoryError(f"Perforce could not preview adding {path}; check ignore rules.")
        return rows[0]["type"]

    def close_file(self, change: str, path: str) -> None:
        self.reader.run("revert", "-k", "-c", change_id(change), self.local_spec(path))

    def open_delete(self, change: str, entry: dict) -> None:
        self.reader.run("delete", "-k", "-c", change_id(change), self.local_spec(entry["path"]))
        opened = next((f for f in self.reader.opened(change) if f["path"] == entry["path"]), None)
        if opened is None:
            raise HistoryError("Perforce did not open the requested deletion.")
        if opened["type"] != entry["type"]:
            self.set_type(change, entry)

    def set_type(self, change: str, entry: dict) -> None:
        self.reader.run(
            "reopen", "-c", change_id(change), "-t", entry["type"], self.local_spec(entry["path"])
        )

    def move_files(self, destination: str, depot_files: list[str]) -> None:
        self.reader.run("reopen", "-c", change_id(destination), input_files=depot_files)
