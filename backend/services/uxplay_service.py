from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from typing import Callable
from urllib.request import urlopen

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
    _DEFAULT_WINDOW_SIZE = "960x540@30"

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
        self._window_probe_delay_seconds = 10.0
        self._window_probe_sequence = 0
        self._pending_window_probes: dict[int, int] = {}
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            self._prune_dead_processes_locked()
            return bool(self._processes)

    def list_process_ids(self) -> list[int]:
        with self._lock:
            self._prune_dead_processes_locked()
            pids: list[int] = []
            for process in self._processes.values():
                pid = process.pid or 0
                if pid > 0:
                    pids.append(pid)
            return pids

    def start(
        self,
        uxplay_path: Path,
        receiver_name: str,
        extra_args: list[str] | None = None,
        append_hostname_suffix: bool = True,
        preferred_interface_alias: str | None = None,  # ignored in this legacy version
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
            # ``preferred_interface_alias`` is accepted by newer versions of
            # this service but ignored in the original implementation; we
            # consume it here to prevent unexpected argument errors.
            _ = preferred_interface_alias

            runtime_args = [arg for arg in runtime_args if arg.lower() != "-nh"]
            if not append_hostname_suffix:
                runtime_args = ["-nh", *runtime_args]
            runtime_args, hint = self._strip_unsupported_runtime_args(runtime_args)
            if hint:
                startup_hints.append(hint)
            preferred_alias = self._normalize_interface_alias(preferred_interface_alias)

            self._try_prioritize_interface_metrics()

            # Ensure compatibility flags by default: legacy ports, keep window
            # visible and non-blocking, default window size and Windows D3D11
            # renderer where applicable. Helpers return (args, hint).
            runtime_args, hint = self._ensure_legacy_ports(runtime_args)
            if hint:
                startup_hints.append(hint)

            runtime_args, hint = self._ensure_window_persistence(runtime_args)
            if hint:
                startup_hints.append(hint)

            runtime_args, hint = self._ensure_window_size(runtime_args)
            if hint:
                startup_hints.append(hint)

            runtime_args, hint = self._ensure_windows_d3d11_renderer(
                runtime_args,
                allow_default=not _from_auto_recovery,
            )
            if hint:
                startup_hints.append(hint)

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
            # Keep console popups suppressed via creationflags, but do not force-hide
            # startup window state; forcing SW_HIDE can prevent renderer window visibility.
            startupinfo = None
            environment = self._build_runtime_env(normalized_path)
            try:
                for instance_name, instance_args, instance_hint in launch_plans:
                    command = [str(normalized_path), "-n", instance_name, *instance_args]
                    # record command for diagnostics
                    command_line = " ".join(command)
                    command_logs.append(command_line)
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
        self._emit_log("[PISTA] Se usa configuracion minimalista de UxPlay para maxima compatibilidad AirPlay.")
        self._emit_log(
            "[PISTA] Espera a ver 'Initialized server socket(s)' antes de conectar en iPhone."
        )
        if _from_auto_recovery:
            self._emit_log("[PISTA] Receptor reiniciado automaticamente para recuperar el primer enlace AirPlay.")
        for hint in self._diagnose_windows_discovery_risks():
            self._emit_log(hint)
        # try to help the user by adding a firewall rule for the uxplay executable
        self._ensure_firewall_rule(normalized_path)
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

    def _strip_unsupported_runtime_args(self, runtime_args: list[str]) -> tuple[list[str], str | None]:
        cleaned: list[str] = []
        removed_any = False
        index = 0
        while index < len(runtime_args):
            token = runtime_args[index]
            low = token.lower()

            if low == "-t":
                removed_any = True
                index += 1
                if index < len(runtime_args):
                    next_token = runtime_args[index]
                    if not str(next_token).strip().startswith("-"):
                        index += 1
                continue

            if low.startswith("-t") and len(low) > 2:
                removed_any = True
                index += 1
                continue

            cleaned.append(token)
            index += 1

        if not removed_any:
            return runtime_args, None

        return (
            cleaned,
            "[PISTA] Se removio parametro no soportado '-t' para compatibilidad con UxPlay 1.68.",
        )

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
        """Build launch plans for AirPlay receiver.

        IMPORTANT: UxPlay MUST bind to a specific MAC address (-m) to ensure
        Bonjour announces through the CORRECT network interface. If we use
        0.0.0.0 (all interfaces), Bonjour may choose a virtual/WSL interface
        instead of the real Wi-Fi interface, making the service invisible to
        the iPhone.
        """
        adapters = self.list_available_interfaces()

        # helper to build plan tuple with optional hint
        def plan(name: str, args: list[str], hint: str | None = None) -> tuple[str, list[str], str | None]:
            return (name, args, hint)

        if adapters:
            # Find first valid adapter following priority: Ethernet/LAN > Wi-Fi 2 > other Wi-Fi > others
            selected_adapter = None

            for iface_name, mac in adapters:
                if self._is_excluded_adapter_name(iface_name):
                    continue
                selected_adapter = (iface_name, mac)
                break

            # If user specified preferred interface, try to use it
            if preferred_interface_alias:
                preferred_sel = self._select_preferred_adapter(adapters, preferred_interface_alias)
                if preferred_sel:
                    selected_adapter = preferred_sel

            if selected_adapter:
                iface_name, mac = selected_adapter
                hint = f"[PISTA] UxPlay enlazado a interfaz '{iface_name}' (MAC {mac}) para que Bonjour anuncie en la red correcta."
                if self._is_windows_vpn_active():
                    hint += " [ADVERTENCIA] VPN activa detectada; puede bloquear mDNS."
                return [plan(f"{receiver_name} [{iface_name}]", [*runtime_args, "-m", mac], hint)]


        # Fallback if no adapters found
        return [(receiver_name, runtime_args, None)]

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
        # Add both -nc (keep window) and -async (non-blocking window creation)
        new_args = [*runtime_args, "-nc", "-async"]
        return (
            new_args,
            "[PISTA] Se activo '-nc -async' para que la ventana aparezca inmediatamente sin bloqueos.",
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

    def _ensure_windows_d3d11_renderer(
        self,
        runtime_args: list[str],
        allow_default: bool = True,
    ) -> tuple[list[str], str | None]:
        if os.name != "nt":
            return runtime_args, None
        if self._has_explicit_renderer_args(runtime_args):
            return runtime_args, None
        if not allow_default:
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
        adapters = self._query_windows_adapters(physical_only=True)
        if not adapters:
            adapters = self._query_windows_adapters(physical_only=False)

        normalized: list[tuple[str, str]] = []
        seen_macs: set[str] = set()
        for adapter_name, raw_mac in adapters:
            clean_name = (adapter_name or "").strip() or "Adaptador"
            if self._is_excluded_adapter_name(clean_name):
                continue
            mac = self._normalize_mac(raw_mac)
            if not mac or mac in seen_macs:
                continue
            seen_macs.add(mac)
            normalized.append((clean_name, mac))

        normalized.sort(key=lambda item: self._adapter_priority(item[0]))
        return normalized

    def _query_windows_adapters(self, physical_only: bool) -> list[tuple[str, str]]:
        if physical_only:
            adapter_source = "Get-NetAdapter -Physical -ErrorAction SilentlyContinue"
        else:
            adapter_source = "Get-NetAdapter -ErrorAction SilentlyContinue"

        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$adapters = {adapter_source} | Where-Object {{ $_.Status -eq 'Up' -and -not [string]::IsNullOrWhiteSpace($_.MacAddress) }};"
            "if ($null -eq $adapters -or @($adapters).Count -eq 0) { return };"
            "$adapters | Select-Object Name, MacAddress | ConvertTo-Json -Compress"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return []

        if completed.returncode not in (0, 1):
            return []

        payload = (completed.stdout or "").strip()
        if not payload:
            return []

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return []

        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            return []

        result: list[tuple[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name") or "").strip()
            mac = str(item.get("MacAddress") or "").strip()
            if name and mac:
                result.append((name, mac))
        return result

    def _adapter_priority(self, adapter_name: str) -> tuple[int, str]:
        low = adapter_name.lower()
        if self._is_excluded_adapter_name(adapter_name):
            return (9, low)
        if self._is_ethernet_adapter_name(adapter_name):
            return (0, low)
        # Prefer explicit Wi-Fi 2 as second option when LAN is unavailable
        if low.strip() == "wi-fi 2" or low.strip() == "wifi 2":
            return (1, low)
        if self._is_wireless_adapter_name(adapter_name):
            return (2, low)
        return (3, low)

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

    def _is_ethernet_adapter_name(self, adapter_name: str) -> bool:
        low = adapter_name.lower()
        return "ethernet" in low or "lan" in low

    def _is_excluded_adapter_name(self, adapter_name: str) -> bool:
        low = adapter_name.lower()
        return any(
            token in low
            for token in (
                "vpn",
                "wireguard",
                "openvpn",
                "checkpoint",
                "anyconnect",
                "fortinet",
                "tailscale",
                "zerotier",
                "hamachi",
                "vethernet",
                "hyper-v",
                "virtualbox",
                "vmware",
                "loopback",
                "tun",
                "tap",
            )
        )

    def _try_prioritize_interface_metrics(self) -> None:
        if os.name != "nt":
            return

        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$changed = New-Object System.Collections.Generic.List[string];"
            "$physical = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Status -eq 'Up' };"
            "$lan = $physical | Where-Object { $_.Name -match 'Ethernet|LAN' } | Select-Object -First 1;"
            "if ($null -ne $lan) {"
            "  $lanIp = Get-NetIPInterface -InterfaceIndex $lan.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue;"
            "  if ($null -ne $lanIp -and $lanIp.InterfaceMetric -gt 10) {"
            "    Set-NetIPInterface -InterfaceIndex $lan.ifIndex -AddressFamily IPv4 -InterfaceMetric 10;"
            "    $changed.Add('LAN:' + $lan.Name) | Out-Null;"
            "  }"
            "};"
            "$wifi = $physical | Where-Object { $_.Name -match 'Wi-Fi 2|WiFi 2|Wi-Fi|WiFi|WLAN' } | Select-Object -First 1;"
            "if ($null -ne $wifi) {"
            "  $wifiIp = Get-NetIPInterface -InterfaceIndex $wifi.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue;"
            "  if ($null -ne $wifiIp -and $wifiIp.InterfaceMetric -gt 20) {"
            "    Set-NetIPInterface -InterfaceIndex $wifi.ifIndex -AddressFamily IPv4 -InterfaceMetric 20;"
            "    $changed.Add('WIFI:' + $wifi.Name) | Out-Null;"
            "  }"
            "};"
            "if ($changed.Count -gt 0) { $changed | ForEach-Object { Write-Output $_ } }"
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
            return

        if completed.returncode not in (0, 1):
            return

        changes = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        if changes:
            self._emit_log(
                "[PISTA] Se ajustaron metricas de red para priorizar anuncio AirPlay en LAN/Wi-Fi: "
                + ", ".join(changes)
            )

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

            # Window still not visible after first attempt
            # Try additional checks with short delays before giving up
            for retry in range(3):
                time.sleep(1.5)
                if self._has_visible_window_for_process(process):
                    with self._lock:
                        self._pending_window_probes.pop(key, None)
                    self._emit_log("[PISTA] Ventana detectada en retry posterior.")
                    return
                
                if self._try_restore_window_for_process(process):
                    time.sleep(0.9)
                    if self._has_visible_window_for_process(process):
                        with self._lock:
                            self._pending_window_probes.pop(key, None)
                        self._emit_log("[PISTA] Ventana restaurada en retry.")
                        return

            with self._lock:
                self._pending_window_probes.pop(key, None)

            if self._should_suppress_auto_recovery():
                return

            self._emit_log(
                "[ADVERTENCIA] Mirroring activo sin ventana de video visible en PC tras multiples intentos. "
                "Se iniciara recuperacion automatica."
            )
            self._request_auto_recovery(
                trigger_line="Mirroring iniciado sin ventana visible detectado por sonda local (tras intentos).",
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
        CHILDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        visible_found = [False]

        def is_qualifying_window(hwnd: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return False

            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if int(owner_pid.value) != pid:
                return False

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False

            width = int(rect.right) - int(rect.left)
            height = int(rect.bottom) - int(rect.top)
            if width < 120 or height < 120:
                return False

            return True

        def child_enum_proc(child_hwnd: int, _lparam: int) -> bool:
            if is_qualifying_window(child_hwnd):
                visible_found[0] = True
                return False
            return True

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if is_qualifying_window(hwnd):
                visible_found[0] = True
                return False

            try:
                user32.EnumChildWindows(hwnd, CHILDENUMPROC(child_enum_proc), 0)
            except (AttributeError, OSError):
                return True

            if visible_found[0]:
                return False
            return True

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

        # attempt a simple mDNS probe to detect if multicast replies are possible
        if not self._probe_mdns():
            hints.append(
                "[ADVERTENCIA] No se recibieron respuestas mDNS; podria haber bloqueo de multicast o falta de servicio Bonjour."
            )

        firewall_profile = self._query_current_firewall_profile_text()
        low_fw = self._normalize_for_match(firewall_profile)
        has_block_inbound = "blockinbound,allowoutbound" in low_fw
        has_gpo_locked_rules = "localfirewallrules" in low_fw and "n/a" in low_fw
        has_effective_discovery_rules = self._has_effective_discovery_firewall_rules()

        if has_block_inbound:
            hints.append(
                "[ADVERTENCIA] El perfil de firewall activo tiene 'BlockInbound'. Puede impedir conexion AirPlay."
            )
        if has_gpo_locked_rules:
            hints.append(
                "[ADVERTENCIA] Reglas locales de firewall deshabilitadas por politica (GPO). Requiere ajuste de TI/red."
            )
        if has_block_inbound and has_gpo_locked_rules and not has_effective_discovery_rules:
            hints.append(
                "[ERROR] Descubrimiento AirPlay bloqueado por politica corporativa (GPO): "
                "firewall en BlockInbound sin reglas locales habilitadas."
            )
            hints.append(
                "[PISTA] Solicita a TI habilitar excepcion en GPO para Bonjour/mDNS (UDP 5353) "
                "y AirPlay (UDP 6000,6001,7011; TCP 7000,7001,7100) en perfil Privado."
            )
        elif has_block_inbound and has_gpo_locked_rules and has_effective_discovery_rules:
            hints.append(
                "[PISTA] Firewall administrado por GPO detectado, pero existen reglas efectivas para AirPlay/mDNS."
            )

        virtual_priority_risk = self._detect_virtual_interface_metric_risk()
        if virtual_priority_risk:
            hints.append(virtual_priority_risk)

        # Check Bonjour service
        if not self._check_bonjour_service():
            hints.append(
                "[ADVERTENCIA] Servicio Bonjour no esta instalado o corriendo. Sin Bonjour, "
                "el iPhone no puede descubrir el receptor. Instala Bonjour desde Apple: "
                "https://support.apple.com/downloads/bonjour"
            )

        return hints

    def get_network_diagnostics(self) -> list[str]:
        """Public wrapper to retrieve network / mDNS diagnostics.

        Returns a list of human-readable hints and warnings useful for
        diagnosing why AirPlay discovery may fail on the current host.
        """
        hints = self._diagnose_windows_discovery_risks()
        # Check Bonjour service availability
        if not self._check_bonjour_service():
            hints.append(
                "[ADVERTENCIA] Servicio Bonjour no esta instalado o corriendo. Sin Bonjour, "
                "el iPhone no puede descubrir el receptor. Instala Bonjour desde Apple: "
                "https://support.apple.com/downloads/bonjour"
            )
        return hints

    def repair_airplay_connectivity(self) -> bool:
        """Run a best-effort local repair workflow for AirPlay discovery on Windows."""
        if os.name != "nt":
            self._emit_log("[ADVERTENCIA] Reparacion de red AirPlay solo disponible en Windows.")
            return False

        self._emit_log("[PISTA] Iniciando reparacion rapida de red AirPlay...")

        self._try_prioritize_interface_metrics()
        self._restart_bonjour_service()
        self._flush_dns_cache()

        restart_request: _StartRequest | None
        with self._lock:
            restart_request = self._last_start_request
            has_running = bool(self._processes)

        if has_running and restart_request is not None:
            self._emit_log("[PISTA] Reiniciando receptor para republicar anuncio AirPlay...")
            self.stop()
            time.sleep(0.8)
            try:
                self.start(
                    uxplay_path=restart_request.uxplay_path,
                    receiver_name=restart_request.receiver_name,
                    extra_args=list(restart_request.extra_args),
                    append_hostname_suffix=restart_request.append_hostname_suffix,
                    preferred_interface_alias=restart_request.preferred_interface_alias,
                )
            except Exception as exc:  # noqa: BLE001
                self._emit_log(f"[ADVERTENCIA] No se pudo reiniciar receptor tras reparacion: {exc}")

        for line in self.get_network_diagnostics():
            self._emit_log(line)

        self._emit_log("[PISTA] Reparacion de red AirPlay finalizada.")
        return True

    def _restart_bonjour_service(self) -> None:
        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$svc = Get-Service -Name 'Bonjour Service' -ErrorAction SilentlyContinue;"
            "if ($null -eq $svc) { Write-Output 'BONJOUR_MISSING'; exit 0 };"
            "try { Restart-Service -Name 'Bonjour Service' -Force -ErrorAction Stop;"
            "Write-Output 'BONJOUR_RESTARTED'; exit 0 } catch {}"
            "try { Start-Service -Name 'Bonjour Service' -ErrorAction Stop;"
            "Write-Output 'BONJOUR_STARTED'; exit 0 } catch {}"
            "Write-Output 'BONJOUR_FAILED';"
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
            self._emit_log("[ADVERTENCIA] No se pudo verificar/reiniciar Bonjour Service.")
            return

        raw = "\n".join((completed.stdout or "").splitlines()).upper()
        if "BONJOUR_RESTARTED" in raw:
            self._emit_log("[PISTA] Bonjour Service reiniciado correctamente.")
        elif "BONJOUR_STARTED" in raw:
            self._emit_log("[PISTA] Bonjour Service iniciado correctamente.")
        elif "BONJOUR_MISSING" in raw:
            self._emit_log("[ADVERTENCIA] Bonjour Service no esta instalado.")
        else:
            self._emit_log("[ADVERTENCIA] No se pudo reiniciar Bonjour Service (posible falta de permisos).")

    def _flush_dns_cache(self) -> None:
        try:
            completed = subprocess.run(
                ["ipconfig", "/flushdns"],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            self._emit_log("[ADVERTENCIA] No se pudo limpiar cache DNS local.")
            return

        if completed.returncode == 0:
            self._emit_log("[PISTA] Cache DNS local limpiada.")
        else:
            self._emit_log("[ADVERTENCIA] No se pudo limpiar cache DNS local (sin privilegios o politica).")

    def _check_bonjour_service(self) -> bool:
        """Check if Bonjour/mDNS service is available on Windows."""
        if os.name != "nt":
            return True  # Assume mDNS available on non-Windows

        try:
            output = subprocess.run(
                ["sc", "query", "Bonjour Service"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
            # Check if service is running
            return "RUNNING" in (output.stdout or "").upper()
        except Exception:  # noqa: BLE001
            return False

    def install_bonjour(self) -> bool:
        """Attempt to automatically download and install Bonjour on Windows.

        Returns True if installation succeeds, False otherwise.
        """
        if os.name != "nt":
            self._emit_log("Bonjour installation solo se soporta en Windows.")
            return False

        # Check if already installed
        if self._check_bonjour_service():
            self._emit_log("[PISTA] Bonjour ya esta instalado.")
            return True

        self._emit_log("[PISTA] Descargando Bonjour desde Apple...")

        try:
            # Download Bonjour installer from Apple
            bonjour_url = "https://support.apple.com/downloads/DL100/en_US/BonjourPSSetup.exe"
            temp_dir = Path(tempfile.gettempdir())
            installer_path = temp_dir / "BonjourPSSetup.exe"

            # Download with timeout
            self._emit_log(f"Descargando desde: {bonjour_url}")
            response = urlopen(bonjour_url, timeout=30)
            with open(installer_path, "wb") as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)

            if not installer_path.exists():
                self._emit_log("[ERROR] No se pudo descargar el instalador de Bonjour.")
                return False

            self._emit_log(f"[PISTA] Instalador descargado: {installer_path}")
            self._emit_log("[PISTA] Ejecutando instalador de Bonjour...")

            # Run installer with admin privileges
            # Use 'runas' through shellexecute via PowerShell
            ps_cmd = (
                f'Start-Process -FilePath "{installer_path}" '
                '-Verb RunAs -Wait'
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
            )

            # Wait for service to start
            for i in range(10):
                time.sleep(1)
                if self._check_bonjour_service():
                    self._emit_log("[PISTA] Bonjour instalado e iniciado exitosamente.")
                    return True

            self._emit_log("[ADVERTENCIA] Bonjour se instalo pero el servicio no inicio. Reinicia tu equipo.")
            return False

        except Exception as e:  # noqa: BLE001
            self._emit_log(f"[ERROR] Error instalando Bonjour: {e}")
            return False

        finally:
            # Clean up temp installer
            try:
                if installer_path.exists():
                    installer_path.unlink()
            except Exception:  # noqa: BLE001
                pass

    def install_uxplay_runtime(self, target_dir: Path) -> Path:
        """Install/copy UxPlay runtime into target_dir and return uxplay.exe path."""
        destination = target_dir.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)

        source = self._find_uxplay_runtime_source()
        if source is None:
            raise FileNotFoundError(
                "No se encontro un runtime de UxPlay para instalar. "
                "Coloca la carpeta 'tools/uxplay' junto al .exe o al proyecto."
            )

        if source != destination:
            shutil.copytree(source, destination, dirs_exist_ok=True)

        uxplay_exe = destination / "bin" / "uxplay.exe"
        if not uxplay_exe.exists():
            uxplay_exe = destination / "uxplay.exe"
        if not uxplay_exe.exists():
            raise FileNotFoundError(f"La instalacion de UxPlay no contiene uxplay.exe en: {destination}")

        self._emit_log(f"[PISTA] Runtime UxPlay instalado en: {destination}")
        return uxplay_exe.resolve()

    def _find_uxplay_runtime_source(self) -> Path | None:
        candidates: list[Path] = []

        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "tools" / "uxplay")
        candidates.append(exe_dir / "uxplay")

        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mei_dir = Path(str(meipass)).resolve()
            candidates.append(mei_dir / "tools" / "uxplay")
            candidates.append(mei_dir / "uxplay")

        module_root = Path(__file__).resolve().parents[2]
        candidates.append(module_root / "tools" / "uxplay")
        candidates.append(Path.cwd() / "tools" / "uxplay")

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if self._is_uxplay_runtime_folder(candidate):
                return candidate
        return None

    def _is_uxplay_runtime_folder(self, folder: Path) -> bool:
        if not folder.exists() or not folder.is_dir():
            return False
        if (folder / "bin" / "uxplay.exe").exists():
            return True
        return (folder / "uxplay.exe").exists()

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

    def _probe_mdns(self) -> bool:
        """Send a quick mDNS query and return True if any response is received."""
        try:
            import socket

            # create IPv4 UDP socket bound to 0.0.0.0:0
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(1.0)
            # allow address reuse so multiple apps can send/receive
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # join multicast group on all interfaces
            group = socket.inet_aton("224.0.0.251")
            mreq = group + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # build simple DNS query for _airplay._tcp.local PTR
            # header: id=0, flags=0x0000, qdcount=1
            query = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            # encode _airplay._tcp.local
            for part in ["_airplay", "_tcp", "local"]:
                query += bytes([len(part)]) + part.encode("ascii")
            query += b"\x00"  # terminator
            query += b"\x00\x0c"  # type PTR
            query += b"\x00\x01"  # class IN

            sock.sendto(query, ("224.0.0.251", 5353))
            try:
                data, addr = sock.recvfrom(1024)
                return True
            except socket.timeout:
                return False
        except Exception:  # noqa: BLE001
            return False

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

    def _ensure_firewall_rule(self, uxplay_executable: Path) -> None:
        """Attempt to add a Windows firewall rule allowing inbound connections to UxPlay.

        This helps users who have a restrictive profile (BlockInbound) or GPO.
        If the rule already exists the command will quietly return; if we lack
        privileges we emit a warning.
        """
        if os.name != "nt":
            return

        firewall_profile = self._query_current_firewall_profile_text()
        low_fw = self._normalize_for_match(firewall_profile)
        has_gpo_locked_rules = "localfirewallrules" in low_fw and "n/a" in low_fw
        has_effective_discovery_rules = self._has_effective_discovery_firewall_rules()
        if has_gpo_locked_rules:
            if has_effective_discovery_rules:
                self._emit_log(
                    "[PISTA] Firewall administrado por GPO: se omitio alta de reglas locales porque ya existen reglas efectivas."
                )
            else:
                self._emit_log(
                    "[ERROR] No se pueden crear reglas locales de firewall: controladas por politica corporativa (GPO)."
                )
                self._emit_log(
                    "[PISTA] Solicita a TI agregar excepciones en GPO para Bonjour/mDNS (UDP 5353) "
                    "y AirPlay (UDP 6000,6001,7011; TCP 7000,7001,7100)."
                )
            return

        rule_name = "UxPlay AirPlay"
        exe_path = str(uxplay_executable)

        rules: list[tuple[str, list[str]]] = [
            (
                rule_name,
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={rule_name}",
                    "dir=in",
                    "action=allow",
                    f"program={exe_path}",
                    "profile=private",
                    "enable=yes",
                ],
            ),
            (
                "UxPlay mDNS",
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    "name=UxPlay mDNS",
                    "dir=in",
                    "action=allow",
                    "protocol=UDP",
                    "localport=5353",
                    "profile=private",
                    "enable=yes",
                ],
            ),
            (
                "UxPlay AirPlay UDP",
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    "name=UxPlay AirPlay UDP",
                    "dir=in",
                    "action=allow",
                    "protocol=UDP",
                    "localport=6000,6001,7011",
                    "profile=private",
                    "enable=yes",
                ],
            ),
            (
                "UxPlay AirPlay TCP",
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    "name=UxPlay AirPlay TCP",
                    "dir=in",
                    "action=allow",
                    "protocol=TCP",
                    "localport=7000,7001,7100",
                    "profile=private",
                    "enable=yes",
                ],
            ),
        ]

        try:
            for label, command in rules:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=8,
                )
                std_err = (result.stderr or "").strip()
                if result.returncode == 0:
                    self._emit_log(f"[PISTA] Regla de firewall aplicada: {label}.")
                else:
                    detail = std_err or f"codigo {result.returncode}"
                    self._emit_log(f"[ADVERTENCIA] No se pudo aplicar regla de firewall '{label}': {detail}")
        except Exception as exc:  # noqa: BLE001
            self._emit_log(f"[ADVERTENCIA] no se pudo crear regla de firewall: {exc}")

    def _startupinfo(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt":
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return startupinfo

    def _has_effective_discovery_firewall_rules(self) -> bool:
        if os.name != "nt":
            return True

        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$count = (Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow | "
            "Where-Object { $_.DisplayName -match 'UxPlay|AirPlay|mDNS|Bonjour|LonelyScreen' } | "
            "Measure-Object).Count;"
            "Write-Output $count;"
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
            return False

        if completed.returncode not in (0, 1):
            return False

        raw = (completed.stdout or "").strip().splitlines()
        if not raw:
            return False
        try:
            return int(raw[-1].strip()) > 0
        except ValueError:
            return False

    def _detect_virtual_interface_metric_risk(self) -> str | None:
        if os.name != "nt":
            return None

        powershell_script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$wifi = Get-NetIPInterface -AddressFamily IPv4 | "
            "Where-Object { $_.InterfaceAlias -like '*Wi-Fi*' -and $_.ConnectionState -eq 'Connected' } | "
            "Sort-Object InterfaceMetric | Select-Object -First 1;"
            "if ($null -eq $wifi) { return };"
            "$candidates = Get-NetIPInterface -AddressFamily IPv4 | "
            "Where-Object { $_.ConnectionState -eq 'Connected' -and $_.InterfaceAlias -ne $wifi.InterfaceAlias -and $_.InterfaceMetric -lt $wifi.InterfaceMetric };"
            "foreach ($item in $candidates) {"
            "  $adapter = Get-NetAdapter -InterfaceIndex $item.InterfaceIndex -ErrorAction SilentlyContinue;"
            "  $joined = (($item.InterfaceAlias + ' ' + ($adapter.InterfaceDescription + ''))).ToLowerInvariant();"
            "  if ($joined -match 'vethernet|hyper-v|virtual|vmware|hamachi|vpn|tap|tun|loopback|tailscale|zerotier') {"
            "    Write-Output ($item.InterfaceAlias + '|' + $item.InterfaceMetric + '|' + $wifi.InterfaceAlias + '|' + $wifi.InterfaceMetric);"
            "    break;"
            "  }"
            "}"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=7,
                stdin=subprocess.DEVNULL,
                creationflags=self._creationflags(),
                startupinfo=self._startupinfo(),
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if completed.returncode not in (0, 1):
            return None

        rows = [line.strip() for line in (completed.stdout or "").splitlines() if "|" in line]
        if not rows:
            return None

        parts = rows[0].split("|")
        if len(parts) != 4:
            return None

        virtual_alias, virtual_metric, wifi_alias, wifi_metric = [part.strip() for part in parts]
        return (
            "[ADVERTENCIA] Interfaz virtual con mayor prioridad detectada "
            f"({virtual_alias}, metrica {virtual_metric}) sobre {wifi_alias} (metrica {wifi_metric}). "
            "Esto puede ocultar el receptor al iPhone. "
            f"Ejecuta como admin: Set-NetIPInterface -InterfaceAlias '{wifi_alias}' -AddressFamily IPv4 -InterfaceMetric 10"
        )

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

