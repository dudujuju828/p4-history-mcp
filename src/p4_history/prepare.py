"""Protect explicitly scoped bytes before opening files for an agent's task."""

import os

from .codec import blob_id
from .errors import HistoryError
from .limits import MAX_FILES
from .models import Snapshot, validate
from .operations import PerforceOperations


class Preparation:
    def __init__(self, history):
        self.history = history
        self.operations = PerforceOperations(history.p4)

    def prepare(self, change: str, paths: list[str]) -> dict:
        h = self.history
        if (
            not paths
            or len(paths) > MAX_FILES
            or len({os.path.normcase(p) for p in paths}) != len(paths)
        ):
            raise HistoryError("Provide 1-2000 unique workspace-relative paths.")
        current, contents = h.capture.capture(change)
        opened = {f["path"]: f for f in current["files"]}
        inspections, new_entries = [], []
        for path in paths:
            inspected = self.operations.inspect_file(path, current["workspace"])
            inspections.append(inspected)
            row = inspected["stat"]
            if path in opened:
                continue
            if row.get("action"):
                raise HistoryError(
                    f"File belongs to another changelist: {path}. Move it explicitly first."
                )
            data = h.files.read(path)
            if data is None:
                raise HistoryError(
                    f"File does not exist: {path}. Use a supported host create tool with hooks, "
                    "or checkpoint the changelist before creating and adding it."
                )
            tracked = int(row.get("haveRev", "0")) > 0
            if not tracked and row.get("headAction") not in {None, "delete", "move/delete"}:
                raise HistoryError(
                    f"File is in the depot but not synced: {path}. Sync it explicitly first."
                )
            oid = blob_id(data)
            contents[oid] = data
            new_entries.append(
                {
                    "depot": inspected["depot"],
                    "path": path,
                    "action": "edit" if tracked else "add",
                    "type": row.get("headType", "binary")
                    if tracked
                    else self.operations.add_type(change, path),
                    "revision": (
                        str(row["haveRev"]) if tracked else str(int(row.get("headRev", "0")) + 1)
                    ),
                    "blob": oid,
                    "size": len(data),
                }
            )
        baseline = validate(
            Snapshot,
            {**current, "files": sorted(current["files"] + new_entries, key=lambda f: f["depot"])},
        )
        if h.capture.capture(change)[0] != current:
            raise HistoryError("Changelist changed during preparation; retry when idle.")
        for inspected in inspections:
            if self.operations.inspect_file(inspected["path"], current["workspace"]) != inspected:
                raise HistoryError("Perforce state changed during preparation; retry when idle.")
        for entry in baseline["files"]:
            data = h.files.read(entry["path"])
            if (blob_id(data) if data is not None else None) != entry["blob"]:
                raise HistoryError("File changed during preparation; retry when idle.")
        # Publish bytes before the first metadata mutation. An interrupted open leaves a
        # usable baseline and can be retried; no rollback can overwrite workspace content.
        saved = h._save(change, baseline, contents, "Checkpoint before preparing task files")
        try:
            for entry in new_entries:
                self.operations.open_file(change, entry)
            actual, _ = h.capture.capture(change)
            if actual != baseline:
                raise HistoryError("Workspace changed while opening files.")
        except Exception as exc:
            raise HistoryError(
                f"Preparation incomplete: {exc}. Baseline {saved['commit']} is retained. "
                "Inspect opened files and retry preparation; no file contents were replaced."
            ) from exc
        return {
            **saved,
            "change": change,
            "prepared": paths,
            "opened": len(new_entries),
            "local_only_history": True,
        }
