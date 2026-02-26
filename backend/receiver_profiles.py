from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReceiverProfile:
    key: str
    label: str
    description: str
    args: tuple[str, ...]


COMPATIBILITY_PROFILE = ReceiverProfile(
    key="compatibility",
    label="Compatibilidad (ajuste NTP)",
    description="Más estable para espejo de iPhone. Desactiva sincronización problemática de tiempo.",
    args=("-vsync", "no"),
)

BALANCED_PROFILE = ReceiverProfile(
    key="balanced",
    label="Equilibrado (UxPlay por defecto)",
    description="Comportamiento nativo de UxPlay. Mejor sincronía A/V cuando NTP funciona correctamente.",
    args=(),
)

LOW_LATENCY_PROFILE = ReceiverProfile(
    key="low_latency",
    label="Baja latencia (seguro)",
    description="Menor retardo percibido con límite de 60 FPS y latencia de audio reducida.",
    args=("-vsync", "no", "-fps", "60", "-al", "0.12"),
)

ULTRA_PERFORMANCE_PROFILE = ReceiverProfile(
    key="ultra_performance",
    label="Ultra baja latencia (DX11)",
    description="Usa decodificación + render Direct3D11 para máximo rendimiento en GPU compatible.",
    args=(
        "-vsync",
        "no",
        "-fps",
        "60",
        "-al",
        "0.08",
        "-vd",
        "d3d11h264dec",
        "-vc",
        "d3d11convert",
        "-vs",
        "d3d11videosink",
    ),
)

ALL_PROFILES: tuple[ReceiverProfile, ...] = (
    COMPATIBILITY_PROFILE,
    LOW_LATENCY_PROFILE,
    ULTRA_PERFORMANCE_PROFILE,
    BALANCED_PROFILE,
)

DEFAULT_PROFILE_KEY = COMPATIBILITY_PROFILE.key


def list_profiles() -> tuple[ReceiverProfile, ...]:
    return ALL_PROFILES


def get_profile(profile_key: str) -> ReceiverProfile:
    for profile in ALL_PROFILES:
        if profile.key == profile_key:
            return profile
    raise KeyError(profile_key)
