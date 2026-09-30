"""Capture a bounded, validated snapshot without changing Git or Perforce state."""

from pathlib import Path
from typing import Protocol

from .codec import blob_id
from .errors import HistoryError
from .limits import MAX_FILES, MAX_SNAPSHOT
from .models import Snapshot, change_id, validate
from .state import LocalState
from .workspace import WorkspaceFiles


class PerforceReader(Protocol):
    root: Path

    def identity(self) -> dict: ...
    def describe(self, change: str, identity: dict) -> dict: ...
    def opened(self, change: str) -> list[dict]: ...
    def changes(self) -> list[dict]: ...


class SnapshotCapture:
    def __init__(self, p4: PerforceReader, files: WorkspaceFiles, state: LocalState):
        self.p4, self.files, self.state = p4, files, state

    def capture(self, change: str) -> tuple[dict, dict[str, bytes]]:
        change_id(change)
        identity = self.p4.identity()
        self.state.check_binding(identity)
        description = self.p4.describe(change, identity)
        opened = self.p4.opened(change)
        if len(opened) > MAX_FILES:
            raise HistoryError("Changelist exceeds the 2000-file prototype limit.")
        entries, contents, total = [], {}, 0
        for item in opened:
            data = self.files.read(item["path"])
            if data is None and item["action"] not in {"delete", "move/delete"}:
                raise HistoryError(f"Opened file is missing: {item['path']}")
            oid = blob_id(data) if data is not None else None
            if data is not None:
                total += len(data)
                if total > MAX_SNAPSHOT:
                    raise HistoryError("Changelist exceeds the 256 MiB prototype limit.")
                contents[oid] = data
            entries.append({**item, "blob": oid, "size": len(data) if data is not None else 0})
        snapshot = validate(
            Snapshot, {"format": 1, "workspace": identity, **description, "files": entries}
        )
        if (
            self.p4.identity() != identity
            or self.p4.describe(change, identity) != description
            or self.p4.opened(change) != opened
        ):
            raise HistoryError("Perforce state changed during capture. Retry when editing is idle.")
        for item in entries:
            data = self.files.read(item["path"])
            if (blob_id(data) if data is not None else None) != item["blob"]:
                raise HistoryError(f"File changed during capture: {item['path']}. Retry.")
        return snapshot, contents
