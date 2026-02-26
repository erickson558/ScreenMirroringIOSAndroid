from __future__ import annotations

from dataclasses import dataclass

DEVICE_MODE_IPHONE = "iphone"
DEVICE_MODE_ANDROID = "android"


@dataclass(frozen=True, slots=True)
class DeviceMode:
    key: str
    label: str
    description: str


IPHONE_MODE = DeviceMode(
    key=DEVICE_MODE_IPHONE,
    label="iPhone (AirPlay)",
    description="Usa AirPlay con UxPlay para espejo de pantalla y audio.",
)

ANDROID_MODE = DeviceMode(
    key=DEVICE_MODE_ANDROID,
    label="Android (Proyección inalámbrica)",
    description=(
        "Usa Miracast/Proyección inalámbrica. En el teléfono: Cast, Smart View o "
        "Proyección inalámbrica y selecciona este PC."
    ),
)

ALL_DEVICE_MODES: tuple[DeviceMode, ...] = (IPHONE_MODE, ANDROID_MODE)
DEFAULT_DEVICE_MODE_KEY = IPHONE_MODE.key


def list_device_modes() -> tuple[DeviceMode, ...]:
    return ALL_DEVICE_MODES


def get_device_mode(mode_key: str) -> DeviceMode:
    for mode in ALL_DEVICE_MODES:
        if mode.key == mode_key:
            return mode
    raise KeyError(mode_key)
