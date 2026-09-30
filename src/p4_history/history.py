"""Application service: tool operations, lock scope and component composition."""

import base64
from pathlib import Path

from .capture import PerforceReader, SnapshotCapture
from .continuity import continue_task
from .diff import changes, metadata_changed, render
from .errors import HistoryError
from .hooks import load_state, save_state, select_task
from .merge import plan_merge
from .models import change_id, commit_message
from .prepare import Preparation
from .restore import RestoreEngine
from .state import LocalState
from .store import Store
from .transitions import ChangelistRestore
from .workspace import WorkspaceFiles


class History:
    def __init__(self, p4: PerforceReader, directory: Path):
        self.p4 = p4
        self.state = LocalState(directory, p4.root)
        self.store = Store(self.state.directory)
        self.files = WorkspaceFiles(p4.root)
        self.capture = SnapshotCapture(p4, self.files, self.state)
        self.restore = RestoreEngine(self.state, self.store, self.files, self.capture, self._save)
        with self.state.locked(allow_recovery=True):
            self.store.initialize()
            if self.store.has_history() and not self.state.binding.exists():
                raise HistoryError(
                    "Workspace binding is missing from an existing history store. "
                    "Restore workspace-binding.json from its backup before writing."
                )

    def _save(
        self,
        change: str,
        snapshot: dict,
        contents: dict,
        message: str,
        *,
        extra_parents: list[str] | None = None,
        allow_empty: bool = False,
    ) -> dict:
        self.state.check_binding(snapshot["workspace"], create=True)
        return self.store.save(
            change,
            snapshot,
            contents,
            message,
            extra_parents=extra_parents,
            allow_empty=allow_empty,
        )

    def _select_work(self, change: str) -> None:
        state = load_state(self)
        state["change"] = change
        save_state(self, state)

    def inspect(self) -> dict:
        with self.state.locked(allow_recovery=True):
            identity = self.p4.identity()
            self.state.check_binding(identity)
            return {
                "workspace": identity,
                "storage": str(self.state.directory),
                "local_only": True,
                "pending_changes": self.p4.changes(),
                "recovery_required": self.state.journal.exists()
                or self.state.transition_journal.exists(),
                "scope": "Only files already opened in the selected changelist are captured.",
            }

    def commit(self, change: str, message: str, allow_empty: bool = False) -> dict:
        change_id(change)
        commit_message(message)
        with self.state.locked():
            snapshot, contents = self.capture.capture(change)
            return {
                **self._save(change, snapshot, contents, message, allow_empty=allow_empty),
                "change": change,
                "files": len(snapshot["files"]),
                "local_only": True,
            }

    def begin(self, change: str, message: str = "Checkpoint before editing") -> dict:
        with self.state.locked():
            result = self.commit(change, message)
            self._select_work(change)
            return {
                **result,
                "next": "Prepare additional files before editing. "
                "Commit automatically at completed milestones and before handoff.",
            }

    def prepare(self, change: str, paths: list[str]) -> dict:
        change_id(change)
        with self.state.locked():
            result = Preparation(self).prepare(change, paths)
            self._select_work(change)
            return result

    def create_change(self, description: str) -> dict:
        with self.state.locked():
            self.state.check_binding(self.p4.identity())
            change = Preparation(self).operations.create_change(description)
            snapshot, contents = self.capture.capture(change)
            result = {"change": change, **self._save(change, snapshot, contents, "Start task")}
            self._select_work(change)
            return result

    def automatic_checkpoints(self, change: str | None) -> dict:
        with self.state.locked():
            if change is not None:
                self.capture.capture(change)
            return select_task(self, change)

    def log(self, change: str, limit: int = 20, offset: int = 0, query: str = "") -> dict:
        with self.state.locked(allow_recovery=True):
            return {
                "change": change_id(change),
                "commits": self.store.log(change, limit, offset, query),
            }

    def status(self, change: str) -> dict:
        with self.state.locked():
            current, _ = self.capture.capture(change)
            head = self.store.head(change)
            previous = self.store.manifest(change, head) if head else {"files": []}
            return {
                "change": change,
                "head": head,
                "active_branch": self.store.active_branch(change),
                "dirty": previous != current,
                "files": changes(previous, current),
                "metadata_changed": metadata_changed(previous, current),
                "uncheckpointed_paths": sorted(
                    {f["path"] for f in current["files"]} - {f["path"] for f in previous["files"]}
                ),
            }

    def diff(self, change: str, before: str = "HEAD", after: str = "WORKSPACE") -> dict:
        with self.state.locked(allow_recovery=after != "WORKSPACE"):
            left = self.store.manifest(change, before)
            right, pending = (
                self.capture.capture(change)
                if after == "WORKSPACE"
                else (self.store.manifest(change, after), {})
            )

            def read_blob(oid: str) -> bytes:
                return pending[oid] if oid in pending else self.store.read_blob(oid)

            return {
                "change": change,
                "before": before,
                "after": after,
                "files": render(left, right, read_blob),
                "metadata_changed": metadata_changed(left, right),
            }

    def plan_restore(
        self,
        change: str,
        revision: str,
        paths: list[str] | None = None,
        mode: str = "content",
    ) -> dict:
        with self.state.locked():
            if mode == "changelist":
                return ChangelistRestore(self).plan(change, revision, paths)
            if mode != "content":
                raise HistoryError("Restore mode must be 'content' or 'changelist'.")
            return self.restore.plan(change, revision, paths)

    def label(self, change: str, name: str, revision: str = "HEAD") -> dict:
        with self.state.locked():
            return self.store.label(change, name, revision)

    def labels(self, change: str) -> dict:
        with self.state.locked(allow_recovery=True):
            return {"change": change_id(change), "labels": self.store.labels(change)}

    def storage(self, compact: bool = False) -> dict:
        with self.state.locked(allow_recovery=not compact):
            return self.store.storage(compact)

    def branch(self, change: str, name: str, revision: str = "HEAD") -> dict:
        with self.state.locked():
            return self.store.create_branch(change, name, revision)

    def branches(self, change: str) -> dict:
        with self.state.locked(allow_recovery=True):
            return self.store.branches(change)

    def plan_switch(self, change: str, name: str, mode: str = "content") -> dict:
        with self.state.locked():
            if mode == "changelist":
                return ChangelistRestore(self).plan(change, f"branch:{name}", switch_branch=name)
            if mode != "content":
                raise HistoryError("Restore mode must be 'content' or 'changelist'.")
            return self.restore.plan(change, f"branch:{name}", switch_branch=name)

    def plan_merge(
        self,
        change: str,
        revision: str,
        mode: str = "cherry-pick",
        paths: list[str] | None = None,
    ) -> dict:
        with self.state.locked():
            return plan_merge(self, change, revision, mode, paths)

    def continue_task(self, source: str, destination: str, move_files: bool = False) -> dict:
        with self.state.locked():
            result = continue_task(self, source, destination, move_files)
            self._select_work(destination)
            return result

    def show(self, change: str, path: str, revision: str = "HEAD") -> dict:
        with self.state.locked(allow_recovery=True):
            snapshot = self.store.manifest(change, revision)
            entry = next((f for f in snapshot["files"] if f["path"] == path), None)
            if not entry:
                raise HistoryError("That path is absent from the selected snapshot.")
            if entry["size"] > 65536:
                return {
                    **entry,
                    "content": None,
                    "reason": "File exceeds the inline display limit.",
                }
            data = self.store.read_blob(entry["blob"]) if entry["blob"] else None
            if data is None:
                return {**entry, "content": None, "encoding": "absent"}
            try:
                text = data.decode("utf-8")
                if "\0" in text:
                    raise ValueError
                return {**entry, "content": text, "encoding": "utf-8"}
            except (UnicodeDecodeError, ValueError):
                return {**entry, "content": base64.b64encode(data).decode(), "encoding": "base64"}

    def apply_restore(self, plan_id: str) -> dict:
        with self.state.locked():
            self.state.plan_path(plan_id)
            if (self.state.directory / f"transition-{plan_id}.json").exists():
                return ChangelistRestore(self).apply(plan_id)
            return self.restore.apply(plan_id)

    def recover_restore(self) -> dict:
        with self.state.locked(allow_recovery=True):
            if self.state.transition_journal.exists():
                return ChangelistRestore(self).recover()
            return self.restore.recover()
