"""VM profiles and process management for the native Crostini app.

Bochs supplies the full x86 PC emulation. This module manages its files and
starts its real GUI window; it does not emulate hardware itself and never
invokes QEMU.
"""
from __future__ import annotations

import errno
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
# Names come from Bochs' documented pre-defined CPU model table. The actual
# set available depends on the CPU level/features compiled into the Bochs build.
CPU_MODEL_LABELS = {
    "default": "Bochs default (build-dependent)",
    "i386": "Intel 386SX",
    "i486dx4": "Intel 486DX4",
    "pentium": "Intel Pentium (P54C)",
    "pentium_mmx": "Intel Pentium MMX",
    "amd_k6_2_chomper": "AMD K6-2",
    "p2_klamath": "Intel Pentium II (Klamath)",
    "p3_katmai": "Intel Pentium III (Katmai)",
    "p4_willamette": "Intel Pentium 4 (Willamette)",
    "core_duo_t2400_yonah": "Intel Core Duo T2400 (Yonah)",
    "atom_n270": "Intel Atom N270",
}
CPU_MODEL_IDS = frozenset(CPU_MODEL_LABELS) - {"default"}
CHIPSET_LABELS = {
    "i430fx": "Intel 430FX (legacy PCI)",
    "i440fx": "Intel 440FX (default PCI)",
    "i440bx": "Intel 440BX (Pentium II-era PCI)",
}
IDE_CDROM_SLOT_LABELS = {
    "ata0-slave": "Primary IDE channel · Slave",
    "ata1-slave": "Secondary IDE channel · Slave",
}
BOOT_DEVICE_LABELS = {
    "floppy": "Floppy A",
    "cdrom": "CD/DVD",
    "disk": "Hard disk",
    "network": "Network (requires a configured NIC/boot ROM)",
}
BOOT_DEVICES = frozenset(BOOT_DEVICE_LABELS)
BOCHSRC_MAX_BYTES = 256 * 1024
BOCHSRC_REQUIRED = {
    "megs", "cpu", "romimage", "vgaromimage", "display_library",
    "vga", "ata0-master", "boot", "log",
}
BOCHSRC_DIRECTIVE = re.compile(r"^[a-z][a-z0-9_-]*$")


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


def validate_cpu_model(cpu_model: str) -> None:
    if not isinstance(cpu_model, str) or cpu_model not in CPU_MODEL_LABELS:
        raise VMError(
            "Choose a CPU model listed by ChromeOS VM Studio; arbitrary model strings are not accepted."
        )


def validate_chipset(chipset: str) -> None:
    if not isinstance(chipset, str) or chipset not in CHIPSET_LABELS:
        raise VMError(
            "Choose a PCI chipset supported by ChromeOS VM Studio; arbitrary chipset strings are not accepted."
        )


def validate_cdrom_slot(slot: str) -> None:
    if not isinstance(slot, str) or slot not in IDE_CDROM_SLOT_LABELS:
        raise VMError(
            "Choose a CD-ROM position supported by ChromeOS VM Studio "
            "(primary-channel slave or secondary-channel slave)."
        )


def validate_boot_order(order: tuple[str, ...] | list[str] | None) -> None:
    """Validate Bochs' documented boot list: one to three unique device names."""
    if order is None:
        return
    if not isinstance(order, (tuple, list)):
        raise VMError("Boot order must be a list of Bochs boot-device names or automatic.")
    if not 1 <= len(order) <= 3:
        raise VMError("Choose between one and three boot devices.")
    if any(not isinstance(device, str) or device not in BOOT_DEVICES for device in order):
        raise VMError("Boot devices must be floppy, cdrom, disk, or network.")
    if len(set(order)) != len(order):
        raise VMError("A boot device can appear only once in the boot sequence.")


def validate_boot_order_for_media(
    order: tuple[str, ...] | list[str] | None,
    *,
    floppy_a: bool,
    cdrom: bool,
) -> None:
    validate_boot_order(order)
    if order is None:
        return
    if "floppy" in order and not floppy_a:
        raise VMError("Floppy boot requires a boot image attached to floppy A.")
    if "cdrom" in order and not cdrom:
        raise VMError("CD/DVD boot requires an ISO attached to the virtual CD-ROM.")


def default_boot_order(config: "VMConfig") -> tuple[str, ...]:
    """Return the existing media-aware boot order used by legacy profiles."""
    order: list[str] = []
    if config.floppy_a_path:
        order.append("floppy")
    if config.iso_path:
        order.append("cdrom")
    order.append("disk")
    return tuple(order[:3])


def bochs_quote(value: str | Path) -> str:
    """Quote a local path for a Bochs config; reject parser-unsafe characters."""
    value = str(value)
    if any(ch in value for ch in ('"', "\n", "\r", "\x00")):
        raise VMError("A file path contains characters that cannot safely be written to a Bochs config.")
    return '"' + value.replace("\\", "\\\\") + '"'


def validate_bochsrc_text(text: str) -> None:
    """Perform conservative structural checks on an editable Bochs config.

    This is deliberately not described as Bochs' own parser: the emulator is
    the final authority when the VM is booted. The checks catch common editor
    mistakes before a custom file is made active.
    """
    if not isinstance(text, str) or not text.strip():
        raise VMError("The Bochs configuration is empty.")
    if len(text.encode("utf-8")) > BOCHSRC_MAX_BYTES:
        raise VMError(f"The Bochs configuration exceeds {BOCHSRC_MAX_BYTES // 1024} KiB.")
    if "\x00" in text:
        raise VMError("The Bochs configuration contains a NUL character.")

    seen: set[str] = set()
    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("#include"):
            raise VMError(
                f"Line {number}: #include is not supported in the built-in editor; "
                "keep the VM configuration self-contained."
            )
        if line.startswith("#"):
            continue
        if ":" not in line:
            raise VMError(f"Line {number}: expected a Bochs 'directive: value' line.")
        directive, value = (part.strip() for part in line.split(":", 1))
        if not BOCHSRC_DIRECTIVE.fullmatch(directive):
            raise VMError(f"Line {number}: invalid Bochs directive name.")
        if (re.fullmatch(r"ata[0-3](?:-(?:master|slave))?", directive)
                and directive not in {
                    "ata0", "ata1", "ata0-master", "ata0-slave",
                    "ata1-master", "ata1-slave",
                }):
            raise VMError(
                f"Line {number}: IDE directive '{directive}' is not supported by this manager."
            )
        if not value:
            raise VMError(f"Line {number}: '{directive}' has no value.")
        if value.count('"') % 2:
            raise VMError(f"Line {number}: unmatched double quote.")
        if directive in BOCHSRC_REQUIRED or directive in {
            "floppya", "floppyb", "pci", "ata0", "ata1",
            "ata0-slave", "ata1-master", "ata1-slave",
            "sound", "speaker", "sb16",
        }:
            if directive in seen:
                raise VMError(f"Line {number}: duplicate directive '{directive}'.")
            seen.add(directive)
            values[directive] = value

    missing = sorted(BOCHSRC_REQUIRED - seen)
    if missing:
        raise VMError("Missing required Bochs directives: " + ", ".join(missing) + ".")

    boot_devices = tuple(part.strip().lower() for part in values["boot"].split(","))
    try:
        validate_boot_order(boot_devices)
    except VMError as exc:
        raise VMError("Invalid 'boot' directive: " + str(exc)) from exc

    try:
        memory = int(values["megs"], 10)
    except ValueError as exc:
        raise VMError("The 'megs' directive must contain a whole number of MiB.") from exc
    if not MIN_MEMORY_MIB <= memory <= MAX_MEMORY_MIB:
        raise VMError(f"The 'megs' value must be between {MIN_MEMORY_MIB} and {MAX_MEMORY_MIB}.")

    ips_match = re.search(r"\bips\s*=\s*(\d+)", values["cpu"])
    if not ips_match:
        raise VMError("The 'cpu' directive must include an ips= value.")
    ips = int(ips_match.group(1))
    if not MIN_IPS <= ips <= MAX_IPS:
        raise VMError(f"CPU IPS must be between {MIN_IPS:,} and {MAX_IPS:,}.")
    model_match = re.search(r"\bmodel\s*=\s*([A-Za-z0-9_]+)", values["cpu"])
    if model_match and model_match.group(1) not in CPU_MODEL_IDS:
        raise VMError(
            f"The Bochs CPU model '{model_match.group(1)}' is not in the supported model list."
        )

    pci_value = values.get("pci")
    if pci_value:
        enabled_match = re.search(r"\benabled\s*=\s*([01])\b", pci_value)
        chipset_match = re.search(r"\bchipset\s*=\s*([A-Za-z0-9]+)\b", pci_value)
        if not enabled_match or not chipset_match:
            raise VMError("The 'pci' directive must include enabled=0|1 and chipset=.")
        if chipset_match.group(1).lower() not in CHIPSET_LABELS:
            raise VMError(
                f"The Bochs PCI chipset '{chipset_match.group(1)}' is not supported by this manager."
            )

    for directive in ("romimage", "vgaromimage"):
        if not re.search(r"\bfile\s*=", values[directive]):
            raise VMError(f"The '{directive}' directive must include file=.")
    disk = values["ata0-master"]
    for required in ("type=disk", "mode=flat", "path=", "cylinders=", "heads=", "spt="):
        if required not in disk.replace(" ", ""):
            raise VMError(f"The 'ata0-master' directive must include {required}.")
    # ATA0 and ATA1 are the standard primary/secondary IDE channels. They are
    # optional here for compatibility with older user-authored bochsrc files;
    # newly generated files write both explicitly.
    controller_defaults = {
        "ata0": ("0x1f0", "0x3f0", "14"),
        "ata1": ("0x170", "0x370", "15"),
    }
    for controller, (io1, io2, irq) in controller_defaults.items():
        if controller in values:
            value = values[controller].replace(" ", "").lower()
            if (not re.search(r"enabled=1\b", value)
                    or f"ioaddr1={io1}" not in value
                    or f"ioaddr2={io2}" not in value
                    or f"irq={irq}" not in value):
                raise VMError(
                    f"The '{controller}' controller must use its standard enabled IDE channel addresses and IRQ."
                )

    if "ata1-master" in values:
        secondary = values["ata1-master"].replace(" ", "")
        for required in ("type=disk", "mode=flat", "path=", "cylinders=", "heads=", "spt="):
            if required not in secondary:
                raise VMError(f"The 'ata1-master' directive must include {required}.")

    for slot in ("ata0-slave", "ata1-slave"):
        if slot in values:
            device = values[slot].replace(" ", "")
            if not all(item in device for item in ("type=cdrom", "path=", "status=inserted")):
                raise VMError(f"The '{slot}' directive must describe an inserted CD-ROM with a path.")

    if not values["display_library"] or not values["vga"] or not values["boot"] or not values["log"]:
        raise VMError("Display, VGA, boot, and log directives must have values.")

    for floppy_directive in ("floppya", "floppyb"):
        if floppy_directive in values:
            floppy = values[floppy_directive]
            if not re.search(r'\bimage\s*=\s*"[^"]+"', floppy):
                raise VMError(f"The '{floppy_directive}' directive must use a quoted image= path.")
            if not re.search(r'\bstatus\s*=\s*inserted\b', floppy):
                raise VMError(f"The '{floppy_directive}' image must have status=inserted.")
            if not re.search(r'\bwrite_protected\s*=\s*1\b', floppy):
                raise VMError(f"The '{floppy_directive}' image must remain write-protected in managed VM profiles.")


def disk_geometry(disk_gib: int) -> tuple[int, int, int, int]:
    """Return BIOS-friendly CHS geometry and its exact flat-image byte size.

    Bochs requires the configured C*H*S*512 geometry to match a flat image's
    byte length. Use 16 heads and 63 sectors/track, rounding down by < one
    cylinder (about 504 KiB) so the virtual disk is always internally valid.
    """
    if type(disk_gib) is not int or not MIN_DISK_GIB <= disk_gib <= MAX_DISK_GIB:
        raise VMError(f"Disk size must be between {MIN_DISK_GIB} and {MAX_DISK_GIB} GiB.")
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
    secondary_disk_gib: int | None = None
    secondary_disk_path: str | None = None
    cdrom_slot: str = "ata0-slave"
    iso_path: str | None = None
    floppy_a_path: str | None = None
    floppy_b_path: str | None = None
    # None preserves the media-aware legacy default; a tuple is an explicit BIOS order.
    boot_order: tuple[str, ...] | None = None
    cpu_ips: int = 10_000_000
    cpu_model: str = "default"
    chipset: str = "i440fx"
    sound_enabled: bool = False
    backend: str = "bochs"
    disk_format: str = "raw"
    config_version: int = CONFIG_VERSION

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VMConfig":
        normalized = dict(raw)
        if isinstance(normalized.get("boot_order"), list):
            normalized["boot_order"] = tuple(normalized["boot_order"])
        try:
            config = cls(**normalized)
        except (TypeError, ValueError) as exc:
            raise VMError(f"Invalid VM configuration: {exc}") from exc
        if config.config_version != CONFIG_VERSION:
            raise VMError(f"Unsupported VM config version {config.config_version}.")
        validate_name(config.name)
        validate_resources(config.memory_mib, config.disk_gib, config.cpu_ips)
        validate_cpu_model(config.cpu_model)
        validate_chipset(config.chipset)
        if type(config.sound_enabled) is not bool:
            raise VMError("The sound setting must be true or false.")
        validate_cdrom_slot(config.cdrom_slot)
        validate_boot_order_for_media(
            config.boot_order,
            floppy_a=bool(config.floppy_a_path),
            cdrom=bool(config.iso_path),
        )
        if config.secondary_disk_gib is None:
            if config.secondary_disk_path is not None:
                raise VMError("A secondary disk path cannot be set without a secondary disk size.")
        else:
            if type(config.secondary_disk_gib) is not int:
                raise VMError("The secondary disk size must be a whole number of GiB.")
            disk_geometry(config.secondary_disk_gib)
            if not isinstance(config.secondary_disk_path, str) or not config.secondary_disk_path:
                raise VMError("The secondary disk profile must contain its private disk path.")
        if config.backend != "bochs" or config.disk_format != "raw":
            raise VMError("This version supports the Bochs backend and raw disks only.")
        if not isinstance(config.disk_path, str) or not config.disk_path:
            raise VMError("The VM profile must contain a disk path.")
        if config.iso_path is not None and not isinstance(config.iso_path, str):
            raise VMError("The VM profile contains an invalid ISO path.")
        if config.floppy_a_path is not None and not isinstance(config.floppy_a_path, str):
            raise VMError("The VM profile contains an invalid floppy A image path.")
        if config.floppy_b_path is not None and not isinstance(config.floppy_b_path, str):
            raise VMError("The VM profile contains an invalid floppy B image path.")
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

    def custom_bochsrc_path(self, name: str) -> Path:
        return self.vm_path(name) / "bochsrc.custom"

    def active_bochsrc_path(self, name: str) -> Path:
        custom = self.custom_bochsrc_path(name)
        return custom if custom.is_file() else self.vm_path(name) / "bochsrc"

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
               cpu_ips: int = 10_000_000, iso: Path | str | None = None,
               cpu_model: str = "default", chipset: str = "i440fx",
               secondary_disk_gib: int | None = None,
               cdrom_slot: str = "ata0-slave",
               boot_order: tuple[str, ...] | None = None,
               sound_enabled: bool = False) -> VMConfig:
        validate_name(name)
        validate_resources(memory_mib, disk_gib, cpu_ips)
        validate_cpu_model(cpu_model)
        validate_chipset(chipset)
        if type(sound_enabled) is not bool:
            raise VMError("The sound setting must be true or false.")
        validate_cdrom_slot(cdrom_slot)
        validate_boot_order(boot_order)
        if secondary_disk_gib is not None:
            if type(secondary_disk_gib) is not int:
                raise VMError("The secondary disk size must be a whole number of GiB.")
            disk_geometry(secondary_disk_gib)
        iso_value = self._validate_iso(iso) if iso else None
        validate_boot_order_for_media(boot_order, floppy_a=False, cdrom=bool(iso_value))
        directory = self.vm_path(name)
        if directory.exists():
            raise VMError(f"VM '{name}' already exists.")
        directory.mkdir(parents=True)
        try:
            disk = directory / "disk.raw"
            _cylinders, _heads, _sectors, image_bytes = disk_geometry(disk_gib)
            with disk.open("xb") as stream:
                stream.truncate(image_bytes)
            secondary_disk_path = None
            if secondary_disk_gib is not None:
                secondary = directory / "disk2.raw"
                _c2, _h2, _s2, secondary_bytes = disk_geometry(secondary_disk_gib)
                with secondary.open("xb") as stream:
                    stream.truncate(secondary_bytes)
                secondary_disk_path = str(secondary.resolve())
            config = VMConfig(
                name=name, memory_mib=memory_mib, disk_gib=disk_gib,
                disk_path=str(disk.resolve()), iso_path=iso_value,
                secondary_disk_gib=secondary_disk_gib,
                secondary_disk_path=secondary_disk_path,
                cdrom_slot=cdrom_slot,
                boot_order=tuple(boot_order) if boot_order is not None else None,
                cpu_ips=cpu_ips, cpu_model=cpu_model, chipset=chipset,
                sound_enabled=sound_enabled,
            )
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
        if self.is_running(name):
            raise VMError("Stop the VM before changing its CD/DVD media.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing media; the custom config may reference the old ISO.")
        iso_value = self._validate_iso(iso) if iso else None
        validate_boot_order_for_media(
            cfg.boot_order, floppy_a=bool(cfg.floppy_a_path), cdrom=bool(iso_value)
        )
        updated = VMConfig(**{**asdict(cfg), "iso_path": iso_value})
        self._save(updated)
        return updated

    @staticmethod
    def _validate_floppy(image: Path | str) -> str:
        """Accept only standard raw floppy image capacities supported by Bochs."""
        path = Path(image).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.R_OK):
            raise VMError(f"Floppy image is not a readable file: {path}")
        bochs_quote(path)
        supported_sizes = {
            160 * 1024, 180 * 1024, 320 * 1024, 360 * 1024,
            720 * 1024, 1_200 * 1024, 1_440 * 1024, 2_880 * 1024,
        }
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise VMError(f"Could not inspect the floppy image: {exc}") from exc
        if size not in supported_sizes:
            allowed = "160K, 180K, 320K, 360K, 720K, 1.2M, 1.44M, or 2.88M"
            raise VMError(f"Bochs floppy images must have a supported exact size ({allowed}); this image is {size:,} bytes.")
        return str(path)

    def set_floppy_a(self, name: str, image: Path | str | None) -> VMConfig:
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before changing floppy A media.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing floppy media; the custom config may reference another image.")
        image_path = self._validate_floppy(image) if image else None
        validate_boot_order_for_media(
            cfg.boot_order, floppy_a=bool(image_path), cdrom=bool(cfg.iso_path)
        )
        updated = VMConfig(**{**asdict(cfg), "floppy_a_path": image_path})
        self._save(updated)
        return updated

    def set_floppy_b(self, name: str, image: Path | str | None) -> VMConfig:
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before changing floppy B media.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing floppy media; the custom config may reference another image.")
        image_path = self._validate_floppy(image) if image else None
        updated = VMConfig(**{**asdict(cfg), "floppy_b_path": image_path})
        self._save(updated)
        return updated

    def set_boot_order(self, name: str, order: tuple[str, ...] | list[str] | None) -> VMConfig:
        """Set an explicit BIOS boot order, or restore the automatic media-aware order."""
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before changing BIOS boot priority.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing BIOS boot priority.")
        normalized = tuple(order) if order is not None else None
        validate_boot_order_for_media(
            normalized, floppy_a=bool(cfg.floppy_a_path), cdrom=bool(cfg.iso_path)
        )
        updated = VMConfig(**{**asdict(cfg), "boot_order": normalized})
        self._save(updated)
        return updated

    def attach_secondary_disk(self, name: str, disk_gib: int) -> VMConfig:
        """Create or reattach the private secondary raw disk on IDE1 master."""
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before changing IDE storage.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing IDE storage.")
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("The VM directory is outside the private storage folder; refusing to create a secondary disk.")
        if cfg.secondary_disk_path is not None:
            raise VMError("This VM already has a secondary IDE disk attached.")
        _cylinders, _heads, _sectors, expected_bytes = disk_geometry(disk_gib)
        disk = directory / "disk2.raw"
        created = False
        if disk.is_symlink():
            raise VMError("The secondary disk path is a symlink; refusing to attach it.")
        if disk.exists():
            if not disk.is_file() or disk.stat().st_nlink != 1:
                raise VMError("The detached secondary disk is not a private regular file.")
            if disk.stat().st_size != expected_bytes:
                raise VMError("The existing detached secondary disk has a different size.")
        else:
            with disk.open("xb") as stream:
                stream.truncate(expected_bytes)
            created = True
        updated = VMConfig(**{
            **asdict(cfg),
            "secondary_disk_gib": disk_gib,
            "secondary_disk_path": str(disk.resolve()),
        })
        try:
            self._save(updated)
        except Exception:
            if created:
                disk.unlink(missing_ok=True)
            raise
        return updated

    def detach_secondary_disk(self, name: str) -> Path:
        """Detach disk2.raw from the guest but preserve its data in the VM folder."""
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before changing IDE storage.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before changing IDE storage.")
        if cfg.secondary_disk_path is None:
            raise VMError("This VM has no secondary IDE disk attached.")
        disk = self.validate_secondary_disk_image(name, cfg)
        updated = VMConfig(**{
            **asdict(cfg),
            "secondary_disk_gib": None,
            "secondary_disk_path": None,
        })
        self._save(updated)
        return disk

    def read_bochsrc(self, name: str) -> str:
        self.load(name)
        custom = self.custom_bochsrc_path(name)
        if custom.is_file():
            return custom.read_text(encoding="utf-8")
        return self.render_bochsrc(self.load(name))

    def save_custom_bochsrc(self, name: str, text: str) -> Path:
        self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before editing its Bochs configuration.")
        self.validate_bochsrc_for_vm(name, text)
        path = self.custom_bochsrc_path(name)
        temporary = path.with_suffix(".custom.tmp")
        temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)
        return path

    def _expected_disk_path(self, name: str) -> Path:
        return (self.vm_path(name) / "disk.raw").resolve()

    def _expected_secondary_disk_path(self, name: str) -> Path:
        return (self.vm_path(name) / "disk2.raw").resolve()

    def validate_bochsrc_for_vm(self, name: str, text: str) -> None:
        """Validate the custom config without allowing it to redirect the managed disk."""
        cfg = self.load(name)
        validate_bochsrc_text(text)
        disk_value = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            directive, value = (part.strip() for part in line.split(":", 1))
            if directive == "ata0-master":
                match = re.search(r'\bpath\s*=\s*"([^"]*)"', value)
                if match:
                    disk_value = match.group(1).replace("\\\\", "\\")
                break
        if not disk_value:
            raise VMError("The managed ata0-master disk must use a quoted path.")
        try:
            configured = Path(disk_value).resolve()
        except OSError as exc:
            raise VMError(f"Could not resolve the configured VM disk path: {exc}") from exc
        if configured != self._expected_disk_path(name):
            raise VMError(
                "The ata0-master disk must remain at this VM's private disk.raw path. "
                "Use the storage tools to manage guest disks; custom configs cannot redirect it."
            )

        def configured_value(directive: str) -> str | None:
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                current, value = (part.strip() for part in line.split(":", 1))
                if current == directive:
                    return value
            return None

        def configured_path(directive: str) -> str | None:
            value = configured_value(directive)
            if value is None:
                return None
            match = re.search(r'\bpath\s*=\s*"([^"]*)"', value)
            return match.group(1).replace("\\\\", "\\") if match else None

        def require_disk_geometry(directive: str, disk_gib: int) -> None:
            value = configured_value(directive) or ""
            actual = []
            for key in ("cylinders", "heads", "spt"):
                match = re.search(rf"\b{key}\s*=\s*(\d+)", value)
                if not match:
                    raise VMError(f"The {directive} geometry is incomplete.")
                actual.append(int(match.group(1)))
            expected = disk_geometry(disk_gib)[:3]
            if tuple(actual) != expected:
                raise VMError(
                    f"The {directive} geometry must match the managed {disk_gib} GiB disk."
                )

        require_disk_geometry("ata0-master", cfg.disk_gib)
        secondary_value = configured_path("ata1-master")
        if cfg.secondary_disk_path:
            if not secondary_value or Path(secondary_value).resolve() != self._expected_secondary_disk_path(name):
                raise VMError(
                    "The ata1-master disk must remain at this VM's private disk2.raw path."
                )
            require_disk_geometry("ata1-master", cfg.secondary_disk_gib)
        elif secondary_value:
            raise VMError("A custom config cannot attach an unmanaged secondary IDE disk.")

        cdrom_slots = [
            slot for slot in ("ata0-slave", "ata1-slave")
            if any(line.strip().startswith(slot + ":") for line in text.splitlines())
        ]
        if cfg.iso_path:
            if cdrom_slots != [cfg.cdrom_slot]:
                raise VMError(
                    f"The CD-ROM must remain in the selected {cfg.cdrom_slot} IDE position."
                )
            cdrom_value = configured_path(cfg.cdrom_slot)
            if not cdrom_value or Path(cdrom_value).resolve() != Path(cfg.iso_path).resolve():
                raise VMError("The CD-ROM image must remain the ISO attached in this VM's profile.")
        elif cdrom_slots:
            raise VMError("A custom config cannot attach an unmanaged CD-ROM image.")

        if cfg.floppy_a_path:
            floppy_value = None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                directive, value = (part.strip() for part in line.split(":", 1))
                if directive == "floppya":
                    match = re.search(r'\bimage\s*=\s*"([^"]+)"', value)
                    if match:
                        floppy_value = match.group(1).replace("\\\\", "\\")
                    break
            if not floppy_value or Path(floppy_value).resolve() != Path(cfg.floppy_a_path).resolve():
                raise VMError(
                    "The floppya image must remain the floppy A image attached in this VM's profile. "
                    "Use the media controls to change it."
                )

        if cfg.floppy_b_path:
            floppy_value = None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                directive, value = (part.strip() for part in line.split(":", 1))
                if directive == "floppyb":
                    match = re.search(r'\bimage\s*=\s*"([^"]+)"', value)
                    if match:
                        floppy_value = match.group(1).replace("\\\\", "\\")
                    break
            if not floppy_value or Path(floppy_value).resolve() != Path(cfg.floppy_b_path).resolve():
                raise VMError(
                    "The floppyb image must remain the floppy B image attached in this VM's profile. "
                    "Use the media controls to change it."
                )

        boot_value = configured_value("boot") or ""
        actual_boot_order = tuple(part.strip().lower() for part in boot_value.split(","))
        expected_boot_order = cfg.boot_order or default_boot_order(cfg)
        if actual_boot_order != expected_boot_order:
            raise VMError(
                "The custom boot directive must match the BIOS boot order saved in this VM profile. "
                "Use the Boot order editor to change it."
            )
        if cfg.sound_enabled:
            sound_value = configured_value("sound") or ""
            speaker_value = configured_value("speaker") or ""
            sb16_value = configured_value("sb16") or ""
            if not (
                re.search(r"\bwaveoutdrv\s*=\s*sdl\b", sound_value, re.IGNORECASE)
                and re.search(r"\benabled\s*=\s*1\b", speaker_value)
                and re.search(r"\bmode\s*=\s*sound\b", speaker_value)
                and re.search(r"\benabled\s*=\s*1\b", sb16_value)
                and re.search(r"\bwavemode\s*=\s*1\b", sb16_value)
            ):
                raise VMError(
                    "The custom config must preserve the SB16/PC-speaker audio enabled in this VM profile."
                )

    def validate_floppy_image(self, name: str, config: VMConfig | None = None) -> Path | None:
        cfg = config or self.load(name)
        if cfg.floppy_a_path is None:
            return None
        resolved = Path(self._validate_floppy(cfg.floppy_a_path))
        if str(resolved) != cfg.floppy_a_path:
            raise VMError("The attached floppy image path changed after it was selected; reattach it before booting.")
        return resolved

    def validate_floppy_b_image(self, name: str, config: VMConfig | None = None) -> Path | None:
        cfg = config or self.load(name)
        if cfg.floppy_b_path is None:
            return None
        resolved = Path(self._validate_floppy(cfg.floppy_b_path))
        if str(resolved) != cfg.floppy_b_path:
            raise VMError("The attached floppy B image path changed after it was selected; reattach it before booting.")
        return resolved

    def validate_secondary_disk_image(self, name: str, config: VMConfig | None = None) -> Path | None:
        """Validate the optional private IDE1-master disk before Bochs opens it."""
        cfg = config or self.load(name)
        if cfg.secondary_disk_path is None or cfg.secondary_disk_gib is None:
            return None
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("The VM directory is outside the private storage folder; refusing to open its secondary disk.")
        expected = self._expected_secondary_disk_path(name)
        try:
            configured = Path(cfg.secondary_disk_path).resolve()
        except (OSError, TypeError) as exc:
            raise VMError(f"The VM profile has an invalid secondary disk path: {exc}") from exc
        if configured != expected:
            raise VMError("The secondary disk must remain at this VM's private disk2.raw path.")
        disk = self.vm_path(name) / "disk2.raw"
        if disk.is_symlink() or not disk.is_file():
            raise VMError("The VM's secondary disk is missing or is not a regular file.")
        try:
            info = disk.stat()
        except OSError as exc:
            raise VMError(f"Could not inspect the secondary disk: {exc}") from exc
        if info.st_nlink != 1:
            raise VMError("The secondary disk has additional hard links; refusing to risk modifying another file.")
        expected_bytes = disk_geometry(cfg.secondary_disk_gib)[3]
        if info.st_size != expected_bytes:
            raise VMError(
                f"The secondary disk size is {info.st_size:,} bytes, but its profile requires {expected_bytes:,} bytes."
            )
        return disk.resolve()

    def validate_disk_image(self, name: str, config: VMConfig | None = None) -> Path:
        """Refuse missing, redirected, linked, or truncated managed disk images."""
        cfg = config or self.load(name)
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("The VM directory is outside the private storage folder; refusing to open its disk.")
        expected = self._expected_disk_path(name)
        try:
            configured = Path(cfg.disk_path).resolve()
        except (OSError, TypeError) as exc:
            raise VMError(f"The VM profile has an invalid disk path: {exc}") from exc
        if configured != expected:
            raise VMError(
                "The VM profile's disk path does not point to its private disk.raw file. "
                "The disk was not opened."
            )

        disk = self.vm_path(name) / "disk.raw"
        if disk.is_symlink() or not disk.is_file():
            raise VMError("The VM's private virtual disk is missing or is not a regular file.")
        try:
            info = disk.stat()
        except OSError as exc:
            raise VMError(f"Could not inspect the VM's virtual disk: {exc}") from exc
        if info.st_nlink != 1:
            raise VMError("The VM disk has additional hard links; refusing to risk modifying another file.")
        expected_bytes = disk_geometry(cfg.disk_gib)[3]
        if info.st_size != expected_bytes:
            raise VMError(
                f"The VM disk size is {info.st_size:,} bytes, but its profile requires "
                f"{expected_bytes:,} bytes. The disk was not opened."
            )
        return disk.resolve()

    def reset_custom_bochsrc(self, name: str) -> bool:
        self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before restoring its generated Bochs configuration.")
        path = self.custom_bochsrc_path(name)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def update_profile(self, name: str, memory_mib: int, cpu_ips: int,
                       iso: Path | str | None, cpu_model: str | None = None,
                       chipset: str | None = None,
                       cdrom_slot: str | None = None,
                       sound_enabled: bool | None = None) -> VMConfig:
        """Update editable profile fields for a stopped VM; never resize its disk."""
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before editing its settings or ejecting its ISO.")
        if self.custom_bochsrc_path(name).exists():
            raise VMError("Restore the generated Bochs config before editing profile settings; the custom config is currently authoritative.")
        validate_resources(memory_mib, cfg.disk_gib, cpu_ips)
        selected_cpu_model = cfg.cpu_model if cpu_model is None else cpu_model
        selected_chipset = cfg.chipset if chipset is None else chipset
        validate_cpu_model(selected_cpu_model)
        validate_chipset(selected_chipset)
        selected_sound_enabled = cfg.sound_enabled if sound_enabled is None else sound_enabled
        if type(selected_sound_enabled) is not bool:
            raise VMError("The sound setting must be true or false.")
        selected_cdrom_slot = cfg.cdrom_slot if cdrom_slot is None else cdrom_slot
        validate_cdrom_slot(selected_cdrom_slot)
        iso_value = self._validate_iso(iso) if iso else None
        validate_boot_order_for_media(
            cfg.boot_order, floppy_a=bool(cfg.floppy_a_path), cdrom=bool(iso_value)
        )
        updated = VMConfig(**{
            **asdict(cfg),
            "memory_mib": memory_mib,
            "cpu_ips": cpu_ips,
            "cpu_model": selected_cpu_model,
            "chipset": selected_chipset,
            "sound_enabled": selected_sound_enabled,
            "cdrom_slot": selected_cdrom_slot,
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
        validate_cpu_model(cfg.cpu_model)
        cpu_model = f"model={cfg.cpu_model}, " if cfg.cpu_model != "default" else ""
        lines = [
            "# Generated by ChromeOS VM Studio. Edit the VM profile in the app.",
            f"megs: {cfg.memory_mib}",
            f"cpu: {cpu_model}count=1, ips={cfg.cpu_ips}, reset_on_triple_fault=1",
            f"pci: enabled=1, chipset={cfg.chipset}",
            "ata0: enabled=1, ioaddr1=0x1f0, ioaddr2=0x3f0, irq=14",
            "ata1: enabled=1, ioaddr1=0x170, ioaddr2=0x370, irq=15",
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
        if cfg.sound_enabled:
            # Bochs documents the SB16 ISA device and SDL wave-output driver.
            # MIDI and microphone input stay disabled.
            lines.extend([
                "sound: waveoutdrv=sdl, waveindrv=dummy, midioutdrv=dummy",
                "speaker: enabled=1, mode=sound, volume=10",
                "sb16: enabled=1, midimode=0, wavemode=1, dmatimer=600000",
            ])
        cylinders, heads, sectors, _image_bytes = disk_geometry(cfg.disk_gib)
        lines.append('ata0-master: type=disk, mode=flat, path=' + bochs_quote(cfg.disk_path) +
                     f", cylinders={cylinders}, heads={heads}, spt={sectors}, translation=auto")
        if cfg.secondary_disk_path and cfg.secondary_disk_gib is not None:
            c2, h2, s2, _bytes2 = disk_geometry(cfg.secondary_disk_gib)
            lines.append(
                "ata1-master: type=disk, mode=flat, path=" + bochs_quote(cfg.secondary_disk_path)
                + f", cylinders={c2}, heads={h2}, spt={s2}, translation=auto"
            )
        if cfg.floppy_a_path:
            lines.append(
                "floppya: image=" + bochs_quote(cfg.floppy_a_path)
                + ", status=inserted, write_protected=1"
            )
        if cfg.floppy_b_path:
            lines.append(
                "floppyb: image=" + bochs_quote(cfg.floppy_b_path)
                + ", status=inserted, write_protected=1"
            )
        if cfg.iso_path:
            lines.append(
                f"{cfg.cdrom_slot}: type=cdrom, path=" + bochs_quote(cfg.iso_path) + ", status=inserted"
            )
        boot_order = cfg.boot_order or default_boot_order(cfg)
        lines.append("boot: " + ", ".join(boot_order))
        lines.extend([
            "log: " + bochs_quote((self.vm_path(cfg.name) / "logs/bochs.log").resolve()),
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

    def validate_bochsrc(self, name: str) -> None:
        self.validate_bochsrc_for_vm(
            name, self.active_bochsrc_path(name).read_text(encoding="utf-8")
        )


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

    @staticmethod
    def _process_start_ticks(pid: int) -> int | None:
        """Read Linux /proc starttime (stat field 22), robust to spaces in comm."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 1:].split()
            # The suffix begins at field 3 (state); starttime is field 22.
            return int(fields[19])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _process_cmdline(pid: int) -> list[str]:
        try:
            return [os.fsdecode(part) for part in
                    Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            return []

    @staticmethod
    def _reap_child(pid: int) -> None:
        """Reap an exited child if this process still owns it."""
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    def _write_pid_record(self, name: str, pid: int, start_ticks: int,
                          config_file: Path) -> None:
        path = self.vm_path(name) / "runtime/bochs.pid"
        temporary = path.with_suffix(".pid.tmp")
        temporary.write_text(json.dumps({
            "pid": pid,
            "start_ticks": start_ticks,
            "config": str(config_file.resolve()),
        }) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _running_pid(self, name: str) -> int | None:
        self.load(name)
        pid_file = self.vm_path(name) / "runtime/bochs.pid"
        generated_config = (self.vm_path(name) / "bochsrc").resolve()
        custom_config = self.custom_bochsrc_path(name).resolve()
        allowed_configs = {str(generated_config), str(custom_config)}
        try:
            raw = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None

        # Older releases wrote only a PID. Never trust that number on its own:
        # migrate it only if the live command line still names this VM's exact
        # Bochs config file. Otherwise it could refer to an unrelated process
        # after PID reuse.
        try:
            if raw.startswith("{"):
                record = json.loads(raw)
                pid = record["pid"]
                recorded_start = record["start_ticks"]
                recorded_config = record["config"]
                if (type(pid) is not int or pid <= 1 or
                        type(recorded_start) is not int or recorded_start <= 0 or
                        recorded_config not in allowed_configs):
                    raise ValueError("invalid process identity")
                config_file = Path(recorded_config)
            else:
                pid = int(raw)
                recorded_start = None
                config_file = self.active_bochsrc_path(name).resolve()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            pid_file.unlink(missing_ok=True)
            return None

        current_start = self._process_start_ticks(pid)
        argv = self._process_cmdline(pid)
        if current_start is None or not argv:
            # If this was a process started by this manager, reap its zombie
            # instead of leaving it behind after the user closes Bochs.
            if recorded_start is not None and (
                    current_start is None or current_start == recorded_start):
                self._reap_child(pid)
            pid_file.unlink(missing_ok=True)
            return None
        if ("-f" not in argv or str(config_file) not in argv or
                (recorded_start is not None and current_start != recorded_start)):
            pid_file.unlink(missing_ok=True)
            return None

        if recorded_start is None:
            # Upgrade a legacy PID marker only after verifying both the config
            # argument and the kernel process start time.
            self._write_pid_record(name, pid, current_start, config_file)
        return pid

    def is_running(self, name: str) -> bool:
        return self._running_pid(name) is not None

    def start(self, name: str, bochs_override: str | None = None) -> int:
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError(f"VM '{name}' appears to be running already.")
        executable = self.find_bochs(bochs_override)
        if not executable:
            raise VMError("Bochs was not found. Install it with: sudo apt install bochs bochs-sdl")
        self.validate_disk_image(name, cfg)
        self.validate_secondary_disk_image(name, cfg)
        self.validate_floppy_image(name, cfg)
        self.validate_floppy_b_image(name, cfg)
        self.write_bochsrc(cfg)
        config_file = self.active_bochsrc_path(name).resolve()
        try:
            self.validate_bochsrc_for_vm(name, config_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise VMError(f"Could not read the active Bochs configuration: {exc}") from exc
        log = self.vm_path(name) / "logs/launcher.log"
        # ROM paths live in the generated config. Do not add command-line
        # overrides here: that would silently override a user's custom ROM
        # selections in bochsrc.custom.
        argv = [executable, "-q", "-f", str(config_file)]
        try:
            with log.open("ab") as output:
                # Use posix_spawn rather than leaving a Popen object behind for
                # a long-lived GUI process. The child inherits only its log
                # streams and /dev/null stdin, and starts in its own session.
                actions = [
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, output.fileno(), 1),
                    (os.POSIX_SPAWN_DUP2, output.fileno(), 2),
                    (os.POSIX_SPAWN_CLOSE, output.fileno()),
                ]
                pid = os.posix_spawn(
                    executable, argv, os.environ.copy(),
                    file_actions=actions, setsid=True,
                )
        except (AttributeError, OSError) as exc:
            raise VMError(f"Could not launch Bochs: {exc}") from exc
        start_ticks = self._process_start_ticks(pid)
        live_argv = self._process_cmdline(pid)
        if (start_ticks is None or "-f" not in live_argv or str(config_file) not in live_argv):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._reap_child(pid)
            raise VMError("Could not verify the launched Bochs process identity; it was stopped for safety.")
        try:
            self._write_pid_record(name, pid, start_ticks, config_file)
        except OSError as exc:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._reap_child(pid)
            raise VMError(f"Could not record the Bochs process identity: {exc}") from exc
        return pid

    def stop(self, name: str) -> None:
        pid_file = self.vm_path(name) / "runtime/bochs.pid"
        pid = self._running_pid(name)
        if pid is None:
            raise VMError(f"VM '{name}' is not running.")
        try:
            record = json.loads(pid_file.read_text(encoding="utf-8"))
            expected_start = record["start_ticks"]
            config_file = record["config"]
            allowed_configs = {
                str((self.vm_path(name) / "bochsrc").resolve()),
                str(self.custom_bochsrc_path(name).resolve()),
            }
            if (record["pid"] != pid or config_file not in allowed_configs):
                raise ValueError("process identity changed")
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                try:
                    pidfd = os.pidfd_open(pid, 0)
                except OSError as exc:
                    if exc.errno not in {errno.ENOSYS, errno.EINVAL}:
                        raise
                    pidfd = None
                if pidfd is not None:
                    try:
                        # A pidfd pins the process identity, preventing a PID
                        # reuse race between verification and signal delivery.
                        if (self._process_start_ticks(pid) != expected_start or
                                config_file not in self._process_cmdline(pid)):
                            raise VMError("Bochs process identity changed; refusing to signal an unrelated process.")
                        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                    finally:
                        os.close(pidfd)
                else:
                    if (self._process_start_ticks(pid) != expected_start or
                            config_file not in self._process_cmdline(pid)):
                        raise VMError("Bochs process identity changed; refusing to signal an unrelated process.")
                    os.kill(pid, signal.SIGTERM)
            else:
                # Older Linux kernels without pidfds get a last-moment identity
                # check before the traditional signal call.
                if (self._process_start_ticks(pid) != expected_start or
                        config_file not in self._process_cmdline(pid)):
                    raise VMError("Bochs process identity changed; refusing to signal an unrelated process.")
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            return
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise VMError(f"Could not safely stop Bochs: {exc}") from exc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.is_running(name):
                return
            time.sleep(0.1)
        raise VMError("Bochs did not exit after SIGTERM. Close its emulator window; the process was not force-killed.")

    def diagnostic_report(self, name: str) -> str:
        """Build a non-destructive VM, emulator, media, disk, and config report."""
        cfg = self.load(name)
        pid = self._running_pid(name)
        bochs = self.find_bochs()
        system_bios = self._find_bios("BIOS-bochs-latest")
        vga_bios = self._find_bios("VGABIOS-lgpl-latest")
        custom_config = self.custom_bochsrc_path(name)
        active_config = self.active_bochsrc_path(name)
        config_kind = "custom" if custom_config.is_file() else "generated"

        lines = [
            f"VM: {cfg.name}",
            f"State: Running (PID {pid})" if pid is not None else "State: Stopped",
            f"Backend: {cfg.backend} (x86 software emulation)",
            f"Bochs: {bochs or 'not installed (apt install bochs bochs-sdl)'}",
            f"System BIOS: {system_bios or 'missing (apt install bochsbios)'}",
            f"VGA BIOS: {vga_bios or 'missing (apt install vgabios)'}",
            f"Active config ({config_kind}): {active_config}",
        ]

        if active_config.is_file():
            try:
                self.validate_bochsrc_for_vm(
                    name, active_config.read_text(encoding="utf-8")
                )
                lines.append("Bochs config validation: OK (static checks only)")
            except (VMError, OSError) as exc:
                lines.append(f"Bochs config validation: PROBLEM — {exc}")
        else:
            lines.append("Bochs config validation: not generated yet; created at first boot")

        def disk_line(label: str, configured_path: str | None, validator) -> None:
            if configured_path is None:
                lines.append(f"{label}: not attached")
                return
            try:
                disk = validator()
                if disk is None:
                    lines.append(f"{label}: not attached")
                    return
                info = disk.stat()
                allocated = getattr(info, "st_blocks", None)
                if isinstance(allocated, int):
                    allocated_bytes = allocated * 512
                    storage = f"{allocated_bytes:,} allocated / {info.st_size:,} logical bytes"
                    if allocated_bytes < info.st_size:
                        storage += " (sparse)"
                else:
                    storage = f"{info.st_size:,} logical bytes"
                lines.append(f"{label}: OK — {storage}")
            except (VMError, OSError) as exc:
                lines.append(f"{label}: PROBLEM — {exc}")

        def media_line(label: str, configured_path: str | None, validator) -> None:
            if configured_path is None:
                lines.append(f"{label}: not attached")
                return
            try:
                media = Path(validator())
                if not media.is_file() or not os.access(media, os.R_OK):
                    raise VMError(f"missing or unreadable file: {media}")
                lines.append(f"{label}: OK — {media}")
            except (VMError, OSError, TypeError) as exc:
                lines.append(f"{label}: PROBLEM — {exc}")

        disk_line("Primary disk", cfg.disk_path,
                  lambda: self.validate_disk_image(name, cfg))
        disk_line("Secondary disk", cfg.secondary_disk_path,
                  lambda: self.validate_secondary_disk_image(name, cfg))
        media_line("CD/DVD ISO", cfg.iso_path,
                   lambda: self._validate_iso(cfg.iso_path) if cfg.iso_path else None)
        media_line("Floppy A", cfg.floppy_a_path,
                   lambda: self.validate_floppy_image(name, cfg))
        media_line("Floppy B", cfg.floppy_b_path,
                   lambda: self.validate_floppy_b_image(name, cfg))

        for log_name in ("bochs.log", "launcher.log"):
            log_path = self.vm_path(name) / "logs" / log_name
            if log_path.is_file():
                try:
                    lines.append(f"{log_name}: present ({log_path.stat().st_size:,} bytes)")
                except OSError as exc:
                    lines.append(f"{log_name}: unavailable ({exc})")
            else:
                lines.append(f"{log_name}: not created yet")

        lines.extend([
            "",
            "Guest shutdown guidance:",
            "Use the guest operating system's own Shut Down command first.",
            "The manager's Stop action sends SIGTERM to the Bochs process; it is not an in-guest ACPI/power-off request.",
            "Stopping while the guest is writing to its disk can lose unsaved work or damage the guest filesystem.",
        ])
        return "\n".join(lines)

    def delete(self, name: str) -> None:
        self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before deleting it.")
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("Refusing to delete a VM directory outside the storage folder.")
        shutil.rmtree(directory)
