# Architecture

## Product boundary

ChromeOS VM Studio is a **native Linux desktop application for ChromeOS's Crostini environment**. It is not a website/PWA and does not emulate ChromeOS. ChromeOS is the host; other x86 PC operating systems are guests.

Crostini provides a Linux container with ChromeOS desktop integration. The user launches the app from the ChromeOS Launcher as a Linux GUI application. The app has no privileged API into ChromeOS Concierge/Termina and must not pretend to create or manage ChromeOS system VMs directly.

## Components

```
ChromeOS Launcher
      |
Crostini Linux desktop
      |
ChromeOS VM Studio (Python + Tkinter)
      |
VM profile + sparse disk + generated bochsrc
      |
Bochs process (separate GUI window)
      |
Emulated x86 PC (CPU, BIOS, VGA, IDE, CD-ROM, input)
      |
Guest OS installed from user-provided media
```

The updater lives in the installed Python package and runs a background check on app launch (12-hour cooldown). It queries this repository's `main` commit, downloads a bounded GitHub archive, rejects traversal and link entries, stages only regular files, and calls the current venv's `pip` to install the package. Installed commit metadata is stored in `~/.local/opt/chromeos-vm-studio/install.json`. It does not touch VM data. The GUI prompts for restart after installation; each guest remains its own Bochs process. A manual check is available with the Updates button or `chromeos-vm update --force`. Before invoking pip, it parses the staged `pyproject.toml` and refuses a lower stable numeric package version than the currently installed version. A refused downgrade keeps the previous installed-commit marker and records only the check time; VM files are outside the update path.

The manager owns `~/.local/share/chromeos-vm-studio/<vm-name>`:
- `vm.json`: versioned VM profile, including CPU model, PCI chipset, CD-ROM IDE slave position, optional secondary disk metadata, external ISO, floppy A/B image paths, and an optional explicit BIOS boot-order list
- `disk.raw`: primary sparse virtual hard disk
- `disk2.raw`: optional secondary sparse virtual hard disk on IDE channel 1 master; a detached disk remains here with its data intact
- `bochsrc`: generated Bochs configuration
- `bochsrc.custom`: optional user-edited Bochs configuration, active in preference to the generated file
- `logs/bochs.log`: emulator log
- `logs/launcher.log`: launcher stdout/stderr
- `runtime/bochs.pid`: process identity while running

The app generates Bochs syntax using quoted local file paths, validates resource bounds and a documented pre-defined CPU-model allowlist, and invokes the Bochs executable with an argument array (never a shell command). It explicitly enables the standard primary and secondary IDE channels, keeps the primary disk at IDE0 master, optionally attaches a second private raw disk at IDE1 master, and places the CD-ROM at the selected IDE0-slave or IDE1-slave position. CPU model and PCI chipset selections are stored in each VM profile; the "Bochs default" choice omits the model property and preserves the package build's default CPU. Explicit CPU choices use the documented `cpu: model=...` property. The PCI chipset selector writes Bochs' documented `pci: enabled=1, chipset=...` directive and supports i430FX, i440FX, and i440BX; older profiles without a chipset field default to i440FX. The selected model changes guest-visible CPU features, not host acceleration; the installed Bochs build remains authoritative for whether a given model is compiled in. Optional floppy A and B images use Bochs' `floppya` and `floppyb` directives, are write-protected by default, and must match a standard Bochs floppy capacity. VM-aware validation also requires custom overrides to retain `write_protected=1` for any configured floppy drive. Drive A is the BIOS floppy boot target and is first in the automatic boot order when attached; B is a secondary drive for guest OSes that support it. A profile may override the automatic order with one to three unique Bochs boot devices (floppy, cdrom, disk, network); profiles without the field keep the automatic media-aware order. The generated `boot:` directive is validated and custom configs must match the profile. Network boot requires a compatible NIC and boot ROM; the manager does not configure a NIC yet. The original user-selected images are referenced in place and are not copied or modified by the app.  The native configuration editor can save a custom bochsrc override; static checks cover required directives and common formatting/resource mistakes, but Bochs itself remains the final parser at boot. While a custom override exists, profile and ISO edits are blocked until the user restores the generated config. Process identity tracking records whichever config file is actually active. The guest display is the actual Bochs GUI window, not a simulated canvas inside the manager.

## Backend choice

Bochs is a complete PC software emulator. It models the CPU and standard PC devices and does not require host KVM. This makes it usable in Crostini even when nested virtualization is unavailable, at the cost of substantially lower speed than hardware virtualization.

crosvm is a VMM using Linux KVM and virtio devices, not a general PC chipset emulator. It may be considered as a future optional backend only for hosts exposing nested KVM and compatible guests. It is not the default and is not a requirement. QEMU is not used.

## Safety and correctness

- Never pass untrusted path text through a shell.
- Do not accept newlines or quote injection in generated configuration paths.
- Track emulator PID, Linux process start time, and the exact VM config path; migrate legacy PID-only markers only when `/proc` confirms the matching command line.
- Use Linux pidfds for stop signals when available, with a final process-identity check on older kernels, so PID reuse cannot redirect a stop request to an unrelated process.
- Keep the primary guest disk at `<private-vm-directory>/disk.raw` and the optional secondary disk at `disk2.raw`; validate each profile path against its exact private location before launch.
- Secondary disk detach is non-destructive: it clears the profile attachment but retains `disk2.raw`; reattachment requires the same size and rechecks file type, link count, and exact geometry.
- The managed CD-ROM may occupy only the profile-selected IDE slave slot; custom configs cannot redirect its ISO or silently move it to another IDE position.
- Explicit BIOS boot order is limited to one through three unique Bochs devices; floppy/CD-ROM choices must correspond to media currently attached in the profile. Custom configs cannot silently change the saved boot priority.
- Refuse VM directories that are symlinks or resolve outside the configured storage root.
- Before handing a disk to Bochs, require a regular non-symlink file, a single hard link, and the exact byte length implied by the VM's CHS geometry. This avoids opening redirected, shared, truncated, or replaced files.
- A custom `ata0-master` directive must still point to the VM's managed `disk.raw`; custom device settings cannot redirect the primary guest disk outside private storage.
- When floppy A or B images are stored in the VM profile, custom `floppya` and `floppyb` directives must continue to reference those exact attached images and retain `write_protected=1`; the static validator rejects attempts to make managed media writable.
- Never delete a VM disk while its emulator is running.
- The Stop action sends SIGTERM to the Bochs process; it does not issue a guest ACPI/power-off request. The UI and diagnostics tell users to shut down inside the guest OS first to reduce filesystem-corruption risk.
- Non-destructive diagnostics check process identity, installed Bochs/BIOS paths, active-config static validation, managed disk integrity/allocation, attached-media availability, and log presence. They do not modify VM profiles or disks; process inspection may reconcile a stale runtime PID marker. They are health hints, not guest-boot verification.
- Ask for explicit confirmation before permanent disk deletion.
- Do not bundle proprietary guest installation media or ROM/BIOS dumps.
- A configured VM is not proof that an OS boots. Only observed end-to-end guest installation/boot testing can establish compatibility.

## Optional audio

A VM profile may opt into the Bochs SB16 ISA sound device and PC speaker. The generated config selects SDL for PCM wave output and disables wave input and MIDI output. The feature is off by default because sound plugins vary between distro builds and Crostini audio forwarding depends on the host environment. The app reports this as optional emulated hardware, not guaranteed Chromebook audio support.


## VM backup and restore

The backup layer exports only a stopped VM. It writes a versioned ZIP containing manifest.json, vm.json, disk.raw, and optional disk2.raw. If disk2.raw exists but has been detached from the profile, it is still included and restored as a detached disk so its data is not lost. The archive does not contain logs, PID/runtime files, generated Bochs configuration, or bochsrc.custom.

Restore never extracts arbitrary archive paths. It accepts only the fixed member names, rejects duplicates, symlinks, encrypted/unsupported entries, unsafe compression ratios, and excessive metadata/expanded sizes, validates the profile and geometry-derived raw disk lengths, then streams disks into a staging directory under the VM storage root. During that stream it writes nonzero 4 KiB blocks and seeks across all-zero blocks, then truncates to the expected logical disk size; this recreates sparse holes on filesystems that support them rather than materializing zero-filled virtual capacity. It rewrites primary and attached-secondary disk paths to the new private directory and renames the completed staging folder into place. An existing VM name is never overwritten.

Guest ISO/floppy paths in the profile remain external references; their image contents are not copied into the backup. The UI and CLI warn that these paths must still be available after restore. A custom Bochs config is deliberately not restored because it contains absolute paths and could point back to the original VM; the generated config is recreated at next boot. Backups are data copies, not live snapshots: export refuses a running VM, and users should shut down the guest cleanly first.
