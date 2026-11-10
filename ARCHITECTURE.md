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

The updater lives in the installed Python package and runs a background check on app launch (12-hour cooldown). It queries this repository's `main` commit, downloads a bounded GitHub archive, rejects traversal and link entries, stages only regular files, and calls the current venv's `pip` to install the package. Installed commit metadata is stored in `~/.local/opt/chromeos-vm-studio/install.json`. It does not touch VM data. The GUI prompts for restart after installation; each guest remains its own Bochs process. A manual check is available with the Updates button or `chromeos-vm update --force`.

The manager owns `~/.local/share/chromeos-vm-studio/<vm-name>`:
- `vm.json`: versioned VM profile
- `disk.raw`: sparse virtual hard disk
- `bochsrc`: generated Bochs configuration
- `logs/bochs.log`: emulator log
- `logs/launcher.log`: launcher stdout/stderr
- `runtime/bochs.pid`: process identity while running

The app generates Bochs syntax using quoted local file paths, validates resource bounds, and invokes the Bochs executable with an argument array (never a shell command). The guest display is the actual Bochs GUI window, not a simulated canvas inside the manager.

## Backend choice

Bochs is a complete PC software emulator. It models the CPU and standard PC devices and does not require host KVM. This makes it usable in Crostini even when nested virtualization is unavailable, at the cost of substantially lower speed than hardware virtualization.

crosvm is a VMM using Linux KVM and virtio devices, not a general PC chipset emulator. It may be considered as a future optional backend only for hosts exposing nested KVM and compatible guests. It is not the default and is not a requirement. QEMU is not used.

## Safety and correctness

- Never pass untrusted path text through a shell.
- Do not accept newlines or quote injection in generated configuration paths.
- Track emulator PID, Linux process start time, and the exact VM config path; migrate legacy PID-only markers only when `/proc` confirms the matching command line.
- Use Linux pidfds for stop signals when available, with a final process-identity check on older kernels, so PID reuse cannot redirect a stop request to an unrelated process.
- Never delete a VM disk while its emulator is running.
- Ask for explicit confirmation before permanent disk deletion.
- Do not bundle proprietary guest installation media or ROM/BIOS dumps.
- A configured VM is not proof that an OS boots. Only observed end-to-end guest installation/boot testing can establish compatibility.
