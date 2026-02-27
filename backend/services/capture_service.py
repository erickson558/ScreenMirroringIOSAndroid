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
    _WINDOW_REGION_PADDING_PX = 10
    _WINDOW_REGION_STABILIZATION_SAMPLES = 4
    _WINDOW_REGION_STABILIZATION_DELAY_SECONDS = 0.1

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
            selected_region: tuple[int, int, int, int] | None = None
            started = False

            for source_arg in candidate_sources:
                source_for_ffmpeg = source_arg
                capture_region: tuple[int, int, int, int] | None = None
                if mode == "window":
                    capture_region = self._resolve_window_region(source_arg)
                    if capture_region is not None:
                        source_for_ffmpeg = "desktop"
                    elif source_arg.startswith("title="):
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
                        capture_region=capture_region,
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
                        selected_region = capture_region
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
            if mode == "window" and selected_region is not None:
                x, y, width, height = selected_region
                self._emit_log(
                    "[PISTA] Grabacion de ventana por region de escritorio "
                    f"({width}x{height} en x={x}, y={y}) para capturar correctamente render D3D."
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
        capture_region: tuple[int, int, int, int] | None = None,
    ) -> list[str]:
        target_fps = max(15, min(fps, 60))
        capture_input_args: list[str] = []
        if capture_region is not None:
            x, y, width, height = capture_region
            capture_input_args.extend(
                [
                    "-offset_x",
                    str(x),
                    "-offset_y",
                    str(y),
                    "-video_size",
                    f"{width}x{height}",
                ]
            )

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
            *capture_input_args,
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
            "-fps_mode",
            "vfr",
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            "90000",
            str(output_path),
        ]

    def _resolve_window_region(self, window_source: str) -> tuple[int, int, int, int] | None:
        if os.name != "nt":
            return None

        source = " ".join(window_source.split()).strip()
        if not source:
            return None

        hwnd: int | None = None
        low_source = source.lower()
        if low_source.startswith("hwnd="):
            hwnd = self._parse_hwnd_value(source.split("=", 1)[1])
        elif low_source.startswith("title="):
            hwnd = self._find_window_handle(source.split("=", 1)[1])
        else:
            hwnd = self._find_window_handle(source)

        if hwnd is None:
            return None
        region = self._resolve_window_region_by_handle(hwnd)
        if region is None:
            return None

        stabilized_region = self._stabilize_window_region(hwnd, region)
        if stabilized_region is not None:
            return stabilized_region
        return region

    def _parse_hwnd_value(self, raw_value: str) -> int | None:
        value = " ".join(raw_value.split()).strip()
        if not value:
            return None
        try:
            hwnd = int(value, 0)
        except ValueError:
            return None
        if hwnd <= 0:
            return None
        return hwnd

    def _resolve_window_region_by_handle(self, hwnd: int) -> tuple[int, int, int, int] | None:
        window_rect = self._get_window_rect(hwnd)
        if window_rect is None:
            return None

        clamped = self._clamp_rect_to_virtual_screen(*window_rect)
        if clamped is None:
            return None

        left, top, right, bottom = self._ensure_even_rect_dimensions(*clamped)
        width = right - left
        height = bottom - top
        if width < 32 or height < 32:
            return None

        return (left, top, width, height)

    def _stabilize_window_region(
        self,
        hwnd: int,
        initial_region: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int] | None:
        left, top, width, height = initial_region
        right = left + width
        bottom = top + height

        for _ in range(self._WINDOW_REGION_STABILIZATION_SAMPLES):
            time.sleep(self._WINDOW_REGION_STABILIZATION_DELAY_SECONDS)
            current = self._resolve_window_region_by_handle(hwnd)
            if current is None:
                continue
            cur_left, cur_top, cur_width, cur_height = current
            cur_right = cur_left + cur_width
            cur_bottom = cur_top + cur_height
            left = min(left, cur_left)
            top = min(top, cur_top)
            right = max(right, cur_right)
            bottom = max(bottom, cur_bottom)

        padded = self._expand_rect_with_padding(left, top, right, bottom, self._WINDOW_REGION_PADDING_PX)
        if padded is None:
            return None

        left, top, right, bottom = self._ensure_even_rect_dimensions(*padded)
        width = right - left
        height = bottom - top
        if width < 32 or height < 32:
            return None

        return (left, top, width, height)

    def _get_window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return None
        if not user32.IsWindowVisible(hwnd):
            return None

        try:
            dwmapi = ctypes.windll.dwmapi
            extended = wintypes.RECT()
            hr = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(9),  # DWMWA_EXTENDED_FRAME_BOUNDS
                ctypes.byref(extended),
                ctypes.sizeof(extended),
            )
            if hr == 0:
                left = int(extended.left)
                top = int(extended.top)
                right = int(extended.right)
                bottom = int(extended.bottom)
                if right - left >= 32 and bottom - top >= 32:
                    return (left, top, right, bottom)
        except (AttributeError, OSError):
            pass

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def _clamp_rect_to_virtual_screen(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> tuple[int, int, int, int] | None:
        virtual_left, virtual_top, virtual_right, virtual_bottom = self._virtual_screen_bounds()
        left = max(left, virtual_left)
        top = max(top, virtual_top)
        right = min(right, virtual_right)
        bottom = min(bottom, virtual_bottom)
        if right - left < 32 or bottom - top < 32:
            return None
        return (left, top, right, bottom)

    def _expand_rect_with_padding(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        padding: int,
    ) -> tuple[int, int, int, int] | None:
        if padding <= 0:
            return self._clamp_rect_to_virtual_screen(left, top, right, bottom)
        return self._clamp_rect_to_virtual_screen(
            left - padding,
            top - padding,
            right + padding,
            bottom + padding,
        )

    def _ensure_even_rect_dimensions(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> tuple[int, int, int, int]:
        virtual_left, virtual_top, virtual_right, virtual_bottom = self._virtual_screen_bounds()

        if (right - left) % 2 != 0:
            if right < virtual_right:
                right += 1
            elif left > virtual_left:
                left -= 1
            else:
                right -= 1

        if (bottom - top) % 2 != 0:
            if bottom < virtual_bottom:
                bottom += 1
            elif top > virtual_top:
                top -= 1
            else:
                bottom -= 1

        return (left, top, right, bottom)

    def _virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        user32 = ctypes.windll.user32
        virtual_x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        virtual_y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        virtual_w = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        virtual_h = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        max_x = virtual_x + max(virtual_w, 1)
        max_y = virtual_y + max(virtual_h, 1)
        return (virtual_x, virtual_y, max_x, max_y)

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
