"""VM profiles and process management for the native Crostini app.

Bochs supplies the full x86 PC emulation. This module manages its files and
starts its real GUI window; it does not emulate hardware itself and never
invokes QEMU.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
CONFIG_VERSION = 2
MIN_MEMORY_MIB, MAX_MEMORY_MIB = 16, 4096
MIN_DISK_GIB, MAX_DISK_GIB = 1, 512
MIN_IPS, MAX_IPS = 100_000, 500_000_000


class VMError(RuntimeError):
    """A user-correctable VM or host configuration error."""


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise VMError("Names must start with a letter/number and use only letters, numbers, - or _ (48 chars max).")


def validate_resources(memory_mib: int, disk_gib: int, cpu_ips: int) -> None:
    if any(type(v) is not int for v in (memory_mib, disk_gib, cpu_ips)):
        raise VMError("RAM, disk size, and emulated CPU speed must be whole numbers.")
    if not MIN_MEMORY_MIB <= memory_mib <= MAX_MEMORY_MIB:
        raise VMError(f"RAM must be {MIN_MEMORY_MIB}–{MAX_MEMORY_MIB} MiB.")
    if not MIN_DISK_GIB <= disk_gib <= MAX_DISK_GIB:
        raise VMError(f"Disk size must be {MIN_DISK_GIB}–{MAX_DISK_GIB} GiB.")
    if not MIN_IPS <= cpu_ips <= MAX_IPS:
        raise VMError(f"CPU speed must be {MIN_IPS:,}–{MAX_IPS:,} instructions per second.")


def bochs_quote(value: str | Path) -> str:
    """Quote a local path for a Bochs config; reject parser-unsafe characters."""
    value = str(value)
    if any(ch in value for ch in ('"', "\n", "\r", "\x00")):
        raise VMError("A file path contains characters that cannot safely be written to a Bochs config.")
    return '"' + value.replace("\\", "\\\\") + '"'


def disk_geometry(disk_gib: int) -> tuple[int, int, int, int]:
    """Return BIOS-friendly CHS geometry and its exact flat-image byte size.

    Bochs requires the configured C*H*S*512 geometry to match a flat image's
    byte length. Use 16 heads and 63 sectors/track, rounding down by < one
    cylinder (about 504 KiB) so the virtual disk is always internally valid.
    """
    if type(disk_gib) is not int or disk_gib < MIN_DISK_GIB:
        raise VMError("Disk size must be a positive whole number of GiB.")
    heads, sectors = 16, 63
    cylinder_bytes = heads * sectors * 512
    cylinders = (disk_gib * 1024**3) // cylinder_bytes
    if cylinders < 1:
        raise VMError("Disk size is too small for the selected geometry.")
    return cylinders, heads, sectors, cylinders * cylinder_bytes


@dataclass(frozen=True)
class VMConfig:
    name: str
    memory_mib: int
    disk_gib: int
    disk_path: str
    iso_path: str | None = None
    cpu_ips: int = 10_000_000
    backend: str = "bochs"
    disk_format: str = "raw"
    config_version: int = CONFIG_VERSION

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VMConfig":
        try:
            config = cls(**raw)
        except (TypeError, ValueError) as exc:
            raise VMError(f"Invalid VM configuration: {exc}") from exc
        if config.config_version != CONFIG_VERSION:
            raise VMError(f"Unsupported VM config version {config.config_version}.")
        validate_name(config.name)
        validate_resources(config.memory_mib, config.disk_gib, config.cpu_ips)
        if config.backend != "bochs" or config.disk_format != "raw":
            raise VMError("This version supports the Bochs backend and raw disks only.")
        return config


class VMManager:
    def __init__(self, home: Path | str | None = None) -> None:
        default = Path.home() / ".local/share/chromeos-vm-studio"
        self.home = Path(home or os.environ.get("CHROMEOS_VM_HOME", default)).expanduser()
        self.home.mkdir(parents=True, exist_ok=True)

    def vm_path(self, name: str) -> Path:
        validate_name(name)
        return self.home / name

    def config_path(self, name: str) -> Path:
        return self.vm_path(name) / "vm.json"

    def load(self, name: str) -> VMConfig:
        path = self.config_path(name)
        if not path.is_file():
            raise VMError(f"VM '{name}' does not exist.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VMError(f"Could not read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise VMError(f"Invalid VM configuration at {path}.")
        return VMConfig.from_dict(raw)

    def _save(self, config: VMConfig) -> None:
        path = self.config_path(config.name)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def create(self, name: str, memory_mib: int = 512, disk_gib: int = 8,
               cpu_ips: int = 10_000_000, iso: Path | str | None = None) -> VMConfig:
        validate_name(name)
        validate_resources(memory_mib, disk_gib, cpu_ips)
        iso_value = self._validate_iso(iso) if iso else None
        directory = self.vm_path(name)
        if directory.exists():
            raise VMError(f"VM '{name}' already exists.")
        directory.mkdir(parents=True)
        try:
            disk = directory / "disk.raw"
            _cylinders, _heads, _sectors, image_bytes = disk_geometry(disk_gib)
            with disk.open("xb") as stream:
                stream.truncate(image_bytes)
            config = VMConfig(name=name, memory_mib=memory_mib, disk_gib=disk_gib,
                              disk_path=str(disk.resolve()), iso_path=iso_value, cpu_ips=cpu_ips)
            self._save(config)
            (directory / "logs").mkdir()
            (directory / "runtime").mkdir()
            # ROM availability is checked at boot time; VM profiles and disks
            # can be created before Bochs packages have finished installing.
            return config
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @staticmethod
    def _validate_iso(iso: Path | str) -> str:
        path = Path(iso).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.R_OK):
            raise VMError(f"Installation image is not a readable file: {path}")
        if path.suffix.lower() not in {".iso", ".img", ".ima"}:
            raise VMError("Choose an ISO or floppy/disk image file.")
        bochs_quote(path)
        return str(path)

    def set_iso(self, name: str, iso: Path | str | None) -> VMConfig:
        cfg = self.load(name)
        updated = VMConfig(**{**asdict(cfg), "iso_path": self._validate_iso(iso) if iso else None})
        self._save(updated)
        return updated

    def update_profile(self, name: str, memory_mib: int, cpu_ips: int,
                       iso: Path | str | None) -> VMConfig:
        """Update editable profile fields for a stopped VM; never resize its disk."""
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before editing its settings or ejecting its ISO.")
        validate_resources(memory_mib, cfg.disk_gib, cpu_ips)
        iso_value = self._validate_iso(iso) if iso else None
        updated = VMConfig(**{
            **asdict(cfg),
            "memory_mib": memory_mib,
            "cpu_ips": cpu_ips,
            "iso_path": iso_value,
        })
        self._save(updated)
        return updated

    @staticmethod
    def _find_bios(filename: str) -> Path | None:
        """Find an installed ROM using Debian's owning package and real paths.

        Debian Trixie's vgabios package installs /usr/share/vgabios/vgabios.bin;
        it does not install Bochs' upstream $BXSHARE/VGABIOS-lgpl-latest.bin.
        """
        if filename == "BIOS-bochs-latest":
            package = "bochsbios"
            names = ("BIOS-bochs-latest", "BIOS-bochs-legacy")
        else:
            package = "vgabios"
            names = (
                "vgabios.bin", "vgabios-stdvga.bin", "vgabios.stdvga.bin",
                "VGABIOS-lgpl-latest.bin", "VGABIOS-lgpl-latest",
                "VGABIOS-elpin-2.40",
            )

        # Query the package that owns this ROM, rather than mixing unrelated
        # and possibly absent packages in one dpkg-query invocation.
        package_paths: list[Path] = []
        if shutil.which("dpkg-query"):
            try:
                result = subprocess.run(
                    ["dpkg-query", "-L", package],
                    capture_output=True, text=True, check=False, timeout=10,
                )
                package_paths.extend(
                    Path(line.strip()) for line in result.stdout.splitlines()
                    if line.strip().startswith("/")
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        env_root = Path(os.environ["BXSHARE"]) if os.environ.get("BXSHARE") else None
        roots = []
        if env_root:
            roots.append(env_root)
        roots.extend([
            Path("/usr/share/vgabios"),
            Path("/usr/share/bochs"),
            Path("/usr/share/bochsbios"),
            Path("/usr/share/seabios"),
            Path("/usr/local/share/bochs"),
        ])

        # Prefer the package manifest's exact files; then inspect known
        # installation roots. Never return a guessed, non-existent ROM path.
        candidates = [path for path in package_paths if path.name in names]
        candidates.extend(root / name for root in roots for name in names)
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(candidate, os.R_OK):
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def render_bochsrc(self, cfg: VMConfig) -> str:
        lines = [
            "# Generated by ChromeOS VM Studio. Edit the VM profile in the app.",
            f"megs: {cfg.memory_mib}",
            f"cpu: count=1, ips={cfg.cpu_ips}, reset_on_triple_fault=1",
        ]
        bios = self._find_bios("BIOS-bochs-latest")
        vgabios = self._find_bios("VGABIOS-lgpl-latest")
        if not bios:
            raise VMError("Bochs system BIOS not found. Run: sudo apt update && sudo apt install -y bochsbios")
        if not vgabios:
            raise VMError(
                "Bochs VGA BIOS not found. Debian 13 installs it as "
                "/usr/share/vgabios/vgabios.bin. Run: "
                "sudo apt update && sudo apt install -y vgabios"
            )
        lines.append(f"romimage: file={bochs_quote(bios)}")
        lines.append(f"vgaromimage: file={bochs_quote(vgabios)}")
        lines.extend([
            "display_library: sdl2",
            "vga: extension=vbe",
            "mouse: enabled=1",
        ])
        cylinders, heads, sectors, _image_bytes = disk_geometry(cfg.disk_gib)
        lines.append('ata0-master: type=disk, mode=flat, path=' + bochs_quote(cfg.disk_path) +
                     f", cylinders={cylinders}, heads={heads}, spt={sectors}, translation=auto")
        if cfg.iso_path:
            lines.append('ata0-slave: type=cdrom, path=' + bochs_quote(cfg.iso_path) + ", status=inserted")
            lines.append("boot: cdrom, disk")
        else:
            lines.append("boot: disk")
        lines.extend([
            "log: " + bochs_quote(self.vm_path(cfg.name) / "logs/bochs.log"),
            "panic: action=ask",
            "error: action=report",
            "info: action=report",
            "debug: action=ignore",
            "",
        ])
        return "\n".join(lines)

    def write_bochsrc(self, config: VMConfig) -> Path:
        path = self.vm_path(config.name) / "bochsrc"
        path.write_text(self.render_bochsrc(config), encoding="utf-8")
        return path

    def list_vms(self) -> list[VMConfig]:
        result = []
        for path in sorted(self.home.iterdir()):
            if path.is_dir() and not path.is_symlink() and (path / "vm.json").is_file():
                result.append(self.load(path.name))
        return result

    @staticmethod
    def find_bochs(override: str | None = None) -> str | None:
        candidate = override or shutil.which("bochs")
        return str(Path(candidate).expanduser().resolve()) if candidate and Path(candidate).is_file() else None

    def is_running(self, name: str) -> bool:
        self.load(name)
        pid_file = self.vm_path(name) / "runtime/bochs.pid"
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            return False
        except PermissionError:
            return True
        # Linux reports a terminated-but-not-yet-reaped child as state Z.
        stat_file = Path(f"/proc/{pid}/stat")
        try:
            fields = stat_file.read_text(encoding="ascii").split()
            if len(fields) > 2 and fields[2] == "Z":
                pid_file.unlink(missing_ok=True)
                return False
        except OSError:
            pass
        return True

    def start(self, name: str, bochs_override: str | None = None) -> int:
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError(f"VM '{name}' appears to be running already.")
        executable = self.find_bochs(bochs_override)
        if not executable:
            raise VMError("Bochs was not found. Install it with: sudo apt install bochs bochs-sdl")
        system_bios = self._find_bios("BIOS-bochs-latest")
        if not system_bios:
            raise VMError("Bochs system BIOS was not found. Run: sudo apt update && sudo apt install -y bochsbios")
        vga_bios = self._find_bios("VGABIOS-lgpl-latest")
        if not vga_bios:
            raise VMError(
                "Bochs VGA BIOS was not found. Debian 13's vgabios package installs "
                "/usr/share/vgabios/vgabios.bin. Run: "
                "sudo apt update && sudo apt install -y vgabios"
            )
        if not cfg.disk_path or not Path(cfg.disk_path).is_file():
            raise VMError("The VM's virtual disk is missing.")
        config_file = self.write_bochsrc(cfg)
        log = self.vm_path(name) / "logs/launcher.log"
        try:
            with log.open("ab") as output:
                # Bochs parses config-style command-line options after the
                # -f file, so these explicit ROM settings override any stale
                # system/default bochsrc and prevent BXSHARE's missing default
                # VGABIOS-lgpl-latest.bin path from being used.
                proc = subprocess.Popen([
                    executable, "-q", "-f", str(config_file),
                    f"romimage: file={bochs_quote(system_bios)}",
                    f"vgaromimage: file={bochs_quote(vga_bios)}",
                ], cwd=self.vm_path(name), stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT,
                                         start_new_session=True, close_fds=True)
        except OSError as exc:
            raise VMError(f"Could not launch Bochs: {exc}") from exc
        (self.vm_path(name) / "runtime/bochs.pid").write_text(f"{proc.pid}\n", encoding="ascii")
        return proc.pid

    def stop(self, name: str) -> None:
        self.load(name)
        pid_file = self.vm_path(name) / "runtime/bochs.pid"
        if not self.is_running(name):
            raise VMError(f"VM '{name}' is not running.")
        pid = int(pid_file.read_text(encoding="ascii").strip())
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.is_running(name):
                return
            time.sleep(0.1)
        raise VMError("Bochs did not exit after SIGTERM. Close its emulator window; the process was not force-killed.")

    def delete(self, name: str) -> None:
        self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before deleting it.")
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("Refusing to delete a VM directory outside the storage folder.")
        shutil.rmtree(directory)
