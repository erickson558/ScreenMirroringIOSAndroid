#!/usr/bin/env python3
"""Script to automatically bump patch version before compilation."""
from __future__ import annotations

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.versioning import bump_patch_version, read_or_create_version


def main() -> None:
    """Bump patch version and display current version."""
    version_path = Path(__file__).parent.parent / "version.json"
    
    # Bump the patch version
    updated = bump_patch_version(version_path)
    print(f"✓ Version bumped to: {updated.version}")
    print(f"✓ Updated at: {updated.updated_at}")


if __name__ == "__main__":
    main()
