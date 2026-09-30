# Design notes

## Product contract

A programmer should connect a local MCP server once, then ask an agent to work normally.
The agent captures the starting state and commits meaningful intermediate steps inside a pending
Perforce changelist. These local commits must remain distinct from a Perforce submit.

History lives locally for this first version. The GitHub repository hosts the software
only. No GitHub authentication, history repository creation or upload belongs in the server.

## Decisions in the prototype

| Concern | Choice | Reason |
| --- | --- | --- |
| History engine | Local bare Git repository | Reuse content addressing and commit ancestry |
| Snapshot unit | Opened files in one pending changelist | Avoid scanning or copying an entire depot |
| Identity | Root, client, user, server, stream and mapping recorded | Detect restoring into a different workspace state |
| Git access | Plumbing commands with byte input | Avoid staging, checkout, filters and hooks in a Perforce workspace |
| Perforce access | Pinned reader and separate explicit metadata operations | Reuse tickets; never implicitly submit, shelve or sync |
| AI interface | MCP stdio tools plus agent instructions | Small setup burden and explicit tool schemas |
| Baseline | Current bytes before agent editing | Preserve pre-existing programmer changes |
| Restore | Preview, state check, safety commit, journal, content replacement | Make overwrites reviewable and interrupted operations recoverable |
| Concurrency | Per-store process lock, optimistic state checks | Coordinate this tool without pretending to lock the editor |

Git's object model is used internally; this is not `git p4` and does not import submitted depot
history. A commit contains `manifest.json` and a `files/` tree whose filenames are blob IDs.
The manifest maps them back to actual depot and workspace paths. Every referenced blob is in
the Git tree, so Git reachability and future packing work normally. Each changelist gets its
own ref and root commit; unchanged state is deduplicated. Restores append a new state. Creating
branches converts the changelist ref into an alias for a real branch ref. Each experimental
branch advances independently; switching protects dirty source work before activating its target.
Clean merges record both parents. Proposed results and auxiliary safety snapshots are pinned
under private refs, so packing cannot discard their content.

Commit capture stores descriptions but does not rewrite them during restore. Snapshotting an
integration records the ordinary opened-file metadata, not Perforce's full integration or
resolve graph. It must not be presented as a fully restorable integration transaction.

The default changelist is supported as a convenience but its identity can span unrelated tasks.
`continue_task` links active ancestry into a fresh destination and can reopen the exact task
file set. Older manifest changelist IDs are accepted only through explicit lineage refs and
normalised for that destination. Other source branches and labels remain at the source. Submitted
changelists retain their local history; further capture and restore require a pending changelist.

## Automatic behaviour

The confirmed product policy is checkpoint before editing, **commit automatically at completed
milestones**, and checkpoint before handoff. The agent should not wait for the programmer to request
each local commit. A milestone is a coherent fix, feature or refactor; its message records the work
and validation. An explicit user instruction can override the cadence. Copilot CLI and Claude Code
hooks prepare recognised file edits and checkpoint opened files before shell tools and handoff.
Observed new-file creation has a persisted intent so a later hook can finish capture after a
missing post event. Hooks never interpret arbitrary shell programs as a safe file scope, and
disabled hooks or host timeouts cannot provide enforcement. Codex retains instruction-based use.

The [engineering ownership document](engineering.md) defines the module boundaries and the
fault handling added after the initial prototype. Existing snapshot format 1 and MCP tool names
remain compatible. Legacy recovery journals without a phase are treated as interrupted operations.

The initial baseline covers current opened files, including modifications made before the agent
started. Preparation saves existing bytes before opening explicitly selected additional files.
Unopened writes cannot be retrospectively given a genuine pre-edit baseline.

## Restore modes

Content restore composes selected historical file contents into the current snapshot, leaving
unselected files unconstrained. Changelist restore has a separate typed plan and journal because
it also owns Perforce action transitions. It validates mapping and have revisions, protects bytes
of currently unopened targets, and uses keep-workspace metadata operations plus `WorkspaceFiles`
for content replacement. It supports edit/add/delete membership and type changes. Reverted task
adds are removed; tracked edits closed by a restore keep their bytes because an older sparse
snapshot cannot supply an unopened file's original content. Descriptions remain current.

Recovery validates the journal against independently hashed snapshots and accepts only known
before/target bytes and permitted intermediate Perforce states. A newer edit or base revision
blocks recovery. This is not a transaction with the Perforce server or unrelated file writers.

## Further work

Move pairs and integration/resolve state require a richer Perforce state model. Base-revision
changes need an explicit sync and conflict workflow. Large binary streaming, configurable storage
retention, richer text encodings, automatic submitted-changelist tracking and multi-session task
selection remain further work. Current compaction packs objects without deleting any history.

No remote history or team collaboration is included in the current scope.

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): the official SDK used
  for protocol transport, tool registration and initialization instructions.
- [Git commit-tree](https://git-scm.com/docs/git-commit-tree): create commits from trees with
  explicit parents without needing a worktree.
- [Perforce global options](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/global.options.html):
  explicit connection options and structured Python output.
- [Perforce shelves](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_shelve.html):
  useful Perforce snapshots, with different semantics from an append-only local commit graph.
