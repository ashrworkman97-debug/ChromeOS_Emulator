import json
import stat
import tempfile
import threading
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from chromeos_vm.backup import clone_vm, export_vm, import_vm
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
        for restored_disk in (Path(attached.disk_path), Path(attached.secondary_disk_path)):
            disk_stat = restored_disk.stat()
            self.assertEqual(disk_stat.st_size, SMALL_GEOMETRY[3])
            self.assertLess(
                disk_stat.st_blocks * 512, disk_stat.st_size,
                "Restore should not allocate the all-zero tail of a sparse virtual disk.",
            )
            with restored_disk.open("rb") as stream:
                stream.seek(-4096, 2)
                self.assertEqual(stream.read(4096), bytes(4096))
        self.assertEqual(attached.iso_path, str(iso.resolve()))
        self.assertFalse(restored_manager.custom_bochsrc_path(attached.name).exists())
        custom.unlink()  # Custom configs are intentionally excluded and block profile edits.

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

    def test_clone_creates_independent_disks_and_excludes_runtime_files(self):
        cfg, iso = self._make_vm()
        self.manager.custom_bochsrc_path(cfg.name).write_text(
            "# source-only custom config\n", encoding="utf-8"
        )
        (self.manager.vm_path(cfg.name) / "logs/bochs.log").write_text(
            "source-only log\n", encoding="utf-8"
        )
        source_primary = Path(cfg.disk_path)
        source_secondary = Path(cfg.secondary_disk_path)
        primary_before = source_primary.read_bytes()[:18]
        secondary_before = source_secondary.read_bytes()[:20]

        cloned = clone_vm(self.manager, cfg.name, "cloned-vm")
        clone_dir = self.manager.vm_path(cloned.name)
        clone_primary = Path(cloned.disk_path)
        clone_secondary = Path(cloned.secondary_disk_path)

        self.assertEqual(cloned.name, "cloned-vm")
        self.assertNotEqual(clone_primary, source_primary)
        self.assertNotEqual(clone_secondary, source_secondary)
        self.assertEqual(clone_primary.read_bytes()[:18], b"primary guest data")
        self.assertEqual(clone_secondary.read_bytes()[:20], b"secondary guest data")
        self.assertEqual(clone_primary.stat().st_size, SMALL_GEOMETRY[3])
        self.assertLess(clone_primary.stat().st_blocks * 512, clone_primary.stat().st_size)
        self.assertEqual(clone_secondary.stat().st_size, SMALL_GEOMETRY[3])
        self.assertEqual(cloned.iso_path, str(iso.resolve()))
        self.assertFalse(self.manager.custom_bochsrc_path(cloned.name).exists())
        self.assertFalse((clone_dir / "logs/bochs.log").exists())
        self.assertEqual(source_primary.read_bytes()[:18], primary_before)
        self.assertEqual(source_secondary.read_bytes()[:20], secondary_before)
        self.assertEqual(list(self.source_home.glob(".clone-*")), [])

    def test_clone_preserves_a_detached_secondary_disk(self):
        cfg, _iso = self._make_vm()
        detached = self.manager.detach_secondary_disk(cfg.name)
        cloned = clone_vm(self.manager, cfg.name, "detached-copy")
        clone_disk = self.manager.vm_path(cloned.name) / "disk2.raw"

        self.assertIsNone(cloned.secondary_disk_path)
        self.assertIsNone(cloned.secondary_disk_gib)
        self.assertTrue(clone_disk.is_file())
        self.assertEqual(clone_disk.stat().st_size, SMALL_GEOMETRY[3])
        self.assertEqual(clone_disk.read_bytes()[:20], b"secondary guest data")
        self.assertEqual(detached.read_bytes()[:20], b"secondary guest data")
        self.assertEqual(list(self.source_home.glob(".clone-*")), [])

    def test_clone_refuses_running_source_and_existing_destination(self):
        cfg, _iso = self._make_vm()
        self.manager.create("already-here")
        with self.assertRaisesRegex(VMError, "already exists"):
            clone_vm(self.manager, cfg.name, "already-here")

        with patch.object(self.manager, "is_running", return_value=True):
            with self.assertRaisesRegex(VMError, "Shut down the guest"):
                clone_vm(self.manager, cfg.name, "running-copy")
        self.assertFalse(self.manager.vm_path("running-copy").exists())
        self.assertEqual(list(self.source_home.glob(".clone-*")), [])

    def test_export_refuses_running_vm_and_existing_destination(self):
        cfg, _iso = self._make_vm()
        destination = self.root / "backup.zip"
        with patch.object(self.manager, "is_running", return_value=True):
            with self.assertRaisesRegex(VMError, "stop Bochs"):
                export_vm(self.manager, cfg.name, destination)
        export_vm(self.manager, cfg.name, destination)
        with self.assertRaisesRegex(VMError, "already exists"):
            export_vm(self.manager, cfg.name, destination)


    def test_export_discards_archive_if_vm_starts_during_copy(self):
        cfg, _iso = self._make_vm()
        destination = self.root / "raced-backup.zip"
        with patch.object(self.manager, "is_running", side_effect=[False, True]):
            with self.assertRaisesRegex(VMError, "started while the backup"):
                export_vm(self.manager, cfg.name, destination)
        self.assertFalse(destination.exists())

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

    def test_import_does_not_replace_empty_directory_created_during_restore(self):
        _cfg, _iso = self._make_vm()
        good = self.root / "good.zip"
        export_vm(self.manager, "source-vm", good)
        restored_manager = VMManager(self.restore_home)
        target_dir = restored_manager.vm_path("source-vm")

        # Simulate another process creating the destination after import's
        # initial existence check but immediately before its final claim.
        original_mkdir = Path.mkdir
        injected = {"done": False}

        def create_racing_directory(path, *args, **kwargs):
            if path == target_dir and not injected["done"]:
                injected["done"] = True
                original_mkdir(path, mode=0o700)
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", autospec=True, side_effect=create_racing_directory):
            with self.assertRaisesRegex(VMError, "appeared during restore"):
                import_vm(restored_manager, good)

        self.assertTrue(injected["done"], "The destination-creation race was not exercised.")
        self.assertTrue(target_dir.is_dir())
        self.assertEqual(list(target_dir.iterdir()), [],
                         "The directory created by the competing process must remain untouched.")
        self.assertEqual(list(self.restore_home.glob(".source-vm.restore-*")), [],
                         "Failed restore staging data must be cleaned up.")

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


    def test_export_lock_prevents_another_manager_from_starting_the_vm(self):
        cfg, _iso = self._make_vm()
        other = VMManager(self.source_home)
        destination = self.root / "paused-export.zip"
        export_entered = threading.Event()
        finish_export = threading.Event()
        export_results = []
        export_errors = []

        def pause_export(_manager, _name, _destination):
            export_entered.set()
            if not finish_export.wait(3):
                raise RuntimeError("test export was not released")
            return destination

        def run_export():
            try:
                export_results.append(export_vm(self.manager, cfg.name, destination))
            except Exception as exc:
                export_errors.append(exc)

        with patch("chromeos_vm.backup._export_vm_locked", side_effect=pause_export):
            worker = threading.Thread(target=run_export, daemon=True)
            worker.start()
            try:
                self.assertTrue(export_entered.wait(2), "export did not enter its locked section")
                with patch.object(other, "_start_locked") as launch:
                    with self.assertRaisesRegex(VMError, "Another app-managed operation"):
                        other.start(cfg.name)
                    launch.assert_not_called()
            finally:
                finish_export.set()
                worker.join(4)

        self.assertFalse(worker.is_alive(), "export worker did not finish")
        self.assertEqual(export_results, [destination])
        self.assertEqual(export_errors, [])


if __name__ == "__main__":
    unittest.main()
