"""Own restore planning, journalling and recovery. Caller holds LocalState's lock."""

import uuid
from collections.abc import Callable

from .capture import SnapshotCapture
from .codec import blob_id, fingerprint
from .diff import changes
from .errors import HistoryError
from .paths import validate_relative
from .state import LocalState
from .store import Store
from .workspace import WorkspaceFiles


def compatible(current: dict, target: dict) -> None:
    if current["workspace"] != target["workspace"] or current["change"] != target["change"]:
        raise HistoryError("Workspace identity or mapping changed; content restore is blocked.")

    def topology(snapshot):
        return {
            f["depot"]: {k: v for k, v in f.items() if k not in {"blob", "size"}}
            for f in snapshot["files"]
        }

    if topology(current) != topology(target):
        raise HistoryError(
            "File membership, actions, types, paths or base revisions changed. "
            "Content mode requires unchanged Perforce state. Use changelist mode for "
            "ordinary edit/add/delete action or membership changes at the same base revision."
        )
    if any(f["action"] not in {"edit", "add", "delete"} for f in current["files"]):
        raise HistoryError("Restoring moves, branches and integrations is not supported yet.")


def select_target(current: dict, target: dict, paths: list[str] | None) -> dict:
    """Compose a selective restore without touching or constraining unselected files."""
    if paths is None:
        compatible(current, target)
        return {**current, "files": target["files"]}
    if not paths or len(set(paths)) != len(paths):
        raise HistoryError("Select at least one unique workspace-relative file path.")
    for path in paths:
        validate_relative(path)
    selected = set(paths)
    left = {f["path"]: f for f in current["files"] if f["path"] in selected}
    right = {f["path"]: f for f in target["files"] if f["path"] in selected}
    if left.keys() != selected or right.keys() != selected:
        raise HistoryError("Each selected path must exist in both snapshots.")
    compatible({**current, "files": list(left.values())}, {**target, "files": list(right.values())})
    return {**current, "files": [right.get(f["path"], f) for f in current["files"]]}


class RestoreEngine:
    def __init__(
        self,
        state: LocalState,
        store: Store,
        files: WorkspaceFiles,
        capture: SnapshotCapture,
        save: Callable[..., dict],
    ):
        self.state, self.store, self.files, self.capture, self.save = (
            state,
            store,
            files,
            capture,
            save,
        )

    def plan(
        self,
        change: str,
        revision: str,
        paths: list[str] | None = None,
        *,
        switch_branch: str | None = None,
    ) -> dict:
        oid = self.store.resolve(change, revision)
        target = self.store.manifest(change, oid)
        current, _ = self.capture.capture(change)
        target = select_target(current, target, paths)
        plan = {
            "id": uuid.uuid4().hex,
            "change": change,
            "target": oid,
            "head": self.store.head(change),
            "fingerprint": fingerprint(current),
            "files": [
                {
                    "path": d["path"],
                    "operation": "write" if d["after"]["blob"] is not None else "remove",
                }
                for d in changes(current, target)
            ],
            "description_restored": False,
            "paths": paths,
            "active_branch": self.store.active_branch(change),
            "switch_branch": switch_branch,
            "branch_head": self.store.branch_tip(change, switch_branch) if switch_branch else None,
        }
        self.state.save_plan(plan)
        return plan

    def _payloads(self, entries: list[dict]) -> dict[str, bytes | None]:
        payloads = {}
        for entry in entries:
            data = self.store.read_blob(entry["blob"]) if entry["blob"] else None
            if data is not None and len(data) != entry["size"]:
                raise HistoryError("Stored blob size does not match the snapshot manifest.")
            self.files.preflight(entry["path"], data)
            payloads[entry["path"]] = data
        return payloads

    def _finish(self, journal: dict, commit: str) -> None:
        # Persist completion before cleanup: retrying cleanup must not undo a finished restore.
        self.state.save_journal({**journal, "phase": "complete", "completed_commit": commit})
        self.state.clear_journal()

    def apply(self, plan_id: str) -> dict:
        plan = self.state.load_plan(plan_id)
        change = plan["change"]
        current, contents = self.capture.capture(change)
        if fingerprint(current) != plan["fingerprint"] or self.store.head(change) != plan["head"]:
            raise HistoryError("The workspace or history changed since the preview. Re-plan.")
        if self.store.active_branch(change) != plan["active_branch"]:
            raise HistoryError("The active branch changed since the preview. Re-plan.")
        if plan["switch_branch"] and (
            self.store.branch_tip(change, plan["switch_branch"]) != plan["branch_head"]
            or plan["branch_head"] != plan["target"]
        ):
            raise HistoryError("The destination branch changed since the preview. Re-plan.")
        target = self.store.manifest(change, plan["target"])
        if plan["merge_parent"]:
            parent = self.store.resolve(change, plan["merge_parent"])
            parents = self.store.git("rev-list", "--parents", "-n", "1", plan["target"])
            if parent not in parents.decode().split()[1:]:
                raise HistoryError("Merge plan does not reference its proposed merge parent.")
        target = select_target(current, target, plan["paths"])
        deltas = changes(current, target)
        operations = [
            {"path": d["path"], "operation": "write" if d["after"]["blob"] else "remove"}
            for d in deltas
        ]
        if operations != plan["files"]:
            raise HistoryError("Invalid restore plan: operations differ from its target snapshot.")
        if not deltas:
            if plan["merge_parent"]:
                self.save(
                    change,
                    current,
                    contents,
                    f"Merge {plan['merge_parent'][:12]}",
                    extra_parents=[plan["merge_parent"]],
                )
            if plan["switch_branch"]:
                self.save(change, current, contents, "Safety checkpoint before branch switch")
                self.store.activate_branch(change, plan["switch_branch"])
            self.state.delete_plan(plan_id)
            return {
                "restored": False,
                "reason": "Workspace already matches the target.",
                "active_branch": self.store.active_branch(change),
            }
        payloads = self._payloads([d["after"] for d in deltas])
        backup = self.save(change, current, contents, "Safety checkpoint before restore")
        journal = {
            "change": change,
            "backup": backup["commit"],
            "target": plan["target"],
            "files": [d["path"] for d in deltas],
            "paths": plan["paths"],
            "branch_before": plan["active_branch"],
            "branch_after": plan["switch_branch"],
        }
        self.state.save_journal(journal)
        try:
            for delta in deltas:
                observed = self.files.read(delta["path"])
                if (blob_id(observed) if observed is not None else None) != delta["before"]["blob"]:
                    raise HistoryError(f"Concurrent edit detected: {delta['path']}")
                self.files.write(delta["path"], payloads[delta["path"]])
            restored, contents = self.capture.capture(change)
            if restored != {**current, "files": target["files"]}:
                raise HistoryError("Workspace changed during restore; recovery is required.")
            if plan["switch_branch"]:
                self.store.activate_branch(change, plan["switch_branch"])
            if plan["merge_parent"]:
                result = self.save(
                    change,
                    restored,
                    contents,
                    f"Merge {plan['merge_parent'][:12]}",
                    extra_parents=[plan["merge_parent"]],
                )
            else:
                result = self.save(
                    change, restored, contents, f"Restore content from {plan['target'][:12]}"
                )
            self._finish(journal, result["commit"])
        except Exception as exc:
            raise HistoryError(
                f"Restore interrupted: {exc}. Safety commit {backup['commit']} "
                "is retained. Call recover_restore; later edits will be preserved."
            ) from exc
        self.state.delete_plan(plan_id)
        return {"restored": True, **result, "safety_commit": backup["commit"], "files": len(deltas)}

    def recover(self) -> dict:
        if not self.state.journal.exists():
            return {"recovered": False, "reason": "No interrupted restore."}
        journal = self.state.load_journal()
        change = journal["change"]
        backup = self.store.manifest(change, journal["backup"])
        target = self.store.manifest(change, journal["target"])
        target = select_target(backup, target, journal["paths"])
        expected_paths = [d["path"] for d in changes(backup, target)]
        if journal["files"] != expected_paths:
            raise HistoryError("Invalid recovery journal: file list differs from its snapshots.")
        if journal["phase"] == "complete":
            completed = self.store.manifest(change, journal["completed_commit"])
            select_target(backup, completed, journal["paths"])
            affected = set(journal["files"])
            completed_files = {f["path"]: f for f in completed["files"] if f["path"] in affected}
            allowed = [
                {f["path"]: f for f in snapshot["files"] if f["path"] in affected}
                for snapshot in (backup, target)
            ]
            if completed_files not in allowed:
                raise HistoryError("Invalid recovery journal: completion snapshot is inconsistent.")
            self.state.clear_journal()
            return {
                "recovered": True,
                "cleanup_only": True,
                "commit": journal["completed_commit"],
                "safety_commit": journal["backup"],
            }
        current, _ = self.capture.capture(change)
        select_target(current, backup, journal["paths"])
        before = {f["path"]: f for f in backup["files"]}
        after = {f["path"]: f for f in target["files"]}
        now = {f["path"]: f for f in current["files"]}
        for path in journal["files"]:
            if now[path]["blob"] not in {before[path]["blob"], after[path]["blob"]}:
                raise HistoryError(
                    f"Recovery preserved a newer edit at {path}. Save it elsewhere "
                    "and restore that file to its backup or target bytes first."
                )
        payloads = self._payloads([before[path] for path in journal["files"]])
        for path, data in payloads.items():
            observed = self.files.read(path)
            if (blob_id(observed) if observed is not None else None) != now[path]["blob"]:
                raise HistoryError(f"Concurrent edit during recovery: {path}. Retry when idle.")
            self.files.write(path, data)
        restored, contents = self.capture.capture(change)
        expected = {
            **current,
            "files": [before[f["path"]] if f["path"] in payloads else f for f in current["files"]],
        }
        if restored != expected:
            raise HistoryError(
                "Workspace changed during recovery; the recovery journal is retained."
            )
        if journal["branch_after"]:
            if not journal["branch_before"]:
                raise HistoryError("Branch recovery is missing the source branch.")
            self.store.activate_branch(change, journal["branch_before"])
        result = self.save(change, restored, contents, "Recover interrupted restore")
        self._finish(journal, result["commit"])
        return {"recovered": True, **result, "safety_commit": journal["backup"]}
