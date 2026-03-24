from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

DEFAULT_VERSION = "V0.0.1"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    version: str
    updated_at: str


def read_or_create_version(version_path: Path) -> VersionInfo:
    info = _read_version_file(version_path)
    if info is not None:
        return info

    created = VersionInfo(
        version=DEFAULT_VERSION,
        updated_at=_utc_timestamp(),
    )
    _write_version_file(version_path, created)
    return created


def bump_patch_version(version_path: Path) -> VersionInfo:
    current = read_or_create_version(version_path)
    major, minor, patch = _parse_semver(current.version)
    next_info = VersionInfo(
        version=_format_semver(major, minor, patch + 1),
        updated_at=_utc_timestamp(),
    )
    _write_version_file(version_path, next_info)
    return next_info


def _read_version_file(version_path: Path) -> VersionInfo | None:
    if not version_path.exists():
        return None

    try:
        raw = version_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    version = str(payload.get("version", "")).strip()
    updated_at = str(payload.get("updated_at", "")).strip()
    if not version:
        return None

    try:
        version = _normalize_semver(version)
    except ValueError:
        return None

    if not updated_at:
        updated_at = _utc_timestamp()

    return VersionInfo(version=version, updated_at=updated_at)


def _write_version_file(version_path: Path, info: VersionInfo) -> None:
    version_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": info.version,
        "updated_at": info.updated_at,
    }
    version_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_semver(version: str) -> tuple[int, int, int]:
    cleaned = version.strip()
    if cleaned[:1].lower() == "v":
        cleaned = cleaned[1:]

    parts = cleaned.split(".")
    if len(parts) != 3:
        raise ValueError("Versión inválida")

    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2])
    except ValueError as exc:
        raise ValueError("Versión inválida") from exc

    if major < 0 or minor < 0 or patch < 0:
        raise ValueError("Versión inválida")

    return major, minor, patch


def _format_semver(major: int, minor: int, patch: int) -> str:
    return f"V{major}.{minor}.{patch}"


def _normalize_semver(version: str) -> str:
    major, minor, patch = _parse_semver(version)
    return _format_semver(major, minor, patch)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
