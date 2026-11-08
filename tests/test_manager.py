import tempfile
import unittest
from pathlib import Path
from chromeos_vm.manager import VMError, VMManager, VMConfig, validate_name, validate_resources


class ValidationTests(unittest.TestCase):
    def test_rejects_bad_names(self):
        for name in ("../outside", "two words", "-leading", "a" * 49):
            with self.subTest(name=name), self.assertRaises(VMError):
                validate_name(name)

    def test_valid_name(self):
        validate_name("chromeos-test_01")

    def test_resource_validation(self):
        for values in ((128, 2, 8), (4096, 0, 8), (4096, 2, 0), ("4G", 2, 8)):
            with self.subTest(values=values), self.assertRaises(VMError):
                validate_resources(*values)

    def test_rejects_qemu_backend(self):
        raw = {"name":"vm1","memory_mib":1024,"cpus":1,"disk_gib":1,
               "disk_path":"/tmp/disk.raw","backend":"qemu","disk_format":"raw"}
        with self.assertRaises(VMError):
            VMConfig.from_dict(raw)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = VMManager(Path(self.temp.name) / "vms")

    def test_create_and_list_sparse_disk(self):
        cfg = self.manager.create("test-vm", 1024, 1, 1)
        self.assertEqual(Path(cfg.disk_path).stat().st_size, 1024**3)
        self.assertEqual(self.manager.load("test-vm"), cfg)
        self.assertEqual([x.name for x in self.manager.list_vms()], ["test-vm"])

    def test_duplicate_name(self):
        self.manager.create("same")
        with self.assertRaises(VMError):
            self.manager.create("same")

    def test_missing_kernel_rejected(self):
        self.manager.create("vm")
        with self.assertRaises(VMError):
            self.manager.set_kernel("vm", Path(self.temp.name) / "no-kernel")

    def test_delete_stopped_vm(self):
        self.manager.create("vm")
        self.manager.delete("vm")
        self.assertFalse(self.manager.vm_path("vm").exists())


if __name__ == "__main__":
    unittest.main()
