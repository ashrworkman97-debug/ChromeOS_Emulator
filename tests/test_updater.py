import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from chromeos_vm import updater


def make_archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(data, str):
                encoded = data.encode()
                info.size = len(encoded)
                archive.addfile(info, io.BytesIO(encoded))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = str(data)
                archive.addfile(info)
    return output.getvalue()


class SafeExtractTests(unittest.TestCase):
    def test_extracts_regular_project_files(self):
        payload = make_archive([
            ("project/", None),
            ("project/pyproject.toml", "[project]\nname='x'\n"),
            ("project/src/", None),
            ("project/src/chromeos_vm/", None),
            ("project/src/chromeos_vm/__init__.py", "__version__='1'\n"),
        ])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "stage"
            dest.mkdir()
            result = updater._safe_extract(payload, dest)
            self.assertEqual(result, dest)
            self.assertTrue((dest / "pyproject.toml").is_file())
            self.assertTrue((dest / "src/chromeos_vm/__init__.py").is_file())

    def test_rejects_parent_traversal(self):
        payload = make_archive([("project/../escape", "bad")])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(updater.UpdateError):
                updater._safe_extract(payload, Path(temp))

    def test_rejects_symbolic_links(self):
        payload = make_archive([("project/link", "../outside")])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(updater.UpdateError):
                updater._safe_extract(payload, Path(temp))


class UpdateFlowTests(unittest.TestCase):
    def test_automatic_check_obeys_twelve_hour_cooldown(self):
        sha = "e" * 40
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "install.json"
            marker.write_text(json.dumps({
                "commit": sha,
                "last_check": datetime.now(timezone.utc).isoformat(),
            }))
            with patch.object(updater, "metadata_path", return_value=marker), \
                 patch.object(updater, "latest_commit") as latest, \
                 patch.object(updater, "apply_update") as apply:
                result = updater.check_for_updates(force=False)
            self.assertEqual(result.status, "current")
            self.assertIn("performed recently", result.message)
            latest.assert_not_called()
            apply.assert_not_called()

    def test_installs_commit_then_records_marker(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "install.json"
            with patch.object(updater, "metadata_path", return_value=marker), \
                 patch.object(updater, "latest_commit", return_value=sha), \
                 patch.object(updater, "apply_update") as apply:
                result = updater.check_for_updates(force=True)
            self.assertEqual(result.status, "updated")
            apply.assert_called_once_with(sha)
            saved = json.loads(marker.read_text())
            self.assertEqual(saved["commit"], sha)
            self.assertEqual(saved["repository"], "ashrworkman97-debug/ChromeOS_Emulator")

    def test_does_not_install_when_commit_is_current(self):
        sha = "b" * 40
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "install.json"
            marker.write_text(json.dumps({"commit": sha}))
            with patch.object(updater, "metadata_path", return_value=marker), \
                 patch.object(updater, "latest_commit", return_value=sha), \
                 patch.object(updater, "apply_update") as apply:
                result = updater.check_for_updates(force=True)
            self.assertEqual(result.status, "current")
            apply.assert_not_called()

    def test_app_update_leaves_vm_profiles_and_disks_untouched(self):
        sha = "f" * 40
        payload = make_archive([
            ("project/", None),
            ("project/pyproject.toml", "[project]\nname='chromeos-vm-studio'\n"),
            ("project/src/", None),
            ("project/src/chromeos_vm/", None),
            ("project/src/chromeos_vm/__init__.py", "__version__='0.3.4'\n"),
        ])
        with tempfile.TemporaryDirectory() as temp:
            vm_dir = Path(temp) / "vms" / "dos-test"
            vm_dir.mkdir(parents=True)
            disk = vm_dir / "disk.raw"
            profile = vm_dir / "vm.json"
            disk.write_bytes(b"guest disk data must survive")
            profile.write_text('{"name":"dos-test"}\n', encoding="utf-8")
            disk_before = disk.read_bytes()
            profile_before = profile.read_bytes()

            fake_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(updater, "_request", return_value=payload):
                with patch.object(updater.subprocess, "run", return_value=fake_result) as install:
                    updater.apply_update(sha)

            install.assert_called_once()
            self.assertIn("pip", install.call_args.args[0])
            self.assertEqual(disk.read_bytes(), disk_before)
            self.assertEqual(profile.read_bytes(), profile_before)

    def test_failed_install_does_not_advance_marker(self):
        old = "c" * 40
        new = "d" * 40
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "install.json"
            marker.write_text(json.dumps({"commit": old}))
            with patch.object(updater, "metadata_path", return_value=marker), \
                 patch.object(updater, "latest_commit", return_value=new), \
                 patch.object(updater, "apply_update", side_effect=updater.UpdateError("broken")):
                result = updater.check_for_updates(force=True)
            self.assertEqual(result.status, "failed")
            self.assertEqual(json.loads(marker.read_text())["commit"], old)


if __name__ == "__main__":
    unittest.main()
