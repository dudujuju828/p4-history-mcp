"""Versioned persistence contracts. Validate all metadata at the storage boundary."""

import os
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import HistoryError
from .limits import MAX_FILE, MAX_FILES, MAX_METADATA, MAX_SNAPSHOT
from .paths import validate_relative

Change = Annotated[str, Field(pattern=r"^(default|[1-9][0-9]*)$")]
ObjectID = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
PlanID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReferenceName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


def change_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"default|[1-9][0-9]*", value):
        raise HistoryError("Use a positive numbered changelist or 'default'.")
    return value


def commit_message(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4000 or "\0" in value:
        raise HistoryError("Provide a commit message of 1 to 4000 characters without NUL bytes.")
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(Record):
    root: str = Field(min_length=1)
    client: str = Field(min_length=1)
    user: str = Field(min_length=1)
    server: str = Field(min_length=1)
    server_id: str


class WorkspaceIdentity(Binding):
    stream: str
    mapping: dict[str, str]


class FileEntry(Record):
    depot: str = Field(pattern=r"^//[^\x00-\x1f]+$")
    path: str
    action: Literal[
        "edit", "add", "delete", "branch", "integrate", "move/add", "move/delete", "import"
    ]
    type: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9]+$")
    blob: ObjectID | None
    size: int = Field(ge=0, le=MAX_FILE)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return validate_relative(value)

    @model_validator(mode="after")
    def absent_file(self) -> Self:
        if self.blob is None and (self.size or self.action not in {"delete", "move/delete"}):
            raise ValueError("Only deleted files can have no content blob, with zero size.")
        return self


class Snapshot(Record):
    format: Literal[1]
    workspace: WorkspaceIdentity
    change: Change
    description: str
    files: list[FileEntry] = Field(max_length=MAX_FILES)

    @model_validator(mode="after")
    def unique_files(self) -> Self:
        if len({f.depot for f in self.files}) != len(self.files):
            raise ValueError("Duplicate depot paths in snapshot.")
        if len({os.path.normcase(f.path) for f in self.files}) != len(self.files):
            raise ValueError("Multiple depot files map to the same local path.")
        if sum(f.size for f in self.files) > MAX_SNAPSHOT:
            raise ValueError("Snapshot exceeds the size limit.")
        return self


class PlannedFile(Record):
    path: str
    operation: Literal["write", "remove"]

    _valid_path = field_validator("path")(validate_relative)


class RestorePlan(Record):
    id: PlanID
    change: Change
    target: ObjectID
    head: ObjectID
    fingerprint: Fingerprint
    files: list[PlannedFile] = Field(max_length=MAX_FILES)
    description_restored: Literal[False]
    paths: list[str] | None = None
    active_branch: ReferenceName | None = None
    switch_branch: ReferenceName | None = None
    branch_head: ObjectID | None = None
    merge_parent: ObjectID | None = None

    @model_validator(mode="after")
    def valid_intent(self) -> Self:
        if self.switch_branch and (
            not self.active_branch or self.branch_head != self.target or self.merge_parent
        ):
            raise ValueError("Inconsistent branch switch plan.")
        return self


class RestoreJournal(Record):
    change: Change
    backup: ObjectID
    target: ObjectID
    files: list[str] = Field(min_length=1, max_length=MAX_FILES)
    phase: Literal["applying", "complete"] = "applying"
    completed_commit: ObjectID | None = None
    paths: list[str] | None = None
    branch_before: ReferenceName | None = None
    branch_after: ReferenceName | None = None

    @model_validator(mode="after")
    def valid_journal(self) -> Self:
        if len(set(self.files)) != len(self.files):
            raise ValueError("Duplicate paths in recovery journal.")
        for path in self.files:
            validate_relative(path)
        if (self.phase == "complete") != (self.completed_commit is not None):
            raise ValueError("Inconsistent recovery journal completion state.")
        if self.branch_after and not self.branch_before:
            raise ValueError("Branch recovery requires its source branch.")
        return self


class TransitionFile(Record):
    path: str
    depot: str = Field(pattern=r"^//[^\x00-\x1f]+$")
    have_revision: str = Field(pattern=r"^[0-9]+$")
    before: FileEntry | None
    after: FileEntry | None
    before_blob: ObjectID | None
    before_size: int = Field(ge=0, le=MAX_FILE)
    after_blob: ObjectID | None
    after_size: int = Field(ge=0, le=MAX_FILE)

    _valid_path = field_validator("path")(validate_relative)

    @model_validator(mode="after")
    def valid_transition(self) -> Self:
        if self.before is None and self.after is None:
            raise ValueError("A transition must affect a historical file.")
        for entry in (self.before, self.after):
            if entry and (entry.path != self.path or entry.depot != self.depot):
                raise ValueError("Transition entries must name the same local and depot paths.")
        if (self.before_blob is None and self.before_size) or (
            self.after_blob is None and self.after_size
        ):
            raise ValueError("Absent files must have zero size.")
        return self


class TransitionPlan(Record):
    id: PlanID
    kind: Literal["changelist"]
    change: Change
    target: ObjectID
    head: ObjectID
    active_branch: ReferenceName | None
    switch_branch: ReferenceName | None = None
    branch_head: ObjectID | None = None
    fingerprint: Fingerprint
    desired: Snapshot
    files: list[TransitionFile] = Field(max_length=MAX_FILES)

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        if self.desired.change != self.change:
            raise ValueError("Transition snapshot belongs to another changelist.")
        if len({os.path.normcase(f.path) for f in self.files}) != len(self.files):
            raise ValueError("Duplicate transition paths.")
        if self.switch_branch and (not self.active_branch or self.branch_head != self.target):
            raise ValueError("Inconsistent branch switch plan.")
        return self


class TransitionJournal(Record):
    plan: TransitionPlan
    backup: ObjectID
    auxiliary: ObjectID
    phase: Literal["applying", "complete"] = "applying"
    completed_commit: ObjectID | None = None

    @model_validator(mode="after")
    def valid_completion(self) -> Self:
        if (self.phase == "complete") != (self.completed_commit is not None):
            raise ValueError("Inconsistent transition journal completion state.")
        return self


def validation_error(model: type[Record], error: ValidationError | ValueError) -> HistoryError:
    if isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['msg']}"
            for e in error.errors(include_input=False, include_url=False)[:3]
        )
    else:
        details = str(error)
    return HistoryError(f"Invalid {model.__name__} metadata: {details[:1000]}")


def validate(model: type[Record], value: dict) -> dict:
    try:
        return model.model_validate(value).model_dump()
    except (ValidationError, ValueError) as exc:
        raise validation_error(model, exc) from exc


def decode(model: type[Record], data: bytes) -> dict:
    if len(data) > MAX_METADATA:
        raise HistoryError(f"Invalid {model.__name__} metadata: size limit exceeded.")
    try:
        return model.model_validate_json(data).model_dump()
    except (ValidationError, ValueError) as exc:
        raise validation_error(model, exc) from exc
