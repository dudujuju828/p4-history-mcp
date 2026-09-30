# p4-history-mcp

**Local Git-backed history for pending Perforce changelists, designed for AI agents.**

Perforce remains the source of truth for submitted work. This MCP server saves the intermediate
steps inside a pending changelist: checkpoint before edits, commit milestones, inspect the history,
and restore earlier file contents. A history commit does **not** submit a changelist.

The programmer connects the server once. The agent calls the history tools as it works and
**automatically commits completed milestones** without waiting for individual commit requests.
All snapshots stay on the local machine, outside the Perforce workspace. The software's GitHub
repository contains only this tool's source code; the server has no remote or upload feature.

## Local development workflows

- Create a task changelist and prepare explicit files: save existing bytes before opening files
  for edit or add. File types for additions follow Perforce's preview and typemap.
- Automatic checkpoints through Copilot CLI and Claude Code hooks, alongside agent-written
  milestone commits. New files created by recognised host tools are tracked after creation.
- Status, searchable and paginated history, text diffs, binary summaries and historical file reads.
- Named milestones, `HEAD~N`, independent local branches, previewed branch switching, three-way
  merges and cherry-picks. Conflicts leave workspace files untouched.
- Restore a whole changelist or selected files. Content mode changes bytes; changelist mode also
  restores ordinary `edit`/`add`/`delete` membership and file types at unchanged base revisions.
- Continue a task's active history when its files move to a fresh changelist, retaining old history.
- Safety checkpoints, validated plans, content integrity checks and durable restore recovery.
- Storage usage and verified Git packing without deleting history.

This is a local development tool with explicit boundaries. It does not submit, shelve, sync,
change streams, resolve integrations or upload history. Moves and integration actions can be
captured and inspected but are not restored. Unsaved editor buffers are never captured.
Only explicitly prepared or already-opened files are protected; shell commands that write
unopened files still require the agent to prepare their scope first.

See [workflows and tool reference](docs/workflows.md) for examples and exact restore semantics.

## Windows setup with an agent

Point your AI at this repository and ask it to install the tool:

> Install p4-history from this repository for my Perforce workspace.
> Follow the repository's setup skill.

The agent clones the repository, reads the
[setup skill](.agents/skills/setup-p4-history/SKILL.md), identifies the workspace and runs its
installer. **GitHub Copilot CLI is the default**. Ask explicitly for Codex or Claude Code to use
`-HostClient codex` or `-HostClient claude-code`. Claude Code registration uses user scope.

Git, the Perforce CLI, an authenticated Perforce workspace and your AI client must already work.
The installer handles uv and Python if needed, using Astral's official installer and Python
downloads. No administrator access or persistent PATH/profile changes are needed. Initial setup
needs access to GitHub and Python package/download services; normal MCP startup uses the installed
runtime directly, without updating dependencies or contacting a package registry.

The runtime is installed under `%LOCALAPPDATA%\Programs\p4-history` from the checked-out Git
commit, with `uv.lock` enforced and production dependencies only. It retains an independent copy
of the committed source and installs the package non-editably. Moving or deleting the original
checkout does not remove the installed server. Re-run setup from a newer checkout to upgrade.
Restart/reconnect the AI client once after setup, then verify with `p4_history_workspace`.

The installer verifies the root/client and uses each host's native MCP registration commands.
Each workspace connection gets a separate server name. Existing config is backed up, unrelated
servers are preserved, and customisations to a managed entry are never overwritten automatically.
Run only one installer at a time and avoid editing the client config during installation.
History remains in its separate local data directory; upgrading the executable does not move it.

Copilot CLI and Claude Code setup also installs scoped user hooks. Existing custom hooks and
permission policies are preserved; customised owned entries are refused. `-SkipHooks` installs
MCP only. Codex currently uses MCP instructions without hook registration. Hook configuration
requires a client version supporting the documented events; restart the client after changes.
The installer never edits workspace `AGENTS.md` or `CLAUDE.md`.

### Optional bundle

A prebuilt Windows x64 zip remains available as an alternative for managed distribution. It
contains Python and dependencies, so recipients do not need package downloads during setup.
Pass `-BundlePath <extracted-directory>` to the installer. Source installation does **not** build
this bundle. Maintainers can build a zip separately:

```powershell
uv run --locked --group windows-build python scripts/build_windows.py
```

The command prints the bundle directory, zip and checksum under `dist/windows/`. It includes the
setup skill, dependency licence texts and a file manifest. The current bundle is **unsigned** and
has been tested locally. Hashes verify file integrity, not publisher identity.

Tool permission settings still apply. Setup does not grant blanket tool approvals, and MCP
instructions cannot enforce that every host checkpoints before editing. Hooks cover recognised
file tools and checkpoint opened files before shell calls and handoff. Disabled hooks, host
timeouts, external editors and unknown tools can bypass that coverage.
VS Code registration is not currently included.
Setup leaves existing workspace `AGENTS.md`, `CLAUDE.md` and other instruction files untouched.
If persistent workspace guidance is added later, the skill requires one marked, additive section
and preservation of all existing custom instructions.

## Manual development connection

Requires Python 3.12+, Git, the Perforce CLI, a logged-in Perforce session and an existing workspace.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if it is not already available.

Clone this tool, then run from its directory:

```sh
uv sync --locked
uv run p4-history doctor --workspace /absolute/workspace/root --client your-client
```

Add an MCP server entry to your AI client's configuration. This is the common JSON form; hosts
with a different format need the same command and arguments:

```json
{
  "mcpServers": {
    "p4-history": {
      "command": "uv",
      "args": [
        "run", "--locked", "--directory", "/absolute/path/to/p4-history-mcp",
        "p4-history", "serve",
        "--workspace", "/absolute/workspace/root",
        "--client", "your-client"
      ]
    }
  }
}
```

Use absolute Windows paths with forward slashes (for example `D:/work/project`) or escaped
backslashes in JSON. Add `--port` and `--user` if the workspace's Perforce configuration does not
already select the right server and user. Authentication uses your existing Perforce ticket;
this server does not collect passwords.

If persistent workspace instructions are wanted, append [the instruction snippet](integrations/AGENTS.md)
as one marked section in the existing instruction file. Preserve all existing content and reuse
the same section on subsequent setup; never replace the file with the example.
The server also supplies the workflow during MCP initialization. MCP instructions influence an
agent's behaviour; they cannot observe arbitrary editor or shell writes. Install the supported
host hooks for additional protection, and keep explicit preparation in the agent workflow.

## Agent workflow

```text
p4_history_workspace()
p4_history_create_change(description="Handle empty input")  # if a new task is needed
p4_history_prepare(change="12345", paths=["src/parser.cpp"])
edit and validate the work
p4_history_commit(change="12345", message="Handle the empty-input case")
p4_history_log(change="12345")
p4_history_diff(change="12345", before="HEAD", after="WORKSPACE")
p4_history_plan_restore(change="12345", revision="HEAD~1", paths=["src/parser.cpp"])
inspect the returned file operations
p4_history_apply_restore(plan_id="<preview id>")
```

`default` is also accepted, but a numbered changelist gives each task a clearer identity.
Use `prepare` when adding files to the task, and `begin` when all intended files are already open.
Unchanged automatic checkpoints return the existing commit ID. Explicit MCP milestone commits
retain their message even when the preceding checkpoint already saved the same bytes; set
`allow_empty=false` to deduplicate those too. To compare two snapshots, give both commit IDs
to `diff`. `log` and diffs between stored commits do not need a live Perforce connection.
`label(change, name)` pins a milestone for later use as `@name`.

## Storage and recovery

Storage defaults to the OS application-data directory under `p4-history/<workspace-key>/`.
`doctor` reports the exact path. You can choose another directory with `--state-dir`; it must
remain outside the Perforce workspace. Keep the same connection arguments between sessions
because they select the local store. One store is intended for one workspace connection.

Each snapshot is a real Git commit in a bare repository, referenced by
`refs/heads/changelists/<number>` (a symbolic reference after branches are created).
A manifest records workspace identity, mappings, changelist
description, depot paths, actions, types, base revisions and content blob IDs. Git stores the
exact bytes, including line endings and binary contents, without worktree filters. This is a
sparse record of opened files, not a mirror of the full depot and not a complete Perforce backup.

Restore locks this server's store, checks the preview against current bytes and Perforce state,
saves a safety commit, writes a durable recovery journal, and replaces changed files individually.
It then verifies the result and appends a restore commit. It does not rewrite old commits.
Content restore preflights destinations before the first write. Completion is recorded before journal
cleanup, so interrupted cleanup can be retried without undoing the completed restore or later edits.
If interrupted, call `p4_history_recover_restore`. Recovery restores the safety snapshot only
when affected files still have either the before or target bytes. A newer edit blocks recovery
with an actionable error rather than being overwritten. History and stored diffs remain readable.

Close or pause other writers while restoring. The lock coordinates instances using the **same
store**, not editors, Perforce commands or differently configured stores. The operation is not
atomic across files, and check-then-write races with unrelated processes cannot be eliminated.
The recovery journal and safety commit cover interruptions; they are not an OS transaction or a
power-loss guarantee. Changelist restore may make affected files writable to apply an explicit
Perforce action transition. Timestamps, ACLs,
alternate data streams and other filesystem metadata are not versioned.

Resource limits: 2,000 opened files, 64 MiB per file, 256 MiB per snapshot and 8 MiB metadata;
symlinks, junctions and ambiguous or nonportable paths are rejected. Text diffs are limited to
64 KiB per input, 4,096 combined input lines and 100,000 output characters per request; larger,
binary and other encodings get summaries. Large stored file blobs are not loaded just for a summary.
Each snapshot is checked in two passes for concurrent changes. Streaming larger asset changelists,
automatic retention and cross-machine history remain outside this release. Explicit compaction
packs objects but does not expire commits, labels, branches or safety snapshots.

## Removing an installation

Disable automatic checkpoints through `p4_history_automatic_checkpoints(change=null)` if needed.
Before deleting an installed runtime, run its `uninstall-hooks --host copilot` or
`uninstall-hooks --host claude-code` command with the same connection arguments used for setup.
This removes only owned hook entries and preserves custom policies and history. Then remove the
MCP entry with the client's native command. Removing MCP alone does not uninstall host hooks.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use temporary workspaces and real local Git repositories. Optional real-Perforce tests use
an isolated temporary `p4d` server when `P4_HISTORY_TEST_P4D` points to its executable. They never
use a developer's existing client or server. See [the design notes](docs/design.md) for scope,
decisions and the next implementation stages, and [engineering ownership](docs/engineering.md)
for component responsibilities, failure guarantees and regression coverage.

## Licence

Released under the [MIT License](LICENSE).
