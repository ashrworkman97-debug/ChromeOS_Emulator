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


if __name__ == "__main__":
    unittest.main()
