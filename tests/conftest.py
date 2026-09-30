import copy
from pathlib import Path

import pytest

from p4_history.history import History


class FakePerforce:
    def __init__(self, root: Path):
        self.root = root
        self.files = []
        self.description = "Example pending work"
        self.workspace = {
            "root": str(root),
            "client": "example-client",
            "user": "tester",
            "server": "localhost:1666",
            "server_id": "test",
            "stream": "//example/main",
            "mapping": {"LineEnd": "local"},
        }

    def identity(self):
        return copy.deepcopy(self.workspace)

    def describe(self, change, identity):
        return {"change": change, "description": self.description}

    def opened(self, change):
        return copy.deepcopy(self.files)

    def changes(self):
        return [{"change": "123", "description": self.description}]

    def add(self, path, content=b"before\r\n", action="edit", file_type="text"):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            destination.write_bytes(content)
        self.files.append(
            {
                "depot": f"//depot/{path}",
                "path": path,
                "action": action,
                "type": file_type,
                "revision": "1",
            }
        )
        self.files.sort(key=lambda item: item["depot"])
        return destination


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return History(FakePerforce(root), tmp_path / "state")
