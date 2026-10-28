"""Catálogo de pares HA + Z-Wave JS UI: incluidos en la app + registrados en este PC."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import date

from models import HaUpdateProfile
from paths import user_data_file

LOCAL_PROFILES_FILE = "ha_profiles.json"

BUILTIN_PROFILES: tuple[HaUpdateProfile, ...] = (
    HaUpdateProfile(
        id="ha-2026.9.3",
        ha="2026.9.3",
        zwave_js_ui="11.24.1",
        validated="2026-09",
        notes=(
            "HA usada en campo en los Nexxo 800. Z-Wave JS UI: release vigente al publicarse "
            "HA 2026.9.3 (schema 50 = máximo que acepta HA). Requiere Node >= 24.20."
        ),
        recommended=True,
    ),
)


def _local_path() -> str:
    return user_data_file(LOCAL_PROFILES_FILE)


def _read_local() -> list[HaUpdateProfile]:
    path = _local_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    profiles: list[HaUpdateProfile] = []
    for item in raw.get("profiles", []):
        if not item.get("ha") or not item.get("zwave_js_ui"):
            continue
        profiles.append(
            HaUpdateProfile(
                id=item.get("id") or f"local-{item['ha']}",
                ha=item["ha"],
                zwave_js_ui=item["zwave_js_ui"],
                label=item.get("label", ""),
                validated=item.get("validated", ""),
                notes=item.get("notes", ""),
                recommended=bool(item.get("recommended")),
                source="local",
            )
        )
    return profiles


def _write_local(profiles: list[HaUpdateProfile]) -> None:
    data = {"profiles": [{k: v for k, v in asdict(p).items() if k != "source"} for p in profiles]}
    with open(_local_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load_profiles() -> list[HaUpdateProfile]:
    """Incluidos + locales (un local con el mismo id reemplaza al incluido)."""
    local = _read_local()
    local_ids = {p.id for p in local}
    profiles = [replace(p) for p in BUILTIN_PROFILES if p.id not in local_ids] + local
    if any(p.recommended and p.source == "local" for p in profiles):
        for p in profiles:
            if p.source != "local":
                p.recommended = False
    profiles.sort(key=lambda p: (not p.recommended, p.source != "builtin", p.id))
    return profiles


def save_local_profile(profile: HaUpdateProfile) -> str:
    profiles = [p for p in _read_local() if p.id != profile.id]
    if profile.recommended:
        for p in profiles:
            p.recommended = False
    profile.source = "local"
    profile.validated = profile.validated or date.today().isoformat()
    profiles.append(profile)
    _write_local(profiles)
    return _local_path()


def delete_local_profile(profile_id: str) -> bool:
    profiles = _read_local()
    remaining = [p for p in profiles if p.id != profile_id]
    if len(remaining) == len(profiles):
        return False
    _write_local(remaining)
    return True
