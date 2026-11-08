"""Automatic, commit-based updater for ChromeOS VM Studio.

Updates the installed Python app from this project's public GitHub main branch.
Guest disks and VM profiles live outside the Python environment and are never
touched by this module.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

OWNER = "ashrworkman97-debug"
REPOSITORY = "ChromeOS_Emulator"
BRANCH = "main"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/commits/{BRANCH}"
ARCHIVE_URL = f"https://codeload.github.com/{OWNER}/{REPOSITORY}/tar.gz/{{sha}}"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 500


class UpdateError(RuntimeError):
    """An update could not be safely downloaded or installed."""


@dataclass(frozen=True)
class UpdateResult:
    status: str  # "updated", "current", or "failed"
    message: str
    commit: str | None = None


def install_directory() -> Path:
    configured = os.environ.get("CHROMEOS_VM_INSTALL_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local/opt/chromeos-vm-studio"


def metadata_path() -> Path:
    return install_directory() / "install.json"


def _read_metadata() -> dict[str, Any]:
    try:
        value = json.loads(metadata_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_metadata(value: dict[str, Any]) -> None:
    path = metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\\n", encoding="utf-8")
    temporary.replace(path)


def _request(url: str, *, limit: int, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "ChromeOS-VM-Studio-Updater",
        "Accept": accept,
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not contact GitHub: {exc}") from exc
    if len(data) > limit:
        raise UpdateError("GitHub update response exceeded the safety size limit.")
    return data


def latest_commit() -> str:
    try:
        payload = json.loads(_request(API_URL, limit=1024 * 1024))
        sha = payload["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UpdateError("GitHub returned an invalid latest-commit response.") from exc
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise UpdateError("GitHub returned an invalid commit identifier.")
    return sha


def _safe_extract(archive_bytes: bytes, destination: Path) -> Path:
    """Extract only regular files under one archive root; reject links/traversal."""
    try:
        archive = tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"Downloaded update archive is invalid: {exc}") from exc

    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_FILES:
            raise UpdateError("Update archive has an unexpected number of entries.")
        roots = {PurePosixPath(member.name).parts[0] for member in members
                 if PurePosixPath(member.name).parts}
        if len(roots) != 1:
            raise UpdateError("Update archive does not contain one repository root.")
        root_name = next(iter(roots))
        total = 0
        files_written = 0
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or not member_path.parts:
                raise UpdateError("Update archive contains an unsafe path.")
            if member_path.parts[0] != root_name:
                raise UpdateError("Update archive has an unexpected top-level path.")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise UpdateError("Update archive contains a link or special file; refusing extraction.")
            relative = Path(*member_path.parts[1:])
            if not relative.parts:
                continue
            target = destination / relative
            if target.resolve().parent != destination.resolve() and destination.resolve() not in target.resolve().parents:
                raise UpdateError("Update archive path escapes its staging folder.")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            files_written += 1
            if total > MAX_EXTRACTED_BYTES or files_written > MAX_ARCHIVE_FILES:
                raise UpdateError("Update archive exceeds the extraction safety limits.")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UpdateError("Could not read a file from the update archive.")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o644)

    if not (destination / "pyproject.toml").is_file() or not (destination / "src/chromeos_vm").is_dir():
        raise UpdateError("The downloaded archive is missing the application source.")
    return destination


def apply_update(sha: str) -> None:
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise UpdateError("Refusing to install an invalid commit identifier.")
    with tempfile.TemporaryDirectory(prefix="chromeos-vm-update-") as temp:
        stage = Path(temp)
        archive = _request(ARCHIVE_URL.format(sha=sha), limit=MAX_ARCHIVE_BYTES,
                           accept="application/vnd.github+json")
        source_dir = stage / "source"
        source_dir.mkdir()
        _safe_extract(archive, source_dir)
        command = [
            sys.executable, "-m", "pip", "install",
            "--no-deps", "--no-build-isolation", "--upgrade", "--force-reinstall",
            str(source_dir),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"Could not run the package installer: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "pip returned an error").strip()
            raise UpdateError("Update installation failed: " + detail[-3000:])


def check_for_updates(*, force: bool = False, cooldown_hours: int = 12) -> UpdateResult:
    metadata = _read_metadata()
    now = datetime.now(timezone.utc)
    if not force:
        last = metadata.get("last_check")
        try:
            last_checked = datetime.fromisoformat(last)
            if last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=timezone.utc)
            if (now - last_checked).total_seconds() < cooldown_hours * 3600:
                return UpdateResult("current", "Automatic update check was performed recently.",
                                    metadata.get("commit"))
        except (TypeError, ValueError):
            pass

    try:
        sha = latest_commit()
        if sha == metadata.get("commit"):
            metadata.update({"repository": f"{OWNER}/{REPOSITORY}", "branch": BRANCH,
                             "last_check": now.isoformat()})
            _write_metadata(metadata)
            return UpdateResult("current", "ChromeOS VM Studio is up to date.", sha)

        apply_update(sha)
        # Write the installed marker only after pip completed successfully.
        _write_metadata({"repository": f"{OWNER}/{REPOSITORY}", "branch": BRANCH,
                         "commit": sha, "last_check": now.isoformat()})
        return UpdateResult("updated", "A new version was installed. Restart ChromeOS VM Studio to use it.", sha)
    except UpdateError as exc:
        return UpdateResult("failed", str(exc), metadata.get("commit"))


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check for ChromeOS VM Studio updates.")
    parser.add_argument("--force", action="store_true", help="ignore the automatic check cooldown")
    args = parser.parse_args(argv)
    result = check_for_updates(force=args.force)
    print(result.message)
    return 0 if result.status in {"current", "updated"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
