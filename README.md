# ChromeOS VM Studio

**A native desktop VM/emulator manager for Chromebooks.** It runs as a Linux application inside ChromeOS's Linux development environment (Crostini), and emulates other x86 PC operating systems. It does **not** emulate ChromeOS, run in a browser, or use QEMU.

This project is at an early stage. The first usable backend is [Bochs](https://bochs.sourceforge.io/), a full-PC software emulator. ChromeOS VM Studio manages VM profiles, creates sparse virtual hard disks, attaches user-provided installation media, and starts each guest in Bochs's own graphical window. The installed app checks GitHub for updates automatically and can install new app versions without replacing VM data.

## Editing an existing VM

Select a VM in the library and choose **Edit VM…**. Stop the VM before changing settings. The editor can change RAM, Bochs CPU IPS, and the attached CD/DVD image; use **Eject ISO** to clear installation media. Save changes, then boot again. Virtual disk resizing is not supported yet.

## Advanced Bochs configuration

Select a stopped VM and choose **Bochs config…** to edit the complete bochsrc text in the native manager. **Validate** runs conservative static checks for required directives, memory/IPS ranges, disk layout, and common formatting mistakes. **Save custom config** stores a separate bochsrc.custom file which is used by the next boot; the generated bochsrc remains available as a reset point.

The built-in checks are not Bochs' parser and cannot prove every device option is supported by the installed build. Bochs performs final parsing when the VM starts. Validation is VM-aware: the primary `ata0-master` disk must remain the VM's own private `disk.raw`; a custom configuration cannot redirect the managed guest disk to another file. If you save a custom configuration, the profile editor and ISO controls are blocked until you choose **Restore generated**, so profile settings cannot silently diverge from the active hardware config. The custom file is kept with the VM and is not touched by app updates.

Before boot, the manager also verifies that the VM directory is inside the configured private storage folder and is not a symlink, and that the disk is a regular, single-link file at the exact expected size. Missing, redirected, linked, or truncated disks are refused rather than handed to Bochs.

## What runs where?

- **Host:** Your Chromebook running ChromeOS.
- **App:** A native Python/Tk desktop application inside the ChromeOS Linux environment (Crostini). Linux GUI apps can appear in the ChromeOS Launcher.
- **Guest:** An emulated x86 PC running another OS, such as DOS, older Windows, Linux, or BSD.
- **Emulation engine:** Bochs. No QEMU or browser/WASM emulator is used.

Crostini must be enabled and available on the Chromebook. This is not a privileged ChromeOS host app and cannot create or manage ChromeOS system VMs through private Concierge APIs.

## Why Bochs?

Bochs simulates a complete x86 PC: CPU, BIOS, VGA, IDE disks, CD-ROM, keyboard, mouse, and other PC peripherals. It supports x86-64 CPU extensions too. Because it simulates instructions in software instead of relying on KVM, it does not require `/dev/kvm` or nested virtualization.

That portability has a performance cost. Bochs is much slower than hardware-accelerated virtualization, especially for modern guest operating systems. The defaults are deliberately modest. Older DOS and Windows versions, lightweight Linux distributions, and OS experimentation are the most realistic starting points on a Chromebook. Do not expect useful Windows 10/11 performance.

The project may add an optional non-QEMU accelerated backend later, but only when a Chromebook actually exposes nested KVM and the guest/device model is compatible. It will not make KVM a requirement for the Bochs path.

## Install on a Chromebook

1. Enable **Settings → Developers → Linux development environment** in ChromeOS.
2. Open the Linux Terminal.
3. Clone this repository in Linux, then run:

```sh
git clone https://github.com/ashrworkman97-debug/ChromeOS_Emulator.git
cd ChromeOS_Emulator
chmod +x scripts/install-crostini.sh
./scripts/install-crostini.sh
```

The installer uses Debian packages for Bochs, its SDL display plugin, the Bochs system BIOS, the separate VGA BIOS package, Tkinter, and Python packaging tools. On Debian 13 (Trixie), `bochsbios` installs `/usr/share/bochs/BIOS-bochs-latest`, while `vgabios` installs the standard VGA ROM at `/usr/share/vgabios/vgabios.bin`. It does **not** install Bochs' upstream default `/usr/share/bochs/VGABIOS-lgpl-latest.bin` path. The app resolves the files from Debian's package manifest and explicitly supplies both ROM paths to Bochs. It creates a Python virtual environment under `~/.local/opt/chromeos-vm-studio` and registers a desktop launcher. After installation, launch **ChromeOS VM Studio** from the ChromeOS Launcher (under Linux apps). The manager has a dark navy title banner, a cleaner VM library with colored running/stopped indicators, and a dedicated action panel. Guest desktops still launch in separate Bochs windows.

If you already have the dependencies, run the app from the project directory with:

```sh
PYTHONPATH=src python3 -m chromeos_vm.gui
```

## Automatic updates

The app checks the repository's latest `main` commit in the background shortly after launch. Automatic checks are throttled to once every 12 hours. If a newer commit exists, the updater downloads the GitHub source archive, validates and safely extracts it, and reinstalls the Python application into the existing private virtual environment. It then asks you to restart the manager to load the new code. Guest profiles and virtual disks remain in `~/.local/share/chromeos-vm-studio`; the updater does not delete or rewrite them.

Use the **Updates** button in the manager to check immediately, or run:

```sh
chromeos-vm update --force
```

The initial updater must be installed once. For an older copy or a repair, update the source checkout and rerun the installer one time. The installer force-reinstalls the Python package even when the version number is unchanged, preventing stale code in `site-packages` after a source refresh:

```sh
cd ~/ChromeOS_Emulator
git pull --ff-only
./scripts/install-crostini.sh
```

After that, future app updates are installed by the app itself. A network connection is required. The updater follows this repository's `main` branch; it updates app code only, not guest operating systems or VM disks.

## Create and boot a guest

Open ChromeOS VM Studio, click **New VM**, choose a name, RAM amount, virtual hard-disk size, and (optionally) a user-provided ISO. Select the VM and click **Boot**. Bochs opens the guest's emulated PC in its own window. Install the guest OS onto the virtual hard disk as you would on a physical PC.

You can attach another ISO later, open the VM's data folder, inspect logs, stop the emulator, or delete a VM. Deleting permanently removes its virtual disk and always requires a confirmation in the GUI. Before launch, the manager checks the private disk's canonical path, file type, link count, and exact geometry-derived size. It also verifies the emulator's Linux process identity (including its start time and VM config path) before reporting it as running or sending a stop signal; stale PID files from older releases are migrated only when they still identify the matching Bochs command.

CLI equivalents are also available:

```sh
chromeos-vm check
chromeos-vm create win98 --memory-mib 512 --disk-gib 8 --iso /path/to/install.iso
chromeos-vm start win98
chromeos-vm info win98
chromeos-vm stop win98
chromeos-vm list
chromeos-vm delete win98
```

VMs live in `~/.local/share/chromeos-vm-studio` by default. Set `CHROMEOS_VM_HOME` to use a different directory.

## Compatibility and limitations

- The initial backend is x86 PC emulation through Bochs. Guest CPU models and performance depend on the Bochs build and its configuration.
- Bochs is software emulation. It is slower than a KVM-backed VM and can be CPU-intensive on a low-power Chromebook.
- The app itself works inside the Crostini Linux environment; it does not have direct privileged access to ChromeOS's host virtualization services.
- Nested virtualization/KVM is not assumed and is not needed by the Bochs backend.
- The app does not bundle operating-system installation media or proprietary firmware/ROM dumps. Use media and firmware that you are legally entitled to use. The installer installs Debian's `bochsbios` and `vgabios` packages. On Debian 13, the VGA ROM is `/usr/share/vgabios/vgabios.bin`; do not create a symlink to the non-existent `/usr/share/bochs/VGABIOS-lgpl-latest.bin`. The app writes the discovered ROM paths directly into its generated Bochs config.
- The graphical VM manager currently launches Bochs in its own window rather than embedding the guest display.

See [COMPATIBILITY.md](COMPATIBILITY.md) and [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## Roadmap

1. Native Crostini desktop manager, VM library, sparse raw disks, Bochs GUI launch/stop, logs, ISO selection/ejection, editable RAM/CPU settings, and confirmed deletion. (Implemented; CI GUI smoke-tested.)
2. Extend hardware configuration (machine family, CPU model, display, IDE/ATA device choices, and removable-media behavior).
3. Verified Bochs process identity, safe legacy PID migration, pidfd-aware stop signaling, stale-marker cleanup, editable custom bochsrc files, and static validation. (Implemented and tested.) Remaining: guest shutdown guidance and richer status diagnostics.
4. Save-state/snapshot support only if Bochs exposes a reliable supported interface; no snapshot claim yet.
5. Storage import/export, clone, backup, and disk resizing with safeguards.
6. Evaluate an optional accelerated non-QEMU KVM backend only for compatible guests on hosts that actually expose nested KVM.
7. Reproducible end-to-end guest boot tests on representative Chromebook hardware and packaging polish.

A milestone is not complete just because a command or UI control exists: tests and actual guest boot verification are required before support is claimed.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

CI runs the suite on Python 3.11, 3.12, and 3.13, exercises the native Tk GUI under a virtual display, verifies Debian 13 ROM package paths, and runs a short headless Bochs benchmark to prove the generated config is accepted by the real emulator. The headless check uses an empty test disk; it is not a guest OS installation/boot test. The management layer uses only the Python standard library; Tkinter is supplied by the OS package.

## License

The project code is MIT licensed. Bochs is a separate upstream project; see its own license and package notices.
