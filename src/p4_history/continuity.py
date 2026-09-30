"""Carry a task's active history across a changelist move, without rewriting old commits."""

from .errors import HistoryError
from .models import change_id
from .operations import PerforceOperations


def continue_task(history, source: str, destination: str, move_files: bool) -> dict:
    h = history
    change_id(source)
    change_id(destination)
    if source == destination:
        raise HistoryError("Choose different source and destination changelists.")
    baseline = h.store.manifest(source)
    target, _ = h.capture.capture(destination)
    if baseline["workspace"] != target["workspace"]:
        raise HistoryError("Workspace identity or mapping changed; task continuation is blocked.")
    if h.store.head(destination):
        existing = h.store.manifest(destination)
        if existing["files"] or h.store.branches(destination)["branches"]:
            raise HistoryError("Destination already has task history. Use a fresh changelist.")
    original = {
        f["depot"]: {k: v for k, v in f.items() if k not in {"blob", "size"}}
        for f in baseline["files"]
    }
    if not original:
        raise HistoryError("The source history contains no task files.")
    if move_files:
        remaining, _ = h.capture.capture(source)
        combined = remaining["files"] + target["files"]
    else:
        remaining = {"files": []}
        combined = target["files"]
    observed = {
        f["depot"]: {k: v for k, v in f.items() if k not in {"blob", "size"}} for f in combined
    }
    if observed != original or len(observed) != len(combined):
        raise HistoryError("The destination must contain exactly the same task file metadata.")
    if move_files and remaining["files"]:
        # reopen only changes ownership metadata; a partial failure can be resumed by
        # comparing the union of source and destination against the retained source tip.
        PerforceOperations(h.p4).move_files(destination, [f["depot"] for f in remaining["files"]])
    current, contents = h.capture.capture(destination)
    actual = {
        f["depot"]: {k: v for k, v in f.items() if k not in {"blob", "size"}}
        for f in current["files"]
    }
    if actual != original:
        raise HistoryError("Task move incomplete. Inspect opened files and retry continuation.")
    saved = h.store.continue_history(source, destination, current, contents)
    return {
        **saved,
        "source": source,
        "change": destination,
        "files": len(current["files"]),
        "source_history_retained": True,
        "scope": "Active source branch ancestry; other branches and labels remain at source.",
    }
