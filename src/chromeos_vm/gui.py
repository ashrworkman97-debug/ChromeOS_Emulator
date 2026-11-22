"""Native Tk desktop manager. Each guest runs in its own Bochs GUI window."""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path

from .manager import (
    BOOT_DEVICE_LABELS, CHIPSET_LABELS, CPU_MODEL_LABELS, IDE_CDROM_SLOT_LABELS,
    VMError, VMManager, default_boot_order,
)
from .updater import UpdateResult, check_for_updates


class ChromeOSVMStudio:
    def __init__(self, root: tk.Tk, manager: VMManager | None = None) -> None:
        self.root = root
        self.manager = manager or VMManager()
        root.title("ChromeOS VM Studio")
        # The media controls add two device actions; leave enough room for
        # the full action panel on a typical Chromebook Linux-app window.
        root.geometry("1020x760")
        root.minsize(820, 680)
        self._build()
        self.refresh()
        # Check for updates off the UI thread; the updater throttles automatic
        # checks so launching the app does not contact GitHub every time.
        self.root.after(1800, lambda: self.check_updates(manual=False))

    def _configure_style(self) -> None:
        """Use a consistent, modern desktop palette with ttk's native widgets."""
        self.root.configure(background="#eef2f7")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background="#eef2f7")
        style.configure("Hero.TFrame", background="#17243a")
        style.configure("HeroTitle.TLabel", background="#17243a", foreground="#ffffff",
                        font=("TkDefaultFont", 21, "bold"))
        style.configure("HeroSubtitle.TLabel", background="#17243a", foreground="#cbd5e1",
                        font=("TkDefaultFont", 10))
        style.configure("HeroBadge.TLabel", background="#263a59", foreground="#a5f3fc",
                        padding=(10, 5), font=("TkDefaultFont", 9, "bold"))
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#17243a",
                        font=("TkDefaultFont", 11, "bold"))
        style.configure("PanelHint.TLabel", background="#ffffff", foreground="#64748b",
                        font=("TkDefaultFont", 9))
        style.configure("App.TLabel", background="#eef2f7", foreground="#334155",
                        font=("TkDefaultFont", 9))
        style.configure("Status.TLabel", background="#e0f2fe", foreground="#075985",
                        padding=(12, 9), font=("TkDefaultFont", 9, "bold"))
        style.configure("Primary.TButton", background="#2563eb", foreground="#ffffff",
                        padding=(12, 9), font=("TkDefaultFont", 9, "bold"), borderwidth=0)
        style.map("Primary.TButton",
                  background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")],
                  foreground=[("disabled", "#e2e8f0")])
        style.configure("Accent.TButton", background="#0f766e", foreground="#ffffff",
                        padding=(12, 8), font=("TkDefaultFont", 9, "bold"), borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#115e59")])
        style.configure("Action.TButton", background="#f1f5f9", foreground="#1e293b",
                        padding=(10, 8), borderwidth=0)
        style.map("Action.TButton", background=[("active", "#e2e8f0")])
        style.configure("Danger.TButton", background="#fff1f2", foreground="#be123c",
                        padding=(10, 8), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#ffe4e6")])
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground="#1e293b", rowheight=34, borderwidth=0,
                        font=("TkDefaultFont", 10))
        style.configure("Treeview.Heading", background="#f1f5f9", foreground="#475569",
                        padding=(9, 9), font=("TkDefaultFont", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", "#1d4ed8")])
        style.configure("Vertical.TScrollbar", background="#e2e8f0",
                        troughcolor="#f8fafc", borderwidth=0, arrowsize=12)

    def _build(self) -> None:
        self._configure_style()
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(20, 18, 20, 16))
        outer.pack(fill="both", expand=True)

        # Dark title banner gives the app a clear identity without relying on
        # external icon/font packages that may not be installed in Crostini.
        hero = ttk.Frame(outer, style="Hero.TFrame", padding=(20, 17))
        hero.pack(fill="x", pady=(0, 16))
        hero_left = ttk.Frame(hero, style="Hero.TFrame")
        hero_left.pack(side="left", fill="x", expand=True)
        ttk.Label(hero_left, text="▣  CHROMEOS VM STUDIO",
                  style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(hero_left,
                  text="Your virtual PCs, all in one place  ·  Native Crostini app",
                  style="HeroSubtitle.TLabel").pack(anchor="w", pady=(5, 0))
        ttk.Label(hero, text="BOCHS  •  X86 EMULATION",
                  style="HeroBadge.TLabel").pack(side="right", anchor="center", padx=(12, 0))

        section = ttk.Frame(outer, style="App.TFrame")
        section.pack(fill="x", pady=(0, 8))
        ttk.Label(section, text="VIRTUAL MACHINES",
                  background="#eef2f7", foreground="#334155",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(section, text="Diagnostics…", style="Action.TButton",
                   command=self.show_diagnostics).pack(side="right", padx=(8, 0))
        ttk.Label(section, text="Select a machine to manage its power and media",
                  style="App.TLabel").pack(side="right")

        content = ttk.Frame(outer, style="App.TFrame")
        content.pack(fill="both", expand=True)

        list_panel = ttk.Frame(content, style="Panel.TFrame", padding=12)
        list_panel.pack(side="left", fill="both", expand=True)
        list_head = ttk.Frame(list_panel, style="Panel.TFrame")
        list_head.pack(fill="x", pady=(0, 10))
        ttk.Label(list_head, text="VM LIBRARY", style="PanelTitle.TLabel").pack(side="left")
        ttk.Label(list_head, text="Double-click a VM to boot",
                  style="PanelHint.TLabel").pack(side="right")

        columns = ("state", "ram", "disk", "media")
        self.table = ttk.Treeview(
            list_panel, columns=columns, show="tree headings",
            selectmode="browse", height=10,
        )
        self.table.heading("#0", text="MACHINE")
        self.table.heading("state", text="STATUS")
        self.table.heading("ram", text="MEMORY")
        self.table.heading("disk", text="DISK")
        self.table.heading("media", text="CD/DVD · FLOPPY A/B")
        self.table.column("#0", width=190, minwidth=130)
        self.table.column("state", width=86, minwidth=78, anchor="center")
        self.table.column("ram", width=94, minwidth=80, anchor="center")
        self.table.column("disk", width=88, minwidth=76, anchor="center")
        self.table.column("media", width=250, minwidth=140)
        scrollbar = ttk.Scrollbar(list_panel, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.tag_configure("running", foreground="#0f766e")
        self.table.tag_configure("stopped", foreground="#64748b")
        self.table.bind("<Double-1>", lambda _event: self.start_vm())

        action_panel = ttk.Frame(content, style="Panel.TFrame", padding=13, width=190)
        action_panel.pack(side="right", fill="y", padx=(12, 0))
        action_panel.pack_propagate(False)
        ttk.Label(action_panel, text="ACTIONS", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 10))

        ttk.Button(action_panel, text="＋  New VM", style="Primary.TButton",
                   command=self.new_vm).pack(fill="x", pady=(0, 7))
        ttk.Button(action_panel, text="▶  Boot", style="Accent.TButton",
                   command=self.start_vm).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="■  Stop", style="Action.TButton",
                   command=self.stop_vm).pack(fill="x", pady=3)

        ttk.Separator(action_panel).pack(fill="x", pady=10)
        ttk.Label(action_panel, text="DEVICES & FILES",
                  style="PanelHint.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Button(action_panel, text="Attach ISO…", style="Action.TButton",
                   command=self.attach_iso).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Attach second disk…", style="Action.TButton",
                   command=self.attach_secondary_disk).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Detach second disk", style="Action.TButton",
                   command=self.detach_secondary_disk).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Attach floppy A…", style="Action.TButton",
                   command=self.attach_floppy_a).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Eject floppy A", style="Action.TButton",
                   command=self.eject_floppy_a).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Attach floppy B…", style="Action.TButton",
                   command=self.attach_floppy_b).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Eject floppy B", style="Action.TButton",
                   command=self.eject_floppy_b).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Boot order…", style="Action.TButton",
                   command=self.edit_boot_order).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Edit VM…", style="Action.TButton",
                   command=self.edit_vm).pack(fill="x", pady=3)
        self.config_button = ttk.Button(action_panel, text="Bochs config…", style="Action.TButton",
                                         command=self.edit_bochs_config)
        self.config_button.pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Open folder", style="Action.TButton",
                   command=self.open_folder).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="View logs", style="Action.TButton",
                   command=self.show_logs).pack(fill="x", pady=3)

        ttk.Separator(action_panel).pack(fill="x", pady=10)
        ttk.Button(action_panel, text="Refresh library", style="Action.TButton",
                   command=self.refresh).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Check updates", style="Action.TButton",
                   command=self.check_updates).pack(fill="x", pady=3)
        ttk.Button(action_panel, text="Delete VM", style="Danger.TButton",
                   command=self.delete_vm).pack(fill="x", pady=(8, 3))

        self.status = tk.StringVar(value="Ready  ·  Create a VM or select one from your library.")
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel",
                  wraplength=900).pack(fill="x", pady=(13, 7))
        note = ("Guests open in their own Bochs window. Software emulation uses CPU; "
                "older DOS and Windows guests are the best fit for Chromebook hardware.")
        ttk.Label(outer, text=note, style="App.TLabel",
                  wraplength=920).pack(anchor="w")
        self.root.after(1200, self._poll)

    def check_updates(self, manual: bool = True) -> None:
        if getattr(self, "_update_check_active", False):
            return
        self._update_check_active = True
        self.status.set("Checking GitHub for ChromeOS VM Studio updates…")

        def worker() -> None:
            try:
                result = check_for_updates(force=manual)
            except Exception as exc:
                result = UpdateResult("failed", str(exc))
            try:
                self.root.after(0, lambda: self._finish_update_check(result, manual))
            except tk.TclError:
                pass  # The user closed the window while the check was running.

        # Keep network access and pip installation off Tk's event thread.
        threading.Thread(target=worker, name="vm-studio-updater", daemon=True).start()

    def _finish_update_check(self, result: UpdateResult, manual: bool) -> None:
        self._update_check_active = False
        self._show_update_result(result, manual)

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
                running = self.manager.is_running(cfg.name)
                state = "Running" if running else "Stopped"
                media_items = []
                if cfg.iso_path:
                    media_items.append("CD: " + Path(cfg.iso_path).name)
                if cfg.floppy_a_path:
                    media_items.append("A: " + Path(cfg.floppy_a_path).name)
                if cfg.floppy_b_path:
                    media_items.append("B: " + Path(cfg.floppy_b_path).name)
                media = " · ".join(media_items) if media_items else "None"
                disk_label = f"{cfg.disk_gib} GiB"
                if cfg.secondary_disk_gib is not None:
                    disk_label += f" + {cfg.secondary_disk_gib} GiB"
                self.table.insert("", "end", iid=cfg.name, text=cfg.name,
                                  values=(state, f"{cfg.memory_mib} MiB", disk_label, media),
                                  tags=("running" if running else "stopped",))
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
        secondary_disk = tk.StringVar()
        iso = tk.StringVar()
        cpu_model = tk.StringVar(value=CPU_MODEL_LABELS["default"])
        chipset = tk.StringVar(value=CHIPSET_LABELS["i440fx"])
        cdrom_slot = tk.StringVar(value=IDE_CDROM_SLOT_LABELS["ata0-slave"])
        sound_enabled = tk.BooleanVar(value=False)

        ttk.Label(frame, text="Name").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=name, width=34).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="RAM (MiB)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=ram, width=34).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Primary disk (GiB)").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=disk, width=34).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="CPU model").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=cpu_model, state="readonly",
                     values=tuple(CPU_MODEL_LABELS.values()), width=40).grid(
                         row=3, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="PCI chipset").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=chipset, state="readonly",
                     values=tuple(CHIPSET_LABELS.values()), width=40).grid(
                         row=4, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="CD-ROM IDE position").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=cdrom_slot, state="readonly",
                     values=tuple(IDE_CDROM_SLOT_LABELS.values()), width=40).grid(
                         row=5, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Second disk (GiB, optional)").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=secondary_disk, width=34).grid(
            row=6, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Install ISO").grid(row=7, column=0, sticky="w", pady=5)
        iso_row = ttk.Frame(frame)
        iso_row.grid(row=7, column=1, sticky="ew", pady=5)
        ttk.Entry(iso_row, textvariable=iso, width=25).pack(side="left", fill="x", expand=True)
        ttk.Button(iso_row, text="Browse…", command=lambda: iso.set(filedialog.askopenfilename(
            parent=dialog, filetypes=[("Disk images", "*.iso *.img *.ima"), ("All files", "*")]))).pack(side="left", padx=(5, 0))
        ttk.Checkbutton(
            frame, text="Enable Sound Blaster 16 / PC speaker audio (SDL output)",
            variable=sound_enabled,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(5, 4))
        ttk.Label(
            frame,
            text=("The second hard disk is created as IDE channel 1 master. "
                  "The CD-ROM can use the slave position on IDE channel 0 or 1. "
                  "CPU and chipset selections change guest-visible hardware, not host speed. "
                  "Audio is optional and may depend on Crostini SDL audio support."),
            foreground="#666", wraplength=500,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(5, 10))

        def create() -> None:
            try:
                selected_model = next(
                    key for key, label in CPU_MODEL_LABELS.items() if label == cpu_model.get()
                )
                selected_chipset = next(
                    key for key, label in CHIPSET_LABELS.items() if label == chipset.get()
                )
                selected_cdrom_slot = next(
                    key for key, label in IDE_CDROM_SLOT_LABELS.items() if label == cdrom_slot.get()
                )
                secondary_size = int(secondary_disk.get()) if secondary_disk.get().strip() else None
                cfg = self.manager.create(
                    name.get().strip(), int(ram.get()), int(disk.get()),
                    iso=iso.get().strip() or None, cpu_model=selected_model,
                    chipset=selected_chipset, secondary_disk_gib=secondary_size,
                    cdrom_slot=selected_cdrom_slot, sound_enabled=sound_enabled.get(),
                )
                dialog.destroy()
                self.refresh()
                self.table.selection_set(cfg.name)
                self.status.set(f"Created {cfg.name}. Press Boot to start the guest.")
            except (ValueError, VMError, OSError) as exc:
                messagebox.showerror("Could not create VM", str(exc), parent=dialog)

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e")
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

    def edit_vm(self) -> None:
        """Edit a stopped VM's CPU, chipset, CD-ROM IDE position and media."""
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            cfg = self.manager.load(name)
        except VMError as exc:
            messagebox.showerror("Could not edit VM", str(exc), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Edit {name}")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ram = tk.StringVar(value=str(cfg.memory_mib))
        cpu = tk.StringVar(value=str(cfg.cpu_ips))
        cpu_model = tk.StringVar(value=CPU_MODEL_LABELS[cfg.cpu_model])
        chipset = tk.StringVar(value=CHIPSET_LABELS[cfg.chipset])
        cdrom_slot = tk.StringVar(value=IDE_CDROM_SLOT_LABELS[cfg.cdrom_slot])
        sound_enabled = tk.BooleanVar(value=cfg.sound_enabled)
        iso = tk.StringVar(value=cfg.iso_path or "")

        ttk.Label(frame, text="Memory (MiB)").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=ram, width=42).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="CPU speed (Bochs IPS)").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=cpu, width=42).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="CPU model").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=cpu_model, state="readonly",
                     values=tuple(CPU_MODEL_LABELS.values()), width=40).grid(
                         row=2, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="PCI chipset").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=chipset, state="readonly",
                     values=tuple(CHIPSET_LABELS.values()), width=40).grid(
                         row=3, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="CD-ROM IDE position").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Combobox(frame, textvariable=cdrom_slot, state="readonly",
                     values=tuple(IDE_CDROM_SLOT_LABELS.values()), width=40).grid(
                         row=4, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="CD/DVD image").grid(row=5, column=0, sticky="w", pady=6)
        media_row = ttk.Frame(frame)
        media_row.grid(row=5, column=1, sticky="ew", pady=6)
        ttk.Entry(media_row, textvariable=iso, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(media_row, text="Browse…", command=lambda: iso.set(
            filedialog.askopenfilename(parent=dialog,
                filetypes=[("Disk images", "*.iso *.img *.ima"), ("All files", "*")])
            or iso.get())).pack(side="left", padx=(5, 0))
        ttk.Button(media_row, text="Eject ISO", command=lambda: iso.set("")).pack(side="left", padx=(5, 0))
        secondary_text = (
            f"Secondary IDE disk: {cfg.secondary_disk_gib} GiB (IDE1 master)"
            if cfg.secondary_disk_gib is not None else "Secondary IDE disk: not attached"
        )
        ttk.Checkbutton(
            frame, text="Enable Sound Blaster 16 / PC speaker audio (SDL output)",
            variable=sound_enabled,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 4))
        ttk.Label(
            frame,
            text=(f"Primary disk: {cfg.disk_gib} GiB. {secondary_text}. "
                  "Detaching a secondary disk preserves disk2.raw in the VM folder. "
                  "Audio output depends on the Bochs build and Crostini SDL audio integration."),
            foreground="#666", wraplength=520,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 12))

        def save() -> None:
            try:
                selected_model = next(
                    key for key, label in CPU_MODEL_LABELS.items() if label == cpu_model.get()
                )
                selected_chipset = next(
                    key for key, label in CHIPSET_LABELS.items() if label == chipset.get()
                )
                selected_cdrom_slot = next(
                    key for key, label in IDE_CDROM_SLOT_LABELS.items() if label == cdrom_slot.get()
                )
                self.manager.update_profile(
                    name, int(ram.get()), int(cpu.get()), iso.get().strip() or None,
                    selected_model, selected_chipset, selected_cdrom_slot,
                    sound_enabled.get(),
                )
                dialog.destroy()
                self.refresh()
                self._status(f"Saved {name}. Changes apply the next time the VM boots.")
            except (ValueError, VMError, OSError) as exc:
                messagebox.showerror("Could not save VM", str(exc), parent=dialog)

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(5, 0))
        ttk.Button(buttons, text="Save changes", command=save).pack(side="right")
        frame.columnconfigure(1, weight=1)

    def edit_boot_order(self) -> None:
        """Edit the BIOS boot sequence using only devices attached to this VM."""
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            cfg = self.manager.load(name)
            if self.manager.is_running(name):
                messagebox.showinfo(
                    "Stop the VM first",
                    "BIOS boot priority can only be changed while this VM is stopped.",
                    parent=self.root,
                )
                return
            if self.manager.custom_bochsrc_path(name).exists():
                messagebox.showinfo(
                    "Restore generated configuration",
                    "The custom Bochs configuration currently controls the boot directive. "
                    "Restore the generated configuration before editing BIOS boot priority.",
                    parent=self.root,
                )
                return
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not edit boot order", str(exc), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"{name} — BIOS boot priority")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)

        available = []
        if cfg.floppy_a_path:
            available.append("floppy")
        if cfg.iso_path:
            available.append("cdrom")
        available.extend(("disk", "network"))
        unused = "— not used —"
        choices = (unused,) + tuple(BOOT_DEVICE_LABELS[item] for item in available)
        reverse = {label: key for key, label in BOOT_DEVICE_LABELS.items()}
        automatic = tk.BooleanVar(value=cfg.boot_order is None)
        current = cfg.boot_order or default_boot_order(cfg)
        values = [
            tk.StringVar(value=BOOT_DEVICE_LABELS.get(current[index], unused)
                         if index < len(current) else unused)
            for index in range(3)
        ]

        ttk.Label(
            frame, text="Choose up to three BIOS boot devices, in priority order.",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Checkbutton(
            frame,
            text="Automatic (Floppy A, CD/DVD, then hard disk when attached)",
            variable=automatic,
            command=lambda: set_editor_state(),
        ).pack(anchor="w", pady=(0, 10))

        selectors = ttk.Frame(frame)
        selectors.pack(fill="x")
        combos = []
        for index, variable in enumerate(values, start=1):
            ttk.Label(selectors, text=f"Priority {index}").grid(
                row=index - 1, column=0, sticky="w", padx=(0, 12), pady=4
            )
            combo = ttk.Combobox(
                selectors, textvariable=variable, state="readonly",
                values=choices, width=58,
            )
            combo.grid(row=index - 1, column=1, sticky="ew", pady=4)
            combos.append(combo)
        selectors.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=("Floppy boot requires an image in drive A; CD/DVD boot requires an attached ISO. "
                  "Network boot is passed to Bochs' boot ROM and also requires a compatible NIC/boot ROM. "
                  "This manager does not configure a network adapter yet."),
            foreground="#666", wraplength=560,
        ).pack(anchor="w", pady=(10, 12))

        def set_editor_state() -> None:
            state = "disabled" if automatic.get() else "readonly"
            for combo in combos:
                combo.configure(state=state)

        def save() -> None:
            if automatic.get():
                selected_order = None
            else:
                selected_labels = [variable.get() for variable in values]
                used = [label != unused for label in selected_labels]
                if any(used[index] and not all(used[:index]) for index in range(3)):
                    messagebox.showerror(
                        "Invalid boot order",
                        "Fill priorities from 1 downward without gaps.",
                        parent=dialog,
                    )
                    return
                selected_order = tuple(
                    reverse[label] for label, is_used in zip(selected_labels, used) if is_used
                )
                if not selected_order:
                    messagebox.showerror(
                        "Invalid boot order", "Choose at least one boot device or enable Automatic.",
                        parent=dialog,
                    )
                    return
            try:
                self.manager.set_boot_order(name, selected_order)
            except (VMError, OSError) as exc:
                messagebox.showerror("Could not save boot order", str(exc), parent=dialog)
                return
            dialog.destroy()
            self.refresh()
            if selected_order is None:
                self._status(f"Restored automatic BIOS boot priority for {name}.")
            else:
                self._status(f"Saved BIOS boot order for {name}: " + ", ".join(selected_order) + ".")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save boot order", style="Primary.TButton",
                   command=save).pack(side="right", padx=(0, 6))
        set_editor_state()

    def edit_bochs_config(self) -> None:
        """Edit a stopped VM's custom bochsrc; generated settings remain recoverable."""
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            if self.manager.is_running(name):
                messagebox.showinfo(
                    "Stop the VM first",
                    "The Bochs configuration can only be changed while this VM is stopped.",
                    parent=self.root,
                )
                return
            original = self.manager.read_bochsrc(name)
        except (VMError, OSError) as exc:
            messagebox.showerror(
                "Could not open Bochs configuration",
                str(exc) + "\\n\\nInstall the Debian Bochs BIOS packages before generating a configuration.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"{name} — Bochs configuration")
        dialog.geometry("920x680")
        dialog.minsize(720, 480)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=("Static validation checks common structure and required devices; it is not Bochs' parser. "
                  "Bochs validates the complete configuration when you boot. A saved custom file overrides "
                  "profile-generated settings until you restore the generated config."),
            wraplength=870,
        ).pack(fill="x", pady=(0, 9))

        editor_frame = ttk.Frame(frame)
        editor_frame.pack(fill="both", expand=True)
        editor = tk.Text(editor_frame, wrap="none", undo=True, font=("TkFixedFont", 10))
        vertical = ttk.Scrollbar(editor_frame, orient="vertical", command=editor.yview)
        horizontal = ttk.Scrollbar(editor_frame, orient="horizontal", command=editor.xview)
        editor.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        editor.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        editor.insert("1.0", original)

        state = tk.StringVar(value=(
            "Custom configuration is active." if self.manager.custom_bochsrc_path(name).is_file()
            else "Showing the generated configuration; saving creates a custom override."
        ))
        ttk.Label(frame, textvariable=state, wraplength=870).pack(fill="x", pady=(8, 6))

        def validate() -> bool:
            try:
                self.manager.validate_bochsrc_for_vm(name, editor.get("1.0", "end-1c"))
            except VMError as exc:
                state.set("Validation failed: " + str(exc))
                messagebox.showerror("Bochs configuration validation", str(exc), parent=dialog)
                return False
            state.set("Static checks passed. Bochs still performs final parsing at boot.")
            return True

        def save() -> None:
            try:
                path = self.manager.save_custom_bochsrc(name, editor.get("1.0", "end-1c"))
            except (VMError, OSError) as exc:
                messagebox.showerror("Could not save configuration", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._status(f"Saved custom Bochs configuration for {name}. It will be used on the next boot.")
            self.refresh()

        def restore() -> None:
            if not self.manager.custom_bochsrc_path(name).is_file():
                state.set("No custom override exists; the generated configuration is already active.")
                return
            if not messagebox.askyesno(
                "Restore generated configuration",
                "Discard the custom Bochs configuration and return to settings generated from the VM profile?",
                parent=dialog,
            ):
                return
            try:
                self.manager.reset_custom_bochsrc(name)
                editor.delete("1.0", "end")
                editor.insert("1.0", self.manager.read_bochsrc(name))
            except (VMError, OSError) as exc:
                messagebox.showerror("Could not restore configuration", str(exc), parent=dialog)
                return
            state.set("Generated configuration restored. Custom edits were discarded.")
            self._status(f"Restored generated Bochs configuration for {name}.")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Validate", command=validate).pack(side="left")
        ttk.Button(buttons, text="Restore generated", command=restore).pack(side="left", padx=6)
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save custom config", style="Primary.TButton",
                   command=save).pack(side="right", padx=(0, 6))

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

    def attach_floppy_a(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            filetypes=[("Floppy images", "*.img *.ima *.flp *.bin"), ("All files", "*")],
        )
        if not path:
            return
        try:
            self.manager.set_floppy_a(name, path)
            self._status(
                f"Attached {Path(path).name} as floppy A in {name}. "
                "It is write-protected by default; boot order now tries floppy first."
            )
            self.refresh()
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not attach floppy image", str(exc), parent=self.root)

    def eject_floppy_a(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            self.manager.set_floppy_a(name, None)
            self._status(f"Ejected floppy A from {name}. The next boot will use its CD/DVD or hard disk.")
            self.refresh()
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not eject floppy image", str(exc), parent=self.root)

    def attach_floppy_b(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            filetypes=[("Floppy images", "*.img *.ima *.flp *.bin"), ("All files", "*")],
        )
        if not path:
            return
        try:
            self.manager.set_floppy_b(name, path)
            self._status(
                f"Attached {Path(path).name} as floppy B in {name}. "
                "It is write-protected by default and available as the secondary floppy drive; boot priority remains on A."
            )
            self.refresh()
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not attach floppy image", str(exc), parent=self.root)

    def eject_floppy_b(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            self.manager.set_floppy_b(name, None)
            self._status(f"Ejected floppy B from {name}. The next boot will use its CD/DVD or hard disk.")
            self.refresh()
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not eject floppy image", str(exc), parent=self.root)

    def attach_secondary_disk(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        size = simpledialog.askinteger(
            "Attach secondary IDE disk",
            "Create or reattach the private IDE1-master disk (GiB):",
            initialvalue=8, minvalue=1, maxvalue=512, parent=self.root,
        )
        if size is None:
            return
        try:
            self.manager.attach_secondary_disk(name, size)
            self.refresh()
            self._status(
                f"Attached a {size} GiB secondary disk to {name} as IDE1 master. "
                "Any existing detached disk image was preserved."
            )
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not attach secondary disk", str(exc), parent=self.root)

    def detach_secondary_disk(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        if not messagebox.askyesno(
            "Detach secondary IDE disk",
            "Detach the secondary disk from the guest? Its disk2.raw file and all data "
            "will remain in the VM folder so it can be reattached later.",
            parent=self.root,
        ):
            return
        try:
            disk = self.manager.detach_secondary_disk(name)
            self.refresh()
            self._status(f"Detached {disk.name}; its data was preserved.")
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not detach secondary disk", str(exc), parent=self.root)

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

    def show_diagnostics(self) -> None:
        name = self.selected_name()
        if not name:
            self._status("Select a virtual machine first.")
            return
        try:
            report = self.manager.diagnostic_report(name)
        except (VMError, OSError) as exc:
            messagebox.showerror("Could not inspect VM", str(exc), parent=self.root)
            return

        window = tk.Toplevel(self.root)
        window.title(f"{name} — Diagnostics")
        window.geometry("780x560")
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Non-destructive checks for the emulator, firmware, disks, media, and configuration.",
            wraplength=740,
        ).pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        report_text = tk.Text(body, wrap="word", height=24)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=report_text.yview)
        report_text.configure(yscrollcommand=scrollbar.set)
        report_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        report_text.insert("1.0", report)
        report_text.configure(state="disabled")
        ttk.Button(frame, text="Close", command=window.destroy).pack(
            anchor="e", pady=(8, 0)
        )

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
