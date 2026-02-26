from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
from typing import Callable

LogCallback = Callable[[str], None]
RecordingStateCallback = Callable[[bool], None]


class CaptureService:
    def __init__(
        self,
        on_log: LogCallback | None = None,
        on_recording_state: RecordingStateCallback | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_recording_state = on_recording_state
        self._record_process: subprocess.Popen[str] | None = None
        self._record_output_path: Path | None = None
        self._record_reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._record_process is not None and self._record_process.poll() is None

    def take_snapshot(
        self,
        uxplay_path: Path,
        output_path: Path,
        source_mode: str = "desktop",
        window_title: str = "UxPlay",
    ) -> None:
        ffmpeg_path = self._resolve_ffmpeg_path(uxplay_path)
        source_arg = self._build_capture_source(source_mode, window_title)
        env = self._build_capture_env(ffmpeg_path)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        output_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "gdigrab",
            "-draw_mouse",
            "0",
            "-i",
            source_arg,
            "-frames:v",
            "1",
            str(output_path),
        ]

        result = subprocess.run(
            command,
            cwd=str(ffmpeg_path.parent),
            env=env,
            capture_output=True,
            text=True,
            creationflags=creationflags,
            check=False,
        )

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "Error desconocido de captura."
            raise RuntimeError(f"La captura falló: {details}")

        self._emit_log(f"Captura guardada: {output_path}")

    def start_recording(
        self,
        uxplay_path: Path,
        output_path: Path,
        source_mode: str = "desktop",
        window_title: str = "UxPlay",
        fps: int = 30,
    ) -> None:
        with self._lock:
            if self._record_process is not None and self._record_process.poll() is None:
                raise RuntimeError("Ya hay una grabación en curso.")

            ffmpeg_path = self._resolve_ffmpeg_path(uxplay_path)
            source_arg = self._build_capture_source(source_mode, window_title)
            env = self._build_capture_env(ffmpeg_path)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            output_path.parent.mkdir(parents=True, exist_ok=True)

            command = [
                str(ffmpeg_path),
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-f",
                "gdigrab",
                "-framerate",
                str(max(10, min(fps, 120))),
                "-draw_mouse",
                "0",
                "-i",
                source_arg,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]

            self._record_process = subprocess.Popen(
                command,
                cwd=str(ffmpeg_path.parent),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                universal_newlines=True,
                bufsize=1,
                creationflags=creationflags,
            )
            self._record_output_path = output_path

            process = self._record_process

        self._emit_log(f"Grabación iniciada: {output_path}")
        self._emit_recording_state(True)

        if process is not None:
            self._record_reader_thread = threading.Thread(
                target=self._stream_record_output,
                args=(process,),
                daemon=True,
            )
            self._record_reader_thread.start()

    def stop_recording(self) -> None:
        process: subprocess.Popen[str] | None
        output_path: Path | None

        with self._lock:
            process = self._record_process
            output_path = self._record_output_path

        if process is None or process.poll() is not None:
            return

        self._emit_log("Deteniendo grabación...")

        try:
            if process.stdin is not None:
                process.stdin.write("q\n")
                process.stdin.flush()
            process.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        should_emit_state = False
        with self._lock:
            if self._record_process is process:
                self._record_process = None
                self._record_output_path = None
                should_emit_state = True

        if should_emit_state:
            if output_path is not None:
                self._emit_log(f"Grabación guardada: {output_path}")
            self._emit_recording_state(False)

    def _stream_record_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    self._emit_log(f"[REC] {line}")

        exit_code = process.wait()
        should_emit_state = False
        output_path: Path | None = None

        with self._lock:
            if self._record_process is process:
                self._record_process = None
                output_path = self._record_output_path
                self._record_output_path = None
                should_emit_state = True

        if not should_emit_state:
            # Recording was already closed by stop_recording(); avoid duplicate terminal log/state.
            return

        if output_path is not None:
            self._emit_log(f"Grabación finalizada (código {exit_code}): {output_path}")
        else:
            self._emit_log(f"El proceso de grabación finalizó con código {exit_code}.")

        self._emit_recording_state(False)

    def _resolve_ffmpeg_path(self, uxplay_path: Path) -> Path:
        uxplay = uxplay_path.expanduser().resolve()
        if not uxplay.exists() or not uxplay.is_file():
            raise FileNotFoundError(f"No se encontró el ejecutable de UxPlay: {uxplay}")

        candidates = (
            uxplay.parent / "ffmpeg.exe",
            uxplay.parent / "ffmpeg",
            uxplay.parent.parent / "bin" / "ffmpeg.exe",
        )

        for candidate in candidates:
            if candidate.exists():
                return candidate

        raise FileNotFoundError("No se encontró ffmpeg.exe junto al runtime de UxPlay.")

    def _build_capture_source(self, source_mode: str, window_title: str) -> str:
        mode = source_mode.strip().lower()
        if mode == "window":
            normalized_title = window_title.strip() or "UxPlay"
            return f"title={normalized_title}"
        return "desktop"

    def _build_capture_env(self, ffmpeg_path: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(ffmpeg_path.parent) + os.pathsep + env.get("PATH", "")
        return env

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _emit_recording_state(self, recording: bool) -> None:
        if self._on_recording_state is not None:
            self._on_recording_state(recording)
