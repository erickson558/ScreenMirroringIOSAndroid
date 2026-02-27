from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
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
        self._record_ffmpeg_path: Path | None = None
        self._record_requested_output_path: Path | None = None
        self._last_completed_output_path: Path | None = None
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
        creationflags = self._creationflags()
        startupinfo = self._startupinfo()

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
            startupinfo=startupinfo,
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
                raise RuntimeError("Ya hay una grabacion en curso.")

            ffmpeg_path = self._resolve_ffmpeg_path(uxplay_path)
            env = self._build_capture_env(ffmpeg_path)
            creationflags = self._creationflags()
            startupinfo = self._startupinfo()

            output_path.parent.mkdir(parents=True, exist_ok=True)

            mode = source_mode.strip().lower()
            candidate_sources = (
                self._window_capture_sources(window_title)
                if mode == "window"
                else [self._build_capture_source(source_mode, window_title)]
            )

            process: subprocess.Popen[str] | None = None
            early_output = ""
            early_code: int | None = None
            selected_source = candidate_sources[0]
            started = False

            for source_arg in candidate_sources:
                source_for_ffmpeg = source_arg
                if mode == "window":
                    if source_arg.startswith("title="):
                        candidate_title = source_arg.removeprefix("title=")
                        if not self._window_title_exists(candidate_title):
                            continue

                max_attempts = 2 if mode == "window" else 1
                for attempt in range(1, max_attempts + 1):
                    command = self._build_record_command(
                        ffmpeg_path,
                        source_for_ffmpeg,
                        output_path,
                        fps,
                    )
                    process = self._spawn_record_process(
                        command=command,
                        ffmpeg_path=ffmpeg_path,
                        env=env,
                        creationflags=creationflags,
                        startupinfo=startupinfo,
                    )
                    exited_early, early_output, early_code = self._check_early_startup_failure(process)
                    if not exited_early:
                        selected_source = source_arg
                        started = True
                        break
                    if mode == "window" and self._is_window_not_found_error(early_output):
                        break
                    if self._is_retryable_startup_error(early_output) and attempt < max_attempts:
                        self._emit_log("[PISTA] Inicio de grabacion inestable. Reintentando automaticamente...")
                        time.sleep(0.35)
                        continue

                    details = self._summarize_early_failure(early_output, early_code)
                    raise RuntimeError(f"La grabacion no pudo iniciar: {details}")
                if started:
                    break
            if not started:
                details = self._summarize_early_failure(early_output, early_code)
                raise RuntimeError(f"La grabacion no pudo iniciar: {details}")

            if mode == "window" and selected_source.startswith("title=") and selected_source != candidate_sources[0]:
                selected_title = selected_source.removeprefix("title=")
                self._emit_log(f"[PISTA] Se detecto la ventana de video como '{selected_title}' para la grabacion.")
            if mode == "window" and selected_source.startswith("hwnd="):
                self._emit_log(
                    "[PISTA] Grabacion vinculada a la ventana D3D por identificador interno (HWND); "
                    "seguira la ventana aunque cambie de posicion en la pantalla."
                )
            if process is None:
                raise RuntimeError("No se pudo iniciar la grabacion.")

            self._record_process = process
            self._record_output_path = output_path
            self._record_ffmpeg_path = ffmpeg_path
            self._record_requested_output_path = None
            self._last_completed_output_path = None

        self._emit_log(f"Grabacion iniciada: {output_path}")
        self._emit_recording_state(True)

        self._record_reader_thread = threading.Thread(
            target=self._stream_record_output,
            args=(process,),
            daemon=True,
        )
        self._record_reader_thread.start()

    def stop_recording(self, output_path: Path | None = None) -> None:
        process: subprocess.Popen[str] | None
        recorded_output_path: Path | None
        ffmpeg_path: Path | None
        requested_output_path: Path | None = None
        last_completed_output: Path | None

        with self._lock:
            if output_path is not None:
                requested_output_path = output_path.expanduser().resolve()
                self._record_requested_output_path = requested_output_path
            process = self._record_process
            recorded_output_path = self._record_output_path
            ffmpeg_path = self._record_ffmpeg_path
            last_completed_output = self._last_completed_output_path

        if process is None or process.poll() is not None:
            if (
                requested_output_path is not None
                and last_completed_output is not None
                and last_completed_output.exists()
            ):
                saved_output = self._finalize_recording_output(last_completed_output, requested_output_path)
                with self._lock:
                    self._last_completed_output_path = saved_output
                self._emit_log(f"Grabación guardada: {saved_output}")
            return

        self._emit_log("Deteniendo grabación...")

        forced_stop = False
        try:
            if process.stdin is not None:
                process.stdin.write("q\n")
                process.stdin.flush()
            process.wait(timeout=25)
        except (subprocess.TimeoutExpired, OSError):
            forced_stop = True
            self._emit_log(
                "[ADVERTENCIA] ffmpeg no cerro a tiempo tras solicitar parada. "
                "Se intentara cierre forzado y reparacion del MP4."
            )
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

        should_emit_state = False
        pending_output_path: Path | None = None
        with self._lock:
            pending_output_path = self._record_requested_output_path
            if self._record_process is process:
                self._record_process = None
                self._record_output_path = None
                self._record_ffmpeg_path = None
                self._record_requested_output_path = None
                should_emit_state = True

        if should_emit_state:
            saved_output = recorded_output_path
            if saved_output is not None and pending_output_path is not None:
                saved_output = self._finalize_recording_output(saved_output, pending_output_path)
            if forced_stop and saved_output is not None and ffmpeg_path is not None:
                self._attempt_repair_recording(ffmpeg_path=ffmpeg_path, output_path=saved_output)
            if saved_output is not None:
                with self._lock:
                    self._last_completed_output_path = saved_output
                self._emit_log(f"Grabación guardada: {saved_output}")
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
        pending_output_path: Path | None = None

        with self._lock:
            if self._record_process is process:
                self._record_process = None
                output_path = self._record_output_path
                pending_output_path = self._record_requested_output_path
                self._record_output_path = None
                self._record_ffmpeg_path = None
                self._record_requested_output_path = None
                should_emit_state = True

        if not should_emit_state:
            # Recording was already closed by stop_recording(); avoid duplicate terminal log/state.
            return

        saved_output = output_path
        if saved_output is not None and pending_output_path is not None:
            saved_output = self._finalize_recording_output(saved_output, pending_output_path)

        if saved_output is not None:
            with self._lock:
                self._last_completed_output_path = saved_output
            self._emit_log(f"Grabación finalizada (código {exit_code}): {saved_output}")
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
            normalized_title = window_title.strip() or "Direct3D11 renderer"
            return f"title={normalized_title}"
        return "desktop"

    def _window_capture_sources(self, window_title: str) -> list[str]:
        ordered_titles = [
            window_title.strip(),
            "Direct3D11 renderer",
            "UxPlay",
        ]
        unique_titles: list[str] = []
        seen: set[str] = set()
        for title in ordered_titles:
            clean = " ".join(title.split()).strip()
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_titles.append(clean)
        if not unique_titles:
            unique_titles.append("Direct3D11 renderer")

        sources: list[str] = []
        seen_sources: set[str] = set()
        for title in unique_titles:
            hwnd = self._find_window_handle(title)
            if hwnd is not None:
                for hwnd_source in (f"hwnd=0x{hwnd:X}", f"hwnd={hwnd}"):
                    if hwnd_source in seen_sources:
                        continue
                    seen_sources.add(hwnd_source)
                    sources.append(hwnd_source)

            title_source = f"title={title}"
            if title_source in seen_sources:
                continue
            seen_sources.add(title_source)
            sources.append(title_source)

        return sources

    def _build_capture_env(self, ffmpeg_path: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(ffmpeg_path.parent) + os.pathsep + env.get("PATH", "")
        return env

    def _build_record_command(
        self,
        ffmpeg_path: Path,
        source_arg: str,
        output_path: Path,
        fps: int,
    ) -> list[str]:
        target_fps = max(15, min(fps, 60))

        return [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "gdigrab",
            "-framerate",
            str(target_fps),
            "-thread_queue_size",
            "1024",
            "-draw_mouse",
            "0",
            "-i",
            source_arg,
            "-an",
            "-vf",
            "crop=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(target_fps),
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            "90000",
            str(output_path),
        ]

    def _resolve_window_region(self, window_title: str) -> tuple[int, int, int, int] | None:
        if os.name != "nt":
            return None

        hwnd = self._find_window_handle(window_title)
        if hwnd is None:
            return None

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)

        virtual_x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        virtual_y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        virtual_w = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        virtual_h = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        max_x = virtual_x + virtual_w
        max_y = virtual_y + virtual_h

        left = max(left, virtual_x)
        top = max(top, virtual_y)
        right = min(right, max_x)
        bottom = min(bottom, max_y)

        width = right - left
        height = bottom - top
        if width < 32 or height < 32:
            return None

        return (left, top, width, height)

    def _window_title_exists(self, window_title: str) -> bool:
        return self._find_window_handle(window_title) is not None

    def _find_window_handle(self, window_title: str) -> int | None:
        if os.name != "nt":
            return None

        normalized_target = " ".join(window_title.split()).strip()
        if not normalized_target:
            return None

        user32 = ctypes.windll.user32
        hwnd = int(user32.FindWindowW(None, normalized_target))
        if hwnd <= 0:
            return None
        if not user32.IsWindowVisible(hwnd):
            return None
        return hwnd

    def _finalize_recording_output(self, source_path: Path, target_path: Path) -> Path:
        if not source_path.exists():
            return source_path

        resolved_source = source_path.expanduser().resolve()
        resolved_target = target_path.expanduser().resolve()
        resolved_target.parent.mkdir(parents=True, exist_ok=True)

        if resolved_source == resolved_target:
            return resolved_source

        try:
            if resolved_target.exists():
                resolved_target.unlink()
            resolved_source.replace(resolved_target)
            return resolved_target
        except OSError as exc:
            self._emit_log(
                "[ADVERTENCIA] No se pudo mover por reemplazo directo; se intentara copia segura. "
                f"Detalle: {exc}"
            )

        try:
            if resolved_target.exists():
                resolved_target.unlink()
            shutil.copy2(resolved_source, resolved_target)
            if resolved_target.exists():
                try:
                    resolved_source.unlink()
                except OSError:
                    # Keep source if it cannot be removed; destination already contains the final video.
                    pass
                return resolved_target
        except OSError as copy_exc:
            self._emit_log(
                "[ADVERTENCIA] No se pudo copiar la grabacion al destino elegido. "
                f"Se conserva en: {resolved_source}. Detalle: {copy_exc}"
            )
            return resolved_source

        self._emit_log(
            "[ADVERTENCIA] El archivo no quedo en el destino elegido tras el intento de guardado. "
            f"Se conserva en: {resolved_source}"
        )
        return resolved_source

    def _attempt_repair_recording(self, ffmpeg_path: Path, output_path: Path) -> None:
        if not output_path.exists():
            return

        temp_output = output_path.with_name(f"{output_path.stem}.repair{output_path.suffix}")
        command = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(output_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temp_output),
        ]
        result = subprocess.run(
            command,
            cwd=str(ffmpeg_path.parent),
            capture_output=True,
            text=True,
            creationflags=self._creationflags(),
            startupinfo=self._startupinfo(),
            check=False,
        )
        if result.returncode != 0 or not temp_output.exists():
            details = result.stderr.strip() or result.stdout.strip()
            if details:
                self._emit_log(f"[ADVERTENCIA] No se pudo reparar el MP4: {details}")
            return

        temp_output.replace(output_path)
        self._emit_log("[PISTA] El archivo de grabacion se reparo automaticamente tras cierre forzado.")

    def _spawn_record_process(
        self,
        command: list[str],
        ffmpeg_path: Path,
        env: dict[str, str],
        creationflags: int,
        startupinfo: subprocess.STARTUPINFO | None,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
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
            startupinfo=startupinfo,
        )

    def _check_early_startup_failure(self, process: subprocess.Popen[str], timeout: float = 1.8) -> tuple[bool, str, int | None]:
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", None

        output = ""
        if process.stdout is not None:
            try:
                output = process.stdout.read() or ""
            except OSError:
                output = ""
        return True, output, exit_code

    def _is_window_not_found_error(self, text: str) -> bool:
        low = text.lower()
        return (
            "can't find window" in low
            or "error opening input file title=" in low
            or "error opening input file hwnd=" in low
        )

    def _is_retryable_startup_error(self, text: str) -> bool:
        low = text.lower()
        markers = (
            "failed to capture image",
            "error during demuxing",
            "error retrieving a packet from demuxer",
            "resource temporarily unavailable",
            "i/o error",
        )
        return any(marker in low for marker in markers)

    def _summarize_early_failure(self, output: str, exit_code: int | None) -> str:
        cleaned = " ".join((output or "").strip().split())
        low = cleaned.lower()
        if (
            "can't find window" in low
            or "error opening input file title=" in low
            or "error opening input file hwnd=" in low
        ):
            return (
                "No se encontro la ventana de UxPlay. Verifica que el mirroring este activo "
                "y que el titulo de ventana coincida."
            )
        if cleaned:
            return cleaned
        if exit_code is not None:
            return f"ffmpeg finalizó al iniciar con código {exit_code}."
        return "Error desconocido al iniciar ffmpeg."

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    def _emit_recording_state(self, recording: bool) -> None:
        if self._on_recording_state is not None:
            self._on_recording_state(recording)

    def _creationflags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _startupinfo(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return startupinfo
