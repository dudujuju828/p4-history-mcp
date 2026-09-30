# Developer workflows

All `change` arguments accept a numbered pending changelist or `default`. Prefer numbered
changelists for tasks. Commits, labels and experimental branches stay local. Opening or moving
files updates ordinary Perforce metadata; nothing submits, shelves or uploads local history.

## Start, protect and finish a task

1. Inspect `p4_history_workspace` and choose the intended changelist, or use
   `p4_history_create_change(description)` to create one.
2. Call `p4_history_prepare(change, paths)` with explicit workspace-relative file paths. It
   validates the entire selection, stores pre-edit bytes, then opens files. Other changelists,
   missing depot revisions, symlinks and ambiguous mappings are refused. Existing untracked files
   can be added; Perforce previews their file types and ignore rules. There is no recursive scan.
3. Edit and validate. Prepare additional files before changing them. `begin` checkpoints files
   already opened and selects the task for installed hooks.
4. Commit coherent milestones with `p4_history_commit(change, message)`. Include the change and
   validation in the message. Automatic checkpoints deduplicate; explicit MCP milestone commits
   retain messages even when bytes match a preceding checkpoint. `allow_empty=false` opts into
   deduplication for those commits. The CLI/application commit default remains false, so pass
   `allow_empty=true` for a validation-only milestone through the JSON CLI. Inspect `status` before handoff;
   its `uncheckpointed_paths` reports opened files absent from the current local checkpoint.

If preparation fails after opening some files, the baseline remains available. Inspect the
reported changelist, then retry the same explicit paths. Preparation never replaces file contents.
It cannot reconstruct bytes changed before the first checkpoint.

## Automatic checkpoints

The Windows installer registers user hooks for Copilot CLI and Claude Code, unless `-SkipHooks`
is requested. It does not change workspace instruction files or blanket permission settings.
Codex uses the same MCP tools and agent instructions; its hooks are not configured in this release.

Hooks prepare recognised `edit`, `write`, `create` and equivalent file tools before they execute.
For a new path, they record its absence and checkpoint the task before creation, then add and
capture the resulting file after the tool completes. A later hook can finish this capture if the
host omitted the post event. Existing custom hooks still run normally.

Before shell tools, hooks checkpoint the task's opened files. They do not parse shell programs
to infer every path those programs might write. The agent must prepare that scope explicitly.
Handoff hooks save remaining opened changes. Hooks never execute commands from the input payload.

The active task is workspace-wide. Concurrent sessions must coordinate their selected changelist;
a file already opened in another changelist is refused instead of silently moved. Semantic
milestones remain the agent's responsibility. External editors, unsupported tools, disabled hooks,
and host timeout behaviour remain outside enforcement. In particular, Copilot hook timeouts can
fail open. A hook is additional protection, not a workspace lock.

`p4_history_automatic_checkpoints(change=null)` disables automatic checkpoints. Selecting a change
with that tool explicitly re-enables them; ordinary begin/prepare preserve a disabled setting.

## Find and inspect work

| Request | Tool |
| --- | --- |
| Compare current work with its checkpoint | `status(change)` or `diff(change)` |
| Find a milestone by message | `log(change, query="parser", limit=20, offset=0)` |
| Name a checkpoint | `label(change, name="before-refactor")` |
| List names | `labels(change)` |
| Read an older file | `show(change, path, revision="@before-refactor")` |
| Compare historical snapshots | `diff(change, before="HEAD~2", after="HEAD")` |
| Inspect storage | `storage()` |
| Verify and pack objects | `storage(compact=true)` |

Names use 1-64 lowercase letters, digits, underscores or hyphens and cannot be overwritten.
Revisions accept `HEAD`, `HEAD~N`, a commit ID of at least seven hex digits, `@milestone`, or
`branch:name`. Log, labels, branches, show and comparisons between stored revisions work offline.
`show` returns UTF-8 or base64 for files up to 64 KiB; larger files get metadata. Text merging is
limited to UTF-8 inputs of 1 MiB each; binary conflicts require explicit resolution.

## Undo and restore

Call `plan_restore(change, revision, paths=null, mode="content")`, inspect the returned file
operations, then call `apply_restore(plan_id)`. Existing authorisation for an undo request still
applies; the two-step interface makes stale-state detection possible. A changed preview must be
recreated. `paths` limits the operation to selected files and preserves unrelated work.

Content mode requires unchanged selected membership, actions, types and base revisions.
Changelist mode additionally restores ordinary edit/add/delete actions, types and membership:

- An added file absent from the target is closed and removed; its bytes remain in the safety commit.
- A tracked edit absent from the target is closed with its current bytes retained. That old
  snapshot did not record the unopened file's bytes, so the tool does not invent a depot baseline.
- A target file currently unopened can be reopened. Its existing bytes are protected in a
  separate safety snapshot before replacement.
- Base revision changes, moves, integrations and resolve state are refused. No implicit sync occurs.
- The current changelist description is retained.

Every apply saves current work and journals intended changes before replacing files. Interrupted
content or changelist restores use `recover_restore()`. Recovery checks actual bytes and Perforce
state, refuses newer edits, and restores the safety state. Completed operations record completion
before cleanup, so later edits survive a cleanup retry. Pause other writers while restoring.

## Try an alternative approach

Create `branch(change, name="experiment")`, then `plan_switch(change, name="experiment")` and
apply its plan. The original branch is named `main`. Switching saves dirty source work first;
subsequent commits advance only the active branch. These are local Git branches, not Perforce streams.
Use `mode="changelist"` when edit/add/delete membership differs between the two branches.

To bring back improvements, use `plan_merge(change, revision="branch:experiment", mode="merge")`,
or `mode="cherry-pick"` to apply one commit's change relative to its parent. Merge uses a common
ancestor and records merge ancestry; cherry-pick applies a single-parent delta. Optional `paths`
limits the scope. Selected Perforce metadata must match in the base, local and incoming snapshots.
Conflicts return paths and reasons without writing files or conflict markers. On success, inspect
and apply the returned `plan.id` through `apply_restore`.

## Move a task to another changelist

Checkpoint the source, create a fresh destination, then call
`continue_task(source, destination, move_files=true)`. This reopens exactly the task's files and
links the active source history into the destination. Changed bytes remain intact. If Perforce
partly completes the reopen, retry: the source/destination union is checked against the retained
source snapshot. An unrelated destination file prevents the operation.

With `move_files=false`, the files must already have moved through ordinary Perforce tools.
Only active source ancestry continues; its other branches and named milestones remain under the
source changelist. Old history is never rewritten. Submitted changelist history remains readable,
but capturing or restoring still requires a pending changelist. Submission tracking is not automatic.

## CLI and removal

Besides `serve` and `doctor`, the CLI exposes the same application operations with `call
--operation <name> --arguments '<JSON object>'`, followed by the usual workspace/client connection
arguments. Operation names omit the `p4_history_` prefix; `inspect` corresponds to `workspace`.
The CLI uses an explicit allowlist and never evaluates supplied JSON as commands.

Run `uninstall-hooks --host copilot` or `uninstall-hooks --host claude-code` with the original
connection arguments before removing a runtime. Owned entries are removed; custom settings,
config backups and history remain. This can run even if the old workspace directory is gone.
Then remove the MCP entry through the host's native command. A customised owned hook is preserved
and reported for manual reconciliation.

References: [Copilot hook contracts](https://docs.github.com/en/copilot/reference/hooks-reference),
[Claude Code hook contracts](https://code.claude.com/docs/en/hooks),
[Perforce keep-workspace revert](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_revert.html).
