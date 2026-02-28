from __future__ import annotations

import logging
from pathlib import Path
from queue import Empty, Queue

from backend.events import AppEvent
from backend.services.capture_service import CaptureService
from backend.services.uxplay_service import UxPlayService
from backend.services.wireless_projection_service import WirelessProjectionService

LOGGER = logging.getLogger(__name__)


class AppController:
    def __init__(self) -> None:
        self._events: Queue[AppEvent] = Queue()
        self._service = UxPlayService(
            on_log=self._handle_service_log,
            on_state_change=self._handle_service_state,
        )
        self._capture_service = CaptureService(
            on_log=self._handle_service_log,
            on_recording_state=self._handle_recording_state,
        )
        self._projection_service = WirelessProjectionService(on_log=self._handle_service_log)

    def _handle_service_log(self, message: str) -> None:
        LOGGER.info("%s", message)
        self._events.put(AppEvent(kind="log", message=message))

    def _handle_service_state(self, running: bool) -> None:
        state_text = "activo" if running else "detenido"
        LOGGER.info("Estado del receptor: %s", state_text)
        self._events.put(AppEvent(kind="state", running=running))

    def _handle_recording_state(self, recording: bool) -> None:
        LOGGER.info("Estado de grabacion: %s", "grabando" if recording else "inactiva")
        self._events.put(AppEvent(kind="recording", recording=recording))

    def start_receiver(
        self,
        uxplay_path: Path,
        receiver_name: str,
        extra_args: list[str] | None = None,
        append_hostname_suffix: bool = True,
        preferred_interface_alias: str | None = None,
    ) -> None:
        self._service.start(
            uxplay_path=uxplay_path,
            receiver_name=receiver_name,
            extra_args=extra_args,
            append_hostname_suffix=append_hostname_suffix,
            preferred_interface_alias=preferred_interface_alias,
        )

    def stop_receiver(self) -> None:
        self._service.stop()

    def is_running(self) -> bool:
        return self._service.is_running

    def list_receiver_process_ids(self) -> list[int]:
        return self._service.list_process_ids()

    def list_airplay_interfaces(self) -> list[tuple[str, str]]:
        return self._service.list_available_interfaces()

    def open_android_projection_portal(self) -> None:
        self._projection_service.open_android_projection_portal()

    def get_android_projection_diagnostics(self) -> dict[str, str | bool | None]:
        return self._projection_service.get_diagnostics()

    def take_snapshot(
        self,
        uxplay_path: Path,
        output_path: Path,
        source_mode: str = "desktop",
        window_title: str = "UxPlay",
    ) -> None:
        self._capture_service.take_snapshot(
            uxplay_path=uxplay_path,
            output_path=output_path,
            source_mode=source_mode,
            window_title=window_title,
        )

    def start_recording(
        self,
        uxplay_path: Path,
        output_path: Path,
        source_mode: str = "desktop",
        window_title: str = "UxPlay",
        fps: int = 30,
        *,
        capture_region: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Begin a recording.

        Parameters mirror those in :class:`CaptureService.start_recording`.
        The new ``capture_region`` argument allows the caller to specify an
        explicit screen rectangle (x, y, width, height) that should be recorded.
        When given the controller will forward the value directly to the
        capture service and the window-title lookup logic will be bypassed. This
        is used by the UI to lock the recording to the embedded preview panel.
        """
        self._capture_service.start_recording(
            uxplay_path=uxplay_path,
            output_path=output_path,
            source_mode=source_mode,
            window_title=window_title,
            fps=fps,
            capture_region=capture_region,
        )

    def stop_recording(self, output_path: Path | None = None) -> None:
        self._capture_service.stop_recording(output_path=output_path)

    def is_recording(self) -> bool:
        return self._capture_service.is_recording


    def drain_events(self) -> list[AppEvent]:
        events: list[AppEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events

    def shutdown(self) -> None:
        if self._capture_service.is_recording:
            self._capture_service.stop_recording()
        if self._service.is_running:
            self._service.stop()
