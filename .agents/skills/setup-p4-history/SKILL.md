---
name: setup-p4-history
description: Install and register p4-history from its Git repository on Windows for GitHub Copilot CLI by default, with Codex and Claude Code as explicit options. Use for initial Perforce changelist-history setup, registration repair or upgrades. A prebuilt Windows bundle is optional.
---

# Set up p4-history on Windows

The result is a per-user installation with a workspace-specific MCP entry. History stays local.
Use the [installer](scripts/install.ps1); it owns the runtime, workspace validation, configuration
backup and native client registration. Prefer installing from a Git checkout. Git, the Perforce
CLI and the selected AI client must already work. The installer reuses uv or installs a pinned
version from Astral's official HTTPS installer without changing PATH or shell profiles. uv reuses
Python 3.12 or downloads it when needed, then installs the locked production dependencies.

1. Identify the intended workspace. Configure GitHub Copilot CLI by default, even when the agent
   performing setup runs in another host. Use `codex` or `claude-code` only when explicitly requested.
   If Copilot CLI is unavailable, report that prerequisite instead of switching clients silently.
   VS Code uses a different configuration format and is not supported by this installer yet.
2. Read applicable workspace instructions. Obtain the exact root, Perforce client, server endpoint
   and user from those instructions or the workspace's Perforce configuration. Run read-only
   `p4 -c <client> -p <server> -u <user> info` from that workspace and compare the root and stream.
   Preserve the configured endpoint including any `ssl:` prefix. Do not substitute a different
   ambient client. If the intended workspace cannot be determined, ask for its location.
3. If given a repository URL, clone that repository outside the Perforce workspace
   using the user's existing GitHub access, then read this skill from that checkout. Never copy
   access tokens into configuration or shell commands. If access is unavailable, report the
   GitHub access issue. Use the intended checked-out revision; do not
   discard local edits or silently pull a newer branch during installation. The installer exports
   committed runtime files, rejects uncommitted runtime changes and retains the copied source.
4. Invoke the installer using PowerShell's call operator and separately quoted arguments:

   ```powershell
   & '<checkout>/.agents/skills/setup-p4-history/scripts/install.ps1' -Workspace '<root>' -Client '<client>' -Port '<server>' -User '<user>'
   ```

   The source checkout is inferred from the script location; `-SourcePath` can select another
   checkout. `-HostClient codex` and `-HostClient claude-code` select the optional hosts. Claude
   registration uses `claude mcp add --scope user --transport stdio`; it does not create a shared
   `.mcp.json`. Copilot and Claude setup also installs user hooks scoped to the workspace,
   preserving existing custom hooks and permission policies. `-SkipHooks` opts out; Codex uses
   MCP instructions without hook registration. `-CheckOnly` prepares the source runtime and verifies the workspace, but does not
   register with any client; it may initialise an empty local history store. Keep connection
   arguments consistent on upgrades: they select the history store. Do not change execution
   policy or bypass enterprise blocks. Report the specific block if normal execution fails.
5. Report the server name, installed location, local storage path and whether the client must
   restart. After reconnection, call `p4_history_workspace` to confirm the intended workspace.
   Do not claim the current conversation has gained tools merely because configuration succeeded.

If the user supplies an extracted Windows bundle instead, pass `-BundlePath '<bundle-directory>'`
to the same installer. It requires no uv/Python setup and checks manifest hashes. Hashes detect
damage, not publisher identity; use a trusted bundle. See [optional packaging](references/source-build.md).

The MCP initialization instructions tell agents to `prepare` explicit files before editing, or
`begin` when files are already opened, then commit coherent milestones and before handoff. Hooks
prepare recognised file edits, track observed file creation, and checkpoint opened files before
shell tools and handoff. Arbitrary shell writes still need explicit file preparation; disabled
or timed-out hooks cannot provide protection. A local
commit does not submit to Perforce. Client tool permission settings still apply; setup does not
silently grant unrestricted tool access. Installation does not edit workspace instruction files.
If the user requests persistent workspace guidance, or the host needs it to follow the workflow:

- Read the complete existing instruction file and applicable instructions first. Existing
  `AGENTS.md`, `CLAUDE.md` and other custom instruction files belong to the workspace's users.
- Append the marked workflow section from the repo's `integrations/AGENTS.md`, keeping exactly
  one `<!-- p4-history:start -->` / `<!-- p4-history:end -->` pair. If that section already exists,
  reuse it; update only that section when necessary. Do not duplicate it.
- Preserve all bytes outside that section, including encoding and line endings. Never replace
  the file with the repo's example, regenerate the full file or reorder existing instructions.
- Follow the workspace's normal Perforce checkout/changelist rules and project-edit authorisation.
  If existing instructions conflict, report the conflict instead of silently changing their policy.

Re-running setup reuses the installed revision or bundle and updates the same managed MCP entry. If an entry was
customised outside setup, installation stops with that entry preserved, including tool restrictions.
Run one installer at a time and avoid simultaneous client-config edits. Different
workspaces get different names. Backups are adjacent to the client config. Old installed runtimes
are retained for rollback. Before deleting a runtime or removing its MCP entry, use that runtime's
`uninstall-hooks --host copilot` or `uninstall-hooks --host claude-code` with the original connection
arguments. It removes only owned entries and leaves custom policies and local history intact.
Then remove MCP using `mcp remove <server-name>`; for Claude add `--scope user`. Removing MCP alone
does not disable installed host hooks. The MCP `automatic_checkpoints` tool can disable automatic
checkpoints without uninstalling.
