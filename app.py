from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk

from backend.config import AppConfig
from backend.controller import AppController
from backend.logging_config import configure_logging
from backend.versioning import read_or_create_version
from frontend.main_window import MainWindow


def _resolve_paths() -> tuple[Path, Path]:
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir)).resolve()
        return app_dir, bundle_dir

    app_dir = Path(__file__).resolve().parent
    return app_dir, app_dir


def main() -> None:
    app_dir, bundle_dir = _resolve_paths()
    config = AppConfig()
    version_info = read_or_create_version(config.resolve_version_path(app_dir=app_dir))
    configure_logging(config.resolve_log_path(app_dir=app_dir), config.log_level, enable_console=False)

    controller = AppController()
    root = tk.Tk()
    MainWindow(
        root=root,
        controller=controller,
        app_name=config.app_name,
        app_version=version_info.version,
        default_receiver_name=config.default_receiver_name,
        default_uxplay_path=config.resolve_default_uxplay_path(app_dir=app_dir, bundle_dir=bundle_dir),
        default_profile_key=config.default_profile_key,
        default_device_mode_key=config.default_device_mode_key,
        gui_config_path=config.resolve_gui_config_path(app_dir=app_dir),
    )
    root.mainloop()


if __name__ == "__main__":
    main()
