import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from chromeos_vm.cli import main
from chromeos_vm.manager import VMManager


class IDEStorageCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "vms"

    def run_cli(self, *args):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = main(["--home", str(self.home), *args])
        return result, output.getvalue() + errors.getvalue()

    def test_create_and_info_report_secondary_disk_and_cdrom_slot(self):
        result, output = self.run_cli(
            "create", "cli-ide", "--disk-gib", "1",
            "--secondary-disk-gib", "2", "--cdrom-slot", "ata1-slave",
        )
        self.assertEqual(result, 0)
        self.assertIn("2 GiB IDE1-master disk", output)
        manager = VMManager(self.home)
        cfg = manager.load("cli-ide")
        self.assertTrue(Path(cfg.secondary_disk_path).is_file())
        self.assertEqual(cfg.cdrom_slot, "ata1-slave")

        result, info = self.run_cli("info", "cli-ide")
        self.assertEqual(result, 0)
        self.assertIn("Secondary disk:", info)
        self.assertIn("CD-ROM IDE position: ata1-slave", info)

    def test_attach_and_detach_keep_secondary_disk_data(self):
        self.run_cli("create", "cli-existing", "--disk-gib", "1")
        result, output = self.run_cli("attach-disk", "cli-existing", "--disk-gib", "1")
        self.assertEqual(result, 0)
        self.assertIn("IDE1 master", output)
        disk = self.home / "cli-existing" / "disk2.raw"
        with disk.open("r+b") as stream:
            stream.write(b"keep this data")
        result, output = self.run_cli("detach-disk", "cli-existing")
        self.assertEqual(result, 0)
        self.assertIn("data remains", output)
        with disk.open("rb") as stream:
            self.assertEqual(stream.read(14), b"keep this data")
        self.assertIsNone(VMManager(self.home).load("cli-existing").secondary_disk_path)

    def test_create_can_enable_sb16_audio_from_cli(self):
        result, output = self.run_cli("create", "cli-sound", "--disk-gib", "1", "--sound")
        self.assertEqual(result, 0)
        self.assertIn("Audio: SB16/PC speaker enabled", output)
        manager = VMManager(self.home)
        self.assertTrue(manager.load("cli-sound").sound_enabled)

        result, info = self.run_cli("info", "cli-sound")
        self.assertEqual(result, 0)
        self.assertIn("Sound: SB16/PC speaker enabled", info)

    def test_cli_rejects_unknown_cdrom_position(self):
        with self.assertRaises(SystemExit):
            self.run_cli("create", "bad-position", "--cdrom-slot", "ata2-master")


    def test_create_and_set_boot_order_are_exposed_by_cli(self):
        iso = Path(self.temp.name) / "install.iso"
        iso.write_bytes(b"test ISO")
        result, output = self.run_cli(
            "create", "cli-boot", "--disk-gib", "1", "--iso", str(iso),
            "--boot-order", "cdrom,disk",
        )
        self.assertEqual(result, 0)
        self.assertIn("BIOS boot order: cdrom, disk", output)
        manager = VMManager(self.home)
        self.assertEqual(manager.load("cli-boot").boot_order, ("cdrom", "disk"))

        result, output = self.run_cli(
            "set-boot-order", "cli-boot", "--order", "network,disk"
        )
        self.assertEqual(result, 0)
        self.assertIn("network, disk", output)
        self.assertEqual(manager.load("cli-boot").boot_order, ("network", "disk"))

        result, output = self.run_cli(
            "set-boot-order", "cli-boot", "--order", "auto"
        )
        self.assertEqual(result, 0)
        self.assertIn("automatic (media-aware)", output)
        self.assertIsNone(manager.load("cli-boot").boot_order)

    def test_cli_rejects_boot_order_for_unattached_media(self):
        result, output = self.run_cli(
            "create", "cli-invalid-boot", "--disk-gib", "1", "--boot-order", "cdrom,disk"
        )
        self.assertEqual(result, 2)
        self.assertIn("CD/DVD boot requires", output)
        self.assertFalse((self.home / "cli-invalid-boot").exists())


    def test_diagnose_reports_vm_health_and_shutdown_guidance(self):
        self.run_cli("create", "cli-diagnostics", "--disk-gib", "1")
        from unittest.mock import patch
        with patch.object(VMManager, "find_bochs", return_value=None), \
             patch.object(VMManager, "_find_bios", return_value=None):
            result, report = self.run_cli("diagnose", "cli-diagnostics")
        self.assertEqual(result, 0)
        self.assertIn("State: Stopped", report)
        self.assertIn("Primary disk: OK", report)
        self.assertIn("Bochs: not installed", report)
        self.assertIn("Guest shutdown guidance:", report)

if __name__ == "__main__":
    unittest.main()
