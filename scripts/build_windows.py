"""Build a Windows x64 folder bundle and zip, using the locked build environment."""

import hashlib
import importlib.metadata
import json
import platform
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if (
        sys.platform != "win32"
        or struct.calcsize("P") != 8
        or platform.machine().lower() not in {"amd64", "x86_64"}
    ):
        raise SystemExit("Build this bundle on Windows x64 with an x64 Python interpreter.")
    version = importlib.metadata.version("p4-history-mcp")
    work = ROOT / "build" / uuid.uuid4().hex
    output = work / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--console",
            "--name",
            "p4-history",
            "--distpath",
            str(output),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(work),
            "--paths",
            str(ROOT / "src"),
            "--recursive-copy-metadata",
            "mcp",
            "--copy-metadata",
            "p4-history-mcp",
            str(ROOT / "scripts/windows_entry.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    bundle = output / "p4-history"
    shutil.copytree(ROOT / ".agents" / "skills", bundle / ".agents" / "skills")
    shutil.copytree(ROOT / "integrations", bundle / "integrations")
    shutil.copytree(ROOT / "docs", bundle / "docs")
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    # Installation provenance can contain the builder's username and local checkout path.
    # It is unnecessary at runtime and must not be distributed with frozen package metadata.
    for provenance in bundle.rglob("*.dist-info/direct_url.json"):
        provenance.unlink()
    # Include dependency licence texts alongside the frozen runtime metadata.
    notices = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        notices.append({"name": name, "version": distribution.version})
        for item in distribution.files or []:
            if any(
                part.lower().startswith(("license", "licence", "copying")) for part in item.parts
            ):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    target = bundle / "licenses" / name / Path(*item.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    (bundle / "build-packages.json").write_text(json.dumps(notices, indent=2), encoding="utf-8")
    files = {
        path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    }
    manifest = {"format": 1, "version": version, "platform": "windows-x64", "files": files}
    (bundle / "bundle.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    identity = hashlib.sha256((bundle / "bundle.json").read_bytes()).hexdigest()[:16]
    destination = ROOT / "dist" / "windows" / f"p4-history-{version}-{identity}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SystemExit(f"Bundle already exists: {destination}")
    shutil.move(str(bundle), destination)
    archive = shutil.make_archive(str(destination), "zip", destination.parent, destination.name)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(f"{digest}  {Path(archive).name}\n", encoding="utf-8")
    print(json.dumps({"bundle": str(destination), "zip": archive, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
