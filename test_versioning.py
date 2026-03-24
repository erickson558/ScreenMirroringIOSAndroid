from __future__ import annotations

import json
from pathlib import Path

from backend.versioning import bump_patch_version, read_or_create_version


def test_read_or_create_version_normalizes_legacy_semver(tmp_path: Path) -> None:
    version_path = tmp_path / "version.json"
    version_path.write_text(
        json.dumps(
            {
                "version": "0.0.83",
                "updated_at": "2026-03-02T20:59:44Z",
            }
        ),
        encoding="utf-8",
    )

    info = read_or_create_version(version_path)

    assert info.version == "V0.0.83"
    assert info.updated_at == "2026-03-02T20:59:44Z"


def test_bump_patch_version_writes_v_prefixed_semver(tmp_path: Path) -> None:
    version_path = tmp_path / "version.json"
    version_path.write_text(
        json.dumps(
            {
                "version": "0.0.83",
                "updated_at": "2026-03-02T20:59:44Z",
            }
        ),
        encoding="utf-8",
    )

    updated = bump_patch_version(version_path)
    payload = json.loads(version_path.read_text(encoding="utf-8"))

    assert updated.version == "V0.0.84"
    assert payload["version"] == "V0.0.84"
    assert payload["updated_at"] == updated.updated_at
