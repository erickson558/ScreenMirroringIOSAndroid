from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import shlex
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

from backend.controller import AppController
from backend.device_modes import (
    DEFAULT_DEVICE_MODE_KEY,
    DEVICE_MODE_ANDROID,
    DEVICE_MODE_IPHONE,
    get_device_mode,
    list_device_modes,
)
from backend.gui_config_store import GuiConfigStore
from backend.receiver_profiles import DEFAULT_PROFILE_KEY, get_profile, list_profiles


class MainWindow:
    POLL_INTERVAL_MS = 150
    SAVE_DEBOUNCE_MS = 300

    def __init__(
        self,
        root: tk.Tk,
        controller: AppController,
        app_name: str,
        app_version: str,
        default_receiver_name: str,
        default_uxplay_path: Path,
        default_profile_key: str,
        default_device_mode_key: str,
        gui_config_path: Path,
    ) -> None:
        self._root = root
        self._controller = controller
        self._store = GuiConfigStore(gui_config_path)
        self._app_name = app_name
        self._app_version = app_version

        self._profiles = list_profiles()
        self._profiles_by_label = {p.label: p for p in self._profiles}
        self._device_modes = list_device_modes()
        self._device_by_key = {m.key: m for m in self._device_modes}
        self._capture_sources = {
            "Escritorio (recomendado)": "desktop",
            "Ventana de UxPlay": "window",
        }

        saved = self._store.load()
        initial_profile = self._resolve_profile(saved.get("profile_key", default_profile_key))
        if str(saved.get("profile_label", "")).strip() in self._profiles_by_label:
            initial_profile = self._profiles_by_label[str(saved["profile_label"]).strip()]

        initial_mode = self._resolve_device_mode(saved.get("device_mode", default_device_mode_key))
        initial_source = str(saved.get("capture_source_label", "Escritorio (recomendado)"))
        if initial_source not in self._capture_sources:
            initial_source = "Escritorio (recomendado)"

        self._receiver_name_var = tk.StringVar(value=str(saved.get("receiver_name", default_receiver_name)))
        self._uxplay_path_var = tk.StringVar(value=str(saved.get("uxplay_path", str(default_uxplay_path))))
        self._profile_var = tk.StringVar(value=initial_profile.label)
        self._profile_desc_var = tk.StringVar(value=initial_profile.description)
        self._custom_args_var = tk.StringVar(value=str(saved.get("custom_args", "")))
        self._append_hostname_suffix_var = tk.BooleanVar(
            value=self._safe_bool(saved.get("append_hostname_suffix", False), False)
        )
        self._capture_source_var = tk.StringVar(value=initial_source)
        self._capture_title_var = tk.StringVar(value=str(saved.get("capture_window_title", "UxPlay")))
        self._capture_fps_var = tk.IntVar(value=self._safe_int(saved.get("capture_fps", 30), 30))
        self._device_mode_var = tk.StringVar(value=initial_mode)
        self._device_hint_var = tk.StringVar(value=self._device_by_key[initial_mode].description)

        self._receiver_status_var = tk.StringVar(value="Receptor: detenido")
        self._record_status_var = tk.StringVar(value="Grabación: inactiva")
        self._status_var = tk.StringVar(value=f"Listo. {self._app_name} v{self._app_version}")

        self._save_after_id: str | None = None
        self._suspend_save = False
        self._ntp_error_count = 0
        self._ntp_hint_shown = False
        self._record_suggested_name: str | None = None
        self._busy_count = 0
        self._closing = False
        self._about_dialog: tk.Toplevel | None = None

        self._build_ui()
        self._apply_device_mode_ui(log_change=False)
        self._set_running_state(False)
        self._set_record_state(False)

        self._apply_saved_geometry(str(saved.get("window_geometry", "")))
        self._install_autosave()
        self._bind_hotkeys()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _build_ui(self) -> None:
        self._root.title(f"{self._app_name} - Estudio Aero")
        self._root.geometry("1120x720")
        self._root.minsize(960, 620)
        self._root.configure(bg="#060b16")

        self._build_menu()

        style = ttk.Style(self._root)
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#060b16")
        style.configure("Card.TLabelframe", background="#101b2b", borderwidth=1, relief="solid")
        style.configure(
            "Card.TLabelframe.Label",
            background="#101b2b",
            foreground="#67e8f9",
            font=("Bahnschrift SemiBold", 10),
        )
        style.configure("Card.TLabel", background="#101b2b", foreground="#d7e6ff", font=("Bahnschrift", 10))
        style.configure("Hint.TLabel", background="#101b2b", foreground="#87a7d6", font=("Bahnschrift", 9))
        style.configure(
            "Primary.TButton",
            font=("Bahnschrift SemiBold", 10),
            foreground="#03121c",
            background="#2dd4bf",
            padding=(12, 8),
            borderwidth=0,
        )
        style.configure(
            "Glass.TButton",
            font=("Bahnschrift SemiBold", 9),
            foreground="#d9e9ff",
            background="#1c2c47",
            padding=(10, 6),
            borderwidth=0,
        )
        style.configure(
            "Danger.TButton",
            font=("Bahnschrift SemiBold", 10),
            foreground="#ffffff",
            background="#e11d48",
            padding=(12, 8),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#5eead4"), ("pressed", "#14b8a6")],
        )
        style.map(
            "Glass.TButton",
            background=[("active", "#24395b"), ("pressed", "#17243b")],
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#fb7185"), ("pressed", "#be123c")],
        )
        style.configure(
            "Card.TRadiobutton",
            background="#101b2b",
            foreground="#d7e6ff",
            font=("Bahnschrift SemiBold", 9),
        )
        style.configure("Status.TLabel", background="#060b16", foreground="#9bd8ff", font=("Bahnschrift", 9))
        style.configure("Version.TLabel", background="#060b16", foreground="#67e8f9", font=("Bahnschrift SemiBold", 9))
        style.configure("TEntry", fieldbackground="#0e1a2c", foreground="#d7e6ff")
        style.configure("TCombobox", fieldbackground="#0e1a2c", foreground="#d7e6ff")

        root = ttk.Frame(self._root, style="Root.TFrame", padding=14)
        root.pack(fill="both", expand=True)

        header_wrap = ttk.Frame(root, style="Root.TFrame")
        header_wrap.pack(fill="x", pady=(0, 12))

        header = ttk.Label(
            header_wrap,
            text=f"{self._app_name}  |  iPhone AirPlay + Android Proyección inalámbrica",
            font=("Bahnschrift SemiBold", 18),
            foreground="#84ccff",
            background="#060b16",
        )
        header.pack(side="left", anchor="w")

        header_version = ttk.Label(
            header_wrap,
            text=f"v{self._app_version}",
            style="Version.TLabel",
        )
        header_version.pack(side="right", anchor="e", padx=(8, 0))

        top = ttk.Frame(root, style="Root.TFrame")
        top.pack(fill="x", pady=(0, 10))

        self._btn_receiver = ttk.Button(
            top,
            text="Iniciar receptor",
            underline=0,
            style="Primary.TButton",
            command=self._toggle_receiver,
        )
        self._btn_receiver.pack(side="left")
        self._btn_snapshot = ttk.Button(
            top,
            text="Captura",
            underline=0,
            style="Glass.TButton",
            command=self._take_snapshot,
        )
        self._btn_snapshot.pack(side="left", padx=(8, 0))
        self._btn_record = ttk.Button(
            top,
            text="Grabar",
            underline=0,
            style="Danger.TButton",
            command=self._toggle_recording,
        )
        self._btn_record.pack(side="left", padx=(8, 0))
        self._btn_exit = ttk.Button(
            top,
            text="Salir",
            underline=0,
            style="Glass.TButton",
            command=self._on_close,
        )
        self._btn_exit.pack(side="left", padx=(8, 0))

        self._pill_receiver = tk.Label(
            top,
            textvariable=self._receiver_status_var,
            font=("Bahnschrift SemiBold", 9),
            padx=10,
            pady=4,
            bd=1,
            relief="solid",
            bg="#13243b",
            fg="#8cc8ff",
            highlightbackground="#345a86",
        )
        self._pill_receiver.pack(side="right")
        self._pill_record = tk.Label(
            top,
            textvariable=self._record_status_var,
            font=("Bahnschrift SemiBold", 9),
            padx=10,
            pady=4,
            bd=1,
            relief="solid",
            bg="#13243b",
            fg="#8cc8ff",
            highlightbackground="#345a86",
        )
        self._pill_record.pack(side="right", padx=(0, 8))

        body = ttk.Frame(root, style="Root.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Root.TFrame")
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        right = ttk.Frame(body, style="Root.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_card_receiver(left)
        self._build_card_stream(left)
        self._build_card_capture(left)

        logs_card = ttk.LabelFrame(right, text="Registros de ejecución", style="Card.TLabelframe", padding=8)
        logs_card.grid(row=0, column=0, sticky="nsew")
        logs_card.rowconfigure(0, weight=1)
        logs_card.columnconfigure(0, weight=1)

        self._log_box = scrolledtext.ScrolledText(
            logs_card,
            wrap="word",
            state="disabled",
            font=("Cascadia Code", 10),
            background="#071225",
            foreground="#b8d7ff",
            insertbackground="#7dd3fc",
            relief="flat",
        )
        self._log_box.grid(row=0, column=0, sticky="nsew")
        self._log_box.tag_configure("error", foreground="#fb7185")
        self._log_box.tag_configure("warning", foreground="#fbbf24")
        self._log_box.tag_configure("hint", foreground="#67e8f9")
        self._log_box.tag_configure("recording", foreground="#f472b6")

        status_wrap = ttk.Frame(root, style="Root.TFrame")
        status_wrap.pack(fill="x", pady=(8, 0))
        self._status_label = ttk.Label(status_wrap, textvariable=self._status_var, style="Status.TLabel")
        self._status_label.pack(side="left", fill="x", expand=True)
        ttk.Label(status_wrap, text=f"Versión {self._app_version}", style="Version.TLabel").pack(side="right")

        self._append_log("Aplicación inicializada.")

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self._root)

        menu_archivo = tk.Menu(menu_bar, tearoff=0)
        menu_archivo.add_command(label="Salir", underline=0, accelerator="Alt+S", command=self._on_close)
        menu_bar.add_cascade(label="Archivo", underline=0, menu=menu_archivo)

        menu_ayuda = tk.Menu(menu_bar, tearoff=0)
        menu_ayuda.add_command(label="Acerca de", underline=0, accelerator="F1", command=self._show_about_dialog)
        menu_bar.add_cascade(label="Ayuda", underline=0, menu=menu_ayuda)

        self._root.configure(menu=menu_bar)

    def _build_card_receiver(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Configuración del receptor", style="Card.TLabelframe", padding=10)
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="Nombre visible en iPhone", underline=0, style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._entry_name = ttk.Entry(card, textvariable=self._receiver_name_var)
        self._entry_name.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        self._chk_hostname_suffix = ttk.Checkbutton(
            card,
            text="Agregar @equipo al nombre visible",
            variable=self._append_hostname_suffix_var,
            underline=8,
            command=self._schedule_save,
        )
        self._chk_hostname_suffix.grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(card, text="Ruta de UxPlay", underline=0, style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self._entry_path = ttk.Entry(card, textvariable=self._uxplay_path_var)
        self._entry_path.grid(row=2, column=1, sticky="ew", pady=(0, 8))
        self._btn_browse = ttk.Button(card, text="Examinar...", underline=1, style="Glass.TButton", command=self._browse_uxplay)
        self._btn_browse.grid(row=2, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))

        ttk.Label(card, text="Dispositivo", underline=0, style="Card.TLabel").grid(row=3, column=0, sticky="nw", padx=(0, 8))
        radios = ttk.Frame(card)
        radios.grid(row=3, column=1, columnspan=2, sticky="w")
        self._device_radio_buttons: list[ttk.Radiobutton] = []
        for i, mode in enumerate(self._device_modes):
            underline = 1 if mode.key == DEVICE_MODE_IPHONE else 0
            radio = ttk.Radiobutton(
                radios,
                text=mode.label,
                underline=underline,
                style="Card.TRadiobutton",
                variable=self._device_mode_var,
                value=mode.key,
                command=self._on_device_mode_changed,
            )
            radio.grid(row=0, column=i, sticky="w", padx=(0, 10))
            self._device_radio_buttons.append(radio)

        ttk.Label(card, textvariable=self._device_hint_var, style="Hint.TLabel", wraplength=320, justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w"
        )

    def _build_card_stream(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Transmisión y latencia", style="Card.TLabelframe", padding=10)
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="Perfil", underline=3, style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._combo_profile = ttk.Combobox(
            card,
            textvariable=self._profile_var,
            values=[p.label for p in self._profiles],
            state="readonly",
        )
        self._combo_profile.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 6))
        self._combo_profile.bind("<<ComboboxSelected>>", self._on_profile_changed)

        ttk.Label(card, textvariable=self._profile_desc_var, style="Hint.TLabel", wraplength=320, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        ttk.Label(card, text="Args extras", underline=0, style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self._entry_args = ttk.Entry(card, textvariable=self._custom_args_var)
        self._entry_args.grid(row=2, column=1, columnspan=2, sticky="ew")

    def _build_card_capture(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text="Captura y grabación", style="Card.TLabelframe", padding=10)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="Fuente", underline=0, style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._combo_capture_source = ttk.Combobox(
            card,
            textvariable=self._capture_source_var,
            values=list(self._capture_sources.keys()),
            state="readonly",
        )
        self._combo_capture_source.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(card, text="Título de ventana", underline=2, style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self._entry_capture_title = ttk.Entry(card, textvariable=self._capture_title_var)
        self._entry_capture_title.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(card, text="FPS de grabación", underline=0, style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8))
        self._spin_capture_fps = ttk.Spinbox(
            card,
            from_=15,
            to=120,
            increment=1,
            textvariable=self._capture_fps_var,
            width=8,
        )
        self._spin_capture_fps.grid(row=2, column=1, sticky="w")
        self._btn_clear = ttk.Button(card, text="Borrar registros", underline=0, style="Glass.TButton", command=self._clear_logs)
        self._btn_clear.grid(row=2, column=2, sticky="e")

    def _bind_hotkeys(self) -> None:
        self._root.bind_all("<Alt-i>", lambda _e: self._shortcut(self._toggle_receiver))
        self._root.bind_all("<Alt-c>", lambda _e: self._shortcut(self._take_snapshot))
        self._root.bind_all("<Alt-g>", lambda _e: self._shortcut(self._toggle_recording))
        self._root.bind_all("<Alt-s>", lambda _e: self._shortcut(self._on_close))
        self._root.bind_all("<Alt-b>", lambda _e: self._shortcut(self._clear_logs))
        self._root.bind_all("<Alt-x>", lambda _e: self._shortcut(self._browse_uxplay))
        self._root.bind_all("<Alt-p>", lambda _e: self._shortcut(self._set_mode_iphone))
        self._root.bind_all("<Alt-a>", lambda _e: self._shortcut(self._set_mode_android))
        self._root.bind_all("<Alt-m>", lambda _e: self._shortcut(lambda: self._entry_name.focus_set()))
        self._root.bind_all("<Alt-r>", lambda _e: self._shortcut(lambda: self._entry_path.focus_set()))
        self._root.bind_all("<Alt-f>", lambda _e: self._shortcut(lambda: self._combo_profile.focus_set()))
        self._root.bind_all("<Alt-e>", lambda _e: self._shortcut(self._toggle_hostname_suffix))
        self._root.bind_all("<Alt-o>", lambda _e: self._shortcut(self._show_about_dialog))
        self._root.bind_all("<F1>", lambda _e: self._shortcut(self._show_about_dialog))

    def _shortcut(self, fn: Callable[[], None]) -> str:
        if self._closing:
            return "break"
        fn()
        return "break"

    def _set_mode_iphone(self) -> None:
        self._device_mode_var.set(DEVICE_MODE_IPHONE)
        self._on_device_mode_changed()

    def _set_mode_android(self) -> None:
        self._device_mode_var.set(DEVICE_MODE_ANDROID)
        self._on_device_mode_changed()

    def _toggle_hostname_suffix(self) -> None:
        self._append_hostname_suffix_var.set(not self._append_hostname_suffix_var.get())
        self._schedule_save()

    def _install_autosave(self) -> None:
        vars_ = (
            self._receiver_name_var,
            self._uxplay_path_var,
            self._profile_var,
            self._custom_args_var,
            self._append_hostname_suffix_var,
            self._capture_source_var,
            self._capture_title_var,
            self._capture_fps_var,
            self._device_mode_var,
        )
        for v in vars_:
            v.trace_add("write", self._on_var_changed)
        self._root.bind("<Configure>", self._on_window_configure)

    def _on_var_changed(self, *_args) -> None:
        if not self._suspend_save and not self._closing:
            self._schedule_save()

    def _on_window_configure(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self._root and not self._suspend_save and not self._closing:
            self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_after_id is not None:
            self._root.after_cancel(self._save_after_id)
        self._save_after_id = self._root.after(self.SAVE_DEBOUNCE_MS, self._save_config)
    def _save_config(self) -> None:
        self._save_after_id = None
        profile = self._profiles_by_label.get(self._profile_var.get())
        profile_key = profile.key if profile else DEFAULT_PROFILE_KEY
        data = {
            "receiver_name": self._receiver_name_var.get().strip(),
            "uxplay_path": self._uxplay_path_var.get().strip(),
            "profile_label": self._profile_var.get(),
            "profile_key": profile_key,
            "custom_args": self._custom_args_var.get(),
            "append_hostname_suffix": bool(self._append_hostname_suffix_var.get()),
            "capture_source_label": self._capture_source_var.get(),
            "capture_window_title": self._capture_title_var.get(),
            "capture_fps": int(self._capture_fps_var.get()),
            "device_mode": self._selected_device_mode(),
            "window_geometry": self._root.geometry(),
        }
        try:
            self._store.save(data)
        except OSError as exc:
            self._append_log(f"[ADVERTENCIA] No se pudo guardar config.json: {exc}")
            self._set_status(f"No se pudo guardar config.json: {exc}", "warning")

    def _apply_saved_geometry(self, geometry: str) -> None:
        value = geometry.strip()
        if not value:
            return
        try:
            self._root.geometry(value)
        except tk.TclError:
            return

    def _safe_int(self, value: object, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(15, min(120, n))

    def _safe_bool(self, value: object, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "si", "sí", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
        return default

    def _resolve_profile(self, profile_key: object):
        if isinstance(profile_key, str):
            try:
                return get_profile(profile_key)
            except KeyError:
                pass
        return get_profile(DEFAULT_PROFILE_KEY)

    def _resolve_device_mode(self, mode_key: object) -> str:
        if isinstance(mode_key, str):
            try:
                return get_device_mode(mode_key).key
            except KeyError:
                pass
        return DEFAULT_DEVICE_MODE_KEY

    def _selected_device_mode(self) -> str:
        return self._resolve_device_mode(self._device_mode_var.get())

    def _on_device_mode_changed(self) -> None:
        mode = self._selected_device_mode()
        if mode == DEVICE_MODE_ANDROID and self._controller.is_running():
            self._append_log("[PISTA] Cambio a Android: se detiene el receptor de iPhone automáticamente.")
            self._run_in_background(
                action_label="Deteniendo receptor de iPhone",
                fn=self._controller.stop_receiver,
                success_status="Receptor de iPhone detenido.",
            )

        self._apply_device_mode_ui(log_change=True)
        if mode == DEVICE_MODE_ANDROID:
            self._run_in_background(
                action_label="Analizando compatibilidad Miracast",
                fn=self._diagnose_android_mode,
                success_status="Diagnóstico Android completado.",
                busy=False,
            )

        self._schedule_save()

    def _apply_device_mode_ui(self, log_change: bool) -> None:
        mode = self._selected_device_mode()
        selected_mode = self._device_by_key.get(mode, self._device_by_key[DEFAULT_DEVICE_MODE_KEY])
        self._device_hint_var.set(selected_mode.description)

        if mode == DEVICE_MODE_IPHONE:
            self._combo_profile.configure(state="readonly")
            self._entry_args.configure(state="normal")
            if log_change:
                self._append_log("Modo de dispositivo: iPhone (AirPlay).")
                self._set_status("Modo iPhone activo.", "info")
            if not self._controller.is_running():
                self._btn_receiver.configure(text="Iniciar receptor", underline=0, style="Primary.TButton")
            self._set_running_state(self._controller.is_running())
            return

        self._combo_profile.configure(state="disabled")
        self._entry_args.configure(state="disabled")
        self._btn_receiver.configure(text="Abrir proyección Android", underline=0, style="Primary.TButton")
        self._receiver_status_var.set("Receptor: modo Android")
        self._pill_receiver.configure(bg="#1f2f4b", fg="#9fc9ff", highlightbackground="#365c8f")
        if log_change:
            self._append_log("Modo de dispositivo: Android (Proyección inalámbrica).")
            self._set_status("Modo Android activo.", "info")

    def _on_profile_changed(self, _event: tk.Event[ttk.Combobox]) -> None:
        profile = self._profiles_by_label.get(self._profile_var.get())
        if profile:
            self._profile_desc_var.set(profile.description)
        self._schedule_save()

    def _browse_uxplay(self) -> None:
        if self._is_busy():
            self._set_status("Espera a que finalice la operación actual.", "warning")
            return

        selected = filedialog.askopenfilename(
            title="Selecciona el ejecutable de UxPlay",
            filetypes=[("Ejecutable", "*.exe"), ("Todos los archivos", "*.*")],
        )
        if selected:
            self._uxplay_path_var.set(selected)
            self._set_status("Ruta de UxPlay actualizada.", "success")
            self._schedule_save()

    def _toggle_receiver(self) -> None:
        if self._is_busy():
            self._set_status("Ya hay una operación en curso. Espera un momento.", "warning")
            return

        if self._selected_device_mode() == DEVICE_MODE_ANDROID:
            self._open_android_projection()
            return

        if self._controller.is_running():
            self._run_in_background(
                action_label="Deteniendo receptor",
                fn=self._controller.stop_receiver,
                success_status="Receptor detenido.",
            )
            return

        uxplay_path = self._uxplay_path_var.get().strip()
        if not uxplay_path:
            self._append_log("[ERROR] Falta la ruta de UxPlay.")
            self._set_status("Falta la ruta de UxPlay.", "error")
            return

        name = self._receiver_name_var.get().strip() or "ScreenMirrorIOSAndroid"
        profile = self._profiles_by_label.get(self._profile_var.get()) or get_profile(DEFAULT_PROFILE_KEY)

        try:
            extra = self._parse_args(self._custom_args_var.get())
        except ValueError as exc:
            self._append_log(f"[ERROR] {exc}")
            self._set_status(str(exc), "error")
            return

        args = [*profile.args, *extra]
        self._append_log(f"Perfil: {profile.label} | Args: {' '.join(args) if args else '(sin argumentos)'}")

        def start_receiver() -> None:
            self._controller.start_receiver(
                Path(uxplay_path),
                name,
                extra_args=args,
                append_hostname_suffix=bool(self._append_hostname_suffix_var.get()),
            )

        suffix_mode = "con sufijo @equipo" if self._append_hostname_suffix_var.get() else "sin sufijo @equipo"
        self._run_in_background(
            action_label="Iniciando receptor",
            fn=start_receiver,
            success_status=f"Receptor iniciándose con nombre: {name} ({suffix_mode})",
        )

    def _open_android_projection(self) -> None:
        self._run_in_background(
            action_label="Abriendo proyección Android",
            fn=self._controller.open_android_projection_portal,
            success_status="Configuración de Proyección Android abierta.",
        )

    def _diagnose_android_mode(self) -> None:
        diag = self._controller.get_android_projection_diagnostics()
        self._root.after(0, lambda d=diag: self._apply_android_diagnostics(d))

    def _apply_android_diagnostics(self, diag: dict[str, str | bool | None]) -> None:
        capability = str(diag.get("wireless_display_capability", "Unknown")).strip()
        line = str(diag.get("miracast_status_line", "")).strip()
        supported = diag.get("miracast_receiver_supported")

        if capability and capability != "Unknown":
            self._append_log(f"[PISTA] Wireless Display: {capability}")
        if line:
            self._append_log(f"[PISTA] Diagnóstico Miracast: {line}")

        if capability == "NotPresent":
            self._append_log("[ADVERTENCIA] La característica opcional 'Wireless Display' no está instalada.")
            self._set_status("Instala 'Wireless Display' en Windows y reinicia.", "warning")
            return

        if capability.startswith("Unknown ("):
            self._append_log(f"[ADVERTENCIA] {capability}")
            self._set_status(capability, "warning")

        if supported is False:
            self._append_log("[ADVERTENCIA] Este equipo no admite recibir Miracast.")
            self._set_status("Este equipo no admite recibir Miracast.", "warning")
            return

        self._append_log("[PISTA] Android activo. Pulsa 'Abrir proyección Android'.")
        self._set_status("Android listo para proyección inalámbrica.", "success")

    def _take_snapshot(self) -> None:
        if self._is_busy():
            self._set_status("Ya hay una operación en curso. Espera un momento.", "warning")
            return

        if not self._controller.is_running():
            self._append_log("[PISTA] El receptor está detenido. Se capturará el escritorio igualmente.")
            self._set_status("Receptor detenido: captura desde escritorio.", "warning")

        path = filedialog.asksaveasfilename(
            title="Guardar captura",
            defaultextension=".png",
            filetypes=[("Imagen PNG", "*.png"), ("Imagen JPEG", "*.jpg"), ("Todos los archivos", "*.*")],
            initialfile=f"captura_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
        )
        if not path:
            return

        def do_snapshot() -> None:
            self._controller.take_snapshot(
                uxplay_path=Path(self._uxplay_path_var.get().strip()),
                output_path=Path(path),
                source_mode=self._capture_sources.get(self._capture_source_var.get(), "desktop"),
                window_title=self._capture_title_var.get().strip(),
            )

        self._run_in_background(
            action_label="Tomando captura",
            fn=do_snapshot,
            success_status="Captura completada.",
        )

    def _toggle_recording(self) -> None:
        if self._is_busy():
            self._set_status("Ya hay una operación en curso. Espera un momento.", "warning")
            return

        if self._controller.is_recording():
            save_path = self._prompt_recording_output_path()
            if save_path is None:
                self._set_status("Debes elegir destino para guardar la grabación antes de detener.", "warning")
                return

            def stop_recording() -> None:
                self._controller.stop_recording(output_path=save_path)

            self._run_in_background(
                action_label="Deteniendo grabación",
                fn=stop_recording,
                success_status="Grabación detenida.",
                busy=False,
            )
            return

        if not self._controller.is_running() and self._selected_device_mode() == DEVICE_MODE_IPHONE:
            self._set_status("Inicia el receptor de iPhone antes de grabar.", "warning")
            return

        if self._selected_device_mode() != DEVICE_MODE_IPHONE:
            self._set_status("La grabación de video solo está habilitada para la ventana UxPlay (modo iPhone).", "warning")
            return

        started_at = datetime.now()
        self._record_suggested_name = f"grabacion_{started_at.strftime('%Y%m%d_%H%M%S')}.mp4"
        temp_path = Path(tempfile.gettempdir()) / f"ScreenMirrorIOSAndroid_{started_at.strftime('%Y%m%d_%H%M%S_%f')}.mp4"

        def start_recording() -> None:
            self._controller.start_recording(
                uxplay_path=Path(self._uxplay_path_var.get().strip()),
                output_path=temp_path,
                source_mode="window",
                window_title=self._capture_title_var.get().strip(),
                fps=int(self._capture_fps_var.get()),
            )

        self._run_in_background(
            action_label="Iniciando grabación",
            fn=start_recording,
            success_status="Grabación iniciada.",
            busy=False,
        )

    def _suggest_recording_name(self) -> str:
        if self._record_suggested_name:
            return self._record_suggested_name
        return f"grabacion_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

    def _default_recording_output_path(self) -> Path:
        documents = Path.home() / "Documents"
        base_dir = documents if documents.exists() else Path.cwd()
        return base_dir / self._suggest_recording_name()

    def _prompt_recording_output_path(self) -> Path | None:
        selected = filedialog.asksaveasfilename(
            title="Guardar grabación",
            defaultextension=".mp4",
            filetypes=[("Video MP4", "*.mp4"), ("Todos los archivos", "*.*")],
            initialfile=self._suggest_recording_name(),
        )
        if not selected:
            return None
        return Path(selected)

    def _parse_args(self, raw: str) -> list[str]:
        text = raw.strip()
        if not text:
            return []
        try:
            return shlex.split(text, posix=False)
        except ValueError as exc:
            raise ValueError(f"Sintaxis inválida en argumentos extras: {exc}") from exc
    def _poll_events(self) -> None:
        if self._closing:
            return

        for event in self._controller.drain_events():
            if event.kind == "log":
                self._append_log(event.message)
                self._inspect_log(event.message)
            elif event.kind == "state":
                self._set_running_state(event.running)
                if not event.running and self._controller.is_recording():
                    auto_save_path = self._default_recording_output_path()

                    def stop_due_to_receiver_stop() -> None:
                        self._controller.stop_recording(output_path=auto_save_path)

                    self._run_in_background(
                        action_label="Deteniendo grabación por parada de receptor",
                        fn=stop_due_to_receiver_stop,
                        success_status=f"Grabación detenida junto al receptor. Archivo: {auto_save_path}",
                        busy=False,
                    )
            elif event.kind == "recording":
                self._set_record_state(event.recording)

        self._root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _inspect_log(self, message: str) -> None:
        low = message.lower()
        if message.startswith("Iniciando receptor:"):
            self._ntp_error_count = 0
            self._ntp_hint_shown = False
        if "initialized server socket(s)" in low:
            self._set_status("Receptor listo para conectar desde iPhone.", "success")
        if "raop_rtp_mirror starting mirroring" in low:
            self._set_status("Conexión AirPlay establecida. Abriendo ventana de video...", "info")
        if "reiniciando receptor automáticamente" in low or "reiniciando receptor automaticamente" in low:
            self._set_status("Recuperando primer enlace AirPlay automáticamente...", "warning")
        if "receptor reiniciado automáticamente" in low or "receptor reiniciado automaticamente" in low:
            self._set_status("Receptor recuperado. Intenta conectar de nuevo si estaba en espera.", "success")
        if "gstreamer error: output window was closed" in low:
            self._append_log("[ADVERTENCIA] UxPlay cerró la ventana de video. Se recomienda usar perfil de render estable.")
            self._set_status("La ventana de video se cerró; cambiando a perfil estable recomendado.", "warning")
        if "invalid ntp_time < gst_video_pipeline_base_time" in low:
            self._ntp_error_count += 1
        if self._ntp_error_count >= 8 and not self._ntp_hint_shown:
            self._ntp_hint_shown = True
            self._append_log("[PISTA] Se detectó desfase NTP. Sincroniza el reloj de Windows.")
            self._set_status("Posible desfase NTP detectado.", "warning")

    def _set_running_state(self, running: bool) -> None:
        if self._selected_device_mode() == DEVICE_MODE_ANDROID:
            self._receiver_status_var.set("Receptor: modo Android")
            self._btn_receiver.configure(text="Abrir proyección Android", underline=0, style="Primary.TButton")
            self._pill_receiver.configure(bg="#1f2f4b", fg="#9fc9ff", highlightbackground="#365c8f")
            return

        if running:
            self._receiver_status_var.set("Receptor: activo")
            self._btn_receiver.configure(text="Detener receptor", underline=0, style="Danger.TButton")
            self._pill_receiver.configure(bg="#0f2a24", fg="#5eead4", highlightbackground="#14b8a6")
            self._set_status("Receptor activo.", "success")
            return

        self._receiver_status_var.set("Receptor: detenido")
        self._btn_receiver.configure(text="Iniciar receptor", underline=0, style="Primary.TButton")
        self._pill_receiver.configure(bg="#2a1620", fg="#fb7185", highlightbackground="#be123c")
        self._set_status("Receptor detenido.", "info")

    def _set_record_state(self, recording: bool) -> None:
        if recording:
            self._record_status_var.set("Grabación: activa")
            self._btn_record.configure(text="Detener grabación", underline=0, style="Danger.TButton")
            self._pill_record.configure(bg="#2a1620", fg="#fb7185", highlightbackground="#be123c")
            self._set_status("Grabación activa.", "success")
            return

        self._record_suggested_name = None
        self._record_status_var.set("Grabación: inactiva")
        self._btn_record.configure(text="Grabar", underline=0, style="Danger.TButton")
        self._pill_record.configure(bg="#1f2f4b", fg="#9fc9ff", highlightbackground="#365c8f")

    def _append_log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
        low = message.lower()
        tag = ""
        if "error" in low:
            tag = "error"
            self._set_status(message, "error")
        elif "advertencia" in low:
            tag = "warning"
            self._set_status(message, "warning")
        elif "pista" in low:
            tag = "hint"
        elif "grabación" in low or message.startswith("[REC]"):
            tag = "recording"

        self._log_box.configure(state="normal")
        if tag:
            self._log_box.insert("end", line, tag)
        else:
            self._log_box.insert("end", line)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _clear_logs(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")
        self._set_status("Registros limpiados.", "info")

    def _show_about_dialog(self) -> None:
        if self._about_dialog is not None and self._about_dialog.winfo_exists():
            self._about_dialog.focus_force()
            return

        year = datetime.now().year
        text = f"Versión {self._app_version} creado por Synyster Rick, {year} Derechos Reservados"

        dialog = tk.Toplevel(self._root)
        dialog.title("Acerca de")
        dialog.resizable(False, False)
        dialog.transient(self._root)
        dialog.grab_set()
        dialog.configure(bg="#0a1222")

        container = ttk.Frame(dialog, style="Root.TFrame", padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text=self._app_name, font=("Bahnschrift SemiBold", 13), style="Status.TLabel").pack(anchor="w")
        ttk.Label(
            container,
            text=text,
            font=("Bahnschrift", 10),
            style="Status.TLabel",
            wraplength=380,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))
        ttk.Button(container, text="Cerrar", command=dialog.destroy).pack(anchor="e")

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._about_dialog = dialog

    def _set_status(self, message: str, level: str) -> None:
        clean = " ".join(message.strip().split())
        if not clean:
            clean = "Listo."

        palette = {
            "info": "#9bd8ff",
            "success": "#5eead4",
            "warning": "#fbbf24",
            "error": "#fb7185",
        }
        color = palette.get(level, palette["info"])
        self._status_label.configure(foreground=color)
        self._status_var.set(clean)

    def _run_in_background(
        self,
        action_label: str,
        fn: Callable[[], None],
        success_status: str,
        busy: bool = True,
    ) -> None:
        if self._closing:
            return

        if busy:
            self._set_busy_state(True)
        self._set_status(f"{action_label}...", "info")

        def worker() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self._root.after(0, lambda e=exc: self._on_background_error(action_label, e))
            else:
                self._root.after(0, lambda: self._set_status(success_status, "success"))
            finally:
                if busy:
                    self._root.after(0, lambda: self._set_busy_state(False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_background_error(self, action_label: str, exc: Exception) -> None:
        self._append_log(f"[ERROR] {exc}")
        self._set_status(f"{action_label} falló: {exc}", "error")

    def _set_busy_state(self, busy: bool) -> None:
        if busy:
            self._busy_count += 1
        else:
            self._busy_count = max(0, self._busy_count - 1)

        is_busy = self._is_busy()
        state = "disabled" if is_busy else "normal"

        self._btn_receiver.configure(state=state)
        self._btn_snapshot.configure(state=state)
        self._btn_record.configure(state=state)
        self._btn_browse.configure(state=state)
        self._btn_clear.configure(state=state)
        self._btn_exit.configure(state="normal")

        if is_busy:
            self._entry_name.configure(state="disabled")
            self._entry_path.configure(state="disabled")
            self._combo_profile.configure(state="disabled")
            self._entry_args.configure(state="disabled")
            self._chk_hostname_suffix.configure(state="disabled")
            self._combo_capture_source.configure(state="disabled")
            self._entry_capture_title.configure(state="disabled")
            self._spin_capture_fps.configure(state="disabled")
            for radio in self._device_radio_buttons:
                radio.configure(state="disabled")
            return

        self._entry_name.configure(state="normal")
        self._entry_path.configure(state="normal")
        self._chk_hostname_suffix.configure(state="normal")
        self._combo_capture_source.configure(state="readonly")
        self._entry_capture_title.configure(state="normal")
        self._spin_capture_fps.configure(state="normal")
        for radio in self._device_radio_buttons:
            radio.configure(state="normal")
        self._apply_device_mode_ui(log_change=False)
        self._set_running_state(self._controller.is_running())
        self._set_record_state(self._controller.is_recording())

    def _is_busy(self) -> bool:
        return self._busy_count > 0

    def _on_close(self) -> None:
        if self._closing:
            return

        self._closing = True
        if self._save_after_id is not None:
            self._root.after_cancel(self._save_after_id)
            self._save_after_id = None
        self._save_config()
        self._set_status("Cerrando aplicación...", "info")

        def shutdown() -> None:
            try:
                self._controller.shutdown()
            finally:
                self._root.after(0, self._root.destroy)

        threading.Thread(target=shutdown, daemon=True).start()
