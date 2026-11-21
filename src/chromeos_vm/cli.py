"""Command-line maintenance interface for ChromeOS VM Studio."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from . import __version__
from .manager import (
    CHIPSET_LABELS, CPU_MODEL_LABELS, IDE_CDROM_SLOT_LABELS, VMError, VMManager,
)


def parse_boot_order(value: str) -> tuple[str, ...] | None:
    """Parse 'auto' or a comma-separated Bochs boot-device sequence."""
    if value.strip().lower() == "auto":
        return None
    return tuple(part.strip().lower() for part in value.split(","))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chromeos-vm",
        description="Manage native Bochs PC emulators on ChromeOS Linux (Crostini).")
    parser.add_argument("--home", type=Path, help="VM storage directory")
    parser.add_argument("--version", action="version", version=f"ChromeOS VM Studio {__version__}")
    cmds = parser.add_subparsers(dest="command", required=True)
    cmds.add_parser("check", help="check Crostini and Bochs readiness")
    update = cmds.add_parser("update", help="check for and install app updates")
    update.add_argument("--force", action="store_true", help="ignore the automatic update-check cooldown")
    create = cmds.add_parser("create", help="create a VM profile and sparse raw disk")
    create.add_argument("name")
    create.add_argument("--memory-mib", type=int, default=512)
    create.add_argument("--disk-gib", type=int, default=8)
    create.add_argument("--cpu-ips", type=int, default=10_000_000)
    create.add_argument("--cpu-model", choices=tuple(CPU_MODEL_LABELS), default="default",
                        help="pre-defined Bochs x86 CPU model")
    create.add_argument("--chipset", choices=tuple(CHIPSET_LABELS), default="i440fx",
                        help="Bochs PCI chipset (i430fx, i440fx, or i440bx)")
    create.add_argument("--secondary-disk-gib", type=int,
                        help="optional second sparse IDE hard disk size")
    create.add_argument("--cdrom-slot", choices=tuple(IDE_CDROM_SLOT_LABELS),
                        default="ata0-slave", help="IDE slave position for the CD-ROM")
    create.add_argument(
        "--boot-order", type=parse_boot_order, default=None,
        help="BIOS boot order: auto or comma-separated floppy,cdrom,disk,network",
    )
    create.add_argument("--iso", type=Path)
    boot_order = cmds.add_parser("set-boot-order", help="set or restore a VM's BIOS boot priority")
    boot_order.add_argument("name")
    boot_order.add_argument("--order", type=parse_boot_order, required=True,
                            help="auto or comma-separated floppy,cdrom,disk,network")
    attach_disk = cmds.add_parser("attach-disk", help="create or reattach the private secondary IDE disk")
    attach_disk.add_argument("name")
    attach_disk.add_argument("--disk-gib", type=int, default=8)
    detach_disk = cmds.add_parser("detach-disk", help="detach the secondary disk without deleting its data")
    detach_disk.add_argument("name")
    for command in ("start", "stop", "info"):
        sub = cmds.add_parser(command)
        sub.add_argument("name")
    diagnose = cmds.add_parser("diagnose", help="run read-only VM health checks")
    diagnose.add_argument("name")
    cmds.add_parser("list", help="list virtual machines")
    delete = cmds.add_parser("delete", help="delete a stopped VM and its disk")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "update":
        from .updater import check_for_updates
        result = check_for_updates(force=args.force)
        print(result.message)
        return 0 if result.status in {"current", "updated"} else 1
    manager = VMManager(args.home)
    try:
        if args.command == "check":
            bochs = manager.find_bochs()
            print(f"Application version: {__version__}")
            print(f"Application code: {Path(__file__).resolve()}")
            print(f"Host: {sys.platform}")
            print(f"Bochs: {bochs or 'not installed'}")
            system_bios = manager._find_bios("BIOS-bochs-latest")
            vga_bios = manager._find_bios("VGABIOS-lgpl-latest")
            print(f"System BIOS: {system_bios or 'missing (install bochsbios)'}")
            print(f"VGA BIOS: {vga_bios or 'missing (install vgabios)'}")
            print(f"Tkinter: {'available' if _tk_available() else 'not installed'}")
            print("ChromeOS Linux integration: use the Linux development environment (Crostini).")
            ready = bool(bochs and system_bios and vga_bios and _tk_available())
            print("Result: ready" if ready else "Result: install missing desktop packages.")
            return 0 if ready else 1
        if args.command == "create":
            cfg = manager.create(
                args.name, args.memory_mib, args.disk_gib, args.cpu_ips, args.iso,
                cpu_model=args.cpu_model, chipset=args.chipset,
                secondary_disk_gib=args.secondary_disk_gib,
                cdrom_slot=args.cdrom_slot,
                boot_order=args.boot_order,
            )
            second = (
                f" plus a {cfg.secondary_disk_gib} GiB IDE1-master disk"
                if cfg.secondary_disk_gib is not None else ""
            )
            print(
                f"Created {cfg.name} with a {cfg.disk_gib} GiB primary sparse disk{second}, "
                f"CPU model {cfg.cpu_model}, {cfg.chipset} chipset, "
                f"and CD-ROM position {cfg.cdrom_slot}. "
                f"BIOS boot order: {', '.join(cfg.boot_order) if cfg.boot_order else 'automatic'}."
            )
        elif args.command == "list":
            for cfg in manager.list_vms():
                status = "running" if manager.is_running(cfg.name) else "stopped"
                disk_summary = f"{cfg.disk_gib} GiB primary disk"
                if cfg.secondary_disk_gib is not None:
                    disk_summary += f" + {cfg.secondary_disk_gib} GiB secondary disk"
                print(f"{cfg.name}: {status}, {cfg.memory_mib} MiB RAM, {disk_summary}")
        elif args.command == "diagnose":
            print(manager.diagnostic_report(args.name))
        elif args.command == "info":
            cfg = manager.load(args.name)
            print(
                f"Name: {cfg.name}\nBackend: {cfg.backend}\n"
                f"State: {'running' if manager.is_running(cfg.name) else 'stopped'}\n"
                f"RAM: {cfg.memory_mib} MiB\nCPU model: {cfg.cpu_model}\n"
                f"Chipset: {cfg.chipset}\nCPU IPS: {cfg.cpu_ips:,}\n"
                f"Primary disk: {cfg.disk_path}\n"
                f"Secondary disk: {cfg.secondary_disk_path or 'not attached'}\n"
                f"CD-ROM IDE position: {cfg.cdrom_slot}\n"
                f"BIOS boot order: {', '.join(cfg.boot_order) if cfg.boot_order else 'automatic (media-aware)'}\n"
                f"ISO: {cfg.iso_path or 'not attached'}\n"
                f"Config: {manager.vm_path(cfg.name) / 'bochsrc'}"
            )
        elif args.command == "attach-disk":
            cfg = manager.attach_secondary_disk(args.name, args.disk_gib)
            print(f"Attached {cfg.secondary_disk_gib} GiB disk at IDE1 master for {cfg.name}.")
        elif args.command == "detach-disk":
            path = manager.detach_secondary_disk(args.name)
            print(f"Detached {path.name}; disk data remains in the VM folder.")
        elif args.command == "set-boot-order":
            cfg = manager.set_boot_order(args.name, args.order)
            order = ", ".join(cfg.boot_order) if cfg.boot_order else "automatic (media-aware)"
            print(f"BIOS boot order for {cfg.name}: {order}")
        elif args.command == "start":
            print(f"Started {args.name}; Bochs PID {manager.start(args.name)}")
        elif args.command == "stop":
            manager.stop(args.name)
            print(f"Stopped {args.name}.")
        elif args.command == "delete":
            manager.load(args.name)
            if not args.yes and input(f"Permanently delete {args.name} and its virtual disk? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled.")
                return 0
            manager.delete(args.name)
            print(f"Deleted {args.name}.")
        return 0
    except (VMError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
