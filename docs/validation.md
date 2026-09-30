# Validation

## Version 0.2

On 2026-09-23 the complete local Windows suite passed **138 tests in 426.39 seconds**, with no
skipped cases. This includes source and bundle installation for Copilot CLI, Codex and Claude
Code, and a real MCP stdio checkpoint/commit/restore exchange through the frozen executable.
The tested runtime source is commit `de8a97a`; subsequent release-evidence edits change documentation
only. The suite includes real Perforce task
creation/preparation, type detection and escaped filenames; task continuation; edit/add/delete
membership restoration; partial metadata failure recovery; stale unopened-file protection;
altered-journal refusal; and offline completion cleanup that preserves later edits.

Additional cases cover independent branches, dirty branch switches, merge ancestry, clean
three-way merges, conflicts without writes, selective undo and recovery, labels/search/packing,
host event payloads and scoped checkpoints, observed new-file creation, user hook ownership,
custom policy preservation, updates and uninstall. A removed workspace does not block hooks in
another project. Source installation includes hook setup for supported hosts. Ruff, formatting,
diff whitespace and setup-skill validation passed. The wheel and source distribution built
successfully. The identifier audit found no matches across 2,155 source/archive entries, including
1,345 embedded Python modules. Dependency attribution remains intact.

The validated Windows archive is `p4-history-0.2.0-84934b4a7518fb48.zip`, SHA-256
`52fc472b9e942f5b285b3699ee1c53bfb44c400c7401050e408b1a3835e681cb`.
GitHub Actions remains disabled. Tests changed only isolated
client profiles and disposable Perforce workspaces; live client configuration was not changed.

The tests exercise hook payloads and registration, not complete interactive model sessions inside
each client. Host timeout/disable behaviour, external writers and arbitrary shell edits remain
explicit limits. Active hook task selection is workspace-wide. Move/integration restoration,
large binary streaming and base-revision changes remain unsupported.

## Version 0.1 evidence

Validated on Windows with Python 3.12, Git for Windows, the official MCP Python SDK 2.2,
and an isolated Perforce 2025.1 server. That suite contained **100 tests**, including thirteen
real Perforce/MCP/Windows-setup integration cases. On 2026-09-23, the full run passed all 93 cases
outside installer setup. After fixing PowerShell 5.1's handling of a default script-relative path,
all seven installer cases passed a targeted rerun against the final scripts and rebuilt bundle.
Ruff lint and formatting checks also passed. The source distribution and wheel build successfully;
the packaged CLI was launched from the wheel in an isolated environment. After the final cleanup
adjustment, all 14 fault/recovery regression tests were rerun and passed.

The Windows x64 PyInstaller folder bundle was built locally and its zip integrity checked.
The frozen executable completed checkpoint, milestone commit and restore through an actual MCP
stdio connection to a disposable Perforce workspace. Its packaged PowerShell installer was tested
with Copilot CLI, Codex and Claude Code 2.1.280 in isolated temporary profiles, covering check-only mode,
repeat setup, updates, unrelated server preservation, custom tool/enable settings, configuration
rollback after deliberately failed native registration, and rejection of a damaged executable.
The user's live AI-client configurations were not changed. The skill validation script passed.
This is local execution evidence, not a clean-machine or enterprise deployment certification.

Release hygiene checks cover tracked files, the wheel, source archive and Windows bundle,
including compressed Python modules inside the executable. Builder-specific account references
and workspace details were removed. Frozen distributions omit `direct_url.json` installation
provenance, which can expose the builder's local checkout path. The rebuilt bundle passed an MCP
checkpoint/commit/restore test. Repository ownership and existing Git commit authorship are
separate from distributable file contents; local runtime metadata still identifies its own
Perforce workspace as required for safe history and restore operations.

Source setup was exercised for all three hosts, including inference of the checkout from the
script location, refusing uncommitted runtime changes and starting MCP after moving the original
checkout. Both source and bundle setup preserve custom `AGENTS.md` and `CLAUDE.md` bytes, including
encoding and line endings. A separate bootstrap check hid uv from PATH and forced a managed Python
download: uv 0.12.5 installed privately, Python 3.12.14 downloaded, locked dependencies installed,
and the server CLI started. User, machine and process PATH values were unchanged.

The tests cover:

- Exact text, mixed line endings, empty files, Unicode names and binary bytes in Git objects.
- Independent changelist histories and deduplication of unchanged checkpoints.
- Dirty-state detection, historical diffs, and retention of objects after Git garbage collection.
- Restoring previous bytes while preserving the latest dirty state in a safety commit.
- Stale preview rejection after file edits, metadata changes or another history commit.
- Refusing changed actions, base revisions, file types, mappings and changelist membership.
- Interrupted restore recovery across a service restart, including preserving later external edits.
- Store locking and identity binding, traversal protection, symlink/junction rejection and limits.
- MCP tool discovery, structured results, error results and an actual stdio client/server exchange.
- Real Perforce edit/add/delete/reopen operations and filenames containing Perforce special characters
  in a disposable server. These tests use synthetic files and a temporary client only.
- The [engineering regressions](engineering.md): partial command failures, corrupted Git blobs,
  malformed persistence records, actual process termination during restore, locking across
  processes, read-only preflight and completion cleanup that preserves subsequent work.

The `doctor` command was also checked against an existing stream workspace using read-only
Perforce metadata calls. No production file contents were captured or changed.

The test suite does not prove atomicity against arbitrary concurrent editor writes or power loss.
Large depot performance, full Perforce integration-state restoration and host-specific enforcement
of pre-edit checkpoints remain outside this prototype's validation scope.

GitHub Actions is now intentionally disabled at repository level, the existing workflow was
disabled, and its YAML file was removed at the owner's request. Validation is run locally.
Linux execution and enterprise deployment validation remain outstanding.

Isolated Perforce integration tests are skipped unless `P4_HISTORY_TEST_P4D` points to a server
executable and `p4` is on PATH. Bundle cases need `P4_HISTORY_TEST_BUNDLE` pointing to an extracted
bundle. Source cases need uv and a Git checkout. Client setup cases need the corresponding CLI;
`P4_HISTORY_TEST_CLAUDE_BIN` can point to a temporary Claude CLI installation's executable directory.
Bundle tests invoke the skill's packaged script, so rebuild the bundle after changing setup code.
On machines where pytest's shared temporary directory has an existing ACL
problem, pass a fresh, task-specific `--basetemp` directory outside the Perforce workspace.
