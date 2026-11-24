"""Portable, defensive VM backup and restore for ChromeOS VM Studio."""
from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

from .manager import (
    MAX_DISK_GIB, VMConfig, VMError, VMManager, disk_geometry, validate_name,
)

BACKUP_FORMAT = "chromeos-vm-studio-backup"
BACKUP_VERSION = 1
METADATA_LIMIT = 64 * 1024
MAX_BACKUP_BYTES = 2 * MAX_DISK_GIB * 1024**3 + 2 * METADATA_LIMIT
ALLOWED_MEMBERS = frozenset({"manifest.json", "vm.json", "disk.raw", "disk2.raw"})
COPY_CHUNK_BYTES = 1024 * 1024
SPARSE_BLOCK_BYTES = 4096


def _vm_directory(manager: VMManager, name: str) -> Path:
    directory = manager.vm_path(name)
    if directory.is_symlink() or directory.resolve().parent != manager.home.resolve():
        raise VMError("The VM directory is outside the private storage folder; refusing backup access.")
    return directory.resolve()


def _regular_file(path: Path, label: str, expected_size: int | None = None) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise VMError(f"Could not inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VMError(f"{label} must be a regular, non-symlink file.")
    if info.st_nlink != 1:
        raise VMError(f"{label} has additional hard links; refusing to copy shared data.")
    if expected_size is not None and info.st_size != expected_size:
        raise VMError(
            f"{label} is {info.st_size:,} bytes; the VM profile requires {expected_size:,} bytes."
        )
    return path


def _disk_size_gib(byte_count: int) -> int:
    for size in range(1, MAX_DISK_GIB + 1):
        if disk_geometry(size)[3] == byte_count:
            return size
    raise VMError(
        "The detached secondary disk does not match a supported 1–512 GiB Bochs disk geometry."
    )


def export_vm(manager: VMManager, name: str, destination: Path | str) -> Path:
    """Write a ZIP backup without changing the source VM or its guest disks."""
    cfg = manager.load(name)
    if manager.is_running(name):
        raise VMError("Shut down the guest and stop Bochs before exporting a consistent disk backup.")
    directory = _vm_directory(manager, name)
    primary = manager.validate_disk_image(name, cfg)
    secondary = manager.validate_secondary_disk_image(name, cfg)

    # A detached disk2.raw still contains user data and must not disappear
    # from a backup merely because it is not currently attached to the guest.
    if secondary is None:
        detached = directory / "disk2.raw"
        if detached.exists() or detached.is_symlink():
            _regular_file(detached, "Detached secondary disk")
            _disk_size_gib(detached.stat().st_size)
            secondary = detached

    profile_path = _regular_file(manager.config_path(name), "VM profile")
    if profile_path.stat().st_size > METADATA_LIMIT:
        raise VMError("The VM profile is unexpectedly large; refusing to export it.")

    target = Path(destination).expanduser()
    if not target.name:
        raise VMError("Choose a filename for the backup.")
    parent = target.parent.resolve()
    try:
        parent.relative_to(directory)
    except ValueError:
        pass
    else:
        raise VMError("Save the backup outside the VM's own folder.")
    target.parent.mkdir(parents=True, exist_ok=True)
    parent = target.parent.resolve()
    if target.exists() or target.is_symlink():
        raise VMError(f"Backup destination already exists: {target}")

    manifest = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "vm_name": cfg.name,
        "secondary_disk_present": secondary is not None,
        "secondary_disk_attached": cfg.secondary_disk_path is not None,
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=1, allowZip64=True,
        ) as archive:
            archive.writestr(
                "manifest.json", json.dumps(manifest, sort_keys=True) + "\n"
            )
            archive.write(profile_path, "vm.json", compress_type=zipfile.ZIP_DEFLATED,
                          compresslevel=1)
            archive.write(primary, "disk.raw", compress_type=zipfile.ZIP_DEFLATED,
                          compresslevel=1)
            if secondary is not None:
                archive.write(secondary, "disk2.raw", compress_type=zipfile.ZIP_DEFLATED,
                              compresslevel=1)

        # A CLI caller could start Bochs while the archive was being written.
        # Do not publish a backup if the guest became active during the copy.
        if manager.is_running(name):
            raise VMError("Bochs started while the backup was being written; the incomplete backup was discarded.")

        # Prefer an atomic same-filesystem link. Fall back to an exclusive
        # create-and-copy on shared folders that do not support hard links.
        try:
            os.link(temporary, target)
        except OSError as exc:
            unsupported = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EOPNOTSUPP}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if exc.errno not in unsupported:
                raise
            out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(out_fd, "wb") as output, temporary.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                target.unlink(missing_ok=True)
                raise
        temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target.resolve()


def _check_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if name not in ALLOWED_MEMBERS or "/" in name or "\\" in name or name in {".", ".."}:
        raise VMError(f"Backup contains an unexpected or unsafe entry: {name!r}")
    if info.is_dir() or info.flag_bits & 0x1:
        raise VMError(f"Backup entry {name!r} is a directory or encrypted.")
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG):
        raise VMError(f"Backup entry {name!r} is not a regular file.")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise VMError(f"Backup entry {name!r} uses an unsupported compression method.")
    if info.file_size < 0 or info.file_size > MAX_BACKUP_BYTES:
        raise VMError(f"Backup entry {name!r} exceeds the supported size limit.")
    if info.file_size and info.file_size / max(info.compress_size, 1) > 10_000:
        raise VMError(f"Backup entry {name!r} has an unsafe compression ratio.")


def _read_metadata(archive: zipfile.ZipFile, info: zipfile.ZipInfo, label: str) -> bytes:
    if info.file_size > METADATA_LIMIT:
        raise VMError(f"The backup {label} exceeds the metadata size limit.")
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise VMError(f"Could not read the backup {label}: {exc}") from exc
    if len(data) != info.file_size:
        raise VMError(f"The backup {label} has an inconsistent length.")
    return data


def _copy_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path, expected_size: int
) -> None:
    """Restore a raw disk while recreating zero-filled regions as sparse holes."""
    copied = 0
    try:
        with archive.open(info, "r") as source, destination.open("xb") as output:
            while True:
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise VMError(f"Backup disk {info.filename} expands beyond its expected size.")

                # Raw disks created by the manager are sparse. ZIP files preserve
                # their logical zero bytes but not their filesystem hole map, so
                # rebuild holes in 4 KiB blocks instead of allocating the entire
                # virtual capacity during restore.
                for offset in range(0, len(chunk), SPARSE_BLOCK_BYTES):
                    block = chunk[offset:offset + SPARSE_BLOCK_BYTES]
                    if block.count(b"\0") == len(block):
                        output.seek(len(block), os.SEEK_CUR)
                    else:
                        output.write(block)

            if copied != expected_size:
                raise VMError(
                    f"Backup disk {info.filename} contains {copied:,} bytes; expected {expected_size:,}."
                )
            # Seeking beyond EOF does not set the final logical length.
            output.truncate(expected_size)
            output.flush()
            os.fsync(output.fileno())
    except VMError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        destination.unlink(missing_ok=True)
        raise VMError(f"Could not restore {info.filename}: {exc}") from exc


def import_vm(manager: VMManager, source: Path | str, name: str | None = None) -> VMConfig:
    """Restore a ZIP backup into a new private VM directory; never overwrite a VM."""
    backup_path = Path(source).expanduser()
    if backup_path.is_symlink() or not backup_path.is_file():
        raise VMError("Choose an existing regular backup ZIP file (not a symlink).")
    backup_path = backup_path.resolve()
    if backup_path.stat().st_size > MAX_BACKUP_BYTES:
        raise VMError("The backup archive exceeds the supported size limit.")

    try:
        archive_context = zipfile.ZipFile(backup_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise VMError(f"Could not open the backup archive: {exc}") from exc

    with archive_context as archive:
        infos = archive.infolist()
        if not 3 <= len(infos) <= 4:
            raise VMError("A backup must contain manifest.json, vm.json, disk.raw, and optional disk2.raw.")
        by_name: dict[str, zipfile.ZipInfo] = {}
        total_size = 0
        for info in infos:
            _check_member(info)
            if info.filename in by_name:
                raise VMError(f"Backup contains duplicate entry {info.filename!r}.")
            by_name[info.filename] = info
            total_size += info.file_size
        if total_size > MAX_BACKUP_BYTES:
            raise VMError("The backup's expanded contents exceed the supported size limit.")
        required = {"manifest.json", "vm.json", "disk.raw"}
        if not required.issubset(by_name):
            raise VMError("The backup is missing its manifest, VM profile, or primary disk.")
        has_secondary = "disk2.raw" in by_name

        manifest_bytes = _read_metadata(archive, by_name["manifest.json"], "manifest")
        profile_bytes = _read_metadata(archive, by_name["vm.json"], "VM profile")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            raw_profile = json.loads(profile_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VMError(f"The backup metadata is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(manifest, dict) or (
            manifest.get("format") != BACKUP_FORMAT or manifest.get("version") != BACKUP_VERSION
        ):
            raise VMError("This is not a supported ChromeOS VM Studio backup.")
        if not isinstance(raw_profile, dict) or not isinstance(raw_profile.get("name"), str):
            raise VMError("The backup does not contain a valid VM profile.")
        original_name = raw_profile["name"]
        if manifest.get("vm_name") != original_name:
            raise VMError("The backup manifest and VM profile names do not match.")
        if type(manifest.get("secondary_disk_present")) is not bool or (
            manifest["secondary_disk_present"] != has_secondary
        ):
            raise VMError("The backup's secondary-disk manifest does not match its contents.")

        target_name = name if name is not None else original_name
        validate_name(target_name)
        target_dir = manager.vm_path(target_name)
        if target_dir.exists() or target_dir.is_symlink():
            raise VMError(f"VM '{target_name}' already exists; restore never overwrites existing VMs.")
        target_resolved = target_dir.resolve()
        raw_profile["name"] = target_name
        raw_profile["disk_path"] = str(target_resolved / "disk.raw")
        attached_secondary = raw_profile.get("secondary_disk_gib") is not None
        if manifest.get("secondary_disk_attached") is not attached_secondary:
            raise VMError("The backup's secondary-disk attachment state is inconsistent.")
        if attached_secondary:
            if not has_secondary:
                raise VMError("The attached secondary disk is missing from the backup.")
            raw_profile["secondary_disk_path"] = str(target_resolved / "disk2.raw")
        else:
            raw_profile["secondary_disk_path"] = None
        try:
            cfg = VMConfig.from_dict(raw_profile)
        except VMError:
            raise
        except (TypeError, ValueError) as exc:
            raise VMError(f"The backup VM profile is invalid: {exc}") from exc

        primary_size = disk_geometry(cfg.disk_gib)[3]
        if by_name["disk.raw"].file_size != primary_size:
            raise VMError("The primary disk entry does not match the geometry in the VM profile.")
        secondary_size: int | None = None
        if has_secondary:
            if attached_secondary:
                secondary_size = disk_geometry(cfg.secondary_disk_gib)[3]
            else:
                _disk_size_gib(by_name["disk2.raw"].file_size)
                secondary_size = by_name["disk2.raw"].file_size
            if by_name["disk2.raw"].file_size != secondary_size:
                raise VMError("The secondary disk entry does not match its expected geometry.")

        stage = Path(tempfile.mkdtemp(
            prefix=f".{target_name}.restore-", dir=manager.home
        ))
        try:
            _copy_member(archive, by_name["disk.raw"], stage / "disk.raw", primary_size)
            if has_secondary and secondary_size is not None:
                _copy_member(archive, by_name["disk2.raw"], stage / "disk2.raw", secondary_size)
            (stage / "logs").mkdir()
            (stage / "runtime").mkdir()
            (stage / "vm.json").write_text(
                json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8"
            )
            if target_dir.exists() or target_dir.is_symlink():
                raise VMError(f"VM '{target_name}' appeared during restore; nothing was overwritten.")
            stage.rename(target_dir)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    try:
        restored = manager.load(target_name)
        manager.validate_disk_image(target_name, restored)
        manager.validate_secondary_disk_image(target_name, restored)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    return restored
