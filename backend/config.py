from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from backend.device_modes import DEFAULT_DEVICE_MODE_KEY
from backend.receiver_profiles import DEFAULT_PROFILE_KEY


@dataclass(frozen=True, slots=True)
class AppConfig:
    app_name: str = "ScreenMirroringIOSAndroid"
    default_receiver_name: str = "ScreenMirroringIOSAndroid"
    default_uxplay_relative_path: str = "tools/uxplay/bin/uxplay.exe"
    default_profile_key: str = DEFAULT_PROFILE_KEY
    default_device_mode_key: str = DEFAULT_DEVICE_MODE_KEY
    gui_config_filename: str = "config.json"
    version_filename: str = "version.json"
    log_filename: str = "log.txt"
    log_level: str = "INFO"

    def resolve_default_uxplay_path(self, app_dir: Path, bundle_dir: Path | None = None) -> Path:
        candidates = list(self._candidate_uxplay_paths(app_dir=app_dir, bundle_dir=bundle_dir))

        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()

        # Return the first expected location as a deterministic fallback.
        return candidates[0].resolve()

    def _candidate_uxplay_paths(self, app_dir: Path, bundle_dir: Path | None = None) -> Iterable[Path]:
        seen: set[str] = set()

        def add(path: Path) -> Iterable[Path]:
            key = str(path).lower()
            if key in seen:
                return []
            seen.add(key)
            return [path]

        paths: list[Path] = []
        paths.extend(add((app_dir / self.default_uxplay_relative_path)))
        paths.extend(add((app_dir / "tools/uxplay/uxplay.exe")))
        paths.extend(add((app_dir / "uxplay.exe")))

        if bundle_dir is not None:
            paths.extend(add((bundle_dir / self.default_uxplay_relative_path)))
            paths.extend(add((bundle_dir / "tools/uxplay/uxplay.exe")))
            paths.extend(add((bundle_dir / "uxplay.exe")))

        return paths

    def resolve_gui_config_path(self, app_dir: Path) -> Path:
        return (app_dir / self.gui_config_filename).resolve()

    def resolve_version_path(self, app_dir: Path) -> Path:
        return (app_dir / self.version_filename).resolve()

    def resolve_log_path(self, app_dir: Path) -> Path:
        return (app_dir / self.log_filename).resolve()
