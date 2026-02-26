from __future__ import annotations

import locale
import os
import subprocess
import unicodedata
from typing import Callable

LogCallback = Callable[[str], None]


class WirelessProjectionService:
    def __init__(self, on_log: LogCallback | None = None) -> None:
        self._on_log = on_log

    def open_android_projection_portal(self) -> None:
        diagnostics = self.get_diagnostics()
        capability = diagnostics["wireless_display_capability"]
        miracast_supported = diagnostics["miracast_receiver_supported"]
        line = diagnostics["miracast_status_line"]

        self._emit_log("Modo Android seleccionado: Proyección inalámbrica (Miracast).")
        if capability != "Unknown":
            self._emit_log(f"Estado de característica Wireless Display: {capability}")
        if line:
            self._emit_log(f"Diagnóstico Miracast: {line}")

        if capability == "NotPresent":
            self._open_optional_features()
            raise RuntimeError(
                "La característica opcional 'Wireless Display' no está instalada. "
                "Se abrió Configuración para instalarla."
            )

        if miracast_supported is False:
            raise RuntimeError(
                "Este equipo no admite recibir Miracast, por eso el teléfono Android no lo encuentra. "
                "Revisa drivers Wi-Fi/GPU, adaptador compatible con Wi-Fi Direct o usa alternativa por cable/ADB."
            )

        launch_errors: list[str] = []
        for launcher in (self._open_projection_settings, self._open_projection_fallback_display_settings):
            try:
                launcher()
                self._emit_log("Se abrió la configuración de proyección de Windows.")
                self._emit_log(
                    "[PISTA] En Android: Cast / Smart View / Proyección inalámbrica y selecciona este equipo."
                )
                return
            except Exception as exc:  # noqa: BLE001
                launch_errors.append(str(exc))

        error_text = "; ".join(launch_errors) if launch_errors else "Error desconocido al abrir configuración."
        raise RuntimeError(f"No se pudo abrir la configuración de Proyección inalámbrica. {error_text}")

    def get_diagnostics(self) -> dict[str, str | bool | None]:
        capability_state = self._probe_wireless_display_capability()
        miracast_line = self._probe_miracast_status_line()
        miracast_supported = self._parse_miracast_supported(miracast_line)

        return {
            "wireless_display_capability": capability_state,
            "miracast_status_line": miracast_line or "",
            "miracast_receiver_supported": miracast_supported,
        }

    def _open_projection_settings(self) -> None:
        self._open_uri("ms-settings:project")

    def _open_projection_fallback_display_settings(self) -> None:
        self._open_uri("ms-settings:display")

    def _open_optional_features(self) -> None:
        self._open_uri("ms-settings:optionalfeatures")
        self._emit_log(
            "[PISTA] Instala 'Wireless Display' y reinicia para habilitar la recepción de proyección."
        )

    def _open_uri(self, uri: str) -> None:
        creationflags = self._creationflags()
        subprocess.Popen(
            ["explorer.exe", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=self._startupinfo(),
        )

    def _probe_wireless_display_capability(self) -> str:
        command = (
            "(Get-WindowsCapability -Online -Name 'App.WirelessDisplay.Connect*' "
            "| Select-Object -First 1 -ExpandProperty State)"
        )
        try:
            result = self._run_command(
                ["powershell", "-NoProfile", "-Command", command],
                timeout=12,
            )
        except Exception:  # noqa: BLE001
            return "Unknown"

        if result.returncode != 0:
            error_text = self._normalize_text(result.stderr or result.stdout)
            if "requiere elevacion" in error_text or "requires elevation" in error_text:
                return "Unknown (se requiere ejecutar como administrador para consultarlo)"
            return "Unknown"

        state = self._sanitize_text(result.stdout).strip()
        return state or "Unknown"

    def _probe_miracast_status_line(self) -> str:
        try:
            result = self._run_command(
                ["netsh", "wlan", "show", "drivers"],
                timeout=12,
            )
        except Exception:  # noqa: BLE001
            return ""

        if result.returncode != 0:
            return ""

        text = self._sanitize_text(result.stdout)
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        for line in lines:
            low = self._normalize_text(line)
            if (
                "monitor inalambrico admitido" in low
                or "wireless display supported" in low
                or "miracast" in low
            ):
                return line
        return ""

    def _parse_miracast_supported(self, line: str) -> bool | None:
        if not line:
            return None

        normalized = self._normalize_text(line)

        if ":" in normalized:
            token = normalized.split(":", 1)[1].split("(", 1)[0].strip()
            if token.startswith("no"):
                return False
            if token.startswith("si") or token.startswith("yes"):
                return True

        negative_tokens = (
            " no",
            "not supported",
            "not available",
            "no disponible",
            "unsupported",
        )
        positive_tokens = (
            " si",
            " yes",
            "available",
            "compatible",
            "supported",
            "admitido: si",
        )

        if any(token in normalized for token in negative_tokens):
            return False
        if any(token in normalized for token in positive_tokens):
            return True
        return None

    def _run_command(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        creationflags = self._creationflags()
        result = subprocess.run(
            command,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=self._startupinfo(),
        )
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout=self._decode_output(result.stdout),
            stderr=self._decode_output(result.stderr),
        )

    def _decode_output(self, data: bytes | None) -> str:
        if not data:
            return ""

        candidates = (
            "utf-8",
            "cp850",
            "cp437",
            locale.getpreferredencoding(False),
            "cp1252",
            "latin-1",
        )
        seen: set[str] = set()
        for encoding in candidates:
            if not encoding or encoding in seen:
                continue
            seen.add(encoding)
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue

        return data.decode("utf-8", errors="replace")

    def _sanitize_text(self, value: str) -> str:
        fixed = value
        replacements = {
            "Ã¡": "á",
            "Ã©": "é",
            "Ã­": "í",
            "Ã³": "ó",
            "Ãº": "ú",
            "Ã±": "ñ",
            "Ã¼": "ü",
            "Â¿": "¿",
            "Â¡": "¡",
        }
        for broken, correct in replacements.items():
            fixed = fixed.replace(broken, correct)

        return fixed

    def _normalize_text(self, value: str) -> str:
        sanitized = self._sanitize_text(value)
        lowered = sanitized.lower()
        return unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("ascii")

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _creationflags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _startupinfo(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return startupinfo
