"""Three-way merge planning. Conflicts never write workspace files or advance branches."""

from .codec import blob_id
from .diff import changes
from .errors import HistoryError
from .restore import select_target


def plan_merge(history, change: str, revision: str, mode: str, paths: list[str] | None) -> dict:
    h = history
    if mode not in {"merge", "cherry-pick"}:
        raise HistoryError("Merge mode must be 'merge' or 'cherry-pick'.")
    incoming_id = h.store.resolve(change, revision)
    current, contents = h.capture.capture(change)
    incoming = h.store.manifest(change, incoming_id)
    if mode == "cherry-pick":
        parents = h.store.git("rev-list", "--parents", "-n", "1", incoming_id).decode().split()
        if len(parents) != 2:
            raise HistoryError("Cherry-pick requires a commit with exactly one parent.")
        base_id = parents[1]
    else:
        base_id = h.store.git("merge-base", h.store.head(change), incoming_id).decode().strip()
        if not base_id:
            raise HistoryError("These histories have no shared merge base.")
    base = h.store.manifest(change, base_id)
    # Scope validation permits unrelated additions and integrations outside selected paths.
    selected = paths if paths is not None else [d["path"] for d in changes(base, incoming)]
    if not selected:
        return {"conflicts": [], "plan": None, "reason": "The incoming revision has no changes."}
    select_target(current, incoming, selected)
    select_target(current, base, selected)
    originals = {f["path"]: f for f in base["files"]}
    theirs = {f["path"]: f for f in incoming["files"]}
    conflicts, entries = [], []
    for ours in current["files"]:
        path = ours["path"]
        if path not in selected:
            entries.append(ours)
            continue
        ancestor, other = originals[path], theirs[path]
        if ours["blob"] == ancestor["blob"]:
            data = h.store.read_blob(other["blob"]) if other["blob"] else None
        elif other["blob"] in {ours["blob"], ancestor["blob"]}:
            entries.append(ours)
            continue
        elif all(f["blob"] is not None for f in (ancestor, ours, other)):
            data = h.store.merge_text(
                h.store.read_blob(ancestor["blob"]),
                contents[ours["blob"]],
                h.store.read_blob(other["blob"]),
            )
            if data is None:
                conflicts.append(
                    {
                        "path": path,
                        "reason": "Overlapping, binary or large changes",
                        "base": ancestor["blob"],
                        "local": ours["blob"],
                        "incoming": other["blob"],
                    }
                )
                continue
        else:
            conflicts.append({"path": path, "reason": "Conflicting presence or deletion"})
            continue
        oid = blob_id(data) if data is not None else None
        if data is not None:
            contents[oid] = data
        entries.append({**ours, "blob": oid, "size": len(data) if data is not None else 0})
    if conflicts:
        return {"conflicts": conflicts, "plan": None, "workspace_changed": False}
    desired = {**current, "files": entries}
    staged = h.store.save(
        change,
        desired,
        contents,
        f"Proposed {mode} of {incoming_id[:12]}",
        publish=False,
        extra_parents=[incoming_id] if mode == "merge" else None,
    )
    plan = h.restore.plan(change, staged["commit"], selected)
    if mode == "merge":
        plan["merge_parent"] = incoming_id
        h.state.save_plan(plan)
    return {"conflicts": [], "plan": plan, "base": base_id, "incoming": incoming_id}
