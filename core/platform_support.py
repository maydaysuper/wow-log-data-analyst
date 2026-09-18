from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


def app_resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


def candidate_wow_addon_dirs() -> list[Path]:
    system = platform.system()
    candidates: list[Path] = []
    home = Path.home()
    if system == "Darwin":
        candidates += [
            Path("/Applications/World of Warcraft/_retail_/Interface/AddOns"),
            home / "Applications/World of Warcraft/_retail_/Interface/AddOns",
        ]
    elif system == "Windows":
        pf86 = os.environ.get("ProgramFiles(x86)")
        pf = os.environ.get("ProgramFiles")
        for root in [pf86, pf]:
            if root:
                candidates.append(Path(root) / "World of Warcraft/_retail_/Interface/AddOns")
    return [p for p in candidates if p.exists()]


def install_telemetry_addon(target_addons_dir: Path | None = None) -> Path:
    src = app_resource_path("addons/WowLogTelemetry")
    if not src.exists():
        raise FileNotFoundError(f"Bundled telemetry addon not found: {src}")
    if target_addons_dir is None:
        dirs = candidate_wow_addon_dirs()
        if not dirs:
            raise FileNotFoundError("未自动找到 World of Warcraft/_retail_/Interface/AddOns")
        target_addons_dir = dirs[0]
    target_addons_dir = Path(target_addons_dir).expanduser()
    if not target_addons_dir.exists():
        raise FileNotFoundError(f"AddOns 目录不存在: {target_addons_dir}")
    dst = target_addons_dir / "WowLogTelemetry"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst
