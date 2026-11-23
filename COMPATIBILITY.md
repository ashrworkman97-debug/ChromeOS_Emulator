# Compatibility notes

| Layer | Current position |
|---|---|
| Host OS | ChromeOS with Linux development environment (Crostini) enabled |
| App UI | Native Linux GUI app (Tkinter); no browser UI |
| Emulator | Bochs full-PC x86 software emulation |
| QEMU | Not used |
| KVM | Not required for Bochs |
| Updates | Background commit-based updater from this repository's `main` branch |
| Guest family | x86 PC operating systems supported by the selected Bochs CPU/device configuration |
| CPU model selector | Bochs default, Intel 386SX/486DX4, Pentium/Pentium MMX, AMD K6-2, Pentium II/III/4, Core Duo T2400, Atom N270 |
| PCI chipset selector | Bochs PCI i430FX, i440FX (default), and i440BX; availability depends on installed Bochs build |
| IDE topology | Primary disk at IDE0 master; optional secondary disk at IDE1 master; CD-ROM selectable between IDE0 slave and IDE1 slave |
| BIOS boot order | Automatic media-aware default or one to three unique devices: floppy, cdrom, disk, network; explicit floppy/CD-ROM entries require attached media |
| Secondary disk | Private sparse raw disk2.raw; attach/detach preserves data; exact path, size, file type, and link count checked before boot |
| Guest display | Bochs's own GUI window |
| Diagnostics | Native GUI and CLI health report; checks host dependencies, process state, config, disks, media, and logs (not guest boot compatibility) |
| Floppy A/B | Optional raw floppy images at standard Bochs capacities; both write-protected by default; BIOS floppy boot targets A |
| Audio | Optional SB16 ISA + PC speaker with SDL PCM output; disabled by default; MIDI/input disabled; requires sound support in Bochs and working Crostini audio routing |
| Bochs configuration | Native text editor, VM-aware static checks, custom override/reset |
| Managed disk safety | Pre-boot private-path, regular-file, link-count, and exact-size checks |
| Backup/restore | Stopped-VM ZIP export and safe no-overwrite restore; managed disks included, external ISO/floppy media and custom config excluded |

## Crostini

ChromeOS officially supports Linux GUI apps integrated alongside ChromeOS apps. The app runs inside this supported Linux environment; it is not a privileged ChromeOS host utility.

Nested virtualization has varied by ChromeOS device/kernel and should not be assumed. The base Bochs path deliberately avoids that dependency. If an optional KVM backend is added later, it must probe the actual runtime environment and report when /dev/kvm or required permissions are missing.

## Bochs guest compatibility

Bochs models a broad set of x86 CPUs and standard PC devices, including VGA, IDE hard disks, ATAPI CD-ROM, keyboard, mouse, sound and selected network adapters depending on build options. Bochs supports x86-64 extensions, but that does not guarantee every modern OS installer or peripheral works with every Bochs configuration.

Software instruction emulation is expensive. Start with modest guest RAM and older/lightweight guest systems. Performance will be limited on low-power Chromebook CPUs. Windows 10/11 is not a target performance promise.

## PCI chipsets

The profile editor supports Bochs' documented i430FX, i440FX, and i440BX PCI chipsets. The generated configuration uses `pci: enabled=1, chipset=...`. Bochs documents i440FX as the default and describes i440BX as limited; a particular distro build must include PCI support. Debian 13 CI runs the real Bochs executable with all three chipset selections and checks BIOS/VGA/IDE initialization. This does not establish guest OS boot compatibility. See the [Bochs PCI directive documentation](https://bochs.sourceforge.io/doc/docbook/user/bochsrc.html#BOCHS-PARAMETERS-PARENT-PCI).

## CPU models

The native profile editor exposes Bochs' documented pre-defined models: i386, i486dx4, pentium, pentium_mmx, amd_k6_2_chomper, p2_klamath, p3_katmai, p4_willamette, core_duo_t2400_yonah, and atom_n270. The "Bochs default" choice omits `model=` and retains the package's build default. Bochs documents these names in its [pre-defined CPU model table](https://bochs.sourceforge.io/doc/docbook/user/cpu-models.html). Availability depends on the CPU level/features compiled into the installed Bochs package; the app performs static allowlist validation, and Bochs performs final validation at launch. Model selection changes the CPU presented to the guest; it does not enable hardware acceleration. Debian 13 CI runs the real Bochs binary against every exposed model and checks that BIOS/VGA/IDE initialization completes. This is configuration initialization testing, not a guest OS boot test.

## IDE storage

The manager explicitly enables Bochs' standard primary and secondary ATA channels. The primary hard disk is fixed at ata0-master; the optional second hard disk is fixed at ata1-master. A CD-ROM can be placed at ata0-slave or ata1-slave. The application does not claim SATA/AHCI support. The selected positions are generated using Bochs' documented ata0, ata1, and ata[0-3]-master/slave directives. The static validator checks channel addresses, IDE disk geometry fields, and CD-ROM status/path syntax; VM-aware validation pins each managed disk and CD-ROM to the corresponding profile path and slot. Debian 13 CI runs the actual Bochs executable with a second disk and the CD-ROM on IDE1 slave.

Attaching a secondary disk creates a sparse raw file under the VM's private data directory. Detaching clears the profile attachment but preserves disk2.raw for later reattachment; a size mismatch is refused. It is never silently overwritten. Deleting the entire VM remains a separate confirmation-protected operation.

## BIOS boot priority

The native editor writes Bochs' documented `boot:` directive and supports up to three unique devices in order: `floppy`, `disk`, `cdrom`, or `network`. Automatic mode preserves the earlier behavior (floppy A when attached, CD/DVD when attached, then hard disk). Existing profiles without a boot-order field remain automatic. The manager refuses to eject floppy A or CD/DVD while an explicit sequence still references that device. Static validation rejects unknown, duplicate, empty, or four-device sequences; VM-aware validation also checks that floppy A and CD/DVD media exist and pins custom configs to the saved profile order.

Network boot is emitted as Bochs' documented `network` boot choice, but it is not a turnkey PXE feature: a compatible NIC device/plugin and boot ROM must be configured and available in the installed Bochs build. The current GUI does not configure a NIC. See the [Bochs boot directive documentation](https://bochs.sourceforge.io/doc/docbook/user/bochsrc.html#BOCHS-PARAMETERS-BOOT).

## Floppy media

The manager supports optional floppy A and B images using Bochs' documented `floppya` and `floppyb` directives. Both accept exact image sizes corresponding to 160K, 180K, 320K, 360K, 720K, 1.2M, 1.44M, and 2.88M media. Generated configurations mark both images write-protected, and VM-aware static validation rejects custom overrides that disable this protection. BIOS floppy boot targets drive A; drive B is secondary media and does not replace A in the boot sequence. The original images are referenced in place and are not copied or modified by the manager. See the [Bochs configuration manual](https://bochs.sourceforge.io/doc/docbook/user/bochsrc.html) for the upstream syntax.

CI checks that generated configurations with both floppy drives and each exposed CPU model are parsed and initialized by the Debian 13 Bochs package. This is not proof that a particular floppy image contains a bootable operating system.

## Configuration validation

The app's editor performs static checks for required directives, quoting, memory/IPS ranges, and the raw IDE disk declaration. It also requires the primary IDE disk path to remain the VM's private `disk.raw`; it cannot be redirected through a custom config. Before launch, the manager verifies the VM directory is within the configured storage root and that the disk is a regular non-symlink file with one hard link and the exact geometry-derived byte size. This is intentionally not represented as a complete Bochs parser: optional directives and device availability vary by Bochs build. Debian 13 CI additionally runs Bochs itself in headless benchmark mode against the generated configuration and an empty test disk. This checks parser/device initialization but does not boot an installed guest OS.

## Diagnostics and shutdown behavior

The native **Diagnostics…** action and `chromeos-vm diagnose <name>` command provide non-destructive checks for the Bochs executable, system/VGA BIOS packages, active Bochs config (static validation only), VM process state, private disk path/type/link-count/geometry, sparse disk allocation, attached ISO/floppy availability, and log presence. A clean report is not proof that an operating system installer or installed guest will boot. The report does not alter VM profiles or disks, though process inspection may reconcile a stale runtime PID marker.

The manager's Stop action sends SIGTERM to the Bochs process. It does not send an ACPI or other shutdown command into the guest. Shut down from the guest OS first whenever possible; stopping while the guest is writing can lose data or damage its filesystem.

## Required user inputs

The app creates an empty virtual hard disk. The user supplies legally obtained guest installation ISO/disk images. Firmware ROM availability depends on the distro's Bochs/BIOS packages; the project does not package proprietary ROM dumps.


## BIOS packages

On Debian-based Crostini, install `bochsbios` and `vgabios` (the latter may alternatively be provided by `seabios`). Debian Trixie's Bochs package depends on the VGA BIOS package, while the Bochs system BIOS is supplied separately by `bochsbios`. The app resolves the installed ROM paths and refuses to launch if it cannot find both files, with a package-install hint. It does not bundle ROM files.


## Backup and restore

The ZIP backup format preserves vm.json, disk.raw, and optional disk2.raw. A detached disk2.raw is preserved even when it is not attached to the VM profile. Restore rewrites the managed disk paths and refuses to overwrite an existing VM. It does not bundle guest ISO/floppy media, custom Bochs config, logs, or runtime PID files. External media paths are retained and must still be valid on the destination Chromebook. This is an offline file backup, not a save-state or live snapshot.
