from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class CharacterLocator:
    character_id: int | None = None
    name: str = ""
    server_slug: str = ""
    server_region: str = ""

    @property
    def is_id(self) -> bool:
        return bool(self.character_id)

    def as_dict(self) -> dict:
        return {
            "character_id": self.character_id,
            "name": self.name,
            "server_slug": self.server_slug,
            "server_region": self.server_region,
        }


def parse_character_locator(value: str) -> CharacterLocator:
    """Parse a WCL character locator.

    Supported user-friendly forms:
      - numeric WCL character id: ``123456``
      - WCL character URL: ``https://www.warcraftlogs.com/character/cn/realm/name``
      - ``region:realm:name`` (recommended manual form)
      - ``name@realm@region``

    We intentionally do not guess a region from a bare character name because names are
    not globally unique.
    """
    raw = unquote((value or "").strip())
    if not raw:
        raise ValueError("请输入 WCL 角色 ID、角色页面链接，或 region:realm:name。")

    if re.fullmatch(r"\d+", raw):
        cid = int(raw)
        if cid <= 0:
            raise ValueError("角色 ID 必须大于 0。")
        return CharacterLocator(character_id=cid)

    if "://" in raw:
        p = urlparse(raw)
        parts = [unquote(x) for x in p.path.split("/") if x]
        # Standard WCL route: /character/{region}/{server}/{character}
        try:
            idx = next(i for i, x in enumerate(parts) if x.lower() == "character")
        except StopIteration:
            idx = -1
        if idx >= 0 and len(parts) >= idx + 4:
            region, server, name = parts[idx + 1: idx + 4]
            return CharacterLocator(name=name, server_slug=server, server_region=region.lower())
        raise ValueError("无法从这个网址识别 WCL 角色。请粘贴角色页面链接。")

    if raw.count(":") >= 2:
        region, server, name = raw.split(":", 2)
        if region.strip() and server.strip() and name.strip():
            return CharacterLocator(name=name.strip(), server_slug=server.strip(), server_region=region.strip().lower())

    if raw.count("@") >= 2:
        name, server, region = raw.split("@", 2)
        if region.strip() and server.strip() and name.strip():
            return CharacterLocator(name=name.strip(), server_slug=server.strip(), server_region=region.strip().lower())

    raise ValueError("无法识别角色。推荐输入数字角色 ID，或 region:realm:name，例如 cn:illidan:Mayday。")
