"""Bare Git object storage. No worktree, filters, hooks, remotes, or network commands."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from .codec import blob_id, encode
from .errors import HistoryError
from .limits import MAX_FILE, MAX_METADATA
from .models import Snapshot, change_id, commit_message, decode, validate


def reference_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise HistoryError("Names must be 1-64 lowercase letters, digits, underscores or hyphens.")
    return name


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.repo = self.directory / "history.git"
        self.hooks = self.directory / "empty-hooks"

    def git(
        self,
        *args: str,
        data: bytes | None = None,
        check: bool = True,
        with_status: bool = False,
    ):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_AUTHOR_NAME": "p4-history",
                "GIT_AUTHOR_EMAIL": "local@p4-history.invalid",
                "GIT_COMMITTER_NAME": "p4-history",
                "GIT_COMMITTER_EMAIL": "local@p4-history.invalid",
            }
        )
        command = [
            "git",
            "--no-pager",
            "-c",
            f"core.hooksPath={self.hooks}",
            "-c",
            "gc.auto=0",
            "-c",
            "protocol.allow=never",
            "--git-dir",
            str(self.repo),
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                input=data,
                capture_output=True,
                env=env,
                cwd=self.directory,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HistoryError(f"Could not run local Git: {exc}") from exc
        if check and result.returncode:
            raise HistoryError(result.stderr.decode("utf-8", errors="replace").strip())
        return (result.returncode, result.stdout) if with_status else result.stdout

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.repo.is_symlink() or self.repo.is_junction():
            raise HistoryError("The history repository must not be a symlink or junction.")
        self.hooks.mkdir(exist_ok=True)
        if not self.repo.exists():
            self.git("init", "--bare", "--object-format=sha1", "--template=", str(self.repo))
        elif not all((self.repo / name).exists() for name in ("HEAD", "config", "objects", "refs")):
            raise HistoryError(
                "The history repository is incomplete or corrupt; do not reinitialize it."
            )
        if (
            self.git("rev-parse", "--is-bare-repository").strip() != b"true"
            or self.git("rev-parse", "--show-object-format").strip() != b"sha1"
        ):
            raise HistoryError("History requires a bare Git repository with SHA-1 object IDs.")

    def has_history(self) -> bool:
        return bool(self.git("for-each-ref", "--count=1", "refs/heads/changelists/").strip())

    @staticmethod
    def ref(change: str) -> str:
        return f"refs/heads/changelists/{change_id(change)}"

    def head(self, change: str) -> str | None:
        ref = self.ref(change)
        result = self.git("for-each-ref", "--format=%(refname) %(objectname)", ref)
        for line in result.decode().splitlines():
            name, oid = line.split(" ", 1)
            if name == ref:
                return oid
        return None

    def blob(self, data: bytes) -> str:
        return self.git("hash-object", "-w", "--stdin", data=data).decode().strip()

    def read_blob(self, oid: str, *, limit: int = MAX_FILE) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise HistoryError("Invalid stored blob ID.")
        size = int(self.git("cat-file", "-s", oid))
        if size > limit:
            raise HistoryError("Stored blob exceeds the size limit.")
        data = self.git("cat-file", "blob", oid)
        if len(data) != size or blob_id(data) != oid:
            raise HistoryError(f"Stored blob integrity mismatch: {oid}.")
        return data

    def save(
        self,
        change: str,
        manifest: dict,
        contents: dict[str, bytes],
        message: str,
        *,
        publish: bool = True,
        extra_parents: list[str] | None = None,
        allow_empty: bool = False,
    ) -> dict:
        commit_message(message)
        manifest = validate(Snapshot, manifest)
        if len(encode(manifest)) > MAX_METADATA:
            raise HistoryError("Snapshot metadata exceeds the size limit.")
        head = self.head(change)
        for entry in manifest["files"]:
            oid = entry["blob"]
            if oid is None:
                continue
            data = contents.get(oid)
            if data is None or len(data) != entry["size"] or blob_id(data) != oid:
                raise HistoryError("Snapshot content does not match its manifest.")
        for oid, data in contents.items():
            if self.blob(data) != oid:
                raise HistoryError("Content hash mismatch while saving snapshot.")
            # hash-object may reuse an already-existing loose object without repairing
            # its bytes. Verify persisted content before reporting a successful commit.
            if self.read_blob(oid) != data:
                raise HistoryError("Persisted snapshot content differs from captured bytes.")
        return self.commit(
            change,
            manifest,
            message,
            head,
            publish=publish,
            extra_parents=extra_parents,
            allow_empty=allow_empty,
        )

    def commit(
        self,
        change: str,
        manifest: dict,
        message: str,
        expected_head: str | None,
        *,
        publish: bool = True,
        extra_parents: list[str] | None = None,
        allow_empty: bool = False,
    ) -> dict:
        commit_message(message)
        manifest = validate(Snapshot, manifest)
        if manifest["change"] != change:
            raise HistoryError("Snapshot belongs to another changelist.")
        if self.head(change) != expected_head:
            raise HistoryError("History advanced concurrently; refresh and try again.")
        if (
            not allow_empty
            and not extra_parents
            and expected_head
            and self.manifest(change, expected_head) == manifest
        ):
            return {"commit": expected_head, "created": False}
        objects = {item["blob"] for item in manifest["files"] if item["blob"] is not None}
        entries = [f"100644 blob {oid}\t{oid}\x00" for oid in sorted(objects)]
        files_tree = self.git("mktree", "-z", data="".join(entries).encode()).decode().strip()
        manifest_blob = self.blob(encode(manifest))
        tree = (
            self.git(
                "mktree",
                "-z",
                data=(
                    f"040000 tree {files_tree}\tfiles\x00"
                    f"100644 blob {manifest_blob}\tmanifest.json\x00"
                ).encode(),
            )
            .decode()
            .strip()
        )
        parents = ["-p", expected_head] if expected_head else []
        for parent in extra_parents or []:
            if not re.fullmatch(r"[0-9a-f]{40}", parent):
                raise HistoryError("Invalid history parent.")
            parents += ["-p", parent]
        oid = self.git("commit-tree", tree, *parents, data=message.encode()).decode().strip()
        if publish:
            self.git(
                "update-ref", "--create-reflog", self.ref(change), oid, expected_head or "0" * 40
            )
        else:
            ref = f"refs/p4-history/targets/{change}/{oid}"
            existing = self.git("for-each-ref", "--format=%(objectname)", ref).decode().splitlines()
            if existing not in ([], [oid]):
                raise HistoryError("A staged snapshot reference is inconsistent.")
            if not existing:
                self.git("update-ref", ref, oid, "0" * 40)
        return {"commit": oid, "created": True}

    def resolve(self, change: str, revision: str) -> str:
        head = self.head(change)
        if not head:
            raise HistoryError("This changelist has no local history. Call begin first.")
        if revision == "HEAD":
            return head
        if revision.startswith("branch:"):
            return self.branch_tip(change, revision[7:])
        if re.fullmatch(r"HEAD~[0-9]{1,4}", revision):
            count = int(revision[5:])
            commits = self.git("rev-list", "--first-parent", f"--max-count={count + 1}", head)
            ancestors = commits.decode().splitlines()
            if len(ancestors) <= count:
                raise HistoryError("This history has fewer ancestors than requested.")
            return ancestors[count]
        if revision.startswith("@"):
            ref = f"refs/tags/changelists/{change_id(change)}/{reference_name(revision[1:])}"
            rows = self.git("for-each-ref", "--format=%(refname) %(objectname)", ref)
            for row in rows.decode().splitlines():
                name, oid = row.split(" ", 1)
                if name == ref:
                    return oid
            raise HistoryError("Named milestone was not found.")
        if not re.fullmatch(r"[0-9a-f]{7,40}", revision):
            raise HistoryError(
                "Use HEAD, HEAD~N, @milestone or a commit ID (at least 7 hex digits)."
            )
        tips = (
            self.git(
                "for-each-ref",
                "--format=%(objectname)",
                f"refs/heads/experiments/{change}/",
                f"refs/p4-history/targets/{change}/",
                f"refs/tags/changelists/{change}/",
            )
            .decode()
            .splitlines()
        )
        matches = [
            c
            for c in self.git("rev-list", head, *tips).decode().splitlines()
            if c.startswith(revision)
        ]
        if len(matches) != 1:
            raise HistoryError("Commit is ambiguous or does not belong to this changelist.")
        return matches[0]

    def manifest(self, change: str, revision: str = "HEAD") -> dict:
        oid = self.resolve(change, revision)
        blob = self.git("rev-parse", "--verify", f"{oid}:manifest.json").decode().strip()
        snapshot = decode(Snapshot, self.read_blob(blob, limit=MAX_METADATA))
        if snapshot["change"] != change:
            source = snapshot["change"]
            ref = f"refs/p4-history/lineage/{change}/{source}"
            rows = self.git("for-each-ref", "--format=%(refname)", ref).decode().splitlines()
            if ref not in rows:
                raise HistoryError("Stored snapshot belongs to another changelist.")
            snapshot = {**snapshot, "change": change}
        return snapshot

    def continue_history(self, source: str, change: str, manifest: dict, contents: dict) -> dict:
        source_head = self.resolve(source, "HEAD")
        prefix = f"refs/p4-history/lineage/{source}/"
        ancestors = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        refs = [(f"refs/p4-history/lineage/{change}/{source}", source_head)]
        refs += [
            (f"refs/p4-history/lineage/{change}/{ref.removeprefix(prefix)}", oid)
            for ref, oid in (row.split(" ", 1) for row in ancestors.decode().splitlines())
        ]
        # Atomic ref transaction: provenance cannot be partly installed on failure.
        commands = "start\n" + "".join(f"update {ref} {oid}\n" for ref, oid in refs)
        self.git("update-ref", "--stdin", data=(commands + "prepare\ncommit\n").encode())
        return self.save(
            change,
            manifest,
            contents,
            f"Continue task from changelist {source}",
            extra_parents=[source_head],
        )

    def log(self, change: str, limit: int = 20, offset: int = 0, query: str = "") -> list[dict]:
        if not 1 <= limit <= 100:
            raise HistoryError("History limit must be between 1 and 100.")
        if not 0 <= offset <= 100000 or len(query) > 256 or "\0" in query:
            raise HistoryError(
                "Use an offset from 0 to 100000 and a search of at most 256 characters."
            )
        head = self.head(change)
        if not head:
            return []
        search = ["--regexp-ignore-case", "--fixed-strings", f"--grep={query}"] if query else []
        commits = (
            self.git("rev-list", f"--max-count={limit}", f"--skip={offset}", *search, head)
            .decode()
            .splitlines()
        )
        result = []
        for commit in commits:
            fields = self.git("show", "-s", "--format=%H%x00%cI%x00%B", commit)
            oid, date, message = fields.decode().split("\0", 2)
            result.append({"commit": oid, "date": date, "message": message.rstrip()})
        return result

    def label(self, change: str, name: str, revision: str) -> dict:
        name = reference_name(name)
        oid = self.resolve(change, revision)
        ref = f"refs/tags/changelists/{change_id(change)}/{name}"
        existing = {item["name"]: item["commit"] for item in self.labels(change)}
        if name in existing:
            if existing[name] != oid:
                raise HistoryError("That milestone name already protects another commit.")
        else:
            self.git("update-ref", ref, oid, "0" * 40)
        return {"name": name, "revision": f"@{name}", "commit": oid}

    def labels(self, change: str) -> list[dict]:
        prefix = f"refs/tags/changelists/{change_id(change)}/"
        rows = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return [
            {"name": ref.removeprefix(prefix), "commit": oid}
            for ref, oid in (row.split(" ", 1) for row in rows.decode().splitlines())
        ]

    def storage(self, compact: bool = False) -> dict:
        if compact:
            self.git("fsck", "--full", "--no-dangling")
            # Repack reachable objects; do not expire reflogs, prune history or reset refs.
            self.git("repack", "-ad")
            self.git("fsck", "--full", "--no-dangling")
        stats = dict(
            line.split(": ", 1) for line in self.git("count-objects", "-v").decode().splitlines()
        )
        return {
            "local_only": True,
            "compacted": compact,
            "loose_objects": int(stats["count"]),
            "packed_objects": int(stats["in-pack"]),
            "packs": int(stats["packs"]),
            "object_bytes_on_disk": 1024 * (int(stats["size"]) + int(stats["size-pack"])),
            "history_deleted": False,
        }

    def active_branch(self, change: str) -> str | None:
        ref = self.git("symbolic-ref", "-q", self.ref(change), check=False).decode().strip()
        if not ref:
            return None
        prefix = f"refs/heads/experiments/{change}/"
        if not ref.startswith(prefix):
            raise HistoryError("Unexpected symbolic history reference.")
        return reference_name(ref.removeprefix(prefix))

    def branch_tip(self, change: str, name: str) -> str:
        ref = f"refs/heads/experiments/{change_id(change)}/{reference_name(name)}"
        rows = self.git("for-each-ref", "--format=%(refname) %(objectname)", ref)
        for row in rows.decode().splitlines():
            candidate, oid = row.split(" ", 1)
            if candidate == ref:
                return oid
        raise HistoryError("Local branch was not found.")

    def create_branch(self, change: str, name: str, revision: str = "HEAD") -> dict:
        name = reference_name(name)
        oid = self.resolve(change, revision)
        if self.active_branch(change) is None:
            # Migration is retryable if interrupted between creating main and the alias.
            ref = f"refs/heads/experiments/{change}/main"
            existing = self.git("for-each-ref", "--format=%(objectname)", ref).decode().strip()
            if existing and existing != self.head(change):
                raise HistoryError("Incomplete branch migration needs inspection; main differs.")
            if not existing:
                self.git("update-ref", ref, self.head(change), "0" * 40)
            self.activate_branch(change, "main")
        ref = f"refs/heads/experiments/{change}/{name}"
        existing = self.git("for-each-ref", "--format=%(refname)", ref).decode().splitlines()
        if ref in existing:
            raise HistoryError("That local branch already exists.")
        self.git("update-ref", "--create-reflog", ref, oid, "0" * 40)
        return {"name": name, "commit": oid, "active": False}

    def activate_branch(self, change: str, name: str) -> None:
        self.branch_tip(change, name)
        self.git("symbolic-ref", self.ref(change), f"refs/heads/experiments/{change}/{name}")

    def branches(self, change: str) -> dict:
        prefix = f"refs/heads/experiments/{change_id(change)}/"
        rows = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return {
            "active": self.active_branch(change),
            "branches": [
                {"name": ref.removeprefix(prefix), "commit": oid}
                for ref, oid in (r.split(" ", 1) for r in rows.decode().splitlines())
            ],
        }

    def merge_text(self, base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
        if any(b"\0" in data or len(data) > 1024 * 1024 for data in (base, ours, theirs)):
            return None
        try:
            for data in (base, ours, theirs):
                data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        with tempfile.TemporaryDirectory(prefix="merge-", dir=self.directory) as directory:
            paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
            for path, data in zip(paths, (ours, base, theirs), strict=True):
                path.write_bytes(data)
            code, output = self.git(
                "merge-file",
                "-p",
                "--diff3",
                "-L",
                "local",
                "-L",
                "base",
                "-L",
                "incoming",
                *map(str, paths),
                check=False,
                with_status=True,
            )
            if code == 0:
                return output
            if 1 <= code <= 127:
                return None
            raise HistoryError("Git could not perform the local three-way merge.")
