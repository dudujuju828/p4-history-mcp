"""Own local metadata files, workspace binding, and the per-store process lock."""

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from .codec import encode
from .errors import HistoryError
from .limits import MAX_METADATA
from .models import Binding, Record, RestoreJournal, RestorePlan, decode, validate


def atomic_json(path: Path, value: dict) -> None:
    payload = encode(value)
    if len(payload) > MAX_METADATA:
        raise HistoryError("Local metadata exceeds the size limit.")
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class LocalState:
    def __init__(self, directory: Path, root: Path):
        self.directory = directory.resolve()
        if self.directory.is_relative_to(root):
            raise HistoryError("History storage must be outside the Perforce workspace.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.directory / "workspace.lock", timeout=5)
        self.journal = self.directory / "restore-in-progress.json"
        self.transition_journal = self.directory / "changelist-restore-in-progress.json"
        self.binding = self.directory / "workspace-binding.json"

    @contextmanager
    def locked(self, *, allow_recovery: bool = False):
        try:
            with self.lock:
                if (
                    self.journal.exists() or self.transition_journal.exists()
                ) and not allow_recovery:
                    raise HistoryError(
                        "An interrupted restore needs recovery. Call recover_restore first."
                    )
                yield
        except Timeout as exc:
            raise HistoryError("Another operation is using this history store; try again.") from exc
        except OSError as exc:
            raise HistoryError(f"Local history operation failed: {exc}") from exc

    def read(self, path: Path, model: type[Record]) -> dict:
        try:
            if path.is_symlink() or path.is_junction():
                raise HistoryError(f"Invalid {model.__name__} metadata: linked metadata file.")
            with path.open("rb") as stream:
                return decode(model, stream.read(MAX_METADATA + 1))
        except OSError as exc:
            raise HistoryError(f"Cannot read {model.__name__} metadata at {path}: {exc}") from exc

    def check_binding(self, identity: dict, *, create: bool = False) -> None:
        selected = validate(Binding, {k: identity[k] for k in Binding.model_fields})
        if self.binding.exists():
            if self.read(self.binding, Binding) != selected:
                raise HistoryError(
                    "This history store is bound to another workspace connection. "
                    "Use its original connection or a separate state directory."
                )
        elif create:
            atomic_json(self.binding, selected)

    def plan_path(self, plan_id: str) -> Path:
        if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise HistoryError("Invalid restore plan ID.")
        return self.directory / f"plan-{plan_id}.json"

    def save_plan(self, plan: dict) -> None:
        atomic_json(self.plan_path(plan["id"]), validate(RestorePlan, plan))

    def load_plan(self, plan_id: str) -> dict:
        path = self.plan_path(plan_id)
        if not path.exists():
            raise HistoryError("Restore plan was not found. Create a new preview.")
        plan = self.read(path, RestorePlan)
        if plan["id"] != plan_id:
            raise HistoryError("Invalid restore plan: stored ID differs from its filename.")
        return plan

    def save_journal(self, journal: dict) -> None:
        atomic_json(self.journal, validate(RestoreJournal, journal))

    def load_journal(self) -> dict:
        return self.read(self.journal, RestoreJournal)

    def clear_journal(self) -> None:
        self.journal.unlink()

    def delete_plan(self, plan_id: str) -> None:
        self.plan_path(plan_id).unlink(missing_ok=True)
