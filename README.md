# ChromeOS VM Studio

**A non-QEMU ChromeOS virtual machine manager, built around Google's `crosvm`.**

ChromeOS VM Studio is intended to grow into a complete desktop VM application for creating, configuring, importing, and running ChromeOS virtual machines. It will use a real virtual machine monitor—not a browser mockup or a fake ChromeOS-themed web page.

> **Development status: early foundation (0.1.0).** This release can create and manage VM definitions and sparse raw virtual disks, check the host's KVM readiness, and launch/stop a guest when a compatible kernel image and a working crosvm/KVM installation are supplied. It does **not** yet import or boot an ordinary ChromeOS recovery image end-to-end. A full ChromeOS boot workflow, graphical manager, networking, firmware/TPM integration, and guest integration are roadmap work.

## Why crosvm instead of QEMU?

The project uses [crosvm](https://github.com/google/crosvm), the Rust virtual machine monitor originally developed for ChromeOS. crosvm is designed around hardware-assisted virtualization and virtio devices. The application will call crosvm directly; it will not shell out to QEMU, depend on QEMU, or present QEMU as its VM engine.

crosvm is not a traditional full hardware emulator: it relies on a supported host hypervisor and provides virtual devices to a guest. That is a good fit for a modern ChromeOS-focused VM manager, but it means host virtualization support is required. See the [crosvm book](https://crosvm.dev/book/) for upstream build and usage details.

## Current capabilities

- Create named VM definitions with configurable memory, virtual CPUs, and disk capacity.
- Create sparse **raw** virtual disks; virtual capacity does not have to be allocated immediately.
- Store each VM's configuration, disk, runtime files, and log in its own directory.
- Inspect and list VMs, configure a guest kernel, and delete a stopped VM.
- Check for Linux, the `crosvm` executable, `/dev/kvm`, and read/write access to KVM.
- Start a configured guest through crosvm and request an orderly stop through crosvm's control socket.
- Keep crosvm's normal sandboxing behavior enabled; the manager does not disable the sandbox.
- Run the test suite using Python's standard library. There are no runtime Python dependencies.

## Requirements

- Python 3.11 or newer.
- Linux for the currently implemented run backend.
- A working `crosvm` installation.
- A host kernel with KVM enabled and accessible at `/dev/kvm`.
- A guest kernel image and a bootable disk layout for the guest OS.

A Linux container does not automatically receive KVM access just because its host is Linux. On a Chromebook, the Linux development environment must expose `/dev/kvm`; otherwise `chromeos-vm check` will report that VMs cannot start there.

## Install

```bash
python -m pip install .
```

Install/build crosvm separately using the [official crosvm instructions](https://crosvm.dev/book/).

## Quick start

Check whether the current host is ready:

```bash
chromeos-vm check
```

Create a VM and a 32 GiB sparse raw disk:

```bash
chromeos-vm create chromeos-test --memory-mib 4096 --cpus 2 --disk-gib 32
```

Add a guest kernel image:

```bash
chromeos-vm configure chromeos-test --kernel /path/to/guest-kernel
```

Inspect and start it:

```bash
chromeos-vm info chromeos-test
chromeos-vm start chromeos-test
```

The crosvm log is stored at `<VM directory>/logs/crosvm.log`. Request an orderly stop with:

```bash
chromeos-vm stop chromeos-test
```

List or delete VMs:

```bash
chromeos-vm list
chromeos-vm delete chromeos-test
```

VMs are stored by default in `~/.local/share/chromeos-vm-studio`. Override the location with `--home PATH` before the subcommand or with the `CHROMEOS_VM_HOME` environment variable.

## Project roadmap

The work is being developed in small, testable stages:

1. **VM manager foundation** — VM definitions, raw disk creation, host checks, crosvm lifecycle commands, and automated tests. *(Started in 0.1.0.)*
2. **ChromeOS guest import** — inspect supported ChromeOS/ChromiumOS image packages, identify boot/kernel/root/stateful partitions, and build a reproducible import workflow. Do not redistribute proprietary OS images; users should provide images they are authorized to use.
3. **Boot compatibility** — determine and implement the required firmware/boot flow, kernel command line, virtual devices, and verified-boot/TPM requirements for supported guest images.
4. **VM experience** — graphical VM library, create/edit wizard, console/display, progress and logs, snapshots, and safe disk management.
5. **Guest integration** — networking, audio, display acceleration where supported, input, shared folders, USB/device options, and graceful shutdown.
6. **Host support** — broaden beyond Linux/KVM only when another non-QEMU backend is supported and can be tested reliably.
7. **Release quality** — compatibility matrix, packaging, documentation, security review, and repeatable end-to-end boot tests.

A feature is not considered complete merely because a command was constructed: it must have tests, accurate documentation, and an end-to-end check where the required host and guest image are available.

## Development and tests

```bash
python -m unittest discover -s tests -v
```

The CI workflow runs the standard-library test suite on Python 3.11, 3.12, and 3.13.

## Safety notes

- Guest images are untrusted input. Keep the crosvm sandbox enabled.
- Do not run a guest with elevated privileges just to work around missing KVM access.
- VM deletion permanently removes the VM directory and its virtual disk; the command asks for confirmation unless `--yes` is supplied.
- Raw disks are used for the initial release to avoid tying storage management to a QEMU-specific runtime or image tool.

## License

MIT. crosvm is a separate upstream project; see its repository for its license and contribution rules.
