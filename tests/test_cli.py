import io
import tempfile
import unittest
from contextlib import redirect_stdout
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
        with redirect_stdout(output):
            result = main(["--home", str(self.home), *args])
        return result, output.getvalue()

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

    def test_cli_rejects_unknown_cdrom_position(self):
        with self.assertRaises(SystemExit):
            self.run_cli("create", "bad-position", "--cdrom-slot", "ata2-master")


if __name__ == "__main__":
    unittest.main()
