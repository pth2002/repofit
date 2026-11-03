"""Per-OS locations shared by several agents."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path.home()

# Editors that share the VS Code storage layout and can host Cline-style extensions.
VSCODE_HOSTS = ["Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Trae", "Trae CN", "Kiro"]


def env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else default


def app_support() -> Path:
    """Root for Electron / VS Code style per-user app data."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support"
    return env_path("XDG_CONFIG_HOME", HOME / ".config")


def xdg_config() -> Path:
    return env_path("XDG_CONFIG_HOME", HOME / ".config")


def xdg_data() -> Path:
    return env_path("XDG_DATA_HOME", HOME / ".local" / "share")
