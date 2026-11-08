"""Command-line maintenance interface for ChromeOS VM Studio."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from . import __version__
from .manager import VMError, VMManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chromeos-vm",
        description="Manage native Bochs PC emulators on ChromeOS Linux (Crostini).")
    parser.add_argument("--home", type=Path, help="VM storage directory")
    parser.add_argument("--version", action="version", version=f"ChromeOS VM Studio {__version__}")
    cmds = parser.add_subparsers(dest="command", required=True)
    cmds.add_parser("check", help="check Crostini and Bochs readiness")
    create = cmds.add_parser("create", help="create a VM profile and sparse raw disk")
    create.add_argument("name")
    create.add_argument("--memory-mib", type=int, default=512)
    create.add_argument("--disk-gib", type=int, default=8)
    create.add_argument("--cpu-ips", type=int, default=10_000_000)
    create.add_argument("--iso", type=Path)
    for command in ("start", "stop", "info"):
        sub = cmds.add_parser(command)
        sub.add_argument("name")
    cmds.add_parser("list", help="list virtual machines")
    delete = cmds.add_parser("delete", help="delete a stopped VM and its disk")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = VMManager(args.home)
    try:
        if args.command == "check":
            bochs = manager.find_bochs()
            print(f"Host: {sys.platform}")
            print(f"Bochs: {bochs or 'not installed'}")
            print(f"Tkinter: {'available' if _tk_available() else 'not installed'}")
            print("ChromeOS Linux integration: use the Linux development environment (Crostini).")
            ready = bool(bochs and _tk_available())
            print("Result: ready" if ready else "Result: install missing desktop packages.")
            return 0 if ready else 1
        if args.command == "create":
            cfg = manager.create(args.name, args.memory_mib, args.disk_gib, args.cpu_ips, args.iso)
            print(f"Created {cfg.name} with a {cfg.disk_gib} GiB sparse disk.")
        elif args.command == "list":
            for cfg in manager.list_vms():
                status = "running" if manager.is_running(cfg.name) else "stopped"
                print(f"{cfg.name}: {status}, {cfg.memory_mib} MiB RAM, {cfg.disk_gib} GiB disk")
        elif args.command == "info":
            cfg = manager.load(args.name)
            print(f"Name: {cfg.name}\nBackend: {cfg.backend}\nState: {'running' if manager.is_running(cfg.name) else 'stopped'}\nRAM: {cfg.memory_mib} MiB\nDisk: {cfg.disk_path}\nISO: {cfg.iso_path or 'not attached'}\nConfig: {manager.vm_path(cfg.name) / 'bochsrc'}")
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
