"""Pinned Perforce CLI transport and read-only workspace queries."""

import io
import marshal
import subprocess
from pathlib import Path

from .errors import HistoryError
from .limits import MAX_FILES, MAX_METADATA
from .models import change_id
from .paths import workspace_path


class Perforce:
    def __init__(self, root: Path, client: str, port: str | None = None, user: str | None = None):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise HistoryError("The Perforce workspace root must be a directory.")
        if not client or client.startswith("-"):
            raise HistoryError("An explicit Perforce client is required.")
        self.client, self.port, self.user = client, port, user

    def run(
        self,
        *args: str,
        allow_empty: bool = False,
        input_files: list[str] | None = None,
        form: dict | None = None,
        include_info: bool = False,
    ) -> list[dict]:
        command = ["p4", "-G", "-c", self.client]
        if self.port:
            command.extend(["-p", self.port])
        if self.user:
            command.extend(["-u", self.user])
        data = None
        if form is not None:
            data = marshal.dumps({k.encode(): v.encode() for k, v in form.items()}, 0)
        if input_files is not None:
            if any(not f.startswith("//") or any(ord(c) < 32 for c in f) for f in input_files):
                raise HistoryError("Perforce returned an unsafe depot path.")
            command.extend(["-x", "-"])
            data = ("\n".join(input_files) + "\n").encode("utf-8")
        try:
            result = subprocess.run(
                command + list(args),
                cwd=self.root,
                input=data,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HistoryError(f"Could not run Perforce: {exc}") from exc
        if len(result.stdout) > MAX_METADATA:
            raise HistoryError("Perforce metadata exceeds the response size limit.")
        records, errors, empty = [], [], False
        stream = io.BytesIO(result.stdout)
        while stream.tell() < len(result.stdout):
            try:
                raw = marshal.load(stream)
            except (EOFError, ValueError, TypeError) as exc:
                raise HistoryError("Perforce returned invalid structured output.") from exc
            try:
                if not isinstance(raw, dict):
                    raise ValueError("Expected a Perforce record.")
                row = {
                    k.decode("utf-8"): v.decode("utf-8") if isinstance(v, bytes) else v
                    for k, v in raw.items()
                }
            except (AttributeError, UnicodeDecodeError, ValueError) as exc:
                raise HistoryError("Perforce returned invalid or non-UTF-8 metadata.") from exc
            if row.get("code") == "error":
                # EV_EMPTY (17), not a text substring that could hide a real failure.
                if allow_empty and str(row.get("generic", "")) == "17":
                    empty = True
                else:
                    errors.append(row.get("data", "Unknown Perforce error"))
            elif row.get("code") == "stat":
                records.append(row)
            elif row.get("code") == "info" and include_info:
                records.append(row)
            elif row.get("code") not in {"info", "text"}:
                raise HistoryError("Perforce returned an unknown structured record type.")
        expected_empty = empty and not records and result.returncode in {0, 1} and not result.stderr
        if errors or (result.returncode and not expected_empty):
            detail = "\n".join(errors) or result.stderr.decode("utf-8", errors="replace")
            raise HistoryError(f"Perforce failed: {detail.strip()}")
        return records

    def identity(self) -> dict:
        rows = self.run("info")
        if len(rows) != 1:
            raise HistoryError("Could not identify the Perforce workspace.")
        info = rows[0]
        if info.get("clientName") != self.client:
            raise HistoryError("Perforce returned a different client than requested.")
        if not all(info.get(k) for k in ("clientRoot", "userName", "serverAddress")):
            raise HistoryError("Perforce returned an incomplete workspace identity.")
        actual_root = Path(info["clientRoot"]).resolve()
        if actual_root != self.root:
            raise HistoryError(f"Workspace root mismatch: expected {self.root}, got {actual_root}.")
        specs = self.run("client", "-o", self.client)
        if len(specs) != 1 or specs[0].get("Client") != self.client:
            raise HistoryError("Could not validate the client specification.")
        spec = specs[0]
        mapping = {
            k: v
            for k, v in spec.items()
            if k.startswith(("View", "ChangeView"))
            or k in {"Stream", "StreamAtChange", "LineEnd", "Root", "Options"}
        }
        return {
            "root": str(self.root),
            "client": self.client,
            "user": info["userName"],
            "server": info["serverAddress"],
            "server_id": info.get("ServerID", info.get("serverName", "")),
            "stream": info.get("clientStream", ""),
            "mapping": mapping,
        }

    def describe(self, change: str, identity: dict) -> dict:
        change_id(change)
        if change == "default":
            return {"change": change, "description": "Default changelist"}
        rows = self.run("change", "-o", change)
        if len(rows) != 1:
            raise HistoryError(f"Changelist {change} was not found.")
        spec = rows[0]
        if spec.get("Status") != "pending":
            raise HistoryError("Only pending changelists can be snapshotted or restored.")
        if spec.get("Client") != self.client or spec.get("User") != identity["user"]:
            raise HistoryError("The changelist belongs to another client or user.")
        return {"change": change, "description": spec.get("Description", "")}

    def opened(self, change: str) -> list[dict]:
        user = self.identity()["user"]
        rows = self.run(
            "opened",
            "-c",
            change_id(change),
            "-u",
            user,
            "-m",
            str(MAX_FILES + 1),
            allow_empty=True,
        )
        if len(rows) > MAX_FILES:
            raise HistoryError("Changelist exceeds the 2000-file prototype limit.")
        # Validate scope before resolving any paths. One batched where avoids O(files)
        # processes and network round-trips while keeping the explicit depot file set.
        for row in rows:
            if (
                row.get("client") != self.client
                or str(row.get("change")) != change
                or row.get("user") != user
            ):
                raise HistoryError("Perforce returned a file outside the selected changelist.")
            if not all(
                isinstance(row.get(k), str) and row[k] for k in ("depotFile", "action", "type")
            ):
                raise HistoryError("Perforce returned incomplete opened-file metadata.")
        mappings = self.run("where", input_files=[r["depotFile"] for r in rows]) if rows else []
        result = []
        for row in rows:
            depot = row["depotFile"]
            mapped = [r for r in mappings if r.get("depotFile") == depot and "unmap" not in r]
            if len(mapped) != 1 or "path" not in mapped[0]:
                raise HistoryError(f"Expected one unambiguous mapping for {depot}.")
            local = Path(mapped[0]["path"])
            try:
                relative = local.relative_to(self.root).as_posix()
            except ValueError as exc:
                raise HistoryError(f"Mapped path is outside the workspace: {local}") from exc
            workspace_path(self.root, relative)
            if row.get("type", "").startswith("symlink"):
                raise HistoryError(f"Symlink file types are not supported: {depot}")
            result.append(
                {
                    "depot": depot,
                    "path": relative,
                    "action": row["action"],
                    "type": row["type"],
                    "revision": str(row.get("rev", "0")),
                }
            )
        return sorted(result, key=lambda item: item["depot"])

    def changes(self) -> list[dict]:
        identity = self.identity()
        rows = self.run(
            "changes",
            "-s",
            "pending",
            "-c",
            self.client,
            "-u",
            identity["user"],
            "-l",
            allow_empty=True,
        )
        return [{"change": r["change"], "description": r.get("desc", "")} for r in rows]
