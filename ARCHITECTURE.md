# Architecture

ChromeOS VM Studio is the management/orchestration layer; crosvm is the VM monitor. The app owns VM definitions, storage, lifecycle, image import, UI, and diagnostics. It does not implement CPU virtualization or emulate a PC chipset itself.

```
CLI / future graphical UI
        |
VM catalog + config + disk/image manager
        |
crosvm process + control socket
        |
Linux KVM (initial host backend)
        |
Guest kernel + OS + virtio devices
```

Each VM directory contains versioned `vm.json`, a sparse `disk.raw`, `logs/crosvm.log`, and runtime PID/socket files.

Starting a VM validates configuration, Linux/KVM availability, crosvm, kernel and disk files, then starts crosvm with CPU, memory, block, serial, and control-socket settings. Stopping requests orderly shutdown over the crosvm control socket; the manager does not silently force-kill a guest.

A kernel plus block device is not a complete ChromeOS importer. ChromeOS boot support requires image-specific partition handling, boot-chain and firmware compatibility, kernel arguments, virtual-device support, and possibly TPM/verified-boot integration. These are future milestones, not claims of this release.
