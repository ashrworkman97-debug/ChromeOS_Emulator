import unittest
from unittest.mock import Mock, patch

from chromeos_vm.gui import ChromeOSVMStudio
from chromeos_vm.updater import UpdateResult


class BackgroundUpdaterTests(unittest.TestCase):
    def test_automatic_check_starts_worker_and_returns_result_to_tk_thread(self):
        app = ChromeOSVMStudio.__new__(ChromeOSVMStudio)
        app.root = Mock()
        app.status = Mock()
        app._update_check_active = False
        result = UpdateResult("current", "ChromeOS VM Studio is up to date.", "a" * 40)

        with patch("chromeos_vm.gui.check_for_updates", return_value=result) as check:
            with patch("chromeos_vm.gui.threading.Thread") as thread_factory:
                with patch.object(app, "_finish_update_check") as finish:
                    app.check_updates(manual=False)

                    thread_factory.assert_called_once()
                    thread = thread_factory.return_value
                    thread.start.assert_called_once_with()
                    self.assertTrue(thread_factory.call_args.kwargs["daemon"])
                    self.assertEqual(thread_factory.call_args.kwargs["name"], "vm-studio-updater")

                    # Execute the worker deterministically without creating a
                    # real thread, then run the callback Tk would process.
                    worker = thread_factory.call_args.kwargs["target"]
                    worker()
                    check.assert_called_once_with(force=False)
                    app.root.after.assert_called_once()
                    callback = app.root.after.call_args.args[1]
                    callback()
                    finish.assert_called_once_with(result, False)


class FloppyBMediaUITests(unittest.TestCase):
    def setUp(self):
        self.app = ChromeOSVMStudio.__new__(ChromeOSVMStudio)
        self.app.root = Mock()
        self.app.manager = Mock()
        self.app.status = Mock()
        self.app.selected_name = Mock(return_value="dos-test")
        self.app.refresh = Mock()

    def test_attach_floppy_b_uses_native_file_picker_and_manager(self):
        with patch("chromeos_vm.gui.filedialog.askopenfilename", return_value="/media/data.img"):
            self.app.attach_floppy_b()
        self.app.manager.set_floppy_b.assert_called_once_with("dos-test", "/media/data.img")
        self.app.refresh.assert_called_once()
        self.app.status.set.assert_called_once()
        self.assertIn("secondary floppy drive", self.app.status.set.call_args.args[0])

    def test_eject_floppy_b_clears_profile_media(self):
        self.app.eject_floppy_b()
        self.app.manager.set_floppy_b.assert_called_once_with("dos-test", None)
        self.app.refresh.assert_called_once()


class FloppyMediaUITests(unittest.TestCase):
    def setUp(self):
        self.app = ChromeOSVMStudio.__new__(ChromeOSVMStudio)
        self.app.root = Mock()
        self.app.manager = Mock()
        self.app.status = Mock()
        self.app.selected_name = Mock(return_value="dos-test")
        self.app.refresh = Mock()

    def test_attach_floppy_a_uses_native_file_picker_and_manager(self):
        with patch("chromeos_vm.gui.filedialog.askopenfilename", return_value="/media/boot.img"):
            self.app.attach_floppy_a()
        self.app.manager.set_floppy_a.assert_called_once_with("dos-test", "/media/boot.img")
        self.app.refresh.assert_called_once()
        self.app.status.set.assert_called_once()
        self.assertIn("write-protected", self.app.status.set.call_args.args[0])

    def test_eject_floppy_a_clears_profile_media(self):
        self.app.eject_floppy_a()
        self.app.manager.set_floppy_a.assert_called_once_with("dos-test", None)
        self.app.refresh.assert_called_once()



class IDEStorageUITests(unittest.TestCase):
    def setUp(self):
        self.app = ChromeOSVMStudio.__new__(ChromeOSVMStudio)
        self.app.root = Mock()
        self.app.manager = Mock()
        self.app.status = Mock()
        self.app.selected_name = Mock(return_value="dos-test")
        self.app.refresh = Mock()

    def test_attach_secondary_disk_uses_native_size_prompt_and_manager(self):
        with patch("chromeos_vm.gui.simpledialog.askinteger", return_value=12) as ask:
            self.app.attach_secondary_disk()
        ask.assert_called_once()
        self.app.manager.attach_secondary_disk.assert_called_once_with("dos-test", 12)
        self.app.refresh.assert_called_once()
        self.assertIn("IDE1 master", self.app.status.set.call_args.args[0])

    def test_cancel_secondary_disk_prompt_does_not_change_profile(self):
        with patch("chromeos_vm.gui.simpledialog.askinteger", return_value=None):
            self.app.attach_secondary_disk()
        self.app.manager.attach_secondary_disk.assert_not_called()

    def test_detach_secondary_disk_requires_confirmation_and_preserves_file(self):
        with patch("chromeos_vm.gui.messagebox.askyesno", return_value=True):
            self.app.manager.detach_secondary_disk.return_value = __import__("pathlib").Path("/vm/disk2.raw")
            self.app.detach_secondary_disk()
        self.app.manager.detach_secondary_disk.assert_called_once_with("dos-test")
        self.app.refresh.assert_called_once()
        self.assertIn("data was preserved", self.app.status.set.call_args.args[0])

    def test_cancel_detach_does_not_change_profile(self):
        with patch("chromeos_vm.gui.messagebox.askyesno", return_value=False):
            self.app.detach_secondary_disk()
        self.app.manager.detach_secondary_disk.assert_not_called()

class CloneUITests(unittest.TestCase):
    def test_clone_prompts_for_name_and_runs_copy_off_the_tk_thread(self):
        from types import SimpleNamespace

        app = ChromeOSVMStudio.__new__(ChromeOSVMStudio)
        app.root = Mock()
        app.manager = Mock()
        app.status = Mock()
        app.selected_name = Mock(return_value="source-vm")
        app.refresh = Mock()
        app.table = Mock()
        app._set_backup_busy = Mock()
        cloned = SimpleNamespace(name="source-copy")

        with patch("chromeos_vm.gui.simpledialog.askstring", return_value="source-copy") as ask:
            with patch("chromeos_vm.gui.clone_vm", return_value=cloned) as clone:
                with patch("chromeos_vm.gui.threading.Thread") as thread_factory:
                    with patch("chromeos_vm.gui.messagebox.showinfo"):
                        app.clone_selected_vm()
                        ask.assert_called_once()
                        thread_factory.assert_called_once()
                        self.assertTrue(thread_factory.call_args.kwargs["daemon"])
                        self.assertEqual(
                            thread_factory.call_args.kwargs["name"], "vm-studio-clone"
                        )
                        worker = thread_factory.call_args.kwargs["target"]
                        worker()
                        clone.assert_called_once_with(app.manager, "source-vm", "source-copy")
                        app.root.after.assert_called_once()
                        finish = app.root.after.call_args.args[1]
                        finish()

        app._set_backup_busy.assert_any_call(True)
        app._set_backup_busy.assert_any_call(False)
        app.refresh.assert_called_once()
        app.table.selection_set.assert_called_once_with("source-copy")


if __name__ == "__main__":
    unittest.main()
