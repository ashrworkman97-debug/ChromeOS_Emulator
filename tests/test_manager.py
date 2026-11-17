import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from chromeos_vm.manager import (
    CHIPSET_LABELS, CPU_MODEL_LABELS, IDE_CDROM_SLOT_LABELS, VMConfig, VMError,
    VMManager, bochs_quote, disk_geometry, validate_name, validate_resources,
    validate_bochsrc_text,
)


class ValidationTests(unittest.TestCase):
    def test_names(self):
        validate_name("win98_test-1")
        for bad in ("../escape", "two words", "-start", "x" * 49):
            with self.subTest(bad=bad), self.assertRaises(VMError):
                validate_name(bad)

    def test_resources(self):
        validate_resources(512, 8, 10_000_000)
        for values in ((8, 8, 10_000_000), (512, 0, 10_000_000),
                       (512, 8, 1), ("512", 8, 10_000_000)):
            with self.subTest(values=values), self.assertRaises(VMError):
                validate_resources(*values)

    def test_config_rejects_qemu(self):
        raw = {"name":"vm","memory_mib":512,"disk_gib":8,"disk_path":"/tmp/disk.raw",
               "backend":"qemu"}
        with self.assertRaises(VMError):
            VMConfig.from_dict(raw)

    def test_quote_rejects_config_injection(self):
        with self.assertRaises(VMError):
            bochs_quote('bad"\nreset: 1')

    def test_geometry_is_exact_and_positive(self):
        for size in (1, 2, 8, 32, 512):
            cylinders, heads, sectors, byte_count = disk_geometry(size)
            self.assertGreater(cylinders, 0)
            self.assertEqual(byte_count, cylinders * heads * sectors * 512)
            self.assertLessEqual(byte_count, size * 1024**3)
            self.assertLess(size * 1024**3 - byte_count, heads * sectors * 512)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = VMManager(self.root / "vms")

    def _spawn_marked_process(self, name):
        config_file = (self.manager.vm_path(name) / "bochsrc").resolve()
        process = subprocess.Popen([
            sys.executable, "-c", "import time; time.sleep(30)",
            "-f", str(config_file),
        ])
        self.addCleanup(self._stop_process, process)
        deadline = time.monotonic() + 2
        while self.manager._process_start_ticks(process.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        ticks = self.manager._process_start_ticks(process.pid)
        self.assertIsNotNone(ticks, "test process did not appear in /proc")
        return process, config_file, ticks

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def test_create_sparse_disk_and_bochsrc(self):
        cfg = self.manager.create("retro-pc", 512, 1, 10_000_000)
        disk = Path(cfg.disk_path)
        cylinders, heads, sectors, expected_bytes = disk_geometry(1)
        self.assertEqual(disk.stat().st_size, expected_bytes)
        self.assertEqual(disk.stat().st_size, cylinders * heads * sectors * 512)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/VGABIOS-lgpl-latest")]):
            config = self.manager.render_bochsrc(cfg)
        self.assertIn("megs: 512", config)
        self.assertEqual(cfg.cpu_model, "default")
        self.assertIn("display_library: sdl2", config)
        self.assertIn(f"cylinders={cylinders}, heads={heads}, spt={sectors}", config)
        self.assertIn("ata0-master: type=disk", config)
        self.assertIn("pci: enabled=1, chipset=i440fx", config)
        self.assertIn("boot: disk", config)
        self.assertIn(
            f'log: "{(self.manager.vm_path(cfg.name) / "logs/bochs.log").resolve()}"',
            config,
        )
        self.assertNotIn("qemu", config.lower())

    def test_cpu_model_round_trips_and_is_rendered_in_documented_syntax(self):
        cfg = self.manager.create("pentium-pc", cpu_model="pentium")
        loaded = self.manager.load(cfg.name)
        self.assertEqual(loaded.cpu_model, "pentium")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/VGABIOS-lgpl-latest")]):
            config = self.manager.render_bochsrc(loaded)
        self.assertIn(
            "cpu: model=pentium, count=1, ips=10000000, reset_on_triple_fault=1",
            config,
        )
        validate_bochsrc_text(config)

    def test_cpu_model_can_be_changed_without_touching_guest_disk(self):
        cfg = self.manager.create("cpu-edit")
        disk = Path(cfg.disk_path)
        before = disk.stat().st_size
        updated = self.manager.update_profile(
            cfg.name, cfg.memory_mib, cfg.cpu_ips, cfg.iso_path, "p3_katmai"
        )
        self.assertEqual(updated.cpu_model, "p3_katmai")
        self.assertEqual(disk.stat().st_size, before)

    def test_unknown_cpu_model_is_rejected_in_profiles_and_custom_configs(self):
        with self.assertRaisesRegex(VMError, "CPU model"):
            self.manager.create("bad-cpu", cpu_model="not_a_real_bochs_cpu")
        cfg = self.manager.create("bad-custom-cpu")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/VGABIOS-lgpl-latest")]):
            config = self.manager.render_bochsrc(cfg)
        config = config.replace("cpu: count=1", "cpu: model=not_a_real_bochs_cpu, count=1")
        with self.assertRaisesRegex(VMError, "not in the supported model list"):
            validate_bochsrc_text(config)

    def test_legacy_profile_without_cpu_model_uses_build_default(self):
        cfg = self.manager.create("legacy-cpu-profile")
        profile_path = self.manager.config_path(cfg.name)
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        raw.pop("cpu_model")
        profile_path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = self.manager.load(cfg.name)
        self.assertEqual(loaded.cpu_model, "default")

    def test_cpu_model_catalog_contains_only_named_choices(self):
        self.assertEqual(CPU_MODEL_LABELS["default"], "Bochs default (build-dependent)")
        self.assertIn("pentium", CPU_MODEL_LABELS)
        self.assertIn("atom_n270", CPU_MODEL_LABELS)

    def test_chipset_round_trips_and_renders_documented_pci_syntax(self):
        for chipset in ("i430fx", "i440fx", "i440bx"):
            with self.subTest(chipset=chipset):
                cfg = self.manager.create("pci-" + chipset, chipset=chipset)
                loaded = self.manager.load(cfg.name)
                self.assertEqual(loaded.chipset, chipset)
                with patch.object(self.manager, "_find_bios",
                                  side_effect=[Path("/mock/BIOS-bochs-latest"),
                                               Path("/mock/VGABIOS-lgpl-latest")]):
                    config = self.manager.render_bochsrc(loaded)
                self.assertIn(f"pci: enabled=1, chipset={chipset}", config)
                validate_bochsrc_text(config)

    def test_chipset_can_be_changed_without_touching_guest_disk(self):
        cfg = self.manager.create("pci-edit")
        disk = Path(cfg.disk_path)
        before = disk.stat().st_size
        updated = self.manager.update_profile(
            cfg.name, cfg.memory_mib, cfg.cpu_ips, cfg.iso_path, cfg.cpu_model, "i440bx"
        )
        self.assertEqual(updated.chipset, "i440bx")
        self.assertEqual(disk.stat().st_size, before)

    def test_unknown_chipset_is_rejected_in_profiles_and_custom_configs(self):
        with self.assertRaisesRegex(VMError, "PCI chipset"):
            self.manager.create("bad-chipset", chipset="i999xx")
        cfg = self.manager.create("bad-custom-chipset")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/VGABIOS-lgpl-latest")]):
            config = self.manager.render_bochsrc(cfg)
        invalid = config.replace("chipset=i440fx", "chipset=i999xx")
        with self.assertRaisesRegex(VMError, "PCI chipset"):
            validate_bochsrc_text(invalid)

    def test_legacy_profile_without_chipset_uses_i440fx_default(self):
        cfg = self.manager.create("legacy-chipset-profile")
        profile_path = self.manager.config_path(cfg.name)
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        raw.pop("chipset")
        profile_path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = self.manager.load(cfg.name)
        self.assertEqual(loaded.chipset, "i440fx")

    def test_chipset_catalog_contains_only_documented_bochs_choices(self):
        self.assertEqual(set(CHIPSET_LABELS), {"i430fx", "i440fx", "i440bx"})

    def test_finds_debian_trixie_vga_rom_from_vgabios_package_manifest(self):
        # Debian 13's vgabios package file list names this exact path.
        rom = self.root / "usr/share/vgabios/vgabios.bin"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"mock VGA BIOS")
        completed = type("Result", (), {
            "stdout": str(rom) + chr(10),
            "stderr": "",
            "returncode": 0,
        })()
        with patch("chromeos_vm.manager.shutil.which", return_value="/usr/bin/dpkg-query"):
            with patch("chromeos_vm.manager.subprocess.run", return_value=completed) as run:
                found = self.manager._find_bios("VGABIOS-lgpl-latest")
        run.assert_called_once_with(
            ["dpkg-query", "-L", "vgabios"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(found, rom.resolve())

    def test_generated_config_uses_detected_rom_paths(self):
        cfg = self.manager.create("rom-path-test")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            config = self.manager.render_bochsrc(cfg)
        self.assertIn('romimage: file="/mock/BIOS-bochs-latest"', config)
        self.assertIn('vgaromimage: file="/mock/vgabios/vgabios.bin"', config)

    def test_custom_bochsrc_cannot_redirect_managed_disk_outside_vm_folder(self):
        cfg = self.manager.create("private-disk-config")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        outside = self.root / "outside.raw"
        outside.write_bytes(b"not the managed VM disk")
        redirected = generated.replace(
            f'path="{cfg.disk_path}"', f'path="{outside.resolve()}"'
        )
        with self.assertRaisesRegex(VMError, "private disk.raw"):
            self.manager.save_custom_bochsrc(cfg.name, redirected)
        self.assertFalse(self.manager.custom_bochsrc_path(cfg.name).exists())

    def test_start_rejects_profile_disk_path_outside_private_storage(self):
        cfg = self.manager.create("redirected-profile")
        outside = self.root / "outside.raw"
        outside.write_bytes(b"do not open this file")
        profile_path = self.manager.config_path(cfg.name)
        raw = __import__("json").loads(profile_path.read_text(encoding="utf-8"))
        raw["disk_path"] = str(outside.resolve())
        profile_path.write_text(__import__("json").dumps(raw), encoding="utf-8")
        with patch.object(self.manager, "find_bochs", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(VMError, "private disk.raw"):
                self.manager.start(cfg.name)
        self.assertEqual(outside.read_bytes(), b"do not open this file")

    def test_start_rejects_truncated_managed_disk_before_launch(self):
        cfg = self.manager.create("truncated-disk")
        Path(cfg.disk_path).write_bytes(b"short disk")
        with patch.object(self.manager, "find_bochs", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(VMError, "profile requires"):
                self.manager.start(cfg.name)

    def test_disk_validation_rejects_symlinks_and_hardlinks(self):
        cfg = self.manager.create("linked-disk")
        disk = Path(cfg.disk_path)
        outside = self.root / "outside.raw"
        outside.write_bytes(b"outside")
        disk.unlink()
        disk.symlink_to(outside)
        with self.assertRaisesRegex(VMError, "not a regular file"):
            self.manager.validate_disk_image(cfg.name)

        disk.unlink()
        disk.write_bytes(b"replacement")
        outside.unlink()
        os.link(disk, outside)
        with self.assertRaisesRegex(VMError, "additional hard links"):
            self.manager.validate_disk_image(cfg.name)

    def test_start_explains_missing_vga_rom(self):
        self.manager.create("missing-rom-test")
        fake = self.root / "fake-bochs"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"), None]):
            with self.assertRaisesRegex(VMError, "apt install -y vgabios"):
                self.manager.start("missing-rom-test", str(fake))

    def test_generated_bochsrc_passes_static_validation(self):
        cfg = self.manager.create("validate-generated")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            text = self.manager.render_bochsrc(cfg)
        validate_bochsrc_text(text)

    def test_bochsrc_validator_rejects_missing_required_device_and_bad_cpu(self):
        cfg = self.manager.create("invalid-config")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            text = self.manager.render_bochsrc(cfg)
        with self.assertRaisesRegex(VMError, "Missing required Bochs directives"):
            validate_bochsrc_text(text.replace(
                'vgaromimage: file="/mock/vgabios/vgabios.bin"\n', ""
            ))
        with self.assertRaisesRegex(VMError, "ips="):
            validate_bochsrc_text(text.replace(
                "ips=10000000", "count=1"
            ))
        with self.assertRaisesRegex(VMError, "#include"):
            validate_bochsrc_text("#include /tmp/other.conf\n" + text)

    def test_custom_bochsrc_is_saved_used_and_reset_safely(self):
        self.manager.create("custom-config")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(self.manager.load("custom-config"))
        custom = generated + "mouse: type=imps2, enabled=1\n"
        path = self.manager.save_custom_bochsrc("custom-config", custom)
        self.assertEqual(path, self.manager.custom_bochsrc_path("custom-config"))
        self.assertEqual(path.read_text(encoding="utf-8"), custom)
        self.assertEqual(self.manager.active_bochsrc_path("custom-config"), path)
        with self.assertRaisesRegex(VMError, "custom config is currently authoritative"):
            self.manager.update_profile("custom-config", 768, 10_000_000, None)
        with self.assertRaisesRegex(VMError, "Restore the generated Bochs config"):
            self.manager.set_iso("custom-config", None)
        self.assertTrue(self.manager.reset_custom_bochsrc("custom-config"))
        self.assertFalse(self.manager.custom_bochsrc_path("custom-config").exists())
        self.assertEqual(self.manager.active_bochsrc_path("custom-config").name, "bochsrc")
        updated = self.manager.update_profile("custom-config", 768, 10_000_000, None)
        self.assertEqual(updated.memory_mib, 768)

    def test_start_tracks_custom_config_path(self):
        self.manager.create("custom-launch")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(self.manager.load("custom-launch"))
        custom = generated + "# custom config\n"
        custom_path = self.manager.save_custom_bochsrc("custom-launch", custom)
        fake = self.root / "fake-bochs-custom"
        args_file = self.root / "custom-bochs-args.txt"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys, time\n"
            f"pathlib.Path({str(args_file)!r}).write_text(chr(10).join(sys.argv[1:]) + chr(10))\n"
            "time.sleep(30)\n"
        )
        fake.chmod(0o755)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            pid = self.manager.start("custom-launch", str(fake))
        self.assertGreater(pid, 0)
        deadline = time.monotonic() + 2
        while not args_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(args_file.exists(), "mock Bochs did not record its custom config")
        args = args_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[args.index("-f") + 1], str(custom_path.resolve()))
        self.assertTrue(self.manager.is_running("custom-launch"))
        record = __import__("json").loads(
            (self.manager.vm_path("custom-launch") / "runtime/bochs.pid").read_text()
        )
        self.assertEqual(record["config"], str(custom_path.resolve()))
        self.manager.stop("custom-launch")
        self.assertFalse(self.manager.is_running("custom-launch"))

    def test_running_custom_config_remains_trackable_if_file_is_removed(self):
        self.manager.create("custom-config-removed")
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(self.manager.load("custom-config-removed"))
        custom_path = self.manager.save_custom_bochsrc(
            "custom-config-removed", generated + "# custom override\n"
        )
        fake = self.root / "fake-bochs-custom-removed"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import time\n"
            "time.sleep(30)\n"
        )
        fake.chmod(0o755)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            self.manager.start("custom-config-removed", str(fake))
        custom_path.unlink()
        self.assertTrue(self.manager.is_running("custom-config-removed"))
        with self.assertRaisesRegex(VMError, "Stop the VM before deleting it"):
            self.manager.delete("custom-config-removed")
        self.manager.stop("custom-config-removed")
        self.assertFalse(self.manager.is_running("custom-config-removed"))

    def test_iso_attach(self):
        iso = self.root / "setup.iso"
        iso.write_bytes(b"not a real ISO; only tests image path validation")
        cfg = self.manager.create("install-me", iso=iso)
        cfg = self.manager.set_iso(cfg.name, iso)
        self.assertEqual(cfg.iso_path, str(iso.resolve()))
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/VGABIOS-lgpl-latest")]):
            config = self.manager.render_bochsrc(cfg)
        self.assertIn("type=cdrom", config)
        self.assertIn("boot: cdrom, disk", config)

    def test_floppy_a_accepts_standard_1440k_image_and_sets_boot_order(self):
        cfg = self.manager.create("floppy-boot")
        floppy = self.root / "dos-boot.img"
        floppy.write_bytes(b"\0" * (1_440 * 1024))
        updated = self.manager.set_floppy_a(cfg.name, floppy)
        self.assertEqual(updated.floppy_a_path, str(floppy.resolve()))
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(updated)
        self.assertIn(
            f'floppya: image="{floppy.resolve()}", status=inserted, write_protected=1',
            generated,
        )
        self.assertIn("boot: floppy, disk", generated)
        validate_bochsrc_text(generated)
        self.manager.validate_bochsrc_for_vm(cfg.name, generated)
        self.assertEqual(self.manager.validate_floppy_image(cfg.name), floppy.resolve())

    def test_floppy_b_attaches_without_changing_floppy_a_boot_priority(self):
        cfg = self.manager.create("floppy-b-data")
        floppy_b = self.root / "dos-data.img"
        floppy_b.write_bytes(b"\\0" * (1_440 * 1024))
        updated = self.manager.set_floppy_b(cfg.name, floppy_b)
        self.assertEqual(updated.floppy_b_path, str(floppy_b.resolve()))
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(updated)
        self.assertIn(
            f'floppyb: image="{floppy_b.resolve()}", status=inserted, write_protected=1',
            generated,
        )
        self.assertIn("boot: disk", generated)
        validate_bochsrc_text(generated)
        self.manager.validate_bochsrc_for_vm(cfg.name, generated)
        self.assertEqual(self.manager.validate_floppy_b_image(cfg.name), floppy_b.resolve())

    def test_floppy_a_and_b_can_be_attached_together(self):
        cfg = self.manager.create("two-floppies")
        floppy_a = self.root / "disk-a.img"
        floppy_b = self.root / "disk-b.img"
        floppy_a.write_bytes(b"\\0" * (1_440 * 1024))
        floppy_b.write_bytes(b"\\0" * (720 * 1024))
        cfg = self.manager.set_floppy_a(cfg.name, floppy_a)
        cfg = self.manager.set_floppy_b(cfg.name, floppy_b)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        self.assertIn(f'floppya: image="{floppy_a.resolve()}"', generated)
        self.assertIn(f'floppyb: image="{floppy_b.resolve()}"', generated)
        self.assertIn("boot: floppy, disk", generated)
        self.manager.validate_bochsrc_for_vm(cfg.name, generated)

    def test_floppy_b_rejects_unsupported_sizes_and_missing_files(self):
        cfg = self.manager.create("bad-floppy-b")
        invalid = self.root / "not-a-floppy-b.img"
        invalid.write_bytes(b"invalid")
        with self.assertRaisesRegex(VMError, "supported exact size"):
            self.manager.set_floppy_b(cfg.name, invalid)
        with self.assertRaises(VMError):
            self.manager.set_floppy_b(cfg.name, self.root / "missing-b.img")
        self.assertIsNone(self.manager.load(cfg.name).floppy_b_path)

    def test_custom_bochsrc_cannot_redirect_attached_floppy_b_image(self):
        cfg = self.manager.create("floppy-b-custom-guard")
        floppy_b = self.root / "original-b.img"
        floppy_b.write_bytes(b"\\0" * (1_440 * 1024))
        cfg = self.manager.set_floppy_b(cfg.name, floppy_b)
        outside = self.root / "other-b.img"
        outside.write_bytes(b"\\0" * (1_440 * 1024))
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        redirected = generated.replace(
            f'image="{floppy_b.resolve()}"',
            f'image="{outside.resolve()}"',
        )
        with self.assertRaisesRegex(VMError, "floppyb image must remain"):
            self.manager.save_custom_bochsrc(cfg.name, redirected)
        self.assertFalse(self.manager.custom_bochsrc_path(cfg.name).exists())

    def test_custom_config_cannot_disable_write_protection_for_either_floppy(self):
        cfg = self.manager.create("floppy-write-protection")
        floppy_a = self.root / "protected-a.img"
        floppy_b = self.root / "protected-b.img"
        floppy_a.write_bytes(b"\\0" * (1_440 * 1024))
        floppy_b.write_bytes(b"\\0" * (720 * 1024))
        cfg = self.manager.set_floppy_a(cfg.name, floppy_a)
        cfg = self.manager.set_floppy_b(cfg.name, floppy_b)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        for directive in ("floppya:", "floppyb:"):
            lines = generated.splitlines()
            index = next(i for i, line in enumerate(lines) if line.startswith(directive))
            lines[index] = lines[index].replace("write_protected=1", "write_protected=0")
            with self.subTest(directive=directive):
                with self.assertRaisesRegex(VMError, "must remain write-protected"):
                    self.manager.save_custom_bochsrc(cfg.name, "\n".join(lines))
        self.assertFalse(self.manager.custom_bochsrc_path(cfg.name).exists())

    def test_boot_rejects_floppy_b_image_removed_after_attachment(self):
        cfg = self.manager.create("removed-floppy-b")
        floppy_b = self.root / "removed-b.img"
        floppy_b.write_bytes(b"\\0" * (1_440 * 1024))
        self.manager.set_floppy_b(cfg.name, floppy_b)
        floppy_b.unlink()
        with patch.object(self.manager, "find_bochs", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(VMError, "not a readable file"):
                self.manager.start(cfg.name)

    def test_floppy_boot_order_prefers_floppy_then_cdrom_then_disk(self):
        iso = self.root / "installer.iso"
        iso.write_bytes(b"test iso")
        floppy = self.root / "boot.img"
        floppy.write_bytes(b"\0" * (1_440 * 1024))
        cfg = self.manager.create("floppy-and-cd", iso=iso)
        cfg = self.manager.set_floppy_a(cfg.name, floppy)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        self.assertIn("boot: floppy, cdrom, disk", generated)
        self.assertIn("ata0-slave: type=cdrom", generated)

    def test_floppy_rejects_unsupported_sizes_and_missing_files(self):
        cfg = self.manager.create("bad-floppy")
        invalid = self.root / "not-a-floppy.img"
        invalid.write_bytes(b"not a supported floppy capacity")
        with self.assertRaisesRegex(VMError, "supported exact size"):
            self.manager.set_floppy_a(cfg.name, invalid)
        with self.assertRaises(VMError):
            self.manager.set_floppy_a(cfg.name, self.root / "missing.img")
        self.assertIsNone(self.manager.load(cfg.name).floppy_a_path)

    def test_legacy_profile_without_floppy_field_still_loads(self):
        cfg = self.manager.create("legacy-floppy-profile")
        raw = cfg.__dict__.copy()
        raw.pop("floppy_a_path")
        raw.pop("floppy_b_path")
        loaded = VMConfig.from_dict(raw)
        self.assertIsNone(loaded.floppy_a_path)
        self.assertIsNone(loaded.floppy_b_path)

    def test_custom_bochsrc_cannot_redirect_attached_floppy_image(self):
        cfg = self.manager.create("floppy-custom-guard")
        floppy = self.root / "original.img"
        floppy.write_bytes(b"\0" * (1_440 * 1024))
        cfg = self.manager.set_floppy_a(cfg.name, floppy)
        outside = self.root / "other.img"
        outside.write_bytes(b"\0" * (1_440 * 1024))
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            generated = self.manager.render_bochsrc(cfg)
        redirected = generated.replace(
            f'image="{floppy.resolve()}"',
            f'image="{outside.resolve()}"',
        )
        with self.assertRaisesRegex(VMError, "floppy A image attached"):
            self.manager.save_custom_bochsrc(cfg.name, redirected)
        self.assertFalse(self.manager.custom_bochsrc_path(cfg.name).exists())

    def test_boot_rejects_floppy_image_removed_after_attachment(self):
        cfg = self.manager.create("removed-floppy")
        floppy = self.root / "removed.img"
        floppy.write_bytes(b"\0" * (1_440 * 1024))
        self.manager.set_floppy_a(cfg.name, floppy)
        floppy.unlink()
        with patch.object(self.manager, "find_bochs", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(VMError, "not a readable file"):
                self.manager.start(cfg.name)

    def test_update_profile_can_eject_iso_and_change_resources(self):
        iso = self.root / "install.iso"
        iso.write_bytes(b"test image")
        cfg = self.manager.create("editable", iso=iso)
        updated = self.manager.update_profile(cfg.name, 768, 12_000_000, None, "p2_klamath")
        self.assertEqual(updated.memory_mib, 768)
        self.assertEqual(updated.cpu_model, "p2_klamath")
        self.assertEqual(updated.cpu_ips, 12_000_000)
        self.assertIsNone(updated.iso_path)
        self.assertTrue(Path(updated.disk_path).is_file())

    def test_update_profile_rejects_running_vm(self):
        cfg = self.manager.create("editing-running")
        process, config_file, ticks = self._spawn_marked_process(cfg.name)
        self.manager._write_pid_record(cfg.name, process.pid, ticks, config_file)
        with self.assertRaisesRegex(VMError, "Stop the VM"):
            self.manager.update_profile(cfg.name, 512, 10_000_000, None)

    def test_missing_iso_rejected(self):
        with self.assertRaises(VMError):
            self.manager.create("bad-iso", iso=self.root / "missing.iso")

    def test_duplicate_and_delete(self):
        self.manager.create("same")
        with self.assertRaises(VMError):
            self.manager.create("same")
        self.manager.delete("same")
        self.assertFalse(self.manager.vm_path("same").exists())

    def test_running_vm_protected_from_deletion(self):
        cfg = self.manager.create("running")
        process, config_file, ticks = self._spawn_marked_process(cfg.name)
        self.manager._write_pid_record(cfg.name, process.pid, ticks, config_file)
        with self.assertRaises(VMError):
            self.manager.delete(cfg.name)

    def test_legacy_pid_marker_migrates_only_for_matching_bochs_command(self):
        cfg = self.manager.create("legacy-pid")
        process, config_file, _ticks = self._spawn_marked_process(cfg.name)
        pid_file = self.manager.vm_path(cfg.name) / "runtime/bochs.pid"
        pid_file.write_text(str(process.pid), encoding="ascii")
        self.assertTrue(self.manager.is_running(cfg.name))
        record = __import__("json").loads(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], process.pid)
        self.assertEqual(record["config"], str(config_file))

    def test_legacy_pid_marker_does_not_adopt_or_signal_unrelated_process(self):
        cfg = self.manager.create("unrelated-pid")
        pid_file = self.manager.vm_path(cfg.name) / "runtime/bochs.pid"
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        self.assertFalse(self.manager.is_running(cfg.name))
        self.assertFalse(pid_file.exists())
        # The test runner itself must remain alive; a stale PID is never killed.
        os.kill(os.getpid(), 0)

    def test_pid_start_time_mismatch_is_treated_as_reused_pid(self):
        cfg = self.manager.create("reused-pid")
        process, config_file, ticks = self._spawn_marked_process(cfg.name)
        self.manager._write_pid_record(cfg.name, process.pid, ticks + 1, config_file)
        self.assertFalse(self.manager.is_running(cfg.name))
        self.assertFalse((self.manager.vm_path(cfg.name) / "runtime/bochs.pid").exists())
        self.assertIsNone(process.poll())

    def test_mock_bochs_start_and_stop(self):
        self.manager.create("launch-test")
        fake = self.root / "fake-bochs"
        args_file = self.root / "bochs-args.txt"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys, time\n"
            f"pathlib.Path({str(args_file)!r}).write_text(chr(10).join(sys.argv[1:]) + chr(10))\n"
            "time.sleep(30)\n"
        )
        fake.chmod(0o755)
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/bochs-bios"), Path("/mock/vga-bios"),
                                       Path("/mock/bochs-bios"), Path("/mock/vga-bios")]):
            pid = self.manager.start("launch-test", str(fake))
        self.assertGreater(pid, 0)
        deadline = time.monotonic() + 2
        launcher_log = self.manager.vm_path("launch-test") / "logs/launcher.log"
        while not launcher_log.exists() or not args_file.exists():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        self.assertTrue(args_file.exists(), "mock Bochs did not record its arguments")
        self.assertTrue(self.manager.is_running("launch-test"))
        self.assertEqual(
            args_file.read_text().splitlines(),
            ["-q", "-f", str((self.manager.vm_path("launch-test") / "bochsrc").resolve())],
        )
        pid_record = __import__("json").loads(
            (self.manager.vm_path("launch-test") / "runtime/bochs.pid").read_text()
        )
        self.assertEqual(pid_record["pid"], pid)
        self.assertIsInstance(pid_record["start_ticks"], int)
        self.manager.stop("launch-test")
        self.assertFalse(self.manager.is_running("launch-test"))




class IDEStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = VMManager(self.root / "vms")

    def _render(self, cfg):
        with patch.object(self.manager, "_find_bios",
                          side_effect=[Path("/mock/BIOS-bochs-latest"),
                                       Path("/mock/vgabios/vgabios.bin")]):
            return self.manager.render_bochsrc(cfg)

    def test_create_second_sparse_disk_and_place_cdrom_on_secondary_channel(self):
        iso = self.root / "install.iso"
        iso.write_bytes(b"test ISO")
        cfg = self.manager.create(
            "dual-ide", disk_gib=1, secondary_disk_gib=2, iso=iso,
            cdrom_slot="ata1-slave",
        )
        second = Path(cfg.secondary_disk_path)
        self.assertEqual(cfg.secondary_disk_gib, 2)
        self.assertEqual(second, self.manager.vm_path(cfg.name) / "disk2.raw")
        self.assertEqual(second.stat().st_size, disk_geometry(2)[3])
        self.assertEqual(self.manager.validate_secondary_disk_image(cfg.name), second.resolve())
        generated = self._render(cfg)
        self.assertIn("ata0: enabled=1, ioaddr1=0x1f0, ioaddr2=0x3f0, irq=14", generated)
        self.assertIn("ata1: enabled=1, ioaddr1=0x170, ioaddr2=0x370, irq=15", generated)
        self.assertIn(f'ata1-master: type=disk, mode=flat, path="{second.resolve()}"', generated)
        self.assertIn(f'ata1-slave: type=cdrom, path="{iso.resolve()}", status=inserted', generated)
        self.assertNotIn("ata0-slave: type=cdrom", generated)
        validate_bochsrc_text(generated)
        self.manager.validate_bochsrc_for_vm(cfg.name, generated)

    def test_attach_detach_and_reattach_preserves_secondary_disk_data(self):
        cfg = self.manager.create("attach-ide")
        attached = self.manager.attach_secondary_disk(cfg.name, 1)
        disk = Path(attached.secondary_disk_path)
        self.assertTrue(disk.is_file())
        with disk.open("r+b") as stream:
            stream.write(b"preserved test data")
        detached_path = self.manager.detach_secondary_disk(cfg.name)
        self.assertEqual(detached_path, disk.resolve())
        self.assertIsNone(self.manager.load(cfg.name).secondary_disk_path)
        with disk.open("rb") as stream:
            self.assertEqual(stream.read(19), b"preserved test data")
        with self.assertRaisesRegex(VMError, "different size"):
            self.manager.attach_secondary_disk(cfg.name, 2)
        reattached = self.manager.attach_secondary_disk(cfg.name, 1)
        self.assertEqual(reattached.secondary_disk_path, str(disk.resolve()))
        with disk.open("rb") as stream:
            self.assertEqual(stream.read(19), b"preserved test data")

    def test_secondary_disk_size_is_bounded(self):
        with self.assertRaisesRegex(VMError, "between 1 and 512"):
            self.manager.create("oversized-secondary", secondary_disk_gib=513)
        cfg = self.manager.create("oversized-attach")
        with self.assertRaisesRegex(VMError, "between 1 and 512"):
            self.manager.attach_secondary_disk(cfg.name, 513)

    def test_secondary_disk_validation_rejects_truncated_and_linked_files(self):
        cfg = self.manager.create("bad-secondary", secondary_disk_gib=1)
        disk = Path(cfg.secondary_disk_path)
        disk.write_bytes(b"truncated")
        with self.assertRaisesRegex(VMError, "profile requires"):
            self.manager.validate_secondary_disk_image(cfg.name)
        disk.unlink()
        disk.touch()
        outside = self.root / "linked-secondary.raw"
        os.link(disk, outside)
        with self.assertRaisesRegex(VMError, "additional hard links"):
            self.manager.validate_secondary_disk_image(cfg.name)

    def test_custom_config_cannot_redirect_or_invent_secondary_disk(self):
        cfg = self.manager.create("secondary-custom", secondary_disk_gib=1)
        generated = self._render(cfg)
        outside = self.root / "outside.raw"
        outside.write_bytes(b"not the guest disk")
        redirected = generated.replace(
            f'path="{cfg.secondary_disk_path}"', f'path="{outside.resolve()}"'
        )
        with self.assertRaisesRegex(VMError, "private disk2.raw"):
            self.manager.save_custom_bochsrc(cfg.name, redirected)

        no_disk = self.manager.create("no-secondary-custom")
        no_disk_config = self._render(no_disk)
        invented = no_disk_config.replace(
            "boot: disk",
            f'ata1-master: type=disk, mode=flat, path="{outside.resolve()}", '
            "cylinders=100, heads=16, spt=63\nboot: disk",
        )
        with self.assertRaisesRegex(VMError, "unmanaged secondary IDE disk"):
            self.manager.save_custom_bochsrc(no_disk.name, invented)

    def test_cdrom_slot_can_change_and_custom_path_is_pinned(self):
        iso = self.root / "media.iso"
        iso.write_bytes(b"media")
        cfg = self.manager.create("cdrom-position", iso=iso, cdrom_slot="ata1-slave")
        generated = self._render(cfg)
        self.assertIn("ata1-slave: type=cdrom", generated)
        self.manager.validate_bochsrc_for_vm(cfg.name, generated)
        changed = self.manager.update_profile(
            cfg.name, cfg.memory_mib, cfg.cpu_ips, iso, cfg.cpu_model,
            cfg.chipset, "ata0-slave",
        )
        self.assertEqual(changed.cdrom_slot, "ata0-slave")
        generated = self._render(changed)
        self.assertIn("ata0-slave: type=cdrom", generated)
        self.assertNotIn("ata1-slave: type=cdrom", generated)
        outside = self.root / "other.iso"
        outside.write_bytes(b"other")
        redirected = generated.replace(f'path="{iso.resolve()}"', f'path="{outside.resolve()}"')
        with self.assertRaisesRegex(VMError, "CD-ROM image must remain"):
            self.manager.save_custom_bochsrc(cfg.name, redirected)

    def test_custom_config_cannot_move_cdrom_to_an_unselected_ide_position(self):
        iso = self.root / "installer.iso"
        iso.write_bytes(b"installer")
        cfg = self.manager.create("cdrom-guard", iso=iso, cdrom_slot="ata0-slave")
        generated = self._render(cfg)
        moved = generated.replace("ata0-slave: type=cdrom", "ata1-slave: type=cdrom")
        with self.assertRaisesRegex(VMError, "selected ata0-slave"):
            self.manager.save_custom_bochsrc(cfg.name, moved)

    def test_legacy_profiles_default_to_primary_channel_cdrom_and_no_second_disk(self):
        cfg = self.manager.create("legacy-ide")
        profile = self.manager.config_path(cfg.name)
        raw = json.loads(profile.read_text(encoding="utf-8"))
        raw.pop("secondary_disk_gib")
        raw.pop("secondary_disk_path")
        raw.pop("cdrom_slot")
        profile.write_text(json.dumps(raw), encoding="utf-8")
        loaded = self.manager.load(cfg.name)
        self.assertIsNone(loaded.secondary_disk_gib)
        self.assertIsNone(loaded.secondary_disk_path)
        self.assertEqual(loaded.cdrom_slot, "ata0-slave")

    def test_ide_slot_allowlist_rejects_unknown_values(self):
        self.assertEqual(set(IDE_CDROM_SLOT_LABELS), {"ata0-slave", "ata1-slave"})
        with self.assertRaisesRegex(VMError, "CD-ROM position"):
            self.manager.create("bad-cdrom-slot", cdrom_slot="ata2-master")

    def test_custom_config_cannot_change_managed_ide_disk_geometry(self):
        cfg = self.manager.create("geometry-guard", secondary_disk_gib=1)
        generated = self._render(cfg)
        primary_cylinders = disk_geometry(cfg.disk_gib)[0]
        secondary_cylinders = disk_geometry(cfg.secondary_disk_gib)[0]
        tampered_primary = generated.replace(
            f"cylinders={primary_cylinders}", "cylinders=1", 1
        )
        with self.assertRaisesRegex(VMError, "ata0-master geometry"):
            self.manager.save_custom_bochsrc(cfg.name, tampered_primary)
        tampered_secondary = generated.replace(
            f"cylinders={secondary_cylinders}", "cylinders=1", 1
        )
        with self.assertRaisesRegex(VMError, "ata1-master geometry"):
            self.manager.save_custom_bochsrc(cfg.name, tampered_secondary)

    def test_secondary_disk_changes_are_blocked_for_running_or_custom_config_vms(self):
        cfg = self.manager.create("ide-running")
        fake = self.root / "fake-bochs"
        fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        fake.chmod(0o755)
        # Use the manager's real launcher identity checks with a long-lived mock.
        with patch.object(self.manager, "find_bochs", return_value=str(fake)):
            with patch.object(self.manager, "_find_bios",
                              side_effect=[Path("/mock/BIOS-bochs-latest"),
                                           Path("/mock/vgabios/vgabios.bin")]):
                self.manager.start(cfg.name)
        with self.assertRaisesRegex(VMError, "Stop the VM"):
            self.manager.attach_secondary_disk(cfg.name, 1)
        self.manager.stop(cfg.name)
        generated = self._render(self.manager.load(cfg.name))
        self.manager.save_custom_bochsrc(cfg.name, generated + "# custom\n")
        with self.assertRaisesRegex(VMError, "Restore the generated Bochs config"):
            self.manager.attach_secondary_disk(cfg.name, 1)

if __name__ == "__main__":
    unittest.main()
