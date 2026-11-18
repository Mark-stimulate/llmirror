"""Build release archives from committed sources and the Windows executable."""
import argparse
import hashlib
import importlib.metadata
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release"


def release_version():
    version = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', (ROOT / "app.py").read_text(encoding="utf-8"), re.M)
    if not version:
        raise SystemExit("Missing numeric VERSION in app.py")
    expected = f"v{version[1]}"
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and os.environ.get("GITHUB_REF_NAME") != expected:
        raise SystemExit(f"Tag must match the application version: {expected}")
    return expected


def source_files():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    allowed = {".gitignore", ".gitattributes", "LICENSE", "README.md", "icon.ico",
               "requirements.txt", "start.sh", "start.bat", "windows-version.txt",
               "llmirror-service.xml", "service.ps1", "tools/BUILD.md",
               "tools/build_windows.py", "tools/package_release.py", "tools/requirements-build.txt"}
    return {name: (ROOT / name).read_bytes() for name in names if
            name in allowed or re.fullmatch(r"[a-z_]+\.py", name) or
            re.fullmatch(r"static/[a-z0-9_-]+\.(html|css|js|svg)", name)}


def windows_licenses():
    licenses = {}
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        licenses["licenses/Python-LICENSE.txt"] = python_license.read_bytes()
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            continue
        for file in dist.files or []:
            if file.is_absolute() or ".." in file.parts or not any(
                part.lower().startswith(("license", "copying", "notice")) for part in file.parts
            ):
                continue
            path = Path(dist.locate_file(file))
            if path.is_file() and file.suffix.lower() not in {".py", ".pyc"}:
                licenses[f"licenses/{name}/{file.as_posix()}"] = path.read_bytes()
    return licenses


def archive_release(version):
    OUTPUT.mkdir(exist_ok=True)
    sources = source_files()
    windows = OUTPUT / f"llmirror-{version}-Windows-x64.zip"
    linux = OUTPUT / f"llmirror-{version}-Linux.tar.gz"
    with zipfile.ZipFile(windows, "w", zipfile.ZIP_DEFLATED) as archive:
        windows_sources = {name: data for name, data in sources.items() if name != "start.sh"}
        windows_sources["llmirror.exe"] = (ROOT / "dist/llmirror.exe").read_bytes()
        windows_sources.update(windows_licenses())
        for name, data in sorted(windows_sources.items()):
            archive.writestr("llmirror/" + name, data)
    with tarfile.open(linux, "w:gz") as archive:
        for name, data in sorted(sources.items()):
            if name in {"start.bat", "service.ps1", "llmirror-service.xml", "windows-version.txt", "icon.ico"} or name.startswith("tools/"):
                continue
            if name == "start.sh":
                data = data.replace(b"\r\n", b"\n")
            entry = tarfile.TarInfo("llmirror/" + name)
            entry.size = len(data)
            entry.mode = 0o755 if name == "start.sh" else 0o644
            archive.addfile(entry, io.BytesIO(data))
    (OUTPUT / "SHA256SUMS.txt").write_text(
        "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in (windows, linux)), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    version = release_version()
    if not args.check:
        archive_release(version)
