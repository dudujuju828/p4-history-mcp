# Optional Windows bundle

The normal setup installs directly from the repository; it does not build an executable bundle.
A maintainer can optionally produce a Windows x64 zip for distribution to machines where Python
package downloads are inconvenient. Build it using Git, Python/uv and this repository:

```powershell
uv run --locked --group windows-build python scripts/build_windows.py
```

Run from the repository root. This uses the locked Python dependencies and PyInstaller. It
prints the bundle directory, zip location and SHA-256 digest under `dist/windows/`. Pass that
directory to the installer with `-BundlePath`.

The zip includes this skill and Python. Recipients extract it, then tell their agent to read
`.agents/skills/setup-p4-history/SKILL.md` in the extracted directory and install it for their
workspace. They do not need the repository, GitHub authentication or a Python development setup
if the zip is supplied through a trusted distribution channel.

Bundles currently have checksums but are not code-signed. A checksum supplied with the zip is
an integrity check, not publisher verification. Use a trusted distribution channel. Do not
disable Windows or organisational controls if they block an unsigned executable.
