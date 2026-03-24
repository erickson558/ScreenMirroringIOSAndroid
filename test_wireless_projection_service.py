from __future__ import annotations

from backend.services.wireless_projection_service import WirelessProjectionService


def test_get_diagnostics_parses_supported_miracast() -> None:
    service = WirelessProjectionService()
    service._probe_wireless_display_capability = lambda: "Unknown"  # type: ignore[method-assign]
    service._probe_miracast_status_line = (  # type: ignore[method-assign]
        lambda: "Monitor inalámbrico admitido: Sí (controlador de gráficos: Sí, controlador de Wi-Fi: Sí)"
    )

    diagnostics = service.get_diagnostics()

    assert diagnostics["wireless_display_capability"] == "Unknown"
    assert diagnostics["miracast_receiver_supported"] is True


def test_get_diagnostics_reports_unknown_when_status_line_is_missing() -> None:
    service = WirelessProjectionService()
    service._probe_wireless_display_capability = lambda: "Unknown"  # type: ignore[method-assign]
    service._probe_miracast_status_line = lambda: ""  # type: ignore[method-assign]

    diagnostics = service.get_diagnostics()

    assert diagnostics["miracast_status_line"] == ""
    assert diagnostics["miracast_receiver_supported"] is None
