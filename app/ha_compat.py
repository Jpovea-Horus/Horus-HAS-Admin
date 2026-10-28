"""Compatibilidad HA ↔ Z-Wave JS UI por schema del Z-Wave JS Server (consultas desde este PC).

HA (zwave_js) fija zwave-js-server-python, que acepta un rango MIN..MAX de schema.
zwave-js-ui incluye @zwave-js/server, que soporta minSchemaVersion..maxSchemaVersion.
Son compatibles si ambos rangos se solapan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_UA = {"User-Agent": "GestorNexxo800-Compat", "Accept": "application/json"}
_RAW = "https://raw.githubusercontent.com"
_NPM = "https://registry.npmjs.org"
_cache: dict[str, str] = {}


class CompatError(Exception):
    pass


@dataclass
class CompatResult:
    compatible: Optional[bool]  # None = no se pudo verificar
    detail: str
    client_range: tuple[int, int] = (0, 0)
    server_range: tuple[int, int] = (0, 0)
    server_version: str = ""


def _get(url: str, timeout: float) -> str:
    if url in _cache:
        return _cache[url]
    try:
        with urlopen(Request(url, headers=_UA), timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise CompatError(f"HTTP {exc.code} en {url}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CompatError(f"Sin conexión ({getattr(exc, 'reason', exc)})") from exc
    _cache[url] = body
    return body


def _get_json(url: str, timeout: float) -> dict:
    try:
        return json.loads(_get(url, timeout))
    except ValueError as exc:
        raise CompatError(f"Respuesta inválida de {url}") from exc


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", (value or "").split("-")[0])[:3])


def latest_ha_release(timeout: float = 10) -> str:
    data = _get_json(
        "https://api.github.com/repos/home-assistant/core/releases/latest", timeout
    )
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        raise CompatError("GitHub no devolvió la última versión de Home Assistant.")
    return tag


def latest_zwave_js_ui(timeout: float = 10) -> str:
    data = _get_json(f"{_NPM}/zwave-js-ui/latest", timeout)
    version = (data.get("version") or "").strip()
    if not version:
        raise CompatError("npm no devolvió la última versión de zwave-js-ui.")
    return version


def zwave_node_requirement(zwui_version: str, timeout: float = 10) -> str:
    data = _get_json(f"{_NPM}/zwave-js-ui/{zwui_version}", timeout)
    return ((data.get("engines") or {}).get("node") or "").strip()


def ha_tag_exists(tag: str, timeout: float = 10) -> bool:
    try:
        _get(f"{_RAW}/home-assistant/core/{tag}/homeassistant/const.py", timeout)
        return True
    except CompatError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


def zwave_version_exists(version: str, timeout: float = 10) -> bool:
    try:
        _get_json(f"{_NPM}/zwave-js-ui/{version}", timeout)
        return True
    except CompatError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


def _client_schema_range(ha_tag: str, timeout: float) -> tuple[int, int]:
    manifest = _get_json(
        f"{_RAW}/home-assistant/core/{ha_tag}/homeassistant/components/zwave_js/manifest.json",
        timeout,
    )
    req = next(
        (r for r in manifest.get("requirements", []) if r.startswith("zwave-js-server-python")),
        "",
    )
    match = re.search(r"==\s*([\w.]+)", req)
    if not match:
        raise CompatError(f"HA {ha_tag}: no se encontró zwave-js-server-python en el manifest.")
    const = _get(
        f"{_RAW}/home-assistant-libs/zwave-js-server-python/{match.group(1)}"
        "/zwave_js_server/const/__init__.py",
        timeout,
    )
    lo = re.search(r"MIN_SERVER_SCHEMA_VERSION\s*=\s*(\d+)", const)
    hi = re.search(r"MAX_SERVER_SCHEMA_VERSION\s*=\s*(\d+)", const)
    if not lo or not hi:
        raise CompatError("No se pudo leer el rango de schema del cliente de HA.")
    return int(lo.group(1)), int(hi.group(1))


def _satisfies(version: tuple[int, ...], spec: str) -> bool:
    spec = spec.strip()
    base = version_tuple(spec)
    if not base:
        return False
    base = (base + (0, 0, 0))[:3]
    if spec.startswith("^"):
        return version[0] == base[0] and version >= base
    if spec.startswith("~"):
        return version[:2] == base[:2] and version >= base
    if spec.startswith(">="):
        return version >= base
    return version == base


def _resolve_server_version(spec: str, timeout: float) -> str:
    data = _get_json(f"{_NPM}/@zwave-js/server", timeout)
    candidates = [
        v for v in (data.get("versions") or {})
        if "-" not in v and _satisfies(version_tuple(v), spec)
    ]
    if not candidates:
        raise CompatError(f"Ninguna versión de @zwave-js/server cumple '{spec}'.")
    return max(candidates, key=version_tuple)


def _server_schema_range(zwui_version: str, timeout: float) -> tuple[tuple[int, int], str]:
    pkg = _get_json(f"{_NPM}/zwave-js-ui/{zwui_version}", timeout)
    spec = (pkg.get("dependencies") or {}).get("@zwave-js/server", "")
    if not spec:
        raise CompatError(f"zwave-js-ui {zwui_version} no declara @zwave-js/server.")
    server = _resolve_server_version(spec, timeout)
    const = _get(f"{_RAW}/zwave-js/zwave-js-server/{server}/src/lib/const.ts", timeout)
    lo = re.search(r"minSchemaVersion\s*=\s*(\d+)", const)
    hi = re.search(r"maxSchemaVersion\s*=\s*(\d+)", const)
    if not lo or not hi:
        raise CompatError(f"No se pudo leer el schema de @zwave-js/server {server}.")
    return (int(lo.group(1)), int(hi.group(1))), server


def check_pair(ha_tag: str, zwui_version: str, timeout: float = 10) -> CompatResult:
    """Compara schemas; nunca lanza: si no se puede verificar devuelve compatible=None."""
    try:
        client = _client_schema_range(ha_tag, timeout)
        server, server_version = _server_schema_range(zwui_version, timeout)
    except CompatError as exc:
        return CompatResult(compatible=None, detail=f"No se pudo verificar: {exc}")
    ok = server[1] >= client[0] and server[0] <= client[1]
    detail = (
        f"HA {ha_tag} acepta schema {client[0]}–{client[1]}; "
        f"Z-Wave JS UI {zwui_version} (server {server_version}) ofrece {server[0]}–{server[1]}"
    )
    return CompatResult(
        compatible=ok,
        detail=detail,
        client_range=client,
        server_range=server,
        server_version=server_version,
    )


def node_satisfies(node_version: str, spec: str) -> Optional[bool]:
    """True/False según engines.node; None si no hay requisito o versión de Node."""
    current = version_tuple(node_version)
    if not spec or not current:
        return None
    current = (current + (0, 0, 0))[:3]
    alternatives = [a.strip() for a in spec.split("||") if a.strip()]
    for alt in alternatives:
        parts = re.findall(r"(>=|<=|>|<|\^|~)?\s*(\d+(?:\.\d+){0,2})", alt)
        if not parts:
            continue
        ok = True
        for op, ver in parts:
            target = (version_tuple(ver) + (0, 0, 0))[:3]
            if op == ">=":
                ok &= current >= target
            elif op == ">":
                ok &= current > target
            elif op == "<=":
                ok &= current <= target
            elif op == "<":
                ok &= current < target
            elif op == "^":
                ok &= current[0] == target[0] and current >= target
            elif op == "~":
                ok &= current[:2] == target[:2] and current >= target
            else:
                ok &= current[0] == target[0]
        if ok:
            return True
    return False
