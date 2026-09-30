# Changelog

## 0.2.0 — 2026-09-23

- Create numbered task changelists and checkpoint explicit files before opening them.
- Install scoped Copilot CLI and Claude Code checkpoint hooks alongside MCP, with ownership
  receipts, preserved custom policies, backups and removal support. Use `-SkipHooks` for MCP only.
- Preserve explicit milestone messages after automatic checkpoints. The MCP commit tool defaults
  to `allow_empty=true`; automatic checkpoints continue to deduplicate unchanged snapshots.
- Restore selected files or ordinary edit/add/delete changelist state with journalled recovery
  and safety copies of unopened files that would be overwritten.
- Create independent local branches, preview switches, merge with a common ancestor, and
  cherry-pick commits. Conflicts leave workspace content unchanged.
- Continue active task history across changelist moves without rewriting the original history.
- Add named milestones, revision selectors, history search/pagination, historical file reads,
  storage reporting and verified compaction that retains all history.
- Expose application operations through a validated JSON CLI interface.

Existing format-1 snapshots remain readable. New restore plans and journals require version 0.2.
Move/integration restoration, changed base revisions, large binary streaming and Codex hook
registration remain outside this release. See [workflows](docs/workflows.md) and
[validation evidence](docs/validation.md) for scope and test results.

## 0.1.0

Initial local snapshots, changelist history, diffs, guarded content restore and recovery,
with Windows source installation and an optional frozen runtime.
