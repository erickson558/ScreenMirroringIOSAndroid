from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import threading
import time
import unicodedata
from typing import Callable

LogCallback = Callable[[str], None]
StateCallback = Callable[[bool], None]


@dataclass(frozen=True, slots=True)
class _StartRequest:
    uxplay_path: Path
    receiver_name: str
    extra_args: tuple[str, ...]
    append_hostname_suffix: bool
    preferred_interface_alias: str | None


class UxPlayService:
    _DEFAULT_WINDOW_SIZE = "1280x720@60"

    def __init__(
        self,
        on_log: LogCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_state_change = on_state_change
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._reader_threads: dict[int, threading.Thread] = {}
        self._mirror_started_at: dict[int, float] = {}
        self._ntp_error_counts: dict[int, int] = {}
        self._last_start_request: _StartRequest | None = None
        self._auto_recovery_in_progress = False
        self._auto_recovery_attempts = 0
        self._max_auto_recovery_attempts = 2
        self._scheduled_recovery_id: int | None = None
        self._recovery_sequence = 0
        self._recovery_delay_seconds = 2.5
        self._manual_stop_until = 0.0
        self._window_probe_delay_seconds = 4.5
        self._window_probe_sequence = 0
        self._pending_window_probes: dict[int, int] = {}
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            self._prune_dead_processes_locked()
            return bool(self._processes)

    def start(
        self,
        uxplay_path: Path,
        receiver_name: str,
        extra_args: list[str] | None = None,
        append_hostname_suffix: bool = True,
        preferred_interface_alias: str | None = None,
        _from_auto_recovery: bool = False,
    ) -> None:
        launched: list[tuple[subprocess.Popen[str], str | None]] = []
        startup_hints: list[str] = []
        command_logs: list[str] = []
        with self._lock:
            self._prune_dead_processes_locked()
            if self._processes:
                raise RuntimeError("El receptor ya esta en ejecucion.")

            normalized_path = uxplay_path.expanduser().resolve()
            if not normalized_path.exists():
                raise FileNotFoundError(f"No se encontro UxPlay: {normalized_path}")
            if not normalized_path.is_file():
                raise FileNotFoundError(f"La ruta de UxPlay no es un archivo valido: {normalized_path}")

            stale_count = self._terminate_stale_uxplay_instances(normalized_path)

            receiver_name = self._sanitize_receiver_name(receiver_name)
            runtime_args = [arg.strip() for arg in (extra_args or []) if arg and arg.strip()]
            runtime_args = [arg for arg in runtime_args if arg.lower() != "-nh"]
            if not append_hostname_suffix:
                runtime_args = ["-nh", *runtime_args]
            preferred_alias = self._normalize_interface_alias(preferred_interface_alias)
            runtime_args, port_hint = self._ensure_legacy_ports(runtime_args)
            runtime_args, persist_hint = self._ensure_window_persistence(runtime_args)
            runtime_args, size_hint = self._ensure_window_size(runtime_args)
            runtime_args, renderer_hint = self._ensure_windows_d3d11_renderer(runtime_args)
            launch_plans = self._build_launch_plans(receiver_name, runtime_args, preferred_alias)
            self._last_start_request = _StartRequest(
                uxplay_path=normalized_path,
                receiver_name=receiver_name,
                extra_args=tuple(runtime_args),
                append_hostname_suffix=append_hostname_suffix,
                preferred_interface_alias=preferred_alias,
            )
            if not _from_auto_recovery:
                self._auto_recovery_attempts = 0
                self._scheduled_recovery_id = None

            creationflags = self._creationflags()
            # Keep UxPlay video window visible while still suppressing console popups.
            startupinfo = None
            environment = self._build_runtime_env(normalized_path)

            try:
                for instance_name, instance_args, instance_hint in launch_plans:
                    command = [str(normalized_path), "-n", instance_name, *instance_args]
                    process = subprocess.Popen(
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
                        startupinfo=startupinfo,
                    )
                    key = self._process_key(process)
                    self._processes[key] = process
                    self._mirror_started_at[key] = 0.0
                    self._ntp_error_counts[key] = 0
                    launched.append((process, instance_name if len(launch_plans) > 1 else None))
                    command_logs.append(" ".join(command))
                    if instance_hint:
                        startup_hints.append(instance_hint)
            except Exception:
                for process in self._processes.values():
                    self._terminate_process_quietly(process)
                self._processes.clear()
                self._reader_threads.clear()
                self._mirror_started_at.clear()
                self._ntp_error_counts.clear()
                self._pending_window_probes.clear()
                self._scheduled_recovery_id = None
                raise

        if len(command_logs) == 1:
            self._emit_log(f"Iniciando receptor: {command_logs[0]}")
        else:
            self._emit_log(f"Iniciando receptor en {len(command_logs)} interfaces activas.")
            for command_line in command_logs:
                self._emit_log(f"[PISTA] {command_line}")
        if stale_count > 0:
            self._emit_log(
                "[PISTA] Se cerraron procesos UxPlay previos en segundo plano "
                f"({stale_count}) para evitar anuncios duplicados en iPhone."
            )
        for hint in startup_hints:
            self._emit_log(hint)
        if port_hint:
            self._emit_log(port_hint)
        if persist_hint:
            self._emit_log(persist_hint)
        if size_hint:
            self._emit_log(size_hint)
        if renderer_hint:
            self._emit_log(renderer_hint)
        self._emit_log(
            "[PISTA] Espera a ver 'Initialized server socket(s)' antes de conectar en iPhone para evitar doble intento."
        )
        if _from_auto_recovery:
            self._emit_log("[PISTA] Receptor reiniciado automaticamente para recuperar el primer enlace AirPlay.")
        for hint in self._diagnose_windows_discovery_risks():
            self._emit_log(hint)
        self._emit_state(True)

        for process, label in launched:
            reader = threading.Thread(
                target=self._stream_output,
                args=(process, label),
                daemon=True,
            )
            with self._lock:
                self._reader_threads[self._process_key(process)] = reader
            reader.start()

    def stop(self) -> None:
        with self._lock:
            self._prune_dead_processes_locked()
            processes = list(self._processes.values())
            if processes:
                # Avoid triggering auto-recovery for intentional local stops.
                self._manual_stop_until = time.monotonic() + 4.0
            if not processes:
                self._scheduled_recovery_id = None

        if not processes:
            return

        self._emit_log("Deteniendo receptor...")

        for process in processes:
            self._terminate_process_quietly(process)

        did_clear = False
        with self._lock:
            if self._processes:
                self._processes.clear()
                self._reader_threads.clear()
                self._mirror_started_at.clear()
                self._ntp_error_counts.clear()
                self._pending_window_probes.clear()
                self._scheduled_recovery_id = None
                did_clear = True

        if did_clear:
            self._emit_state(False)

    def _stream_output(self, process: subprocess.Popen[str], label: str | None = None) -> None:
        if process.stdout is None:
            return

        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                self._emit_log(self._format_instance_message(line, label))
                self._handle_stream_health(process, line)

        exit_code = process.wait()
        should_emit_state = False

        with self._lock:
            key = self._process_key(process)
            removed = self._processes.pop(key, None) is not None
            self._reader_threads.pop(key, None)
            started_at = self._mirror_started_at.pop(key, 0.0)
            self._ntp_error_counts.pop(key, None)
            self._pending_window_probes.pop(key, None)
            if removed and not self._processes:
                should_emit_state = True

        self._emit_log(self._format_instance_message(f"El proceso del receptor finalizo con codigo {exit_code}.", label))
        if should_emit_state:
            self._emit_state(False)
        self._maybe_recover_on_early_exit(exit_code=exit_code, started_at=started_at)

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
            return "ScreenMirrorIOSAndroid"

        if any(ord(ch) < 32 for ch in name):
            raise ValueError("El nombre del receptor contiene caracteres de control no permitidos.")

        normalized = unicodedata.normalize("NFKC", name)
        if len(normalized) > 63:
            raise ValueError("El nombre del receptor no debe superar 63 caracteres.")

        return normalized

    def _build_launch_plans(
        self,
        receiver_name: str,
        runtime_args: list[str],
        preferred_interface_alias: str | None = None,
    ) -> list[tuple[str, list[str], str | None]]:
        if os.name != "nt" or self._has_explicit_mac_arg(runtime_args):
            return [(receiver_name, runtime_args, None)]

        adapters = self._resolve_windows_active_adapters()
        if not adapters:
            return [(receiver_name, runtime_args, None)]

        preferred_adapter = self._select_preferred_adapter(adapters, preferred_interface_alias)
        adapter_name, mac = preferred_adapter if preferred_adapter is not None else adapters[0]
        vpn_active = self._is_windows_vpn_active()

        if vpn_active:
            primary_args = ["-m", mac, *runtime_args]
            if not self._has_explicit_sync_arg(primary_args):
                primary_args = ["-vsync", "no", *primary_args]

            primary_hint = (
                f"[PISTA] VPN activa detectada: se fija interfaz principal '{adapter_name}' "
                f"({mac}) para anuncio AirPlay unico y estable."
            )
            return [(receiver_name, primary_args, primary_hint)]

        if preferred_adapter is not None:
            primary_hint = f"[PISTA] Interfaz AirPlay seleccionada manualmente: {adapter_name} ({mac})."
        else:
            primary_hint = f"[PISTA] MAC AirPlay fijada automaticamente a {mac} ({adapter_name})."
        primary_plan = (receiver_name, ["-m", mac, *runtime_args], primary_hint)

        adapter_list = ", ".join(name for name, _mac in adapters)
        multi_hint = (
            f"[PISTA] MAC AirPlay fijada automaticamente a {mac} ({adapter_name}). "
            f"Interfaces activas detectadas: {adapter_list}."
        )
        if preferred_adapter is None and len(adapters) > 1:
            primary_plan = (receiver_name, ["-m", mac, *runtime_args], multi_hint)

        return [primary_plan]

    def _terminate_stale_uxplay_instances(self, uxplay_path: Path) -> int:
        if os.name != "nt":
            return 0

        escaped_path = str(uxplay_path).replace("'", "''")
        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$target='{escaped_path}'.ToLowerInvariant();"
            "$killed=0;"
            "Get-CimInstance Win32_Process -Filter \"Name='uxplay.exe'\" | ForEach-Object {"
            "  $procPath = ($_.ExecutablePath + '').ToLowerInvariant();"
            "  if ($procPath -eq $target) {"
            "    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}"
            "  }"
            "};"
            "Write-Output $killed;"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return 0

        if completed.returncode not in (0, 1):
            return 0

        raw = (completed.stdout or "").strip().splitlines()
        if not raw:
            return 0

        try:
            return max(0, int(raw[-1].strip()))
        except ValueError:
            return 0

    def _has_explicit_mac_arg(self, runtime_args: list[str]) -> bool:
        for token in runtime_args:
            low = token.lower()
            if low == "-m":
                return True
            if low.startswith("-m") and len(low) > 2:
                return True
            if low == "--mac":
                return True
        return False

    def _has_explicit_sync_arg(self, runtime_args: list[str]) -> bool:
        for token in runtime_args:
            low = token.lower()
            if low == "-vsync" or low.startswith("-vsync"):
                return True
            if low == "-async" or low.startswith("-async"):
                return True
        return False

    def _has_explicit_port_arg(self, runtime_args: list[str]) -> bool:
        for token in runtime_args:
            low = token.lower()
            if low == "-p":
                return True
            if low.startswith("-p") and len(low) > 2 and low[2].isdigit():
                return True
        return False

    def _ensure_legacy_ports(self, runtime_args: list[str]) -> tuple[list[str], str | None]:
        if self._has_explicit_port_arg(runtime_args):
            return runtime_args, None
        return (
            ["-p", *runtime_args],
            "[PISTA] Se habilitaron puertos AirPlay legados (-p) para maxima compatibilidad de red/firewall.",
        )

    def _ensure_window_persistence(self, runtime_args: list[str]) -> tuple[list[str], str | None]:
        lowered = {arg.lower() for arg in runtime_args}
        if "-nc" in lowered:
            return runtime_args, None
        return (
            [*runtime_args, "-nc"],
            "[PISTA] Se activo '-nc' para mantener la ventana abierta entre reconexiones AirPlay.",
        )

    def _has_explicit_window_size_arg(self, runtime_args: list[str]) -> bool:
        for token in runtime_args:
            low = token.lower()
            if low == "-s":
                return True
            if low.startswith("-s") and len(low) > 2:
                return True
        return False

    def _ensure_window_size(self, runtime_args: list[str]) -> tuple[list[str], str | None]:
        if self._has_explicit_window_size_arg(runtime_args):
            return runtime_args, None
        return (
            [*runtime_args, "-s", self._DEFAULT_WINDOW_SIZE],
            (
                "[PISTA] Se aplico tamano de ventana UxPlay "
                f"{self._DEFAULT_WINDOW_SIZE} para mejorar visibilidad y grabacion."
            ),
        )

    def _has_explicit_renderer_args(self, runtime_args: list[str]) -> bool:
        index = 0
        while index < len(runtime_args):
            token = runtime_args[index]
            low = token.lower()
            if low in {"-vd", "-vc", "-vs"}:
                return True
            if low.startswith("-vd") or low.startswith("-vc"):
                return True
            if low.startswith("-vs") and not low.startswith("-vsync"):
                return True
            index += 1
        return False

    def _ensure_windows_d3d11_renderer(self, runtime_args: list[str]) -> tuple[list[str], str | None]:
        if os.name != "nt":
            return runtime_args, None
        if self._has_explicit_renderer_args(runtime_args):
            return runtime_args, None
        return (
            [
                *runtime_args,
                "-vd",
                "d3d11h264dec",
                "-vc",
                "d3d11convert",
                "-vs",
                "d3d11videosink",
            ],
            "[PISTA] Render Direct3D11 activado por defecto para asegurar ventana visible de mirroring.",
        )

    def _resolve_windows_active_adapters(self) -> list[tuple[str, str]]:
        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$selected = New-Object System.Collections.Generic.List[Object];"
            "function Add-Adapter([object]$adapter) {"
            "  if ($null -eq $adapter) { return };"
            "  if ($adapter.Status -ne 'Up') { return };"
            "  if ([string]::IsNullOrWhiteSpace($adapter.MacAddress)) { return };"
            "  if ($adapter.Name -like 'vEthernet*') { return };"
            "  if ($adapter.InterfaceDescription -like '*Hyper-V*') { return };"
            "  $name = ($adapter.Name + ' ' + $adapter.InterfaceDescription).ToLowerInvariant();"
            "  if ($name -match 'vpn|wireguard|openvpn|checkpoint|anyconnect|fortinet|tailscale|zerotier|tap|tun|virtualbox|vmware|hyper-v|loopback') { return };"
            "  foreach ($item in $selected) {"
            "    if ($item.ifIndex -eq $adapter.ifIndex) { return };"
            "  };"
            "  $selected.Add($adapter) | Out-Null;"
            "};"
            "$routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
            "Sort-Object -Property RouteMetric, InterfaceMetric;"
            "foreach ($route in $routes) {"
            "  $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex;"
            "  Add-Adapter $adapter;"
            "};"
            "$all = Get-NetAdapter | "
            "Sort-Object -Property @{Expression={ if ($_.HardwareInterface) { 0 } else { 1 } }}, ifIndex;"
            "foreach ($adapter in $all) {"
            "  Add-Adapter $adapter;"
            "};"
            "foreach ($adapter in $selected) {"
            "  Write-Output ($adapter.Name + '|' + $adapter.MacAddress);"
            "};"
            "if ($selected.Count -eq 0) {"
            "  $fallback = Get-NetAdapter | Where-Object {"
            "    $_.Status -eq 'Up' -and -not [string]::IsNullOrWhiteSpace($_.MacAddress)"
            "  } | Select-Object -First 1;"
            "  if ($null -ne $fallback) {"
            "    Write-Output ($fallback.Name + '|' + $fallback.MacAddress);"
            "  }"
            "};"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return []

        if completed.returncode not in (0, 1):
            return []

        rows = [line.strip() for line in (completed.stdout or "").splitlines() if "|" in line]
        adapters: list[tuple[str, str]] = []
        seen_macs: set[str] = set()
        for row in rows:
            adapter_name, raw_mac = row.split("|", 1)
            mac = self._normalize_mac(raw_mac)
            if not mac or mac in seen_macs:
                continue
            seen_macs.add(mac)
            adapters.append((adapter_name.strip() or "Adaptador", mac))
        adapters.sort(key=lambda item: self._adapter_priority(item[0]))
        return adapters

    def _adapter_priority(self, adapter_name: str) -> tuple[int, str]:
        low = adapter_name.lower()
        if self._is_wireless_adapter_name(adapter_name):
            return (0, low)
        if "ethernet" in low:
            return (1, low)
        return (2, low)

    def list_available_interfaces(self) -> list[tuple[str, str]]:
        if os.name != "nt":
            return []
        return self._resolve_windows_active_adapters()

    def _normalize_interface_alias(self, value: str | None) -> str | None:
        if value is None:
            return None
        alias = " ".join(str(value).split()).strip()
        if not alias:
            return None
        return alias

    def _select_preferred_adapter(
        self,
        adapters: list[tuple[str, str]],
        preferred_interface_alias: str | None,
    ) -> tuple[str, str] | None:
        if not preferred_interface_alias:
            return None
        preferred = preferred_interface_alias.casefold()
        for adapter_name, mac in adapters:
            if adapter_name.casefold() == preferred:
                return (adapter_name, mac)
        return None

    def _is_wireless_adapter_name(self, adapter_name: str) -> bool:
        low = adapter_name.lower()
        return "wi-fi" in low or "wifi" in low or "wlan" in low or "wireless" in low

    def _process_key(self, process: subprocess.Popen[str]) -> int:
        return process.pid if process.pid is not None else id(process)

    def _prune_dead_processes_locked(self) -> None:
        dead_keys = [key for key, process in self._processes.items() if process.poll() is not None]
        for key in dead_keys:
            self._processes.pop(key, None)
            self._reader_threads.pop(key, None)
            self._mirror_started_at.pop(key, None)
            self._ntp_error_counts.pop(key, None)
            self._pending_window_probes.pop(key, None)

    def _handle_stream_health(self, process: subprocess.Popen[str], line: str) -> None:
        low = line.lower()
        key = self._process_key(process)
        now = time.monotonic()

        if "raop_rtp_mirror starting mirroring" in low:
            canceled_pending = False
            with self._lock:
                self._mirror_started_at[key] = now
                self._ntp_error_counts[key] = 0
                if self._scheduled_recovery_id is not None:
                    self._scheduled_recovery_id = None
                    canceled_pending = True
            if canceled_pending:
                self._emit_log(
                    "[PISTA] Se detecto reconexion AirPlay. Se cancela reinicio automatico pendiente."
                )
            self._schedule_window_probe(process)
            return

        if "gstreamer error: output window was closed" in low:
            with self._lock:
                started_at = self._mirror_started_at.get(key, 0.0)

            if started_at > 0.0 and self._should_suppress_auto_recovery(now):
                return

            self._request_auto_recovery(
                trigger_line="GStreamer reporto cierre de la ventana de video durante mirroring.",
                prefer_d3d11=True,
            )
            return

        if "invalid ntp_time < gst_video_pipeline_base_time" in low:
            with self._lock:
                started_at = self._mirror_started_at.get(key, 0.0)
                count = self._ntp_error_counts.get(key, 0) + 1
                self._ntp_error_counts[key] = count

            if started_at <= 0.0:
                return
            if now - started_at > 22:
                return
            if count < 24:
                return
            if self._should_suppress_auto_recovery(now):
                return

            self._request_auto_recovery(
                trigger_line=(
                    "Se detectaron errores NTP repetidos al iniciar mirroring "
                    f"({count} eventos)."
                )
            )
            return

        has_running_stopped = "raop_rtp_mirror->running is no longer true" in low
        has_connection_error = (
            "raop_rtp_mirror error in accept" in low
            or "httpd error in parsing: hpe_closed_connection" in low
        )

        if not has_running_stopped and not has_connection_error:
            return

        with self._lock:
            started_at = self._mirror_started_at.get(key, 0.0)

        if started_at <= 0.0:
            return

        if has_running_stopped:
            with self._lock:
                # Mark session as closed to avoid stale recovery triggers while idle.
                self._mirror_started_at[key] = 0.0
            if now - started_at > 120:
                return

        if self._should_suppress_auto_recovery(now):
            return

        self._request_auto_recovery(trigger_line=line)

    def _schedule_window_probe(self, process: subprocess.Popen[str]) -> None:
        if os.name != "nt":
            return

        key = self._process_key(process)
        with self._lock:
            self._window_probe_sequence += 1
            probe_id = self._window_probe_sequence
            self._pending_window_probes[key] = probe_id

        def probe() -> None:
            time.sleep(self._window_probe_delay_seconds)

            with self._lock:
                current_probe_id = self._pending_window_probes.get(key)

            if current_probe_id != probe_id:
                return

            if process.poll() is not None:
                with self._lock:
                    self._pending_window_probes.pop(key, None)
                return

            if self._has_visible_window_for_process(process):
                with self._lock:
                    self._pending_window_probes.pop(key, None)
                return

            if self._try_restore_window_for_process(process):
                time.sleep(0.9)
                if self._has_visible_window_for_process(process):
                    with self._lock:
                        self._pending_window_probes.pop(key, None)
                    self._emit_log(
                        "[PISTA] Se restauro una ventana de video oculta/minimizada para este mirroring."
                    )
                    return

            with self._lock:
                self._pending_window_probes.pop(key, None)

            if self._should_suppress_auto_recovery():
                return

            self._emit_log(
                "[ADVERTENCIA] Mirroring activo sin ventana de video visible en PC. "
                "Se iniciara recuperacion automatica."
            )
            self._request_auto_recovery(
                trigger_line="Mirroring iniciado sin ventana visible detectado por sonda local.",
                prefer_d3d11=True,
            )

        threading.Thread(target=probe, daemon=True).start()

    def _try_restore_window_for_process(self, process: subprocess.Popen[str]) -> bool:
        if os.name != "nt":
            return False

        pid = process.pid or 0
        if pid <= 0:
            return False

        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return False

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        restored = [False]

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) != pid:
                return True

            title_len = int(user32.GetWindowTextLengthW(hwnd))
            if title_len <= 0:
                return True

            buffer = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buffer, title_len + 1)
            title = buffer.value.strip().lower()
            if not title:
                return True

            # Prefer mirroring windows and avoid touching hidden utility windows.
            if "renderer" not in title and "uxplay" not in title:
                return True

            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.SetForegroundWindow(hwnd)
            restored[0] = True
            return False

        try:
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except (AttributeError, OSError):
            return False

        return restored[0]

    def _has_visible_window_for_process(self, process: subprocess.Popen[str]) -> bool:
        if os.name != "nt":
            return False

        pid = process.pid or 0
        if pid <= 0:
            return False

        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            return False

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        visible_found = [False]

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True

            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) != pid:
                return True

            title_len = int(user32.GetWindowTextLengthW(hwnd))
            if title_len <= 0:
                return True

            buffer = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buffer, title_len + 1)
            title = buffer.value.strip()
            if not title:
                return True

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True

            width = int(rect.right) - int(rect.left)
            height = int(rect.bottom) - int(rect.top)
            if width < 120 or height < 120:
                return True

            visible_found[0] = True
            return False

        try:
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except (AttributeError, OSError):
            return False

        return visible_found[0]

    def _maybe_recover_on_early_exit(self, exit_code: int, started_at: float) -> None:
        if exit_code == 0 or started_at <= 0.0:
            return

        now = time.monotonic()
        if now - started_at > 45:
            return
        if self._should_suppress_auto_recovery(now):
            return

        self._request_auto_recovery(
            trigger_line=f"Proceso finalizo pronto tras iniciar mirroring (codigo {exit_code})."
        )

    def _should_suppress_auto_recovery(self, now: float | None = None) -> bool:
        instant = now if now is not None else time.monotonic()
        with self._lock:
            return instant < self._manual_stop_until

    def _request_auto_recovery(self, trigger_line: str, prefer_d3d11: bool = False) -> None:
        with self._lock:
            if self._auto_recovery_in_progress:
                return
            if self._scheduled_recovery_id is not None:
                return
            if self._auto_recovery_attempts >= self._max_auto_recovery_attempts:
                return
            if self._last_start_request is None:
                return

            request = self._last_start_request
            self._recovery_sequence += 1
            recovery_id = self._recovery_sequence
            self._scheduled_recovery_id = recovery_id

        self._emit_log(
            "[ADVERTENCIA] Se detecto un corte temprano de mirroring. "
            "Se programo recuperacion automatica."
        )
        self._emit_log(f"[PISTA] Detalle tecnico: {trigger_line}")

        def recover() -> None:
            try:
                time.sleep(self._recovery_delay_seconds)

                with self._lock:
                    if self._scheduled_recovery_id != recovery_id:
                        return
                    self._scheduled_recovery_id = None
                    if self._auto_recovery_attempts >= self._max_auto_recovery_attempts:
                        return
                    self._auto_recovery_in_progress = True
                    self._auto_recovery_attempts += 1
                    attempt = self._auto_recovery_attempts

                recovery_args = self._build_recovery_args(
                    list(request.extra_args),
                    attempt,
                    prefer_d3d11=prefer_d3d11,
                )
                uses_stable_fallback = recovery_args != list(request.extra_args)
                self._emit_log(
                    f"[ADVERTENCIA] Reiniciando receptor automaticamente (intento {attempt}/{self._max_auto_recovery_attempts})."
                )
                if uses_stable_fallback and prefer_d3d11:
                    self._emit_log(
                        "[PISTA] Recuperacion automatica: se forzara videosink D3D11 para recuperar ventana visible."
                    )
                elif uses_stable_fallback:
                    self._emit_log(
                        "[PISTA] Recuperacion automatica: se usara render estable sin DX11 para priorizar apertura de ventana."
                    )

                self.stop()
                time.sleep(0.8)
                self.start(
                    uxplay_path=request.uxplay_path,
                    receiver_name=request.receiver_name,
                    extra_args=recovery_args,
                    append_hostname_suffix=request.append_hostname_suffix,
                    preferred_interface_alias=request.preferred_interface_alias,
                    _from_auto_recovery=True,
                )
            except Exception as exc:  # noqa: BLE001
                self._emit_log(f"[ERROR] No se pudo recuperar el receptor automaticamente: {exc}")
            finally:
                with self._lock:
                    if self._scheduled_recovery_id == recovery_id:
                        self._scheduled_recovery_id = None
                    self._auto_recovery_in_progress = False

        threading.Thread(target=recover, daemon=True).start()

    def _build_recovery_args(self, args: list[str], attempt: int, prefer_d3d11: bool = False) -> list[str]:
        if attempt <= 0:
            return args
        if prefer_d3d11:
            forced = self._set_videosink_arg(args, "d3d11videosink")
            lowered = [token.lower() for token in forced]
            if "-vsync" not in lowered and "-async" not in lowered:
                forced.extend(["-vsync", "no"])
            return forced
        if not self._needs_stable_renderer(args):
            return args

        filtered: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            low = token.lower()
            if low in {"-vd", "-vc", "-vs"}:
                index += 2
                continue
            filtered.append(token)
            index += 1

        lowered = [token.lower() for token in filtered]
        if "-vsync" not in lowered:
            filtered.extend(["-vsync", "no"])
        return filtered

    def _set_videosink_arg(self, args: list[str], videosink: str) -> list[str]:
        filtered: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            low = token.lower()
            if low == "-vs":
                index += 1
                if index < len(args) and not args[index].startswith("-"):
                    index += 1
                continue
            if low.startswith("-vs") and len(low) > 3 and not low.startswith("-vsync"):
                index += 1
                continue
            filtered.append(token)
            index += 1
        return [*filtered, "-vs", videosink]

    def _needs_stable_renderer(self, args: list[str]) -> bool:
        joined = " ".join(args).lower()
        return (
            "d3d11videosink" in joined
            or "d3d11h264dec" in joined
            or "d3d11convert" in joined
        )

    def _terminate_process_quietly(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._emit_log("El proceso del receptor no cerro a tiempo. Forzando cierre.")
            process.kill()
            process.wait(timeout=3)

    def _format_instance_message(self, message: str, label: str | None) -> str:
        if not label:
            return message
        return f"[{label}] {message}"

    def _diagnose_windows_discovery_risks(self) -> list[str]:
        if os.name != "nt":
            return []

        hints: list[str] = []

        profiles = self._query_connection_profiles()
        has_public_wifi = False
        has_active_vpn = False
        for alias, category, ipv4, ipv6 in profiles:
            if ipv4 == "Disconnected" and ipv6 == "Disconnected":
                continue

            low_alias = alias.lower()
            if category == "Public" and "wi-fi" in low_alias:
                has_public_wifi = True
            if "vpn" in low_alias:
                has_active_vpn = True

        if has_public_wifi:
            hints.append(
                "[ADVERTENCIA] Wi-Fi esta en perfil Publico. Ese perfil suele bloquear descubrimiento AirPlay."
            )

        if has_active_vpn:
            hints.append(
                "[ADVERTENCIA] VPN activa detectada. Algunas VPN bloquean trafico local/mDNS y el iPhone no ve el receptor."
            )

        firewall_profile = self._query_current_firewall_profile_text()
        low_fw = self._normalize_for_match(firewall_profile)
        if "blockinbound,allowoutbound" in low_fw:
            hints.append(
                "[ADVERTENCIA] El perfil de firewall activo tiene 'BlockInbound'. Puede impedir conexion AirPlay."
            )
        if "localfirewallrules" in low_fw and "n/a" in low_fw:
            hints.append(
                "[ADVERTENCIA] Reglas locales de firewall deshabilitadas por politica (GPO). Requiere ajuste de TI/red."
            )

        return hints

    def _is_windows_vpn_active(self) -> bool:
        if os.name != "nt":
            return False
        profiles = self._query_connection_profiles()
        for alias, _category, ipv4, ipv6 in profiles:
            if ipv4 == "Disconnected" and ipv6 == "Disconnected":
                continue
            if "vpn" in alias.lower():
                return True
        return False

    def _query_connection_profiles(self) -> list[tuple[str, str, str, str]]:
        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Get-NetConnectionProfile | "
            "Select-Object InterfaceAlias,NetworkCategory,IPv4Connectivity,IPv6Connectivity | "
            "ForEach-Object {"
            "  Write-Output ($_.InterfaceAlias + '|' + $_.NetworkCategory + '|' + "
            "$_.IPv4Connectivity + '|' + $_.IPv6Connectivity)"
            "};"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return []

        if completed.returncode not in (0, 1):
            return []

        rows: list[tuple[str, str, str, str]] = []
        for line in (completed.stdout or "").splitlines():
            raw = line.strip()
            if not raw:
                continue
            parts = raw.split("|")
            if len(parts) != 4:
                continue
            alias, category, ipv4, ipv6 = [part.strip() for part in parts]
            rows.append((alias, category, ipv4, ipv6))
        return rows

    def _query_current_firewall_profile_text(self) -> str:
        try:
            completed = subprocess.run(
                ["netsh", "advfirewall", "show", "currentprofile"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return ""

        output = (completed.stdout or "").strip()
        if output:
            return output
        return (completed.stderr or "").strip()

    def _normalize_for_match(self, value: str) -> str:
        lowered = value.lower()
        return "".join(ch for ch in lowered if ch.isascii())

    def _normalize_mac(self, raw_mac: str) -> str | None:
        compact = "".join(ch for ch in raw_mac if ch.isalnum())
        if len(compact) != 12:
            return None
        hex_part = compact.upper()
        if any(ch not in "0123456789ABCDEF" for ch in hex_part):
            return None
        return ":".join(hex_part[i : i + 2] for i in range(0, 12, 2))

    def _emit_log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(self._normalize_log_message(message))

    def _emit_state(self, running: bool) -> None:
        if self._on_state_change is not None:
            self._on_state_change(running)

    def _creationflags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _startupinfo(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return startupinfo

    def _normalize_log_message(self, message: str) -> str:
        # Normalize common mojibake sequences produced by mixed code pages.
        if "\u00c3" not in message and "\u00c2" not in message:
            return message

        normalized = message
        for _ in range(2):
            try:
                repaired = normalized.encode("latin-1").decode("utf-8")
            except UnicodeError:
                break
            if repaired == normalized:
                break
            normalized = repaired
            if "\u00c3" not in normalized and "\u00c2" not in normalized:
                break
        return normalized

