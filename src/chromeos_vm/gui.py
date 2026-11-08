"""Native Tk desktop manager. Each guest runs in its own Bochs GUI window."""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from .manager import VMError, VMManager
from .updater import UpdateResult, check_for_updates


class ChromeOSVMStudio:
    def __init__(self, root: tk.Tk, manager: VMManager | None = None) -> None:
        self.root = root
        self.manager = manager or VMManager()
        root.title("ChromeOS VM Studio")
        root.geometry("820x500")
        root.minsize(640, 380)
        self._build()
        self.refresh()
        # Check for updates off the UI thread; the updater throttles automatic
        # checks so launching the app does not contact GitHub every time.
        self.root.after(1800, lambda: self.check_updates(manual=False))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="ChromeOS VM Studio", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(header, text="Native PC emulation for ChromeOS Linux · Powered by Bochs · No QEMU").pack(anchor="w", pady=(3, 0))

        columns = ("state", "ram", "disk", "media")
        self.table = ttk.Treeview(outer, columns=columns, show="tree headings", selectmode="browse")
        self.table.heading("#0", text="Virtual machine")
        self.table.heading("state", text="State")
        self.table.heading("ram", text="RAM")
        self.table.heading("disk", text="Disk")
        self.table.heading("media", text="Install media")
        self.table.column("#0", width=200)
        self.table.column("state", width=90, anchor="center")
        self.table.column("ram", width=90, anchor="e")
        self.table.column("disk", width=90, anchor="e")
        self.table.column("media", width=260)
        self.table.pack(fill="both", expand=True)

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(12, 6))
        actions = [
            ("New VM", self.new_vm),
            ("Boot", self.start_vm),
            ("Stop", self.stop_vm),
            ("Attach ISO", self.attach_iso),
            ("Open folder", self.open_folder),
            ("Logs", self.show_logs),
            ("Delete", self.delete_vm),
            ("Refresh", self.refresh),
            ("Updates", self.check_updates),
        ]
        for label, callback in actions:
            ttk.Button(bar, text=label, command=callback).pack(side="left", padx=(0, 5))

        self.status = tk.StringVar(value="Ready. Create a VM, attach installation media, then boot it.")
        ttk.Label(outer, textvariable=self.status, wraplength=780).pack(anchor="w", pady=(6, 0))
        note = "Guests run in a separate Bochs window. Software emulation is CPU-intensive; older x86 systems are the best fit."
        ttk.Label(outer, text=note, foreground="#666").pack(anchor="w", pady=(5, 0))
        self.root.after(1200, self._poll)

    def check_updates(self, manual: bool = True) -> None:
        self.status.set("Checking GitHub for ChromeOS VM Studio updates…")

        def worker() -> None:
            result = check_for_updates(force=manual)
            try:
                self.root.after(0, lambda: self._show_update_result(result, manual))
            except tk.TclError:
                pass  # The user closed the window while the check was running.

        threading.Thread(target=worker, name="vm-studio-updater", daemon=True).start()

    def _show_update_result(self, result: UpdateResult, manual: bool) -> None:
        if result.status == "updated":
            self.status.set(result.message)
            if messagebox.askyesno(
                "Update installed",
                result.message + "\n\nRestart the manager now? Any running guest remains a separate Bochs process; close guests safely first.",
                parent=self.root,
            ):
                self.root.destroy()
                # Relaunch the installed entry point using the same interpreter.
                import sys
                subprocess.Popen([sys.executable, "-m", "chromeos_vm.gui"],
                                 start_new_session=True, close_fds=True)
        elif result.status == "current":
            self.status.set("ChromeOS VM Studio is up to date.")
            if manual:
                messagebox.showinfo("No updates available", result.message, parent=self.root)
        else:
            self.status.set("Automatic update check failed; the current app is still available.")
            if manual:
                messagebox.showwarning("Update check failed", result.message, parent=self.root)

    def selected_name(self) -> str | None:
        selection = self.table.selection()
        return selection[0] if selection else None

    def refresh(self) -> None:
        selected = self.selected_name()
        self.table.delete(*self.table.get_children())
        try:
            for cfg in self.manager.list_vms():
                state = "Running" if self.manager.is_running(cfg.name) else "Stopped"
                media = Path(cfg.iso_path).name if cfg.iso_path else "None"
                self.table.insert("", "end", iid=cfg.name, text=cfg.name,
                                  values=(state, f"{cfg.memory_mib} MiB", f"{cfg.disk_gib} GiB", media))
            if selected and self.table.exists(selected):
                self.table.selection_set(selected)
        except VMError as exc:
            self.status.set(str(exc))

    def _poll(self) -> None:
        if not self.root.winfo_exists():
            return
        self.refresh()
        self.root.after(1200, self._poll)

    def new_vm(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Create virtual machine")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        name = tk.StringVar()
        ram = tk.StringVar(value="512")
        disk = tk.StringVar(value="8")
        iso = tk.StringVar()
        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=name, width=34).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="RAM (MiB)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=ram, width=34).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Disk (GiB)").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=disk, width=34).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Install ISO").grid(row=3, column=0, sticky="w", pady=5)
        iso_row = ttk.Frame(frame)
        iso_row.grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Entry(iso_row, textvariable=iso, width=25).pack(side="left", fill="x", expand=True)
        ttk.Button(iso_row, text="Browse…", command=lambda: iso.set(filedialog.askopenfilename(
            parent=dialog, filetypes=[("Disk images", "*.iso *.img *.ima"), ("All files", "*")]))).pack(side="left", padx=(5, 0))
        ttk.Label(frame, text="Defaults are conservative for Chromebook CPUs.", foreground="#666").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(5, 10))

        def create() -> None:
            try:
                cfg = self.manager.create(name.get().strip(), int(ram.get()), int(disk.get()),
                                          iso=iso.get().strip() or None)
                dialog.destroy()
                self.refresh()
                self.table.selection_set(cfg.name)
                self.status.set(f"Created {cfg.name}. Press Boot to start the guest.")
            except (ValueError, VMError, OSError) as exc:
                messagebox.showerror("Could not create VM", str(exc), parent=dialog)

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(5, 0))
        ttk.Button(buttons, text="Create", command=create).pack(side="right")
        frame.columnconfigure(1, weight=1)

    def start_vm(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            pid = self.manager.start(name)
            self._status(f"Started {name} in a Bochs window (PID {pid}).")
        except VMError as exc:
            messagebox.showerror("Could not boot VM", str(exc), parent=self.root)

    def stop_vm(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        if not messagebox.askyesno("Stop emulator",
                "Shut down the guest operating system first when possible. Stopping an active guest may lose unsaved work. Continue?",
                parent=self.root):
            return
        try:
            self.manager.stop(name)
            self._status(f"Stop signal sent to {name}.")
        except VMError as exc:
            messagebox.showerror("Could not stop VM", str(exc), parent=self.root)

    def attach_iso(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        path = filedialog.askopenfilename(parent=self.root,
            filetypes=[("Disk images", "*.iso *.img *.ima"), ("All files", "*")])
        if not path:
            return
        try:
            self.manager.set_iso(name, path)
            self._status(f"Attached {Path(path).name} to {name}. Restart the VM to boot from the CD-ROM.")
            self.refresh()
        except VMError as exc:
            messagebox.showerror("Could not attach image", str(exc), parent=self.root)

    def open_folder(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            subprocess.Popen(["xdg-open", str(self.manager.vm_path(name))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self.root)

    def show_logs(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        log = self.manager.vm_path(name) / "logs/bochs.log"
        launcher = self.manager.vm_path(name) / "logs/launcher.log"
        content = ""
        for path in (log, launcher):
            if path.is_file():
                content += f"\n--- {path.name} ---\n" + path.read_text(encoding="utf-8", errors="replace")[-12000:]
        if not content:
            content = "No logs yet. Boot the VM to create a log."
        window = tk.Toplevel(self.root)
        window.title(f"{name} — Logs")
        window.geometry("760x480")
        text = tk.Text(window, wrap="none")
        text.pack(fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def delete_vm(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        if not messagebox.askyesno("Delete virtual machine",
                f"Permanently delete {name}, including its virtual hard disk? This cannot be undone.",
                icon="warning", parent=self.root):
            return
        try:
            self.manager.delete(name)
            self.refresh()
            self._status(f"Deleted {name}.")
        except VMError as exc:
            messagebox.showerror("Could not delete VM", str(exc), parent=self.root)

    def _status(self, message: str) -> None:
        self.status.set(message)


def main() -> int:
    root = tk.Tk()
    try:
        manager = VMManager()
        ChromeOSVMStudio(root, manager)
        if not manager.find_bochs():
            messagebox.showwarning("Bochs is not installed",
                "Install the emulator in ChromeOS Linux with:\n\nsudo apt update\nsudo apt install bochs bochs-sdl python3-tk",
                parent=root)
        root.mainloop()
        return 0
    except Exception as exc:
        messagebox.showerror("ChromeOS VM Studio", str(exc), parent=root)
        root.destroy()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
