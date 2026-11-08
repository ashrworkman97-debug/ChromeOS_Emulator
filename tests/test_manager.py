import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path

from chromeos_vm.manager import (
    VMConfig, VMError, VMManager, bochs_quote, disk_geometry,
    validate_name, validate_resources,
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

    def test_create_sparse_disk_and_bochsrc(self):
        cfg = self.manager.create("retro-pc", 512, 1, 10_000_000)
        disk = Path(cfg.disk_path)
        cylinders, heads, sectors, expected_bytes = disk_geometry(1)
        self.assertEqual(disk.stat().st_size, expected_bytes)
        self.assertEqual(disk.stat().st_size, cylinders * heads * sectors * 512)
        config = (self.manager.vm_path(cfg.name) / "bochsrc").read_text()
        self.assertIn("megs: 512", config)
        self.assertIn("display_library: sdl2", config)
        self.assertIn(f"cylinders={cylinders}, heads={heads}, spt={sectors}", config)
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
        args_file = self.root / "bochs-args.txt"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > " + shlex.quote(str(args_file)) + "\n"
            "exec /bin/sleep 30\n"
        )
        fake.chmod(0o755)
        pid = self.manager.start("launch-test", str(fake))
        self.assertGreater(pid, 0)
        deadline = time.monotonic() + 2
        while not args_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(args_file.exists(), "mock Bochs did not record its arguments")
        self.assertTrue(self.manager.is_running("launch-test"))
        self.assertEqual(
            args_file.read_text().splitlines(),
            ["-q", "-f", str(self.manager.vm_path("launch-test") / "bochsrc")],
        )
        self.manager.stop("launch-test")
        self.assertFalse(self.manager.is_running("launch-test"))


if __name__ == "__main__":
    unittest.main()
