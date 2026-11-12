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
| Guest display | Bochs's own GUI window |
| Bochs configuration | Native text editor, VM-aware static checks, custom override/reset |
| Managed disk safety | Pre-boot private-path, regular-file, link-count, and exact-size checks |

## Crostini

ChromeOS officially supports Linux GUI apps integrated alongside ChromeOS apps. The app runs inside this supported Linux environment; it is not a privileged ChromeOS host utility.

Nested virtualization has varied by ChromeOS device/kernel and should not be assumed. The base Bochs path deliberately avoids that dependency. If an optional KVM backend is added later, it must probe the actual runtime environment and report when /dev/kvm or required permissions are missing.

## Bochs guest compatibility

Bochs models a broad set of x86 CPUs and standard PC devices, including VGA, IDE hard disks, ATAPI CD-ROM, keyboard, mouse, sound and selected network adapters depending on build options. Bochs supports x86-64 extensions, but that does not guarantee every modern OS installer or peripheral works with every Bochs configuration.

Software instruction emulation is expensive. Start with modest guest RAM and older/lightweight guest systems. Performance will be limited on low-power Chromebook CPUs. Windows 10/11 is not a target performance promise.

## Configuration validation

The app's editor performs static checks for required directives, quoting, memory/IPS ranges, and the raw IDE disk declaration. It also requires the primary IDE disk path to remain the VM's private `disk.raw`; it cannot be redirected through a custom config. Before launch, the manager verifies the VM directory is within the configured storage root and that the disk is a regular non-symlink file with one hard link and the exact geometry-derived byte size. This is intentionally not represented as a complete Bochs parser: optional directives and device availability vary by Bochs build. Debian 13 CI additionally runs Bochs itself in headless benchmark mode against the generated configuration and an empty test disk. This checks parser/device initialization but does not boot an installed guest OS.

## Required user inputs

The app creates an empty virtual hard disk. The user supplies legally obtained guest installation ISO/disk images. Firmware ROM availability depends on the distro's Bochs/BIOS packages; the project does not package proprietary ROM dumps.


## BIOS packages

On Debian-based Crostini, install `bochsbios` and `vgabios` (the latter may alternatively be provided by `seabios`). Debian Trixie's Bochs package depends on the VGA BIOS package, while the Bochs system BIOS is supplied separately by `bochsbios`. The app resolves the installed ROM paths and refuses to launch if it cannot find both files, with a package-install hint. It does not bundle ROM files.
