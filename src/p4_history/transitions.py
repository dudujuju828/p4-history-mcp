"""Journalled edit/add/delete membership restoration. Never sync or submit depot content."""

import uuid

from .codec import blob_id, fingerprint
from .diff import changes
from .errors import HistoryError
from .models import Snapshot, TransitionJournal, TransitionPlan, validate
from .operations import PerforceOperations
from .paths import validate_relative
from .state import atomic_json


def topology(entry: dict | None) -> dict | None:
    return {k: v for k, v in entry.items() if k not in {"blob", "size"}} if entry else None


class ChangelistRestore:
    def __init__(self, history):
        self.h = history
        self.operations = PerforceOperations(history.p4)

    def _inspect(self, path: str, identity: dict, change: str) -> dict:
        inspected = self.operations.inspect_file(path, identity)
        row = inspected["stat"]
        if row.get("action") and str(row.get("change")) != change:
            raise HistoryError(f"File is opened in another changelist: {path}")
        return inspected

    def _entry(self, inspected: dict) -> dict | None:
        row = inspected["stat"]
        if not row.get("action"):
            return None
        # opened reports the new depot revision for adds; fstat's haveRev remains zero.
        revision = (
            str(int(row.get("headRev", "0")) + 1)
            if row["action"] == "add"
            else str(row.get("haveRev", "0"))
        )
        return {
            "path": inspected["path"],
            "depot": inspected["depot"],
            "action": row["action"],
            "type": row.get("type", row.get("headType", "")),
            "revision": revision,
        }

    def plan(
        self,
        change: str,
        revision: str,
        paths: list[str] | None = None,
        switch_branch: str | None = None,
    ) -> dict:
        h = self.h
        oid = h.store.resolve(change, revision)
        target = h.store.manifest(change, oid)
        current, _ = h.capture.capture(change)
        if current["workspace"] != target["workspace"]:
            raise HistoryError("Workspace identity or mapping changed; restore is blocked.")
        left = {f["path"]: f for f in current["files"]}
        right = {f["path"]: f for f in target["files"]}
        selected = set(left) | set(right) if paths is None else set(paths)
        if paths is not None and (not paths or len(selected) != len(paths)):
            raise HistoryError("Select at least one unique path.")
        for path in selected:
            validate_relative(path)
            if path not in left and path not in right:
                raise HistoryError(f"Selected path is absent from both snapshots: {path}")
        desired_files = {**left}
        steps = []
        for path in sorted(selected):
            before, after = left.get(path), right.get(path)
            if before == after:
                continue
            if any(e and e["action"] not in {"edit", "add", "delete"} for e in (before, after)):
                raise HistoryError(
                    "Changelist restore supports edit/add/delete; resolve moves first."
                )
            if (
                before
                and after
                and (before["depot"], before["revision"]) != (after["depot"], after["revision"])
            ):
                raise HistoryError("Depot mapping or base revision changed; restore is blocked.")
            inspected = self._inspect(path, current["workspace"], change)
            if self._entry(inspected) != topology(before):
                raise HistoryError(f"Perforce state changed during planning: {path}")
            have = str(inspected["stat"].get("haveRev", "0"))
            if after:
                expected_revision = (
                    str(int(inspected["stat"].get("headRev", "0")) + 1)
                    if after["action"] == "add"
                    else have
                )
                if after["revision"] != expected_revision or (
                    after["action"] == "add" and have != "0"
                ):
                    raise HistoryError(f"Base revision changed: {path}. Sync is never implicit.")
                desired_files[path] = after
            else:
                desired_files.pop(path, None)
            data = h.files.read(path)
            before_blob = blob_id(data) if data is not None else None
            if before and before_blob != before["blob"]:
                raise HistoryError(f"File changed during planning: {path}")
            # A file absent from a snapshot has no known historical depot bytes. Keep
            # existing tracked bytes when closing an edit; remove a reverted task add.
            remove_add = before and before["action"] == "add" and after is None
            after_blob = after["blob"] if after else (None if remove_add else before_blob)
            after_size = after["size"] if after else (0 if remove_add else len(data or b""))
            steps.append(
                {
                    "path": path,
                    "depot": inspected["depot"],
                    "have_revision": have,
                    "before": before,
                    "after": after,
                    "before_blob": before_blob,
                    "before_size": len(data or b""),
                    "after_blob": after_blob,
                    "after_size": after_size,
                }
            )
        desired = validate(
            Snapshot, {**current, "files": sorted(desired_files.values(), key=lambda f: f["depot"])}
        )
        plan = validate(
            TransitionPlan,
            {
                "id": uuid.uuid4().hex,
                "kind": "changelist",
                "change": change,
                "target": oid,
                "head": h.store.head(change),
                "active_branch": h.store.active_branch(change),
                "switch_branch": switch_branch,
                "branch_head": h.store.branch_tip(change, switch_branch) if switch_branch else None,
                "fingerprint": fingerprint(current),
                "desired": desired,
                "files": steps,
            },
        )
        atomic_json(h.state.directory / f"transition-{plan['id']}.json", plan)
        return plan

    def _check(self, plan: dict, recovering: bool) -> dict:
        h = self.h
        identity = h.p4.identity()
        h.state.check_binding(identity)
        if identity != plan["desired"]["workspace"]:
            raise HistoryError("Workspace identity or mapping changed; restore is blocked.")
        observed = {}
        for step in plan["files"]:
            inspected = self._inspect(step["path"], identity, plan["change"])
            if (
                inspected["depot"] != step["depot"]
                or str(inspected["stat"].get("haveRev", "0")) != step["have_revision"]
            ):
                raise HistoryError("Mapping or base revision changed during restore.")
            entry = self._entry(inspected)
            allowed = [topology(step["before"])]
            if recovering:
                allowed += [None, topology(step["after"])]
            if entry not in allowed:
                raise HistoryError(f"Newer Perforce changes were preserved: {step['path']}")
            data = h.files.read(step["path"])
            oid = blob_id(data) if data is not None else None
            allowed_bytes = {step["before_blob"]}
            if recovering:
                allowed_bytes.add(step["after_blob"])
            if oid not in allowed_bytes:
                raise HistoryError(f"Newer edits were preserved: {step['path']}")
            observed[step["path"]] = (entry, oid)
        return observed

    def _payloads(self, plan: dict, before: bool, pending: dict | None = None) -> dict:
        result = {}
        prefix = "before" if before else "after"
        for step in plan["files"]:
            oid = step[f"{prefix}_blob"]
            data = (
                ((pending or {})[oid] if oid in (pending or {}) else self.h.store.read_blob(oid))
                if oid
                else None
            )
            if len(data or b"") != step[f"{prefix}_size"]:
                raise HistoryError("Restore payload size mismatch.")
            result[step["path"]] = data
        return result

    def _write(self, plan: dict, payloads: dict, observed: dict, before: bool) -> None:
        h = self.h
        for step in plan["files"]:
            path, desired = step["path"], step["before" if before else "after"]
            expected_entry, expected_blob = observed[path]
            now = self._inspect(path, plan["desired"]["workspace"], plan["change"])
            data = h.files.read(path)
            if (
                self._entry(now) != expected_entry
                or (blob_id(data) if data is not None else None) != expected_blob
            ):
                raise HistoryError(f"Concurrent change detected: {path}")
            if expected_entry != topology(desired) and expected_entry:
                self.operations.close_file(plan["change"], path)
                expected_entry = None
            h.files.make_writable(path)
            h.files.write(path, payloads[path])
            if desired and not expected_entry:
                if desired["action"] in {"edit", "add"}:
                    self.operations.open_file(plan["change"], desired)
                elif desired["action"] == "delete":
                    self.operations.open_delete(plan["change"], desired)

    def _finish(self, journal: dict, commit: str) -> None:
        atomic_json(
            self.h.state.transition_journal,
            validate(
                TransitionJournal,
                {
                    **journal,
                    "phase": "complete",
                    "completed_commit": commit,
                },
            ),
        )
        self.h.state.transition_journal.unlink()

    def apply(self, plan_id: str) -> dict:
        h = self.h
        h.state.plan_path(plan_id)  # Validate ID before composing a path.
        path = h.state.directory / f"transition-{plan_id}.json"
        plan = h.state.read(path, TransitionPlan)
        if plan["id"] != plan_id:
            raise HistoryError("Restore plan ID differs from its filename.")
        change = plan["change"]
        current, contents = h.capture.capture(change)
        if (
            fingerprint(current) != plan["fingerprint"]
            or h.store.head(change) != plan["head"]
            or h.store.active_branch(change) != plan["active_branch"]
        ):
            raise HistoryError("Workspace or history changed since the preview. Re-plan.")
        if (
            plan["switch_branch"]
            and h.store.branch_tip(change, plan["switch_branch"]) != plan["branch_head"]
        ):
            raise HistoryError("Destination branch changed since the preview.")
        # Re-derive the operation list from stored history before trusting a local plan.
        selected = [step["path"] for step in plan["files"]]
        if selected:
            rebuilt = self.plan(change, plan["target"], selected, plan["switch_branch"])
            (h.state.directory / f"transition-{rebuilt['id']}.json").unlink()
            if rebuilt["files"] != plan["files"] or rebuilt["desired"] != plan["desired"]:
                raise HistoryError("Restore plan differs from its historical target.")
        elif changes(current, plan["desired"]):
            raise HistoryError("Restore plan is missing its file operations.")
        observed = self._check(plan, False)
        # Add pre-existing unopened bytes to a separately pinned auxiliary snapshot.
        auxiliary = {f["path"]: f for f in current["files"]}
        for step in plan["files"]:
            data = h.files.read(step["path"])
            if (blob_id(data) if data is not None else None) != step["before_blob"]:
                raise HistoryError("Workspace changed before the safety checkpoint.")
            if data is not None:
                contents[step["before_blob"]] = data
            entry = step["before"] or step["after"]
            auxiliary[step["path"]] = {
                **entry,
                "blob": step["before_blob"],
                "size": step["before_size"],
                "action": entry["action"] if data is not None else "delete",
            }
        desired_payloads = self._payloads(plan, False, contents)
        backup = h._save(change, current, contents, "Safety checkpoint before changelist restore")
        aux = h.store.save(
            change,
            {**current, "files": sorted(auxiliary.values(), key=lambda f: f["depot"])},
            contents,
            "Safety bytes including unopened files",
            publish=False,
        )
        journal = validate(
            TransitionJournal,
            {"plan": plan, "backup": backup["commit"], "auxiliary": aux["commit"]},
        )
        atomic_json(h.state.transition_journal, journal)
        try:
            self._write(plan, desired_payloads, observed, False)
            restored, captured = h.capture.capture(change)
            if restored != plan["desired"]:
                delta = changes(plan["desired"], restored)
                raise HistoryError(f"Workspace changed during changelist restore: {delta}")
            self._verify_bytes(plan, False)
            if plan["switch_branch"]:
                h.store.activate_branch(change, plan["switch_branch"])
            result = h._save(
                change, restored, captured, f"Restore changelist from {plan['target'][:12]}"
            )
            self._finish(journal, result["commit"])
        except Exception as exc:
            raise HistoryError(
                f"Changelist restore interrupted: {exc}. Call recover_restore."
            ) from exc
        path.unlink(missing_ok=True)
        return {
            **result,
            "restored": True,
            "safety_commit": backup["commit"],
            "safety_workspace_commit": aux["commit"],
            "files": len(plan["files"]),
        }

    def _verify_bytes(self, plan: dict, before: bool) -> None:
        for step in plan["files"]:
            data = self.h.files.read(step["path"])
            if (blob_id(data) if data is not None else None) != step[
                "before_blob" if before else "after_blob"
            ]:
                raise HistoryError("Workspace changed during restore; recovery is required.")

    def recover(self) -> dict:
        h = self.h
        journal = h.state.read(h.state.transition_journal, TransitionJournal)
        plan, change = journal["plan"], journal["plan"]["change"]
        backup = h.store.manifest(change, journal["backup"])
        historical = h.store.manifest(change, plan["target"])
        left = {f["path"]: f for f in backup["files"]}
        right = {f["path"]: f for f in plan["desired"]["files"]}
        target = {f["path"]: f for f in historical["files"]}
        expected_paths = {d["path"] for d in changes(backup, plan["desired"])}
        if (
            fingerprint(backup) != plan["fingerprint"]
            or expected_paths != {s["path"] for s in plan["files"]}
            or len(expected_paths) != len(plan["files"])
            or historical["workspace"] != backup["workspace"]
            or plan["desired"]["workspace"] != backup["workspace"]
        ):
            raise HistoryError("Recovery journal differs from its historical snapshots.")
        for step in plan["files"]:
            if (
                step["before"] != left.get(step["path"])
                or step["after"] != right.get(step["path"])
                or step["after"] != target.get(step["path"])
            ):
                raise HistoryError("Recovery operations differ from their historical snapshots.")
            after = step["after"]
            expected_blob = (
                after["blob"]
                if after
                else None
                if step["before"]["action"] == "add"
                else step["before_blob"]
            )
            expected_size = (
                after["size"]
                if after
                else 0
                if step["before"]["action"] == "add"
                else step["before_size"]
            )
            if (step["after_blob"], step["after_size"]) != (expected_blob, expected_size):
                raise HistoryError("Recovery target bytes differ from their historical snapshot.")
        aux = {f["path"]: f for f in h.store.manifest(change, journal["auxiliary"])["files"]}
        # Validate journal bytes against independently hashed Git manifests.
        for step in plan["files"]:
            if step["path"] not in aux or (
                aux[step["path"]]["blob"],
                aux[step["path"]]["size"],
            ) != (step["before_blob"], step["before_size"]):
                raise HistoryError("Recovery journal differs from its safety snapshot.")
        if journal["phase"] == "complete":
            completed = h.store.manifest(change, journal["completed_commit"])
            actual = [f for f in completed["files"] if f["path"] in expected_paths]
            allowed = [
                [f for f in snapshot["files"] if f["path"] in expected_paths]
                for snapshot in (backup, plan["desired"])
            ]
            if actual not in allowed:
                raise HistoryError("Invalid completed recovery journal.")
            h.state.transition_journal.unlink()
            return {"recovered": True, "cleanup_only": True}
        observed = self._check(plan, True)
        payloads = self._payloads(plan, True)
        self._write(plan, payloads, observed, True)
        restored, contents = h.capture.capture(change)
        # Preserve unrelated files changed after interruption.
        affected = {step["path"] for step in plan["files"]}
        if [f for f in restored["files"] if f["path"] in affected] != [
            f for f in backup["files"] if f["path"] in affected
        ]:
            raise HistoryError("Perforce state changed during recovery.")
        self._verify_bytes(plan, True)
        if plan["switch_branch"]:
            h.store.activate_branch(change, plan["active_branch"])
        result = h._save(change, restored, contents, "Recover interrupted changelist restore")
        self._finish(journal, result["commit"])
        return {**result, "recovered": True, "safety_commit": journal["backup"]}
