"""Historial local de hosts (sin contraseñas)."""

from __future__ import annotations

import json
import os
from typing import Any

from paths import EXE_DIR

HOSTS_FILE = os.path.join(EXE_DIR, "session_hosts.json")
MAX_HOSTS = 8


def load_hosts() -> list[dict[str, Any]]:
    if not os.path.isfile(HOSTS_FILE):
        return []
    try:
        with open(HOSTS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [h for h in data if isinstance(h, dict) and h.get("host")]
    except (OSError, json.JSONDecodeError):
        return []
    return []


def remember_host(host: str, user: str, use_cloudflare: bool) -> None:
    host = host.strip()
    user = user.strip() or "root"
    if not host:
        return
    entries = [
        h
        for h in load_hosts()
        if not (
            h.get("host") == host
            and bool(h.get("use_cloudflare")) == use_cloudflare
            and h.get("user") == user
        )
    ]
    entries.insert(
        0,
        {"host": host, "user": user, "use_cloudflare": use_cloudflare},
    )
    try:
        with open(HOSTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(entries[:MAX_HOSTS], fh, indent=2)
    except OSError:
        pass
