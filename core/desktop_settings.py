from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SERVICE_NAME = "WoW Log Data Analyst"


def app_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    p = base / "WoW Log Data Analyst"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict[str, Any]) -> None:
    p = settings_path()
    safe = dict(data)
    # Secrets belong in OS keychain, never JSON.
    for k in list(safe):
        if "secret" in k.lower() or "api_key" in k.lower() or k.lower().endswith("token"):
            safe.pop(k, None)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get_secret(name: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return kr.get_password(SERVICE_NAME, name) or ""
    except Exception:
        return ""


def set_secret(name: str, value: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        if value:
            kr.set_password(SERVICE_NAME, name, value)
        else:
            try:
                kr.delete_password(SERVICE_NAME, name)
            except Exception:
                pass
        return True
    except Exception:
        return False
