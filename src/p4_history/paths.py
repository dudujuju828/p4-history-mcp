from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import HistoryError


def validate_relative(relative: str) -> str:
    if not isinstance(relative, str):
        raise HistoryError("Unsafe workspace-relative path: expected a string.")
    part = PurePosixPath(relative)
    if (
        not relative
        or relative == "."
        or any(ord(c) < 32 for c in relative)
        or part.is_absolute()
        or "\\" in relative
        or any(p in {".", ".."} or ":" in p for p in part.parts)
        or any(p.endswith((".", " ")) or PureWindowsPath(p).is_reserved() for p in part.parts)
        or relative != part.as_posix()
    ):
        raise HistoryError(f"Unsafe workspace-relative path: {relative!r}")
    return relative


def workspace_path(root: Path, relative: str) -> Path:
    """Reject traversal and every symlink/junction, including a redirected root."""
    part = PurePosixPath(validate_relative(relative))
    if root.is_symlink() or root.is_junction() or root.resolve() != root:
        raise HistoryError("The workspace root was redirected; reconnect using its real root.")
    target = root.joinpath(*part.parts)
    cursor = root
    for name in part.parts:
        cursor = cursor / name
        if cursor.is_symlink() or cursor.is_junction():
            raise HistoryError(f"Symlinks and junctions are not supported: {relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise HistoryError(f"Path leaves the workspace: {relative}")
    if target.exists() and not target.is_file():
        raise HistoryError(f"Expected a regular file: {relative}")
    return target
