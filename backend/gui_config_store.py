from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


class GuiConfigStore:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path

    def load(self) -> dict[str, Any]:
        if not self._config_path.exists():
            return {}

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}

        if isinstance(data, dict):
            return data
        return {}

    def save(self, data: dict[str, Any]) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(self._config_path.parent),
            suffix=".tmp",
        ) as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp_file.name)

        tmp_path.replace(self._config_path)
