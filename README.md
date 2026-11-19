# ChromeOS VM Studio

**A native desktop VM/emulator manager for Chromebooks.** It runs as a Linux application inside ChromeOS's Linux development environment (Crostini), and emulates other x86 PC operating systems. It does **not** emulate ChromeOS, run in a browser, or use QEMU.

This project is at an early stage. The first usable backend is [Bochs](https://bochs.sourceforge.io/), a full-PC software emulator. ChromeOS VM Studio manages VM profiles, creates sparse virtual hard disks, attaches user-provided installation media, and starts each guest in Bochs's own graphical window. The installed app checks GitHub for updates automatically and can install new app versions without replacing VM data.

## Editing an existing VM

Select a VM in the library and choose **Edit VM…**. Stop the VM before changing settings. The editor can change RAM, Bochs CPU IPS, the pre-defined x86 CPU model, and the attached CD/DVD image; use **Eject ISO** to clear CD/DVD media. CPU model choices include 386SX, 486DX4, Pentium/Pentium MMX, AMD K6-2, Pentium II/III/4, Core Duo T2400, and Atom N270, plus the Bochs build default. CPU IPS remains a separate timing calibration setting. The PCI chipset can be selected independently as Intel i430FX, i440FX, or i440BX; i440FX is the default. These are Bochs-documented PCI chipset models, not host CPU settings, and availability depends on PCI support in the installed Bochs build. The model changes guest-visible CPU features; it does not accelerate Bochs on the Chromebook. The **Attach/Eject floppy A** and **Attach/Eject floppy B** controls manage two raw floppy drives separately. Bochs supports standard 160K through 2.88M image capacities; the manager checks exact sizes and attaches both images write-protected by default. BIOS floppy boot uses drive A, so the boot order tries A first when present, then CD/DVD if present, then hard disk. Drive B remains available as secondary media to guests that support it. Virtual disk resizing is not supported yet.

## BIOS boot priority

Select a VM and choose **Boot order…** to edit the BIOS sequence. The editor accepts one to three unique devices in priority order: floppy A, CD/DVD, hard disk, or network boot ROM. **Automatic** preserves the original media-aware behavior: floppy A first when attached, then CD/DVD when attached, then the primary hard disk. A manually selected floppy or CD/DVD entry requires that media to remain attached; the manager refuses to eject a selected boot device until the order is changed. Existing profiles without a `boot_order` field remain automatic.

Network boot is written as Bochs' documented `network` boot choice. It requires a compatible Bochs network adapter and boot ROM; the current manager does not configure a NIC, so network boot is an advanced option rather than a turnkey PXE setup. The app validates the sequence and passes it to Bochs; the guest firmware and installed Bochs build determine whether a particular network boot ROM is available.

The CLI accepts the same sequence:

```sh
chromeos-vm create dos-installer --iso /path/to/install.iso --boot-order cdrom,disk
chromeos-vm set-boot-order dos-installer --order disk
chromeos-vm set-boot-order dos-installer --order auto
```

The Bochs configuration editor pins the `boot:` directive to the saved VM profile, preventing a custom config from silently changing the boot priority behind the GUI.

## IDE storage topology

ChromeOS VM Studio now exposes a conservative two-channel IDE layout using Bochs' documented ATA devices:

- **IDE channel 0 master (ata0-master):** the VM's primary private disk.raw.
- **IDE channel 1 master (ata1-master):** an optional second private sparse disk2.raw.
- **CD-ROM slave:** choose the primary-channel slave (ata0-slave) or secondary-channel slave (ata1-slave) in New VM or Edit VM.
- **IDE controllers:** generated configs explicitly enable the standard primary and secondary legacy IDE ports and IRQs. This is ordinary emulated PC IDE, not SATA/AHCI.

New VM can create the second disk immediately. For an existing stopped VM, choose **Attach second disk…** and enter a size from 1–512 GiB. **Detach second disk** removes it from the guest profile but deliberately keeps disk2.raw and its data in the VM folder; it can be reattached at the same size. Permanently deleting the VM remains a separate confirmed operation. The second disk is checked for a private path, regular-file type, link count, and exact geometry-derived size before Bochs opens it.

CLI examples: chromeos-vm create retro-pc --disk-gib 8 --secondary-disk-gib 4 --cdrom-slot ata1-slave; chromeos-vm attach-disk retro-pc --disk-gib 4; chromeos-vm detach-disk retro-pc.

Detaching never erases the secondary disk. The CD-ROM slot can be changed in **Edit VM…** while the VM is stopped and no custom bochsrc override is active.

## Advanced Bochs configuration

Select a stopped VM and choose **Bochs config…** to edit the complete bochsrc text in the native manager. **Validate** runs conservative static checks for required directives, memory/IPS ranges, disk layout, and common formatting mistakes. **Save custom config** stores a separate bochsrc.custom file which is used by the next boot; the generated bochsrc remains available as a reset point.

The built-in checks are not Bochs' parser and cannot prove every device option is supported by the installed build. Bochs performs final parsing when the VM starts. Validation is VM-aware: the primary `ata0-master` disk must remain the VM's own private `disk.raw`; a custom configuration cannot redirect the managed guest disk to another file. If a floppy A or B image is attached in the VM profile, a custom configuration must continue to reference that exact image and retain `write_protected=1`. If you save a custom configuration, the profile editor and media controls are blocked until you choose **Restore generated**, so profile settings cannot silently diverge from the active hardware config. The custom file is kept with the VM and is not touched by app updates.

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

Open ChromeOS VM Studio, click **New VM**, choose a name, RAM amount, virtual hard-disk size, CPU model, PCI chipset, and (optionally) a user-provided ISO. Select the VM and click **Boot**. Bochs opens the guest's emulated PC in its own window. Install the guest OS onto the virtual hard disk as you would on a physical PC.

You can attach another ISO or floppy A/B images later, eject any medium, open the VM's data folder, inspect logs, stop the emulator, or delete a VM. Floppy images are write-protected by default so the original media is not modified by a guest. Deleting permanently removes the VM's private virtual hard disk and always requires a confirmation in the GUI. Before launch, the manager checks the private disk's canonical path, file type, link count, and exact geometry-derived size, and verifies any attached floppy image still has a supported capacity. It also verifies the emulator's Linux process identity (including its start time and VM config path) before reporting it as running or sending a stop signal; stale PID files from older releases are migrated only when they still identify the matching Bochs command.

CLI equivalents are also available:

```sh
chromeos-vm check
chromeos-vm create win98 --memory-mib 512 --disk-gib 8 --cpu-model pentium --chipset i440fx --iso /path/to/install.iso --boot-order cdrom,disk
chromeos-vm set-boot-order win98 --order disk
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
2. Extend hardware configuration (machine family, display, IDE/ATA device choices, and removable-media behavior). (Pre-defined CPU model and PCI chipset selection, read-only floppy A/B media, two-channel IDE storage, selectable CD-ROM placement, and the native BIOS boot-priority editor are implemented; broader device selection remains.)
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

CI runs the suite on Python 3.11, 3.12, and 3.13, exercises the native Tk GUI under a virtual display (including the BIOS boot-order editor), verifies Debian 13 ROM package paths, and runs real Bochs parser/initialization benchmarks for the generated primary disk, optional secondary IDE disk and CD-ROM position, attached floppy A/B images, explicit BIOS boot sequences, every CPU model, and all three PCI chipsets exposed by the app. Updater tests also exercise archive path, link, entry-count, and expanded-size defenses. These checks use empty test media; they are not guest OS installation/boot tests. The management layer uses only the Python standard library; Tkinter is supplied by the OS package.

## License

The project code is MIT licensed. Bochs is a separate upstream project; see its own license and package notices.
