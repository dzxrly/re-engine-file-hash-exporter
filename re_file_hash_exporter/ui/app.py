from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    EXTENDED,
    LEFT,
    RIGHT,
    TOP,
    BooleanVar,
    Button,
    Checkbutton,
    Entry,
    Frame,
    Label,
    LabelFrame,
    Listbox,
    StringVar,
    Text,
    Tk,
    filedialog,
    messagebox,
)
from tkinter import ttk

from ..core.models import BruteForceOptions, DmpScanResult
from ..core.version_profiles import any_extension_uses_date_profile, default_date_range
from ..core.workflow import ExportWorkflow


class ExporterApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("RE File Hash Exporter")
        self.root.geometry("1040x760")
        self.workflow = ExportWorkflow()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.active_task: str | None = None
        self.cancel_event: threading.Event | None = None

        self.dmp_path = StringVar()
        self.output_path = StringVar(value=str(Path.cwd() / "config.toml"))
        self.processes = StringVar(value="0")
        self.mode = StringVar(value="small_range")
        self.min_version = StringVar(value="0")
        self.max_version = StringVar(value="4096")
        self.custom_versions = StringVar(value="")
        self.neighbor_radius = StringVar(value="32")
        default_date_start, default_date_end = self._default_date_range()
        self.date_start = StringVar(value=default_date_start)
        self.date_end = StringVar(value=default_date_end)
        self.include_platform = BooleanVar(value=True)
        self.include_languages = BooleanVar(value=True)
        self.include_streaming = BooleanVar(value=True)
        self.request_gpu = BooleanVar(value=False)
        self.gpu_batch_size = StringVar(value="16384")
        self.show_versioned_extensions = BooleanVar(value=False)
        self.pak_paths: list[Path] = []
        self.last_scan: DmpScanResult | None = None

        self._build()
        self._toggle_gpu_options()
        self._toggle_date_options()
        self._set_step2_enabled(False)
        self.root.after(100, self._poll_events)

    def _build(self) -> None:
        outer = Frame(self.root, padx=10, pady=10)
        outer.pack(fill=BOTH, expand=True)
        self._build_inputs(outer)
        self._build_step1(outer)
        self._build_step2(outer)
        self._build_log(outer)

    def _build_inputs(self, parent: Frame) -> None:
        box = LabelFrame(parent, text="Inputs", padx=8, pady=8)
        box.pack(fill="x", side=TOP)

        row = Frame(box)
        row.pack(fill="x", pady=3)
        Label(row, text="DMP file", width=14, anchor="w").pack(side=LEFT)
        self.dmp_entry = Entry(row, textvariable=self.dmp_path)
        self.dmp_entry.pack(side=LEFT, fill="x", expand=True, padx=4)
        self.dmp_browse_button = Button(row, text="Browse", command=self._browse_dmp_file)
        self.dmp_browse_button.pack(side=RIGHT)

        row = Frame(box)
        row.pack(fill="x", pady=3)
        Label(row, text="Output config", width=14, anchor="w").pack(side=LEFT)
        self.output_entry = Entry(row, textvariable=self.output_path)
        self.output_entry.pack(side=LEFT, fill="x", expand=True, padx=4)
        self.output_browse_button = Button(row, text="Save as", command=self._browse_output)
        self.output_browse_button.pack(side=RIGHT)

        pak_row = Frame(box)
        pak_row.pack(fill="x", pady=3)
        Label(pak_row, text="PAK files", width=14, anchor="nw").pack(side=LEFT)
        self.pak_list = Listbox(pak_row, height=5, selectmode=EXTENDED)
        self.pak_list.pack(side=LEFT, fill="x", expand=True, padx=4)
        buttons = Frame(pak_row)
        buttons.pack(side=RIGHT, fill="y")
        self.add_paks_button = Button(buttons, text="Add PAKs", command=self._add_paks)
        self.add_paks_button.pack(fill="x", pady=1)
        self.add_folder_button = Button(buttons, text="Add Folder", command=self._add_pak_folder)
        self.add_folder_button.pack(fill="x", pady=1)
        self.remove_paks_button = Button(buttons, text="Remove", command=self._remove_selected_paks)
        self.remove_paks_button.pack(fill="x", pady=1)
        self.clear_paks_button = Button(buttons, text="Clear", command=self._clear_paks)
        self.clear_paks_button.pack(fill="x", pady=1)

    def _build_step1(self, parent: Frame) -> None:
        box = LabelFrame(parent, text="Step 1: simple export from DMP suffixes", padx=8, pady=8)
        box.pack(fill="x", side=TOP, pady=8)
        self.step1_button = Button(box, text="Scan DMP and Export Config", command=self._run_step1)
        self.step1_button.pack(side=LEFT)
        self.step1_summary = Label(box, text="No scan yet.", anchor="w")
        self.step1_summary.pack(side=LEFT, padx=12, fill="x", expand=True)

    def _build_step2(self, parent: Frame) -> None:
        box = LabelFrame(parent, text="Step 2: optional brute-force suffix matching", padx=8, pady=8)
        box.pack(fill=BOTH, expand=True, side=TOP)

        left = Frame(box)
        left.pack(side=LEFT, fill=BOTH, expand=True)
        list_header = Frame(left)
        list_header.pack(fill="x")
        Label(list_header, text="Selectable extensions").pack(side=LEFT, anchor="w")
        self.show_versioned_check = Checkbutton(
            list_header,
            text="Show versioned extensions",
            variable=self.show_versioned_extensions,
            command=self._refresh_extension_list,
        )
        self.show_versioned_check.pack(side=RIGHT, anchor="e")
        self.missing_exts = Listbox(left, height=10, selectmode=EXTENDED)
        self.missing_exts.pack(fill=BOTH, expand=True)

        right = Frame(box)
        right.pack(side=RIGHT, fill="y", padx=(10, 0))
        Label(right, text="Candidate mode").pack(anchor="w")
        self.mode_combo = ttk.Combobox(
            right,
            textvariable=self.mode,
            values=["small_range", "adaptive", "custom", "auto_detect"],
            state="readonly",
            width=18,
        )
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)
        self.mode_combo.pack(anchor="w", pady=(0, 6))

        self.min_version_entry = self._labeled_entry(right, "Min version", self.min_version)
        self.max_version_entry = self._labeled_entry(right, "Max version", self.max_version)
        self.neighbor_radius_entry = self._labeled_entry(right, "Neighbor radius", self.neighbor_radius)
        self.custom_versions_entry = self._labeled_entry(right, "Custom versions", self.custom_versions)
        self.date_options = Frame(right)
        self.date_start_entry = self._labeled_entry(self.date_options, "Date -days", self.date_start)
        self.date_end_entry = self._labeled_entry(self.date_options, "Date +days", self.date_end)
        self.processes_entry = self._labeled_entry(right, "Processes", self.processes)

        self.platform_check = Checkbutton(right, text="Platform suffixes", variable=self.include_platform)
        self.platform_check.pack(anchor="w")
        self.languages_check = Checkbutton(right, text="Languages", variable=self.include_languages)
        self.languages_check.pack(anchor="w")
        self.streaming_check = Checkbutton(right, text="Streaming variants", variable=self.include_streaming)
        self.streaming_check.pack(anchor="w")
        self.gpu_check = Checkbutton(
            right,
            text="GPU acceleration (CUDA only)",
            variable=self.request_gpu,
            command=self._toggle_gpu_options,
        )
        self.gpu_check.pack(anchor="w")
        self.gpu_options = Frame(right)
        self.gpu_batch_entry = self._labeled_entry(self.gpu_options, "GPU batch size", self.gpu_batch_size)
        self.step2_button = Button(right, text="Run Brute Force", command=self._run_step2)
        self.step2_button.pack(fill="x", pady=(10, 0))
        self.stop_button = Button(right, text="Stop", command=self._stop_search, state="disabled")
        self.stop_button.pack(fill="x", pady=(4, 0))

    def _build_log(self, parent: Frame) -> None:
        box = LabelFrame(parent, text="Log", padx=8, pady=8)
        box.pack(fill=BOTH, expand=True, side=TOP, pady=(8, 0))
        self.log = Text(box, height=10, wrap="word")
        self.log.pack(fill=BOTH, expand=True)

    def _labeled_entry(self, parent: Frame, label: str, variable: StringVar) -> Entry:
        row = Frame(parent)
        row.pack(fill="x", pady=2)
        Label(row, text=label, width=14, anchor="w").pack(side=LEFT)
        entry = Entry(row, textvariable=variable, width=18)
        entry.pack(side=RIGHT)
        return entry

    def _default_date_range(self) -> tuple[str, str]:
        try:
            return default_date_range()
        except Exception:
            return "", ""

    def _on_mode_changed(self, _event=None) -> None:
        self._toggle_date_options()

    def _toggle_date_options(self) -> None:
        if self.mode.get() == "auto_detect":
            self.date_options.pack(fill="x", pady=(2, 0), before=self.processes_entry.master)
        else:
            self.date_options.pack_forget()

    def _toggle_gpu_options(self) -> None:
        if self.request_gpu.get():
            self.gpu_options.pack(fill="x", pady=(2, 0), before=self.step2_button)
        else:
            self.gpu_options.pack_forget()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.dmp_entry,
            self.dmp_browse_button,
            self.output_entry,
            self.output_browse_button,
            self.pak_list,
            self.add_paks_button,
            self.add_folder_button,
            self.remove_paks_button,
            self.clear_paks_button,
        ):
            widget.config(state=state)

    def _set_step2_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        self.missing_exts.config(state=state)
        self.mode_combo.config(state=combo_state)
        for widget in (
            self.min_version_entry,
            self.max_version_entry,
            self.neighbor_radius_entry,
            self.custom_versions_entry,
            self.date_start_entry,
            self.date_end_entry,
            self.processes_entry,
            self.platform_check,
            self.languages_check,
            self.streaming_check,
            self.gpu_check,
            self.gpu_batch_entry,
            self.show_versioned_check,
            self.step2_button,
        ):
            widget.config(state=state)
        self.stop_button.config(state="disabled")

    def _browse_dmp_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select DMP file",
            filetypes=[("Memory dump", "*.dmp *.DMP"), ("All files", "*.*")],
        )
        if selected:
            self.dmp_path.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Save config.toml",
            defaultextension=".toml",
            filetypes=[("TOML", "*.toml"), ("All files", "*.*")],
        )
        if selected:
            self.output_path.set(selected)

    def _add_paks(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Select PAK files",
            filetypes=[("RE Engine PAK", "*.pak"), ("All files", "*.*")],
        )
        self._append_paks(Path(path) for path in selected)

    def _add_pak_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select folder containing PAK files")
        if not selected:
            return
        self._append_paks(sorted(Path(selected).glob("*.pak")))

    def _append_paks(self, paths) -> None:
        known = {path.resolve() for path in self.pak_paths}
        for path in paths:
            path = Path(path)
            if path.is_file() and path.resolve() not in known:
                self.pak_paths.append(path)
                self.pak_list.insert(END, str(path))
                known.add(path.resolve())

    def _remove_selected_paks(self) -> None:
        selected = list(self.pak_list.curselection())
        for index in reversed(selected):
            self.pak_list.delete(index)
            del self.pak_paths[index]

    def _clear_paks(self) -> None:
        self.pak_paths.clear()
        self.pak_list.delete(0, END)

    def _run_in_worker(self, target, task_name: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A task is already running.")
            return

        self.active_task = task_name
        self.cancel_event = threading.Event()
        self._set_task_running(task_name, True)

        def wrapper() -> None:
            try:
                target()
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("task_done", task_name))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _set_task_running(self, task_name: str, running: bool) -> None:
        if task_name == "step1":
            self._set_inputs_enabled(not running)
            self.step1_button.config(
                state="disabled" if running else "normal",
                text="Scanning DMP..." if running else "Scan DMP and Export Config",
            )
            if running:
                self.last_scan = None
                self._set_step2_enabled(False)
                self.step1_summary.config(text="Scanning DMP and writing config...")
                self._refresh_extension_list()
            else:
                self._set_step2_enabled(self.last_scan is not None)
        elif task_name == "step2":
            if running:
                self._set_inputs_enabled(False)
                self.step1_button.config(state="disabled")
                self._set_step2_enabled(False)
                self.step2_button.config(text="Matching...")
                self.stop_button.config(state="normal", text="Stop")
                self._log("Starting brute-force matching...")
            else:
                self._set_inputs_enabled(True)
                self.step1_button.config(state="normal")
                self._set_step2_enabled(self.last_scan is not None)
                self.step2_button.config(text="Run Brute Force")
                self.stop_button.config(state="disabled", text="Stop")

    def _stop_search(self) -> None:
        if self.active_task != "step2" or self.cancel_event is None:
            return
        self.cancel_event.set()
        self.stop_button.config(state="disabled", text="Stopping...")
        self._log("Stop requested. Waiting for running workers to exit...")

    def _run_step1(self) -> None:
        dmp_path_text = self.dmp_path.get().strip()
        output_text = self.output_path.get().strip()
        if not dmp_path_text:
            messagebox.showerror("Missing input", "Select a DMP file first.")
            return
        if not output_text:
            messagebox.showerror("Missing output", "Select an output config path first.")
            return

        dmp_path = Path(dmp_path_text)
        output = Path(output_text)

        def task() -> None:
            scan = self.workflow.run_simple_export(
                dmp_path,
                output,
                progress=self._thread_log,
            )
            self.events.put(("scan", scan))

        self._run_in_worker(task, "step1")

    def _selected_extensions(self) -> list[str]:
        selected = []
        for index in self.missing_exts.curselection():
            text = self.missing_exts.get(index)
            selected.append(text.split()[0].lstrip("."))
        return selected

    def _format_extension_item(self, extension: str, scan: DmpScanResult) -> str:
        details: list[str] = []
        missing_paths = scan.unversioned_paths.get(extension)
        versioned_paths = scan.versioned_paths.get(extension)
        known_versions = scan.suffix_counts.get(extension)
        if missing_paths:
            details.append(f"missing {len(missing_paths)}")
        if self.show_versioned_extensions.get() and versioned_paths:
            version_count = len(known_versions) if known_versions else 0
            details.append(f"versioned {len(versioned_paths)}, versions {version_count}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f".{extension}{suffix}"

    def _refresh_extension_list(self) -> None:
        previous_state = str(self.missing_exts.cget("state"))
        self.missing_exts.config(state="normal")
        self.missing_exts.delete(0, END)

        try:
            if self.last_scan is None:
                return

            extensions = set(self.last_scan.unversioned_paths)
            if self.show_versioned_extensions.get():
                extensions.update(self.last_scan.versioned_paths)

            for extension in sorted(extensions):
                self.missing_exts.insert(END, self._format_extension_item(extension, self.last_scan))
        finally:
            self.missing_exts.config(state=previous_state)

    def _run_step2(self) -> None:
        if self.last_scan is None:
            messagebox.showerror("Missing scan", "Run Step 1 first.")
            return
        if not self.pak_paths:
            messagebox.showerror("Missing PAKs", "Add one or more PAK files first.")
            return
        selected = self._selected_extensions()
        if not selected:
            messagebox.showerror("Missing extensions", "Select one or more extensions.")
            return
        if not self._validate_auto_detect_date_options(selected):
            return

        output = Path(self.output_path.get().strip())
        gpu_batch_size = 16384
        if self.request_gpu.get():
            try:
                gpu_batch_size = int(self.gpu_batch_size.get() or 16384)
            except ValueError:
                messagebox.showerror("Invalid GPU batch size", "GPU batch size must be a positive integer.")
                return
            if gpu_batch_size <= 0:
                messagebox.showerror("Invalid GPU batch size", "GPU batch size must be a positive integer.")
                return

        options = BruteForceOptions(
            selected_extensions=selected,
            min_version=int(self.min_version.get() or 0),
            max_version=int(self.max_version.get() or 4096),
            mode=self.mode.get(),
            custom_versions=self.custom_versions.get(),
            neighbor_radius=int(self.neighbor_radius.get() or 32),
            date_start=self.date_start.get().strip(),
            date_end=self.date_end.get().strip(),
            processes=int(self.processes.get() or 0),
            include_platform_suffixes=self.include_platform.get(),
            include_languages=self.include_languages.get(),
            include_streaming=self.include_streaming.get(),
            request_gpu=self.request_gpu.get(),
            gpu_batch_size=gpu_batch_size,
            include_versioned_extensions=self.show_versioned_extensions.get(),
        )

        def task() -> None:
            result = self.workflow.run_bruteforce(
                self.pak_paths,
                output,
                options,
                progress=self._thread_log,
                cancel_requested=lambda: bool(self.cancel_event and self.cancel_event.is_set()),
            )
            self.events.put(("brute", result))

        self._run_in_worker(task, "step2")

    def _validate_auto_detect_date_options(self, selected: list[str]) -> bool:
        if self.mode.get() != "auto_detect":
            return True
        try:
            needs_dates = any_extension_uses_date_profile(selected)
        except Exception as err:
            messagebox.showerror("Invalid profile config", str(err))
            return False
        if not needs_dates:
            return True

        for label, value in (("Date -days", self.date_start.get()), ("Date +days", self.date_end.get())):
            try:
                days = int(value.strip() or 0)
            except ValueError:
                messagebox.showerror("Invalid date range", f"{label} must be a non-negative integer.")
                return False
            if days < 0:
                messagebox.showerror("Invalid date range", f"{label} must be a non-negative integer.")
                return False
        return True

    def _thread_log(self, message: str) -> None:
        self.events.put(("log", message))

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._log(str(payload))
            elif kind == "scan":
                self._on_scan(payload)  # type: ignore[arg-type]
            elif kind == "brute":
                if payload.cancelled:  # type: ignore[union-attr]
                    self._log(f"Brute force stopped: {len(payload.matches)} partial matched paths.")
                else:
                    self._log(f"Brute force finished: {len(payload.matches)} matched paths.")  # type: ignore[union-attr]
            elif kind == "error":
                self._log(str(payload))
                if self.active_task == "step1":
                    self.step1_summary.config(text="Scan failed. Check the log for details.")
                messagebox.showerror("Task failed", str(payload))
            elif kind == "task_done":
                self._set_task_running(str(payload), False)
                if self.active_task == payload:
                    self.active_task = None
                    self.cancel_event = None
                self._log("Task finished.")
        self.root.after(100, self._poll_events)

    def _on_scan(self, scan: DmpScanResult) -> None:
        self.last_scan = scan
        self._refresh_extension_list()
        self.step1_summary.config(
            text=(
                f"{len(scan.dmp_files)} DMP, "
                f"{scan.detected_extension_count} versioned extensions, "
                f"{scan.unversioned_extension_count} missing extensions."
            )
        )
        self._log(
            f"Step 1 found {scan.detected_extension_count} extensions with suffixes and "
            f"{scan.unversioned_unique_path_count} raw paths without suffixes."
        )
        self._log(f"Step 1 also tracked {scan.versioned_unique_path_count} versioned raw paths for optional searches.")
        self._set_step2_enabled(True)

    def _log(self, message: str) -> None:
        self.log.insert(END, message.rstrip() + "\n")
        self.log.see(END)


def run_app() -> None:
    root = Tk()
    ExporterApp(root)
    root.mainloop()
