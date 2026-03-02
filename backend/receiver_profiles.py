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
    description="Mas estable para espejo de iPhone. Desactiva sincronizacion problematica de tiempo.",
    args=("-vsync", "no"),
)

BALANCED_PROFILE = ReceiverProfile(
    key="balanced",
    label="Equilibrado (UxPlay por defecto)",
    description="Comportamiento nativo de UxPlay. Mejor sincronia A/V cuando NTP funciona correctamente.",
    args=(),
)

LOW_LATENCY_PROFILE = ReceiverProfile(
    key="low_latency",
    label="Baja latencia (seguro)",
    description="Menor retardo percibido con limite de 60 FPS y latencia de audio reducida.",
    args=("-vsync", "no", "-fps", "60", "-al", "0.12"),
)

ULTRA_PERFORMANCE_PROFILE = ReceiverProfile(
    key="ultra_performance",
    label="Ultra baja latencia (estable)",
    description="Mantiene latencia minima sin forzar pipeline DX11 para evitar cierres de ventana.",
    args=(
        "-vsync",
        "no",
        "-fps",
        "60",
        "-al",
        "0.08",
    ),
)

PERFORMANCE_PROFILE = ReceiverProfile(
    key="performance",
    label="Rendimiento optimizado (FPS)",
    description="Resolución reducida 640x480 @30 FPS para evitar lagging. Ideal si tienes bajo FPS.",
    args=("-vsync", "no", "-fps", "30", "-s", "640x480@30", "-al", "0.08"),
)

ALL_PROFILES: tuple[ReceiverProfile, ...] = (
    COMPATIBILITY_PROFILE,
    LOW_LATENCY_PROFILE,
    ULTRA_PERFORMANCE_PROFILE,
    PERFORMANCE_PROFILE,
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
