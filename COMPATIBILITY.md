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
| Guest display | Bochs's own GUI window |
| Floppy A/B | Optional raw floppy images at standard Bochs capacities; both write-protected by default; BIOS floppy boot targets A |
| Bochs configuration | Native text editor, VM-aware static checks, custom override/reset |
| Managed disk safety | Pre-boot private-path, regular-file, link-count, and exact-size checks |

## Crostini

ChromeOS officially supports Linux GUI apps integrated alongside ChromeOS apps. The app runs inside this supported Linux environment; it is not a privileged ChromeOS host utility.

Nested virtualization has varied by ChromeOS device/kernel and should not be assumed. The base Bochs path deliberately avoids that dependency. If an optional KVM backend is added later, it must probe the actual runtime environment and report when /dev/kvm or required permissions are missing.

## Bochs guest compatibility

Bochs models a broad set of x86 CPUs and standard PC devices, including VGA, IDE hard disks, ATAPI CD-ROM, keyboard, mouse, sound and selected network adapters depending on build options. Bochs supports x86-64 extensions, but that does not guarantee every modern OS installer or peripheral works with every Bochs configuration.

Software instruction emulation is expensive. Start with modest guest RAM and older/lightweight guest systems. Performance will be limited on low-power Chromebook CPUs. Windows 10/11 is not a target performance promise.

## CPU models

The native profile editor exposes Bochs' documented pre-defined models: i386, i486dx4, pentium, pentium_mmx, amd_k6_2_chomper, p2_klamath, p3_katmai, p4_willamette, core_duo_t2400_yonah, and atom_n270. The "Bochs default" choice omits `model=` and retains the package's build default. Bochs documents these names in its [pre-defined CPU model table](https://bochs.sourceforge.io/doc/docbook/user/cpu-models.html). Availability depends on the CPU level/features compiled into the installed Bochs package; the app performs static allowlist validation, and Bochs performs final validation at launch. Model selection changes the CPU presented to the guest; it does not enable hardware acceleration. Debian 13 CI runs the real Bochs binary against every exposed model and checks that BIOS/VGA/IDE initialization completes. This is configuration initialization testing, not a guest OS boot test.

## Floppy media

The manager supports optional floppy A and B images using Bochs' documented `floppya` and `floppyb` directives. Both accept exact image sizes corresponding to 160K, 180K, 320K, 360K, 720K, 1.2M, 1.44M, and 2.88M media. Generated configurations mark both images write-protected, and VM-aware static validation rejects custom overrides that disable this protection. BIOS floppy boot targets drive A; drive B is secondary media and does not replace A in the boot sequence. The original images are referenced in place and are not copied or modified by the manager. See the [Bochs configuration manual](https://bochs.sourceforge.io/doc/docbook/user/bochsrc.html) for the upstream syntax.

CI checks that generated configurations with both floppy drives and each exposed CPU model are parsed and initialized by the Debian 13 Bochs package. This is not proof that a particular floppy image contains a bootable operating system.

## Configuration validation

The app's editor performs static checks for required directives, quoting, memory/IPS ranges, and the raw IDE disk declaration. It also requires the primary IDE disk path to remain the VM's private `disk.raw`; it cannot be redirected through a custom config. Before launch, the manager verifies the VM directory is within the configured storage root and that the disk is a regular non-symlink file with one hard link and the exact geometry-derived byte size. This is intentionally not represented as a complete Bochs parser: optional directives and device availability vary by Bochs build. Debian 13 CI additionally runs Bochs itself in headless benchmark mode against the generated configuration and an empty test disk. This checks parser/device initialization but does not boot an installed guest OS.

## Required user inputs

The app creates an empty virtual hard disk. The user supplies legally obtained guest installation ISO/disk images. Firmware ROM availability depends on the distro's Bochs/BIOS packages; the project does not package proprietary ROM dumps.


## BIOS packages

On Debian-based Crostini, install `bochsbios` and `vgabios` (the latter may alternatively be provided by `seabios`). Debian Trixie's Bochs package depends on the VGA BIOS package, while the Bochs system BIOS is supplied separately by `bochsbios`. The app resolves the installed ROM paths and refuses to launch if it cannot find both files, with a package-install hint. It does not bundle ROM files.
