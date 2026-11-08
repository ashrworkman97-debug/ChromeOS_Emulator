"""Command-line interface."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from . import __version__
from .manager import VMError, VMManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chromeos-vm",
        description="Create and manage crosvm virtual machines without QEMU.")
    parser.add_argument("--home", type=Path, help="VM storage directory")
    parser.add_argument("--version", action="version", version=f"ChromeOS VM Studio {__version__}")
    cmds = parser.add_subparsers(dest="command", required=True)
    cmds.add_parser("check", help="check host crosvm/KVM readiness")
    create = cmds.add_parser("create", help="create a VM and sparse raw disk")
    create.add_argument("name")
    create.add_argument("--memory-mib", type=int, default=4096)
    create.add_argument("--cpus", type=int, default=2)
    create.add_argument("--disk-gib", type=int, default=32)
    create.add_argument("--kernel", type=Path)
    configure = cmds.add_parser("configure", help="configure guest kernel")
    configure.add_argument("name")
    configure.add_argument("--kernel", type=Path, required=True)
    cmds.add_parser("list", help="list VMs")
    info = cmds.add_parser("info", help="show VM details")
    info.add_argument("name")
    start = cmds.add_parser("start", help="start VM with crosvm")
    start.add_argument("name")
    start.add_argument("--crosvm")
    stop = cmds.add_parser("stop", help="request orderly crosvm shutdown")
    stop.add_argument("name")
    stop.add_argument("--crosvm")
    delete = cmds.add_parser("delete", help="delete a stopped VM and its disk")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = VMManager(args.home)
    try:
        if args.command == "check":
            e = manager.environment()
            print(f"Host: {e['platform']} / {e['architecture']}")
            print(f"crosvm: {e['crosvm'] or 'not found'}")
            print(f"/dev/kvm: {'present' if e['kvm_device'] else 'missing'}")
            ready = e['platform'] == 'Linux' and e['crosvm'] and e['kvm_device'] and e['kvm_readable'] and e['kvm_writable']
            print("Result: ready" if ready else "Result: not ready to start VMs")
            if not ready:
                print("This release requires Linux, crosvm, and readable/writable /dev/kvm.")
            return 0 if ready else 1
        if args.command == "create":
            c = manager.create(args.name, args.memory_mib, args.cpus, args.disk_gib, args.kernel)
            print(f"Created {c.name}: {c.memory_mib} MiB RAM, {c.cpus} CPUs, {c.disk_gib} GiB sparse raw disk.")
            if not c.kernel_path:
                print(f"Configure a kernel: chromeos-vm configure {c.name} --kernel PATH")
        elif args.command == "configure":
            c = manager.set_kernel(args.name, args.kernel)
            print(f"Configured kernel for {c.name}: {c.kernel_path}")
        elif args.command == "list":
            configs = manager.list_vms()
            if not configs:
                print("No VMs yet.")
            for c in configs:
                print(f"{c.name}: {'running' if manager.is_running(c.name) else 'stopped'}, {c.cpus} CPUs, {c.memory_mib} MiB, {c.disk_gib} GiB")
        elif args.command == "info":
            c = manager.load(args.name)
            print(f"Name: {c.name}\nState: {'running' if manager.is_running(c.name) else 'stopped'}\nBackend: {c.backend}\nMemory: {c.memory_mib} MiB\nCPUs: {c.cpus}\nDisk: {c.disk_path}\nKernel: {c.kernel_path or 'not configured'}\nLog: {manager.vm_path(c.name) / 'logs/crosvm.log'}")
        elif args.command == "start":
            print(f"Started {args.name}, crosvm PID {manager.start(args.name, args.crosvm)}")
        elif args.command == "stop":
            manager.stop(args.name, args.crosvm)
            print(f"Sent orderly stop request to {args.name}.")
        elif args.command == "delete":
            manager.load(args.name)
            if not args.yes and input(f"Permanently delete {args.name} and its disk? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled.")
                return 0
            manager.delete(args.name)
            print(f"Deleted {args.name}.")
        return 0
    except (VMError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
