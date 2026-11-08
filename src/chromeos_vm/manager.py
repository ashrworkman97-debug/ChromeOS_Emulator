"""VM storage, host checks, and crosvm process management. No QEMU backend."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
CONFIG_VERSION = 1
MIN_MEMORY_MIB, MAX_MEMORY_MIB, MAX_CPUS, MAX_DISK_GIB = 256, 262144, 64, 2048


class VMError(RuntimeError):
    """A user-correctable VM or host configuration error."""


@dataclass(frozen=True)
class VMConfig:
    name: str
    memory_mib: int
    cpus: int
    disk_gib: int
    disk_path: str
    kernel_path: str | None = None
    backend: str = "crosvm"
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
        validate_resources(config.memory_mib, config.cpus, config.disk_gib)
        if config.backend != "crosvm" or config.disk_format != "raw":
            raise VMError("Only the crosvm backend and raw disks are supported.")
        return config


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise VMError("Names must start with a letter/number and use only letters, numbers, - or _ (48 chars max).")


def validate_resources(memory_mib: int, cpus: int, disk_gib: int) -> None:
    if any(type(v) is not int for v in (memory_mib, cpus, disk_gib)):
        raise VMError("Memory, CPU count, and disk size must be whole numbers.")
    if not MIN_MEMORY_MIB <= memory_mib <= MAX_MEMORY_MIB:
        raise VMError(f"Memory must be {MIN_MEMORY_MIB}–{MAX_MEMORY_MIB} MiB.")
    if not 1 <= cpus <= MAX_CPUS:
        raise VMError(f"CPU count must be 1–{MAX_CPUS}.")
    if not 1 <= disk_gib <= MAX_DISK_GIB:
        raise VMError(f"Disk size must be 1–{MAX_DISK_GIB} GiB.")


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

    def create(self, name: str, memory_mib: int = 4096, cpus: int = 2,
               disk_gib: int = 32, kernel: Path | str | None = None) -> VMConfig:
        validate_name(name)
        validate_resources(memory_mib, cpus, disk_gib)
        kernel_value = None
        if kernel is not None:
            kp = Path(kernel).expanduser().resolve()
            if not kp.is_file() or not os.access(kp, os.R_OK):
                raise VMError(f"Kernel image is not readable: {kp}")
            kernel_value = str(kp)
        directory = self.vm_path(name)
        if directory.exists():
            raise VMError(f"VM '{name}' already exists.")
        directory.mkdir()
        try:
            disk = directory / "disk.raw"
            with disk.open("xb") as stream:
                stream.truncate(disk_gib * 1024**3)
            config = VMConfig(name, memory_mib, cpus, disk_gib, str(disk.resolve()), kernel_value)
            self._save(config)
            (directory / "logs").mkdir()
            (directory / "runtime").mkdir()
            return config
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def set_kernel(self, name: str, kernel: Path | str) -> VMConfig:
        config = self.load(name)
        kp = Path(kernel).expanduser().resolve()
        if not kp.is_file() or not os.access(kp, os.R_OK):
            raise VMError(f"Kernel image is not readable: {kp}")
        updated = VMConfig(**{**asdict(config), "kernel_path": str(kp)})
        self._save(updated)
        return updated

    def list_vms(self) -> list[VMConfig]:
        return [self.load(p.name) for p in sorted(self.home.iterdir())
                if p.is_dir() and not p.is_symlink() and (p / "vm.json").is_file()]

    def is_running(self, name: str) -> bool:
        self.load(name)
        pid_file = self.vm_path(name) / "runtime/crosvm.pid"
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
        return True

    def environment(self, crosvm_override: str | None = None) -> dict[str, Any]:
        kvm = Path("/dev/kvm")
        return {"platform": platform.system(), "architecture": platform.machine(),
                "crosvm": crosvm_override or shutil.which("crosvm"),
                "kvm_device": kvm.exists(), "kvm_readable": os.access(kvm, os.R_OK),
                "kvm_writable": os.access(kvm, os.W_OK)}

    def start(self, name: str, crosvm_override: str | None = None) -> int:
        cfg = self.load(name)
        if self.is_running(name):
            raise VMError(f"VM '{name}' appears to be running.")
        if not cfg.kernel_path:
            raise VMError("No kernel configured; use 'chromeos-vm configure NAME --kernel PATH'.")
        kernel, disk = Path(cfg.kernel_path), Path(cfg.disk_path)
        if not kernel.is_file():
            raise VMError(f"Configured kernel is missing: {kernel}")
        if not disk.is_file():
            raise VMError(f"Virtual disk is missing: {disk}")
        env = self.environment(crosvm_override)
        if env["platform"] != "Linux":
            raise VMError("The implemented backend requires Linux with KVM.")
        if not env["crosvm"]:
            raise VMError("crosvm was not found; install it first.")
        if not env["kvm_device"]:
            raise VMError("/dev/kvm is missing; expose hardware virtualization to this host/container.")
        if not env["kvm_readable"] or not env["kvm_writable"]:
            raise VMError("The current user needs read/write access to /dev/kvm.")
        runtime = self.vm_path(name) / "runtime"
        sock = runtime / "crosvm.sock"
        sock.unlink(missing_ok=True)
        command = [str(env["crosvm"]), "run", "--cpus", str(cfg.cpus), "--mem",
                   str(cfg.memory_mib), "--block", str(disk), "-s", str(sock),
                   "--serial", "type=stdout,console,stdin", str(kernel)]
        log = self.vm_path(name) / "logs/crosvm.log"
        try:
            with log.open("ab") as output:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except OSError as exc:
            raise VMError(f"Could not start crosvm: {exc}") from exc
        (runtime / "crosvm.pid").write_text(f"{proc.pid}\n", encoding="ascii")
        return proc.pid

    def stop(self, name: str, crosvm_override: str | None = None) -> None:
        self.load(name)
        runtime = self.vm_path(name) / "runtime"
        pid_file, sock = runtime / "crosvm.pid", runtime / "crosvm.sock"
        if not self.is_running(name):
            raise VMError(f"VM '{name}' is not running.")
        exe = crosvm_override or shutil.which("crosvm")
        if not exe:
            raise VMError("crosvm not found; cannot request orderly shutdown.")
        try:
            result = subprocess.run([exe, "stop", str(sock)], capture_output=True, text=True,
                                    check=False, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VMError(f"Could not request crosvm shutdown: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise VMError("crosvm refused the stop request; VM was not force-killed. " + detail)
        pid_file.unlink(missing_ok=True)

    def delete(self, name: str) -> None:
        self.load(name)
        if self.is_running(name):
            raise VMError("Stop the VM before deleting it.")
        directory = self.vm_path(name)
        if directory.is_symlink() or directory.resolve().parent != self.home.resolve():
            raise VMError("Refusing to delete a VM directory outside the storage folder.")
        shutil.rmtree(directory)
