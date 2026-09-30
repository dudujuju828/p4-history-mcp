import os
import subprocess

import pytest

from p4_history.errors import HistoryError
from p4_history.paths import workspace_path


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "C:/file", "a\\file", "a/../b", "a//b", "a/./b", "", "file:stream"],
)
def test_path_traversal_is_rejected(tmp_path, path):
    with pytest.raises(HistoryError, match="Unsafe"):
        workspace_path(tmp_path, path)


@pytest.mark.parametrize(
    "path",
    [
        "file.",
        "folder /file",
        "NUL",
        "con.txt",
        "con.a.b",
        "COM1.txt",
        "file\x00name",
        "file\nname",
    ],
)
def test_alias_and_control_character_paths_are_rejected(tmp_path, path):
    with pytest.raises(HistoryError, match="Unsafe"):
        workspace_path(tmp_path, path)


def test_symlink_or_junction_is_rejected_even_inside_workspace(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("preserve")
    link = tmp_path / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(HistoryError, match="Symlinks and junctions"):
        workspace_path(tmp_path, "linked/file.txt")
    assert (target / "file.txt").read_text() == "preserve"
