from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import unicodedata
from typing import Callable

LogCallback = Callable[[str], None]
StateCallback = Callable[[bool], None]


class UxPlayService:
    def __init__(
        self,
        on_log: LogCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_state_change = on_state_change
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(
        self,
        uxplay_path: Path,
        receiver_name: str,
        extra_args: list[str] | None = None,
        append_hostname_suffix: bool = True,
    ) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("El receptor ya está en ejecución.")

            normalized_path = uxplay_path.expanduser().resolve()
            if not normalized_path.exists():
                raise FileNotFoundError(f"No se encontró UxPlay: {normalized_path}")
            if not normalized_path.is_file():
                raise FileNotFoundError(f"La ruta de UxPlay no es un archivo válido: {normalized_path}")

            receiver_name = self._sanitize_receiver_name(receiver_name)
            runtime_args = [arg.strip() for arg in (extra_args or []) if arg and arg.strip()]
            runtime_args = [arg for arg in runtime_args if arg.lower() != "-nh"]
            if not append_hostname_suffix:
                runtime_args = ["-nh", *runtime_args]

            command = [str(normalized_path), "-n", receiver_name, *runtime_args]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            environment = self._build_runtime_env(normalized_path)

            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(normalized_path.parent),
                env=environment,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creationflags,
            )

            process = self._process

        self._emit_log(f"Iniciando receptor: {' '.join(command)}")
        self._emit_state(True)

        if process is not None:
            self._reader_thread = threading.Thread(
                target=self._stream_output,
                args=(process,),
                daemon=True,
            )
            self._reader_thread.start()

    def stop(self) -> None:
        process: subprocess.Popen[str] | None
        with self._lock:
            process = self._process

        if process is None or process.poll() is not None:
            return

        self._emit_log("Deteniendo receptor...")

        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._emit_log("El proceso del receptor no cerró a tiempo. Forzando cierre.")
            process.kill()
            process.wait(timeout=3)

        did_clear = False
        with self._lock:
            if self._process is process:
                self._process = None
                did_clear = True

        if did_clear:
            self._emit_state(False)

    def _stream_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return

        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                self._emit_log(line)

        exit_code = process.wait()
        should_emit_state = False

        with self._lock:
            if self._process is process:
                self._process = None
                should_emit_state = True

        self._emit_log(f"El proceso del receptor finalizó con código {exit_code}.")
        if should_emit_state:
            self._emit_state(False)

    def _build_runtime_env(self, uxplay_executable: Path) -> dict[str, str]:
        env = os.environ.copy()

        bin_dir = uxplay_executable.parent
        uxplay_root = bin_dir.parent if bin_dir.name.lower() == "bin" else bin_dir
        lib_dir = uxplay_root / "lib"
        gst_plugin_dir = lib_dir / "gstreamer-1.0"
        plugin_scanner = bin_dir / "gst-plugin-scanner.exe"

        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        if gst_plugin_dir.exists():
            gst_plugin_path = str(gst_plugin_dir)
            env["GST_PLUGIN_PATH"] = gst_plugin_path
            env["GST_PLUGIN_SYSTEM_PATH"] = gst_plugin_path

        if plugin_scanner.exists():
            env["GST_PLUGIN_SCANNER"] = str(plugin_scanner)

        return env

    def _sanitize_receiver_name(self, value: str) -> str:
        name = " ".join(value.strip().split())
        if not name:
            return "LonelyScreenIOS"

        if any(ord(ch) < 32 for ch in name):
            raise ValueError("El nombre del receptor contiene caracteres de control no permitidos.")

        normalized = unicodedata.normalize("NFKC", name)
        if len(normalized) > 63:
            raise ValueError("El nombre del receptor no debe superar 63 caracteres.")

        return normalized

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _emit_state(self, running: bool) -> None:
        if self._on_state_change is not None:
            self._on_state_change(running)
