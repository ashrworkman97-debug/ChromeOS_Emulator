import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from chromeos_vm.backup import export_vm, import_vm
from chromeos_vm.manager import VMError, VMManager


SMALL_GEOMETRY = (1, 1, 2048, 1024 * 1024)


class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source_home = self.root / "source"
        self.restore_home = self.root / "restore"
        self.manager = VMManager(self.source_home)
        self.patches = [
            patch("chromeos_vm.manager.disk_geometry", return_value=SMALL_GEOMETRY),
            patch("chromeos_vm.backup.disk_geometry", return_value=SMALL_GEOMETRY),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _make_vm(self, name="source-vm"):
        iso = self.root / "installer.iso"
        iso.write_bytes(b"external installer media")
        cfg = self.manager.create(
            name, disk_gib=1, secondary_disk_gib=1, iso=iso
        )
        with Path(cfg.disk_path).open("r+b") as stream:
            stream.write(b"primary guest data")
        with Path(cfg.secondary_disk_path).open("r+b") as stream:
            stream.write(b"secondary guest data")
        return cfg, iso

    def test_round_trip_rewrites_private_disk_paths_and_preserves_detached_disk(self):
        cfg, iso = self._make_vm()
        custom = self.manager.custom_bochsrc_path(cfg.name)
        custom.write_text("# custom config is intentionally not portable\n", encoding="utf-8")

        attached_archive = self.root / "attached.zip"
        export_vm(self.manager, cfg.name, attached_archive)
        restored_manager = VMManager(self.restore_home)
        attached = import_vm(restored_manager, attached_archive, "restored-attached")
        self.assertEqual(attached.name, "restored-attached")
        self.assertEqual(Path(attached.disk_path), restored_manager.vm_path(attached.name) / "disk.raw")
        self.assertEqual(Path(attached.secondary_disk_path), restored_manager.vm_path(attached.name) / "disk2.raw")
        self.assertEqual(Path(attached.disk_path).read_bytes()[:18], b"primary guest data")
        self.assertEqual(Path(attached.secondary_disk_path).read_bytes()[:20], b"secondary guest data")
        self.assertEqual(attached.iso_path, str(iso.resolve()))
        self.assertFalse(restored_manager.custom_bochsrc_path(attached.name).exists())

        detached_disk = self.manager.detach_secondary_disk(cfg.name)
        self.assertEqual(detached_disk.read_bytes()[:20], b"secondary guest data")
        detached_archive = self.root / "detached.zip"
        export_vm(self.manager, cfg.name, detached_archive)
        detached = import_vm(restored_manager, detached_archive, "restored-detached")
        self.assertIsNone(detached.secondary_disk_path)
        self.assertIsNone(detached.secondary_disk_gib)
        self.assertEqual(
            (restored_manager.vm_path(detached.name) / "disk2.raw").read_bytes()[:20],
            b"secondary guest data",
        )
        self.assertEqual(detached.iso_path, str(iso.resolve()))

        with zipfile.ZipFile(detached_archive) as archive:
            self.assertEqual(set(archive.namelist()), {
                "manifest.json", "vm.json", "disk.raw", "disk2.raw"
            })

    def test_export_refuses_running_vm_and_existing_destination(self):
        cfg, _iso = self._make_vm()
        destination = self.root / "backup.zip"
        with patch.object(self.manager, "is_running", return_value=True):
            with self.assertRaisesRegex(VMError, "stop Bochs"):
                export_vm(self.manager, cfg.name, destination)
        export_vm(self.manager, cfg.name, destination)
        with self.assertRaisesRegex(VMError, "already exists"):
            export_vm(self.manager, cfg.name, destination)

    def test_import_rejects_path_traversal_and_unknown_entries(self):
        cfg, _iso = self._make_vm()
        good = self.root / "good.zip"
        export_vm(self.manager, cfg.name, good)
        with zipfile.ZipFile(good) as source:
            entries = [(info.filename, source.read(info)) for info in source.infolist()]
        bad = self.root / "traversal.zip"
        with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, data in entries:
                archive.writestr("../outside.txt" if filename == "disk2.raw" else filename, data)
        with self.assertRaisesRegex(VMError, "unsafe entry"):
            import_vm(VMManager(self.restore_home), bad)

    def test_import_rejects_symlink_members(self):
        cfg, _iso = self._make_vm()
        good = self.root / "good.zip"
        export_vm(self.manager, cfg.name, good)
        with zipfile.ZipFile(good) as source:
            entries = {info.filename: source.read(info) for info in source.infolist()}
        bad = self.root / "symlink.zip"
        with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, data in entries.items():
                if filename == "disk.raw":
                    info = zipfile.ZipInfo(filename)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, b"target")
                else:
                    archive.writestr(filename, data)
        with self.assertRaisesRegex(VMError, "not a regular file"):
            import_vm(VMManager(self.restore_home), bad)

    def test_import_rejects_wrong_disk_length_and_existing_vm(self):
        cfg, _iso = self._make_vm()
        good = self.root / "good.zip"
        export_vm(self.manager, cfg.name, good)
        with zipfile.ZipFile(good) as source:
            entries = {info.filename: source.read(info) for info in source.infolist()}
        bad = self.root / "short-disk.zip"
        with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, data in entries.items():
                archive.writestr(filename, data[:-1] if filename == "disk.raw" else data)
        with self.assertRaisesRegex(VMError, "does not match the geometry"):
            import_vm(VMManager(self.restore_home), bad)

        restored_manager = VMManager(self.restore_home)
        import_vm(restored_manager, good)
        with self.assertRaisesRegex(VMError, "already exists"):
            import_vm(restored_manager, good)

    def test_import_rejects_duplicate_zip_members(self):
        cfg, _iso = self._make_vm()
        good = self.root / "good.zip"
        export_vm(self.manager, cfg.name, good)
        with zipfile.ZipFile(good) as source:
            entries = [(info.filename, source.read(info)) for info in source.infolist()]
        bad = self.root / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for filename, data in entries:
                    if filename != "disk2.raw":
                        archive.writestr(filename, data)
                archive.writestr("vm.json", json.dumps({"name": "replacement"}))
        with self.assertRaisesRegex(VMError, "duplicate entry"):
            import_vm(VMManager(self.restore_home), bad)


if __name__ == "__main__":
    unittest.main()
