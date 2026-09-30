"""The sole owner of reading and replacing workspace bytes."""

import os
import stat
import tempfile
from pathlib import Path

from .errors import HistoryError
from .limits import MAX_FILE
from .paths import workspace_path


class WorkspaceFiles:
    def __init__(self, root: Path):
        self.root = root

    def read(self, relative: str) -> bytes | None:
        path = workspace_path(self.root, relative)
        try:
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise HistoryError(f"Not a regular file: {relative}")
                if before.st_size > MAX_FILE:
                    raise HistoryError(f"File exceeds the 64 MiB prototype limit: {relative}")
                data = handle.read(MAX_FILE + 1)
                after = os.fstat(handle.fileno())
            current = workspace_path(self.root, relative).stat()
        except FileNotFoundError:
            if not path.exists():
                return None
            raise HistoryError(
                f"File changed while it was being read: {relative}. Retry."
            ) from None
        except OSError as exc:
            raise HistoryError(f"Cannot read workspace file {relative}: {exc}") from exc

        def stamp(value):
            # Windows fstat/stat disagree on ctime in Python 3.12. Identity, size and
            # mtime plus the capture's second hash pass provide a portable check.
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

        if len(data) > MAX_FILE or stamp(before) != stamp(after) or stamp(after) != stamp(current):
            raise HistoryError(f"File changed while it was being read: {relative}. Retry.")
        return data

    def preflight(self, relative: str, data: bytes | None) -> None:
        path = workspace_path(self.root, relative)
        if not path.parent.is_dir():
            raise HistoryError(f"Parent directory is missing: {relative}")
        if path.exists() and not stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR:
            raise HistoryError(f"File is read-only; refusing to change permissions: {relative}")

    def write(self, relative: str, data: bytes | None) -> None:
        self.preflight(relative, data)
        target = workspace_path(self.root, relative)
        if data is None:
            target.unlink(missing_ok=True)
            return
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        descriptor, temporary = tempfile.mkstemp(prefix=".p4-history-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            workspace_path(self.root, relative)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def make_writable(self, relative: str) -> None:
        """Only used by an explicit, journalled Perforce action transition."""
        path = workspace_path(self.root, relative)
        if path.exists():
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
