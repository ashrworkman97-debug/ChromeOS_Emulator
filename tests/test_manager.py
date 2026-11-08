import os
import tempfile
import unittest
from pathlib import Path

from chromeos_vm.manager import VMConfig, VMError, VMManager, bochs_quote, validate_name, validate_resources


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


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = VMManager(self.root / "vms")

    def test_create_sparse_disk_and_bochsrc(self):
        cfg = self.manager.create("retro-pc", 512, 1, 10_000_000)
        disk = Path(cfg.disk_path)
        self.assertEqual(disk.stat().st_size, 1024**3)
        config = (self.manager.vm_path(cfg.name) / "bochsrc").read_text()
        self.assertIn("megs: 512", config)
        self.assertIn("ata0-master: type=disk", config)
        self.assertIn("boot: disk", config)
        self.assertNotIn("qemu", config.lower())

    def test_iso_attach(self):
        iso = self.root / "setup.iso"
        iso.write_bytes(b"not a real ISO; only tests image path validation")
        cfg = self.manager.create("install-me", iso=iso)
        cfg = self.manager.set_iso(cfg.name, iso)
        self.assertEqual(cfg.iso_path, str(iso.resolve()))
        config = (self.manager.vm_path(cfg.name) / "bochsrc").read_text()
        self.assertIn("type=cdrom", config)
        self.assertIn("boot: cdrom, disk", config)

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
        runtime = self.manager.vm_path(cfg.name) / "runtime/bochs.pid"
        runtime.write_text(str(os.getpid()))
        with self.assertRaises(VMError):
            self.manager.delete(cfg.name)

    def test_mock_bochs_start_and_stop(self):
        self.manager.create("launch-test")
        fake = self.root / "fake-bochs"
        fake.write_text("#!/bin/sh\\nexec /bin/sleep 30\\n")
        fake.chmod(0o755)
        pid = self.manager.start("launch-test", str(fake))
        self.assertGreater(pid, 0)
        self.assertTrue(self.manager.is_running("launch-test"))
        self.manager.stop("launch-test")
        self.assertFalse(self.manager.is_running("launch-test"))


if __name__ == "__main__":
    unittest.main()
