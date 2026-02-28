from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
from datetime import datetime
import math
import os
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
from backend.i18n import (
    LANGUAGE_EN,
    LANGUAGE_ES,
    LANGUAGE_LABELS,
    localize_log,
    normalize_language,
    tr,
)
from backend.receiver_profiles import DEFAULT_PROFILE_KEY, get_profile, list_profiles


class MainWindow:
    POLL_INTERVAL_MS = 150
    SAVE_DEBOUNCE_MS = 300
    _BIND_TARGET_AUTO = "__auto__"
    _EMBED_RETRY_DELAY_MS = 450
    _EMBED_MAX_RETRIES = 24
    _IPHONE_PREVIEW_ASPECT_WIDTH = 9
    _IPHONE_PREVIEW_ASPECT_HEIGHT = 16

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
        self._capture_source_modes = ("desktop", "window")

        saved = self._store.load()
        self._language_code = normalize_language(saved.get("language", LANGUAGE_ES))
        self._language_var = tk.StringVar(value=self._language_code)

        initial_profile = self._resolve_profile(saved.get("profile_key", default_profile_key))
        if str(saved.get("profile_label", "")).strip() in self._profiles_by_label:
            initial_profile = self._profiles_by_label[str(saved["profile_label"]).strip()]

        initial_mode = self._resolve_device_mode(saved.get("device_mode", default_device_mode_key))
        initial_source_mode = self._capture_source_mode_from_value(
            str(saved.get("capture_source_mode", saved.get("capture_source_label", "desktop")))
        )

        self._receiver_name_var = tk.StringVar(value=str(saved.get("receiver_name", default_receiver_name)))
        self._uxplay_path_var = tk.StringVar(value=str(saved.get("uxplay_path", str(default_uxplay_path))))
        self._profile_var = tk.StringVar(value=initial_profile.label)
        self._profile_desc_var = tk.StringVar(value=initial_profile.description)
        self._custom_args_var = tk.StringVar(value=str(saved.get("custom_args", "")))
        self._append_hostname_suffix_var = tk.BooleanVar(
            value=self._safe_bool(saved.get("append_hostname_suffix", False), False)
        )
        self._capture_source_var = tk.StringVar(value=self._capture_source_label(initial_source_mode))
        self._capture_title_var = tk.StringVar(value=str(saved.get("capture_window_title", "Direct3D11 renderer")))
        self._capture_fps_var = tk.IntVar(value=self._safe_int(saved.get("capture_fps", 30), 30))
        self._device_mode_var = tk.StringVar(value=initial_mode)
        self._device_hint_var = tk.StringVar(value=self._device_mode_description(initial_mode))
        self._bind_target_var = tk.StringVar(value=str(saved.get("bind_target", self._BIND_TARGET_AUTO)))
        self._bind_hint_var = tk.StringVar(value=self._tr("bind_hint_auto"))

        self._receiver_status_var = tk.StringVar(value=self._tr("receiver_status_stopped"))
        self._record_status_var = tk.StringVar(value=self._tr("record_status_inactive"))
        self._status_var = tk.StringVar(value=self._tr("status_ready", app_name=self._app_name, version=self._app_version))

        self._save_after_id: str | None = None
        self._suspend_save = False
        self._ntp_error_count = 0
        self._ntp_hint_shown = False
        self._record_suggested_name: str | None = None
        self._record_stop_requested = False
        self._recording_was_active = False
        self._animation_after_id: str | None = None
        self._animation_phase = 0.0
        self._intro_alpha = 1.0
        self._busy_count = 0
        self._closing = False
        self._about_dialog: tk.Toplevel | None = None
        self._log_history: list[tuple[str, str]] = []
        self._bind_target_buttons: list[ttk.Radiobutton] = []
        self._bind_target_choices: list[tuple[str, str]] = []
        self._startup_receiver_requested = False
        self._embed_retry_after_id: str | None = None
        self._embed_retry_left = 0
        self._embedded_window_hwnd: int | None = None
        self._embedded_original_style: int | None = None

        self._build_ui()
        self._refresh_bind_targets(log_change=False)
        self._apply_device_mode_ui(log_change=False)
        self._set_running_state(False)
        self._set_record_state(False)

        self._apply_saved_geometry(str(saved.get("window_geometry", "")))
        self._install_autosave()
        self._bind_hotkeys()
        self._start_intro_animation()

        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.after(self.POLL_INTERVAL_MS, self._poll_events)
        self._root.after(650, self._auto_start_receiver_on_launch)

    def _tr(self, key: str, **kwargs: object) -> str:
        return tr(self._language_code, key, **kwargs)

    def _capture_source_label(self, mode: str) -> str:
        key = "source_desktop" if mode == "desktop" else "source_window"
        return self._tr(key)

    def _capture_source_mode_from_value(self, value: str) -> str:
        normalized = " ".join(str(value).split()).strip().lower()
        if normalized in self._capture_source_modes:
            return normalized

        for mode in self._capture_source_modes:
            es_label = tr(LANGUAGE_ES, "source_desktop" if mode == "desktop" else "source_window").lower()
            en_label = tr(LANGUAGE_EN, "source_desktop" if mode == "desktop" else "source_window").lower()
            if normalized in {es_label, en_label}:
                return mode

        return "desktop"

    def _device_mode_label(self, mode_key: str) -> str:
        if mode_key == DEVICE_MODE_ANDROID:
            return self._tr("device_android")
        return self._tr("device_iphone")

    def _device_mode_description(self, mode_key: str) -> str:
        if mode_key == DEVICE_MODE_ANDROID:
            return self._tr("device_hint_android")
        return self._tr("device_hint_iphone")

    def _build_ui(self, announce_init: bool = True) -> None:
        self._root.title(self._tr("window_title", app_name=self._app_name))
        self._root.geometry("1120x720")
        self._root.minsize(960, 620)
        self._root.configure(bg="#050814")

        self._build_menu()

        style = ttk.Style(self._root)
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#050814")
        style.configure("Card.TLabelframe", background="#0e1a2a", borderwidth=1, relief="flat")
        style.configure(
            "Card.TLabelframe.Label",
            background="#0e1a2a",
            foreground="#64d8ff",
            font=("Segoe UI Semibold", 10),
        )
        style.configure("Card.TLabel", background="#0e1a2a", foreground="#d8e7ff", font=("Segoe UI", 10))
        style.configure("Hint.TLabel", background="#0e1a2a", foreground="#89a8d8", font=("Segoe UI", 9))
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 10),
            foreground="#03121c",
            background="#3fe6c8",
            padding=(14, 9),
            borderwidth=0,
        )
        style.configure(
            "Glass.TButton",
            font=("Segoe UI Semibold", 9),
            foreground="#d9e9ff",
            background="#1b2f4d",
            padding=(11, 7),
            borderwidth=0,
        )
        style.configure(
            "Danger.TButton",
            font=("Segoe UI Semibold", 10),
            foreground="#ffffff",
            background="#e11d48",
            padding=(14, 9),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#73efd5"), ("pressed", "#1bb9a4")],
        )
        style.map(
            "Glass.TButton",
            background=[("active", "#264263"), ("pressed", "#16283e")],
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#fb7185"), ("pressed", "#be123c")],
        )
        style.configure(
            "Card.TRadiobutton",
            background="#0e1a2a",
            foreground="#d7e6ff",
            font=("Segoe UI Semibold", 9),
        )
        style.configure("Status.TLabel", background="#050814", foreground="#9bd8ff", font=("Segoe UI", 9))
        style.configure("Version.TLabel", background="#050814", foreground="#67e8f9", font=("Segoe UI Semibold", 9))
        style.configure("TEntry", fieldbackground="#0e1a2c", foreground="#d7e6ff")
        style.configure("TCombobox", fieldbackground="#0e1a2c", foreground="#d7e6ff")

        root = ttk.Frame(self._root, style="Root.TFrame", padding=14)
        root.pack(fill="both", expand=True)

        header_wrap = ttk.Frame(root, style="Root.TFrame")
        header_wrap.pack(fill="x", pady=(0, 10))

        self._header_label = ttk.Label(
            header_wrap,
            text=self._tr("header_title", app_name=self._app_name),
            font=("Segoe UI Semibold", 18),
            foreground="#8bd4ff",
            background="#050814",
        )
        self._header_label.pack(side="left", anchor="w")

        ttk.Label(
            header_wrap,
            text=self._tr("header_mode"),
            font=("Segoe UI", 10),
            foreground="#89a8d8",
            background="#050814",
        ).pack(side="left", padx=(10, 0), anchor="s")

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
            text=self._tr("btn_start_receiver"),
            underline=0,
            style="Primary.TButton",
            command=self._toggle_receiver,
        )
        self._btn_receiver.pack(side="left")
        self._btn_snapshot = ttk.Button(
            top,
            text=self._tr("btn_snapshot"),
            underline=0,
            style="Glass.TButton",
            command=self._take_snapshot,
        )
        self._btn_snapshot.pack(side="left", padx=(8, 0))
        self._btn_record = ttk.Button(
            top,
            text=self._tr("btn_record"),
            underline=0,
            style="Danger.TButton",
            command=self._toggle_recording,
        )
        self._btn_record.pack(side="left", padx=(8, 0))
        self._btn_exit = ttk.Button(
            top,
            text=self._tr("btn_exit"),
            underline=0,
            style="Glass.TButton",
            command=self._on_close,
        )
        self._btn_exit.pack(side="left", padx=(8, 0))

        self._pill_receiver = tk.Label(
            top,
            textvariable=self._receiver_status_var,
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=5,
            bd=0,
            relief="flat",
            bg="#12253d",
            fg="#9ad6ff",
            highlightthickness=1,
            highlightbackground="#345a86",
        )
        self._pill_receiver.pack(side="right")
        self._pill_record = tk.Label(
            top,
            textvariable=self._record_status_var,
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=5,
            bd=0,
            relief="flat",
            bg="#12253d",
            fg="#9ad6ff",
            highlightthickness=1,
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

        self._content_tabs = ttk.Notebook(right)
        self._content_tabs.grid(row=0, column=0, sticky="nsew")

        self._preview_tab = ttk.Frame(self._content_tabs, style="Root.TFrame")
        self._logs_tab = ttk.Frame(self._content_tabs, style="Root.TFrame")
        self._content_tabs.add(self._preview_tab, text=self._tr("tab_preview"))
        self._content_tabs.add(self._logs_tab, text=self._tr("tab_logs"))

        self._build_preview_tab(self._preview_tab)
        self._build_logs_tab(self._logs_tab)

        status_wrap = ttk.Frame(root, style="Root.TFrame")
        status_wrap.pack(fill="x", pady=(8, 0))
        self._status_label = ttk.Label(status_wrap, textvariable=self._status_var, style="Status.TLabel")
        self._status_label.pack(side="left", fill="x", expand=True)
        ttk.Label(status_wrap, text=self._tr("version_label", version=self._app_version), style="Version.TLabel").pack(
            side="right"
        )

        if announce_init:
            self._append_log(self._tr("app_initialized"))

    def _build_preview_tab(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        preview_card = ttk.LabelFrame(parent, text=self._tr("card_live_preview"), style="Card.TLabelframe", padding=10)
        preview_card.grid(row=0, column=0, sticky="nsew")
        preview_card.rowconfigure(0, weight=1)
        preview_card.columnconfigure(0, weight=1)

        self._preview_host_frame = tk.Frame(
            preview_card,
            bg="#030712",
            highlightthickness=1,
            highlightbackground="#28456b",
            bd=0,
        )
        self._preview_host_frame.grid(row=0, column=0, sticky="nsew")
        self._preview_host_frame.bind("<Configure>", self._on_preview_host_resized)

        self._preview_overlay = tk.Label(
            self._preview_host_frame,
            text=self._tr("preview_placeholder"),
            font=("Segoe UI", 11),
            bg="#030712",
            fg="#7fa3d7",
            justify="center",
        )
        self._preview_overlay.place(relx=0.5, rely=0.5, anchor="center")

        self._preview_hint_var = tk.StringVar(value=self._tr("preview_hint_idle"))
        self._preview_hint_label = ttk.Label(
            preview_card,
            textvariable=self._preview_hint_var,
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        )
        self._preview_hint_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _build_logs_tab(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        logs_card = ttk.LabelFrame(parent, text=self._tr("card_runtime_logs"), style="Card.TLabelframe", padding=8)
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

    def _build_menu(self) -> None:
        self._menu_bar = tk.Menu(self._root)

        self._menu_file = tk.Menu(self._menu_bar, tearoff=0)
        self._menu_file.add_command(label=self._tr("menu_exit"), underline=0, accelerator="Alt+S", command=self._on_close)
        self._menu_bar.add_cascade(label=self._tr("menu_file"), underline=0, menu=self._menu_file)

        self._menu_language = tk.Menu(self._menu_bar, tearoff=0)
        self._menu_language.add_radiobutton(
            label=self._tr("menu_language_es"),
            value=LANGUAGE_ES,
            variable=self._language_var,
            command=lambda: self._on_language_selected(LANGUAGE_ES),
        )
        self._menu_language.add_radiobutton(
            label=self._tr("menu_language_en"),
            value=LANGUAGE_EN,
            variable=self._language_var,
            command=lambda: self._on_language_selected(LANGUAGE_EN),
        )
        self._menu_bar.add_cascade(label=self._tr("menu_language"), underline=0, menu=self._menu_language)

        self._menu_help = tk.Menu(self._menu_bar, tearoff=0)
        self._menu_help.add_command(label=self._tr("menu_about"), underline=0, accelerator="F1", command=self._show_about_dialog)
        self._menu_bar.add_cascade(label=self._tr("menu_help"), underline=0, menu=self._menu_help)

        self._root.configure(menu=self._menu_bar)

    def _build_card_receiver(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=self._tr("card_receiver_config"), style="Card.TLabelframe", padding=10)
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text=self._tr("lbl_receiver_name"), underline=0, style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._entry_name = ttk.Entry(card, textvariable=self._receiver_name_var)
        self._entry_name.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        self._chk_hostname_suffix = ttk.Checkbutton(
            card,
            text=self._tr("chk_append_hostname"),
            variable=self._append_hostname_suffix_var,
            underline=8,
            command=self._schedule_save,
        )
        self._chk_hostname_suffix.grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(card, text=self._tr("lbl_uxplay_path"), underline=0, style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        self._entry_path = ttk.Entry(card, textvariable=self._uxplay_path_var)
        self._entry_path.grid(row=2, column=1, sticky="ew", pady=(0, 8))
        self._btn_browse = ttk.Button(
            card,
            text=self._tr("btn_browse"),
            underline=1,
            style="Glass.TButton",
            command=self._browse_uxplay,
        )
        self._btn_browse.grid(row=2, column=2, sticky="ew", padx=(8, 0), pady=(0, 8))

        ttk.Label(card, text=self._tr("lbl_device"), underline=0, style="Card.TLabel").grid(
            row=3, column=0, sticky="nw", padx=(0, 8)
        )
        radios = ttk.Frame(card)
        radios.grid(row=3, column=1, columnspan=2, sticky="w")
        self._device_radio_buttons: list[ttk.Radiobutton] = []
        for i, mode in enumerate(self._device_modes):
            underline = 1 if mode.key == DEVICE_MODE_IPHONE else 0
            radio = ttk.Radiobutton(
                radios,
                text=self._device_mode_label(mode.key),
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

        ttk.Label(card, text=self._tr("lbl_bind_interface"), underline=0, style="Card.TLabel").grid(
            row=5, column=0, sticky="nw", padx=(0, 8), pady=(8, 0)
        )
        self._bind_targets_frame = ttk.Frame(card)
        self._bind_targets_frame.grid(row=5, column=1, columnspan=2, sticky="w", pady=(8, 0))

        self._btn_refresh_bind_targets = ttk.Button(
            card,
            text=self._tr("btn_refresh_bind_targets"),
            style="Glass.TButton",
            command=self._on_refresh_bind_targets,
        )
        self._btn_refresh_bind_targets.grid(row=6, column=2, sticky="e", pady=(4, 0))
        ttk.Label(card, textvariable=self._bind_hint_var, style="Hint.TLabel", wraplength=320, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

    def _build_card_stream(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=self._tr("card_stream_latency"), style="Card.TLabelframe", padding=10)
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text=self._tr("lbl_profile"), underline=3, style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
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

        ttk.Label(card, text=self._tr("lbl_extra_args"), underline=0, style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        self._entry_args = ttk.Entry(card, textvariable=self._custom_args_var)
        self._entry_args.grid(row=2, column=1, columnspan=2, sticky="ew")

    def _build_card_capture(self, parent: ttk.Frame) -> None:
        card = ttk.LabelFrame(parent, text=self._tr("card_capture_record"), style="Card.TLabelframe", padding=10)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text=self._tr("lbl_source"), underline=0, style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self._combo_capture_source = ttk.Combobox(
            card,
            textvariable=self._capture_source_var,
            values=[self._capture_source_label(mode) for mode in self._capture_source_modes],
            state="readonly",
        )
        self._combo_capture_source.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(
            card,
            text=self._tr("lbl_window_title"),
            underline=2,
            style="Card.TLabel",
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self._entry_capture_title = ttk.Entry(card, textvariable=self._capture_title_var)
        self._entry_capture_title.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(card, text=self._tr("lbl_capture_fps"), underline=0, style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8)
        )
        self._spin_capture_fps = ttk.Spinbox(
            card,
            from_=15,
            to=60,
            increment=1,
            textvariable=self._capture_fps_var,
            width=8,
        )
        self._spin_capture_fps.grid(row=2, column=1, sticky="w")
        self._btn_clear = ttk.Button(
            card,
            text=self._tr("btn_clear_logs"),
            underline=0,
            style="Glass.TButton",
            command=self._clear_logs,
        )
        self._btn_clear.grid(row=2, column=2, sticky="e")

    def _normalize_bind_target_key(self, key: object) -> str:
        raw = str(key or "").strip()
        if raw.lower() == self._BIND_TARGET_AUTO:
            return self._BIND_TARGET_AUTO
        if raw.startswith("if:") and len(raw) > 3:
            return raw
        return self._BIND_TARGET_AUTO

    def _selected_bind_interface_alias(self) -> str | None:
        selected = self._normalize_bind_target_key(self._bind_target_var.get())
        if selected == self._BIND_TARGET_AUTO:
            return None
        if selected.startswith("if:"):
            return selected[3:]
        return None

    def _on_refresh_bind_targets(self) -> None:
        if self._is_busy():
            self._set_status(self._tr("status_wait_current_operation"), "warning")
            return
        self._refresh_bind_targets(log_change=True)

    def _on_bind_target_changed(self) -> None:
        self._bind_target_var.set(self._normalize_bind_target_key(self._bind_target_var.get()))
        self._update_bind_hint()
        self._schedule_save()

    def _refresh_bind_targets(self, log_change: bool) -> None:
        choices: list[tuple[str, str]] = [(self._BIND_TARGET_AUTO, self._tr("bind_option_auto"))]
        try:
            interfaces = self._controller.list_airplay_interfaces()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[ADVERTENCIA] {self._tr('warn_bind_targets_refresh_failed', error=exc)}")
            self._set_status(self._tr("warn_bind_targets_refresh_failed", error=exc), "warning")
            self._bind_target_choices = choices
            self._rebuild_bind_target_radios()
            return

        for adapter_name, mac in interfaces:
            key = f"if:{adapter_name}"
            label = self._tr("bind_option_adapter", name=adapter_name, mac=mac)
            choices.append((key, label))

        self._bind_target_choices = choices
        self._rebuild_bind_target_radios()
        if log_change:
            self._append_log(self._tr("hint_bind_targets_refreshed"))
            self._set_status(self._tr("status_bind_targets_refreshed"), "success")

    def _rebuild_bind_target_radios(self) -> None:
        for child in self._bind_targets_frame.winfo_children():
            child.destroy()

        valid_keys = {key for key, _label in self._bind_target_choices}
        current_key = self._normalize_bind_target_key(self._bind_target_var.get())
        if current_key not in valid_keys:
            current_key = self._BIND_TARGET_AUTO if self._BIND_TARGET_AUTO in valid_keys else next(iter(valid_keys))
        self._bind_target_var.set(current_key)

        self._bind_target_buttons = []
        for row, (key, label) in enumerate(self._bind_target_choices):
            radio = ttk.Radiobutton(
                self._bind_targets_frame,
                text=label,
                style="Card.TRadiobutton",
                variable=self._bind_target_var,
                value=key,
                command=self._on_bind_target_changed,
            )
            radio.grid(row=row, column=0, sticky="w", pady=(0, 2))
            self._bind_target_buttons.append(radio)

        self._update_bind_hint()

    def _update_bind_hint(self) -> None:
        selected = self._normalize_bind_target_key(self._bind_target_var.get())
        if selected == self._BIND_TARGET_AUTO:
            self._bind_hint_var.set(self._tr("bind_hint_auto"))
            return

        label_map = {key: label for key, label in self._bind_target_choices}
        selected_label = label_map.get(selected, selected[3:] if selected.startswith("if:") else selected)
        self._bind_hint_var.set(self._tr("bind_hint_selected", interface=selected_label))

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

    def _start_intro_animation(self) -> None:
        try:
            self._intro_alpha = 0.92
            self._root.attributes("-alpha", self._intro_alpha)
        except tk.TclError:
            self._intro_alpha = 1.0
        self._animate_chrome()

    def _animate_chrome(self) -> None:
        if self._closing:
            return

        self._animation_phase += 0.18
        glow = 0.5 + 0.5 * math.sin(self._animation_phase)

        header_color = self._mix_color("#64d8ff", "#22d3ee", glow)
        self._header_label.configure(foreground=header_color)

        idle_bg = self._mix_color("#12253d", "#173057", glow * 0.45)
        idle_ring = self._mix_color("#345a86", "#2f7db3", glow * 0.45)
        if not (self._controller.is_running() and self._selected_device_mode() == DEVICE_MODE_IPHONE):
            self._pill_receiver.configure(bg=idle_bg, highlightbackground=idle_ring)
        if not self._controller.is_recording():
            self._pill_record.configure(bg=idle_bg, highlightbackground=idle_ring)

        if self._intro_alpha < 1.0:
            self._intro_alpha = min(1.0, self._intro_alpha + 0.02)
            try:
                self._root.attributes("-alpha", self._intro_alpha)
            except tk.TclError:
                self._intro_alpha = 1.0

        self._animation_after_id = self._root.after(90, self._animate_chrome)

    def _mix_color(self, c1: str, c2: str, t: float) -> str:
        ratio = max(0.0, min(1.0, t))
        r1, g1, b1 = (int(c1[i : i + 2], 16) for i in (1, 3, 5))
        r2, g2, b2 = (int(c2[i : i + 2], 16) for i in (1, 3, 5))
        r = round(r1 + (r2 - r1) * ratio)
        g = round(g1 + (g2 - g1) * ratio)
        b = round(b1 + (b2 - b1) * ratio)
        return f"#{r:02x}{g:02x}{b:02x}"

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

    def _on_language_selected(self, language_code: str) -> None:
        normalized = normalize_language(language_code)
        if normalized == self._language_code:
            self._language_var.set(self._language_code)
            return
        self._language_code = normalized
        self._language_var.set(self._language_code)
        self._rebuild_ui_for_language()
        self._append_log(self._tr("lang_changed_log", language_label=LANGUAGE_LABELS[self._language_code]))
        self._schedule_save()

    def _rebuild_ui_for_language(self) -> None:
        current_geometry = self._root.geometry()
        self._release_embedded_window()
        source_mode = self._capture_source_mode_from_value(self._capture_source_var.get())
        self._capture_source_var.set(self._capture_source_label(source_mode))
        self._device_hint_var.set(self._device_mode_description(self._selected_device_mode()))

        if self._animation_after_id is not None:
            self._root.after_cancel(self._animation_after_id)
            self._animation_after_id = None
        if self._about_dialog is not None and self._about_dialog.winfo_exists():
            self._about_dialog.destroy()
        self._about_dialog = None

        for child in self._root.winfo_children():
            child.destroy()

        self._build_ui(announce_init=False)
        self._refresh_bind_targets(log_change=False)
        self._apply_saved_geometry(current_geometry)
        self._restore_log_history()
        self._apply_device_mode_ui(log_change=False)
        self._set_running_state(self._controller.is_running())
        self._set_record_state(self._controller.is_recording())
        self._set_status(self._tr("status_ready", app_name=self._app_name, version=self._app_version), "info")
        self._start_intro_animation()

    def _restore_log_history(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        for line, tag in self._log_history:
            if tag:
                self._log_box.insert("end", line, tag)
            else:
                self._log_box.insert("end", line)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

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
            self._bind_target_var,
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
        capture_source_mode = self._capture_source_mode_from_value(self._capture_source_var.get())
        data = {
            "receiver_name": self._receiver_name_var.get().strip(),
            "uxplay_path": self._uxplay_path_var.get().strip(),
            "profile_label": self._profile_var.get(),
            "profile_key": profile_key,
            "custom_args": self._custom_args_var.get(),
            "append_hostname_suffix": bool(self._append_hostname_suffix_var.get()),
            "capture_source_label": self._capture_source_var.get(),
            "capture_source_mode": capture_source_mode,
            "capture_window_title": self._capture_title_var.get(),
            "capture_fps": int(self._capture_fps_var.get()),
            "device_mode": self._selected_device_mode(),
            "bind_target": self._normalize_bind_target_key(self._bind_target_var.get()),
            "language": self._language_code,
            "window_geometry": self._root.geometry(),
        }
        try:
            self._store.save(data)
        except OSError as exc:
            self._append_log(self._tr("warn_save_config_failed", error=exc))
            self._set_status(self._tr("status_save_config_failed", error=exc), "warning")

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
        return max(15, min(60, n))

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
            self._append_log(self._tr("log_switch_to_android"))
            self._run_in_background(
                action_label=self._tr("action_stop_receiver_iphone"),
                fn=self._controller.stop_receiver,
                success_status=self._tr("success_stop_receiver_iphone"),
            )

        self._apply_device_mode_ui(log_change=True)
        if mode == DEVICE_MODE_ANDROID:
            self._run_in_background(
                action_label=self._tr("action_diagnose_android"),
                fn=self._diagnose_android_mode,
                success_status=self._tr("success_diagnose_android"),
                busy=False,
            )

        self._schedule_save()

    def _apply_device_mode_ui(self, log_change: bool) -> None:
        mode = self._selected_device_mode()
        self._device_hint_var.set(self._device_mode_description(mode))

        if mode == DEVICE_MODE_IPHONE:
            self._combo_profile.configure(state="readonly")
            self._entry_args.configure(state="normal")
            self._btn_refresh_bind_targets.configure(state="normal")
            for radio in self._bind_target_buttons:
                radio.configure(state="normal")
            if log_change:
                self._append_log(self._tr("log_mode_iphone"))
                self._set_status(self._tr("status_mode_iphone"), "info")
            if not self._controller.is_running():
                self._btn_receiver.configure(text=self._tr("btn_start_receiver"), underline=0, style="Primary.TButton")
            self._set_running_state(self._controller.is_running())
            return

        self._combo_profile.configure(state="disabled")
        self._entry_args.configure(state="disabled")
        self._btn_refresh_bind_targets.configure(state="disabled")
        for radio in self._bind_target_buttons:
            radio.configure(state="disabled")
        self._btn_receiver.configure(text=self._tr("btn_open_android_projection"), underline=0, style="Primary.TButton")
        self._receiver_status_var.set(self._tr("receiver_status_android"))
        self._pill_receiver.configure(bg="#1f2f4b", fg="#9fc9ff", highlightbackground="#365c8f")
        if log_change:
            self._append_log(self._tr("log_mode_android"))
            self._set_status(self._tr("status_mode_android"), "info")

    def _on_profile_changed(self, _event: tk.Event[ttk.Combobox]) -> None:
        profile = self._profiles_by_label.get(self._profile_var.get())
        if profile:
            self._profile_desc_var.set(profile.description)
        self._schedule_save()

    def _browse_uxplay(self) -> None:
        if self._is_busy():
            self._set_status(self._tr("status_wait_current_operation"), "warning")
            return

        selected = filedialog.askopenfilename(
            title=self._tr("title_pick_uxplay"),
            filetypes=[("Ejecutable", "*.exe"), ("Todos los archivos", "*.*")],
        )
        if selected:
            self._uxplay_path_var.set(selected)
            self._set_status(self._tr("status_uxplay_path_updated"), "success")
            self._schedule_save()

    def _toggle_receiver(self) -> None:
        if self._is_busy():
            self._set_status(self._tr("status_operation_in_progress"), "warning")
            return

        if self._selected_device_mode() == DEVICE_MODE_ANDROID:
            self._open_android_projection()
            return

        if self._controller.is_running():
            self._run_in_background(
                action_label=self._tr("action_stop_receiver"),
                fn=self._controller.stop_receiver,
                success_status=self._tr("success_receiver_stopped"),
            )
            return

        uxplay_path = self._uxplay_path_var.get().strip()
        if not uxplay_path:
            self._append_log(f"[ERROR] {self._tr('error_missing_uxplay_path')}")
            self._set_status(self._tr("error_missing_uxplay_path"), "error")
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
        self._append_log(
            self._tr(
                "profile_args_log",
                profile_label=profile.label,
                args=" ".join(args) if args else self._tr("args_none"),
            )
        )

        def start_receiver() -> None:
            self._controller.start_receiver(
                Path(uxplay_path),
                name,
                extra_args=args,
                append_hostname_suffix=bool(self._append_hostname_suffix_var.get()),
                preferred_interface_alias=self._selected_bind_interface_alias(),
            )

        suffix_mode = self._tr("suffix_with_host") if self._append_hostname_suffix_var.get() else self._tr(
            "suffix_without_host"
        )
        self._run_in_background(
            action_label=self._tr("action_start_receiver"),
            fn=start_receiver,
            success_status=self._tr("success_start_receiver", name=name, suffix_mode=suffix_mode),
        )

    def _auto_start_receiver_on_launch(self) -> None:
        if self._closing or self._startup_receiver_requested:
            return
        self._startup_receiver_requested = True

        if self._selected_device_mode() != DEVICE_MODE_IPHONE:
            return
        if self._controller.is_running():
            return

        uxplay_path = Path(self._uxplay_path_var.get().strip())
        if not uxplay_path.exists():
            return

        self._append_log(self._tr("hint_autostart_receiver"))
        self._toggle_receiver()

    def _open_android_projection(self) -> None:
        self._run_in_background(
            action_label=self._tr("action_open_android_projection"),
            fn=self._controller.open_android_projection_portal,
            success_status=self._tr("success_open_android_projection"),
        )

    def _diagnose_android_mode(self) -> None:
        diag = self._controller.get_android_projection_diagnostics()
        self._root.after(0, lambda d=diag: self._apply_android_diagnostics(d))

    def _apply_android_diagnostics(self, diag: dict[str, str | bool | None]) -> None:
        capability = str(diag.get("wireless_display_capability", "Unknown")).strip()
        line = str(diag.get("miracast_status_line", "")).strip()
        supported = diag.get("miracast_receiver_supported")

        if capability and capability != "Unknown":
            self._append_log(self._tr("hint_wireless_display", capability=capability))
        if line:
            self._append_log(self._tr("hint_miracast_diag", line=line))

        if capability == "NotPresent":
            self._append_log(self._tr("warn_wireless_display_missing"))
            self._set_status(self._tr("status_install_wireless_display"), "warning")
            return

        if capability.startswith("Unknown ("):
            self._append_log(f"[ADVERTENCIA] {capability}")
            self._set_status(capability, "warning")

        if supported is False:
            self._append_log(self._tr("warn_miracast_not_supported"))
            self._set_status(self._tr("status_miracast_not_supported"), "warning")
            return

        self._append_log(self._tr("hint_android_ready"))
        self._set_status(self._tr("status_android_ready"), "success")

    def _take_snapshot(self) -> None:
        if self._is_busy():
            self._set_status(self._tr("status_operation_in_progress"), "warning")
            return

        if not self._controller.is_running():
            self._append_log(self._tr("hint_receiver_stopped_snapshot"))
            self._set_status(self._tr("status_receiver_stopped_snapshot"), "warning")

        path = filedialog.asksaveasfilename(
            title=self._tr("title_save_snapshot"),
            defaultextension=".png",
            filetypes=[("Imagen PNG", "*.png"), ("Imagen JPEG", "*.jpg"), ("Todos los archivos", "*.*")],
            initialfile=self._tr("initial_snapshot_name", timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")),
        )
        if not path:
            return

        def do_snapshot() -> None:
            self._controller.take_snapshot(
                uxplay_path=Path(self._uxplay_path_var.get().strip()),
                output_path=Path(path),
                source_mode=self._capture_source_mode_from_value(self._capture_source_var.get()),
                window_title=self._capture_title_var.get().strip(),
            )

        self._run_in_background(
            action_label=self._tr("action_take_snapshot"),
            fn=do_snapshot,
            success_status=self._tr("success_snapshot"),
        )

    def _toggle_recording(self) -> None:
        if self._is_busy():
            self._set_status(self._tr("status_operation_in_progress"), "warning")
            return

        if self._controller.is_recording():
            save_path = self._prompt_recording_output_path()
            if save_path is None:
                self._set_status(self._tr("warn_choose_recording_path"), "warning")
                return

            self._record_stop_requested = True

            def stop_recording() -> None:
                self._controller.stop_recording(output_path=save_path)

            self._run_in_background(
                action_label=self._tr("action_stop_recording"),
                fn=stop_recording,
                success_status=self._tr("success_stop_recording"),
                busy=False,
            )
            return

        if not self._controller.is_running() and self._selected_device_mode() == DEVICE_MODE_IPHONE:
            self._set_status(self._tr("status_start_receiver_before_record"), "warning")
            return

        if self._selected_device_mode() != DEVICE_MODE_IPHONE:
            self._set_status(self._tr("status_recording_only_iphone"), "warning")
            return

        started_at = datetime.now()
        self._record_suggested_name = f"grabacion_{started_at.strftime('%Y%m%d_%H%M%S')}.mp4"
        self._record_stop_requested = False
        temp_path = Path(tempfile.gettempdir()) / f"ScreenMirrorIOSAndroid_{started_at.strftime('%Y%m%d_%H%M%S_%f')}.mp4"

        capture_window_source = self._capture_title_var.get().strip()
        if os.name == "nt" and self._embedded_window_hwnd is not None:
            capture_window_source = f"hwnd=0x{self._embedded_window_hwnd:X}"
            self._append_log("[PISTA] Grabacion enfocada al panel integrado de previsualizacion.")

        def start_recording() -> None:
            self._controller.start_recording(
                uxplay_path=Path(self._uxplay_path_var.get().strip()),
                output_path=temp_path,
                source_mode="window",
                window_title=capture_window_source,
                fps=int(self._capture_fps_var.get()),
            )

        def start_recording() -> None:
            # if we computed an explicit region, ask the controller to use it
            self._controller.start_recording(
                uxplay_path=Path(self._uxplay_path_var.get().strip()),
                output_path=temp_path,
                source_mode="desktop" if capture_region is not None else "window",
                window_title=capture_window_source,
                fps=int(self._capture_fps_var.get()),
                capture_region=capture_region,
            )

        self._run_in_background(
            action_label=self._tr("action_start_recording"),
            fn=start_recording,
            success_status=self._tr("success_start_recording"),
            busy=False,
        )

    def _suggest_recording_name(self) -> str:
        if self._record_suggested_name:
            return self._record_suggested_name
        return self._tr("initial_record_name", timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"))

    def _default_recording_output_path(self) -> Path:
        documents = Path.home() / "Documents"
        base_dir = documents if documents.exists() else Path.cwd()
        return base_dir / self._suggest_recording_name()

    def _prompt_recording_output_path(self) -> Path | None:
        selected = filedialog.asksaveasfilename(
            title=self._tr("title_save_recording"),
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
            raise ValueError(self._tr("error_invalid_extra_args", error=exc)) from exc
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
                        action_label=self._tr("action_stop_recording_due_receiver_stop"),
                        fn=stop_due_to_receiver_stop,
                        success_status=self._tr("success_stop_recording_due_receiver_stop", path=auto_save_path),
                        busy=False,
                    )
            elif event.kind == "recording":
                if self._recording_was_active and not event.recording:
                    if self._record_stop_requested:
                        self._record_stop_requested = False
                    else:
                        recovered_path = self._default_recording_output_path()
                        self._append_log(
                            "[ADVERTENCIA] La grabacion se detuvo por desconexion/cambio de receptor. "
                            "Se intentara guardar automaticamente."
                        )

                        def recover_recording_file() -> None:
                            self._controller.stop_recording(output_path=recovered_path)

                        self._run_in_background(
                            action_label=self._tr("action_stop_recording_due_receiver_stop"),
                            fn=recover_recording_file,
                            success_status=self._tr(
                                "success_stop_recording_due_receiver_stop",
                                path=recovered_path,
                            ),
                            busy=False,
                        )
                self._set_record_state(event.recording)

        self._root.after(self.POLL_INTERVAL_MS, self._poll_events)

    def _inspect_log(self, message: str) -> None:
        low = message.lower()
        if message.startswith("Iniciando receptor:"):
            self._ntp_error_count = 0
            self._ntp_hint_shown = False
        if "initialized server socket(s)" in low:
            self._set_status(self._tr("status_receiver_ready_connect"), "success")
        if "raop_rtp_mirror starting mirroring" in low:
            self._set_status(self._tr("status_airplay_connected"), "info")
            self._schedule_embed_window("[PISTA] Intentando acoplar ventana de video al panel integrado...")
        if "begin streaming to gstreamer video pipeline" in low:
            self._schedule_embed_window()
        if "reiniciando receptor automáticamente" in low or "reiniciando receptor automaticamente" in low:
            self._set_status(self._tr("status_recovering_first_link"), "warning")
        if "receptor reiniciado automáticamente" in low or "receptor reiniciado automaticamente" in low:
            self._set_status(self._tr("status_receiver_recovered"), "success")
        if "gstreamer error: output window was closed" in low:
            self._append_log(self._tr("warn_uxplay_window_closed"))
            self._set_status(self._tr("status_uxplay_window_closed"), "warning")
        if "invalid ntp_time < gst_video_pipeline_base_time" in low:
            self._ntp_error_count += 1
        if self._ntp_error_count >= 8 and not self._ntp_hint_shown:
            self._ntp_hint_shown = True
            self._append_log(self._tr("hint_ntp_skew"))
            self._set_status(self._tr("status_ntp_skew"), "warning")

    def _on_preview_host_resized(self, _event: tk.Event[tk.Misc]) -> None:
        self._resize_embedded_window()
        # nothing special to do when resizing; overlay window is repositioned elsewhere

    # previous region-tracking facility removed; recording now locks to window handle

    def _schedule_embed_window(self, announce_log: str | None = None) -> None:
        if os.name != "nt":
            return
        if self._selected_device_mode() != DEVICE_MODE_IPHONE:
            return
        if not self._controller.is_running():
            return
        if announce_log:
            self._append_log(announce_log)

        self._embed_retry_left = max(self._embed_retry_left, self._EMBED_MAX_RETRIES)
        if self._embed_retry_after_id is None:
            self._embed_retry_after_id = self._root.after(120, self._attempt_embed_window)

    def _attempt_embed_window(self) -> None:
        self._embed_retry_after_id = None
        if self._closing:
            return
        if os.name != "nt":
            return
        if self._selected_device_mode() != DEVICE_MODE_IPHONE or not self._controller.is_running():
            return

        if self._embedded_window_hwnd is not None:
            try:
                user32 = ctypes.windll.user32
                if user32.IsWindow(self._embedded_window_hwnd):
                    self._resize_embedded_window()
                    self._preview_overlay.place_forget()
                    self._set_preview_hint(self._tr("preview_hint_embedded"))
                    return
            except (AttributeError, OSError):
                pass

        target_hwnd = self._find_uxplay_preview_window()
        if target_hwnd is not None and self._embed_window_into_preview(target_hwnd):
            self._append_log("[PISTA] Ventana de iPhone acoplada al panel integrado de la app.")
            return

        self._embed_retry_left = max(0, self._embed_retry_left - 1)
        if self._embed_retry_left > 0:
            self._embed_retry_after_id = self._root.after(self._EMBED_RETRY_DELAY_MS, self._attempt_embed_window)
            return

        self._set_preview_hint(self._tr("preview_hint_not_found"))

    def _find_uxplay_preview_window(self) -> int | None:
        if os.name != "nt":
            return None

        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return None

        process_ids = set(self._controller.list_receiver_process_ids())
        if not process_ids:
            return None

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        candidates: list[tuple[int, int]] = []

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if hwnd <= 0:
                return True
            if not user32.IsWindow(hwnd):
                return True

            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            pid = int(owner_pid.value)
            if pid not in process_ids:
                return True

            title_len = int(user32.GetWindowTextLengthW(hwnd))
            if title_len <= 0:
                return True

            title_buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
            title = title_buf.value.strip()
            if not title:
                return True

            low = title.lower()
            score = 0
            if "direct3d11 renderer" in low:
                score = 30
            elif "renderer" in low:
                score = 20
            elif "uxplay" in low:
                score = 10
            if score <= 0:
                return True

            candidates.append((score, int(hwnd)))
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except (AttributeError, OSError):
            return None

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _embed_window_into_preview(self, hwnd: int) -> bool:
        # Overlay the uxplay video window on top of our preview frame instead
        # of reparenting it.  Keeping it top-level ensures that ffmpeg's
        # gdigrab input can still capture the window contents (child windows are
        # ignored).  We simply remove decorations and move the window to match
        # the frame's screen coordinates.
        if os.name != "nt":
            return False
        if hwnd <= 0:
            return False

        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return False

        if not user32.IsWindow(hwnd):
            return False

        # if a different preview window was already attached, restore it first
        if self._embedded_window_hwnd is not None and self._embedded_window_hwnd != hwnd:
            self._release_embedded_window()

        GWL_STYLE = -16
        WS_VISIBLE = 0x10000000
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_SYSMENU = 0x00080000
        WS_POPUP = 0x80000000

        # record original style so we can restore later
        original_style = int(user32.GetWindowLongW(hwnd, GWL_STYLE))

        # create a borderless popup style
        new_style = original_style
        new_style &= ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        new_style |= WS_POPUP | WS_VISIBLE

        user32.SetWindowLongW(hwnd, GWL_STYLE, new_style)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW

        self._embedded_window_hwnd = hwnd
        self._embedded_original_style = original_style
        self._preview_overlay.place_forget()
        self._set_preview_hint(self._tr("preview_hint_embedded"))
        self._content_tabs.select(self._preview_tab)
        self._resize_embedded_window()
        return True

    def _resize_embedded_window(self) -> None:
        # reposition/resize the overlay window to exactly cover the preview
        # host frame.  This runs frequently during animations and when the main
        # window moves.
        if os.name != "nt":
            return
        hwnd = self._embedded_window_hwnd
        if hwnd is None:
            return

        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return

        if not user32.IsWindow(hwnd):
            self._embedded_window_hwnd = None
            self._preview_overlay.place(relx=0.5, rely=0.5, anchor="center")
            self._set_preview_hint(self._tr("preview_hint_wait_stream"))
            return

        # compute absolute screen coordinates of the preview frame
        x = self._preview_host_frame.winfo_rootx()
        y = self._preview_host_frame.winfo_rooty()
        width = max(64, int(self._preview_host_frame.winfo_width()))
        height = max(64, int(self._preview_host_frame.winfo_height()))
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE)

    def _release_embedded_window(self) -> None:
        if self._embed_retry_after_id is not None:
            self._root.after_cancel(self._embed_retry_after_id)
            self._embed_retry_after_id = None
        self._embed_retry_left = 0

        hwnd = self._embedded_window_hwnd
        self._embedded_window_hwnd = None

        if os.name == "nt" and hwnd is not None:
            try:
                user32 = ctypes.windll.user32
                if user32.IsWindow(hwnd):
                    # restore original style so the video window behaves normally
                    GWL_STYLE = -16
                    if self._embedded_original_style is not None:
                        user32.SetWindowLongW(hwnd, GWL_STYLE, int(self._embedded_original_style))
                        user32.SetWindowPos(
                            hwnd,
                            0,
                            0,
                            0,
                            0,
                            0,
                            0x0027,  # SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_FRAMECHANGED
                        )
                    # move it offscreen so it doesn't interfere visually
                    user32.SetWindowPos(hwnd, 0, -10000, -10000, 0, 0, 0x0001)
            except Exception:
                pass
        self._preview_overlay.place(relx=0.5, rely=0.5, anchor="center")
        self._set_preview_hint(self._tr("preview_hint_wait_stream"))

    def _set_preview_hint(self, message: str) -> None:
        self._preview_hint_var.set(message)

    def _set_running_state(self, running: bool) -> None:
        if self._selected_device_mode() == DEVICE_MODE_ANDROID:
            self._release_embedded_window()
            self._receiver_status_var.set(self._tr("receiver_status_android"))
            self._btn_receiver.configure(text=self._tr("btn_open_android_projection"), underline=0, style="Primary.TButton")
            self._pill_receiver.configure(bg="#1d304d", fg="#9fc9ff", highlightbackground="#365c8f")
            self._set_preview_hint(self._tr("preview_hint_android"))
            return

        if running:
            self._receiver_status_var.set(self._tr("receiver_status_running"))
            self._btn_receiver.configure(text=self._tr("btn_stop_receiver"), underline=0, style="Danger.TButton")
            self._pill_receiver.configure(bg="#103026", fg="#72f2de", highlightbackground="#18c3ae")
            self._set_status(self._tr("status_receiver_running"), "success")
            self._set_preview_hint(self._tr("preview_hint_wait_stream"))
            self._schedule_embed_window()
            return

        self._release_embedded_window()
        self._receiver_status_var.set(self._tr("receiver_status_stopped"))
        self._btn_receiver.configure(text=self._tr("btn_start_receiver"), underline=0, style="Primary.TButton")
        self._pill_receiver.configure(bg="#2b1620", fg="#ff8aa0", highlightbackground="#cc2a56")
        self._set_status(self._tr("status_receiver_stopped_short"), "info")
        self._set_preview_hint(self._tr("preview_hint_idle"))

    def _set_record_state(self, recording: bool) -> None:
        if recording:
            self._record_status_var.set(self._tr("record_status_active"))
            self._btn_record.configure(text=self._tr("btn_stop_recording"), underline=0, style="Danger.TButton")
            self._pill_record.configure(bg="#3a1424", fg="#ff8cad", highlightbackground="#cf2f61")
            self._set_status(self._tr("status_recording_running"), "success")
            self._recording_was_active = True
            return

        self._record_suggested_name = None
        self._record_status_var.set(self._tr("record_status_inactive"))
        self._btn_record.configure(text=self._tr("btn_record"), underline=0, style="Danger.TButton")
        self._pill_record.configure(bg="#1d304d", fg="#9fc9ff", highlightbackground="#365c8f")
        self._recording_was_active = False

    def _append_log(self, message: str) -> None:
        localized_message = localize_log(self._language_code, message)
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {localized_message}\n"
        low = localized_message.lower()
        tag = ""
        if "error" in low:
            tag = "error"
            self._set_status(localized_message, "error")
        elif "advertencia" in low or "warning" in low:
            tag = "warning"
            self._set_status(localized_message, "warning")
        elif "pista" in low or "hint" in low:
            tag = "hint"
        elif "grabación" in low or "grabacion" in low or "recording" in low or localized_message.startswith("[REC]"):
            tag = "recording"

        self._log_box.configure(state="normal")
        if tag:
            self._log_box.insert("end", line, tag)
        else:
            self._log_box.insert("end", line)
        self._log_history.append((line, tag))
        if len(self._log_history) > 800:
            self._log_history = self._log_history[-800:]
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _clear_logs(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")
        self._log_history.clear()
        self._set_status(self._tr("logs_cleared"), "info")

    def _show_about_dialog(self) -> None:
        if self._about_dialog is not None and self._about_dialog.winfo_exists():
            self._about_dialog.focus_force()
            return

        year = datetime.now().year
        text = self._tr("about_text", version=self._app_version, year=year)

        dialog = tk.Toplevel(self._root)
        dialog.title(self._tr("about_title"))
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
        ttk.Button(container, text=self._tr("about_close"), command=dialog.destroy).pack(anchor="e")

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._about_dialog = dialog

    def _set_status(self, message: str, level: str) -> None:
        clean = " ".join(message.strip().split())
        if not clean:
            clean = self._tr("status_default")

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
        self._set_status(self._tr("status_action_in_progress", action_label=action_label), "info")

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
        self._set_status(self._tr("status_action_failed", action_label=action_label, error=exc), "error")

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
        self._btn_refresh_bind_targets.configure(state=state)
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
            for radio in self._bind_target_buttons:
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
        if self._animation_after_id is not None:
            self._root.after_cancel(self._animation_after_id)
            self._animation_after_id = None
        if self._save_after_id is not None:
            self._root.after_cancel(self._save_after_id)
            self._save_after_id = None
        self._release_embedded_window()
        self._save_config()
        self._set_status(self._tr("status_app_closing"), "info")

        def shutdown() -> None:
            try:
                self._controller.shutdown()
            finally:
                self._root.after(0, self._root.destroy)

        threading.Thread(target=shutdown, daemon=True).start()
