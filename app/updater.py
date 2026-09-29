"""Auto-actualización desde GitHub Releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from paths import APP_VERSION, EXE_DIR

GITHUB_OWNER = "Jpovea-Horus"
GITHUB_REPO = "Horus-HAS-Admin"
ASSET_NAME = "Gestor Nexxo 800.exe"
RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"


class UpdateError(Exception):
    pass


@dataclass
class ReleaseInfo:
    version: str
    notes: str
    download_url: str
    size: int


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "GestorNexxo800-Updater"}
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_version(value: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", value.lstrip("vV").split("-")[0]))


def check_latest(timeout: float = 10) -> ReleaseInfo | None:
    """Devuelve la release si es más nueva que APP_VERSION; None si ya está al día."""
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    try:
        with urlopen(Request(url, headers=_headers()), timeout=timeout) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub respondió HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        raise UpdateError(f"No se pudo conectar a GitHub: {getattr(exc, 'reason', exc)}") from exc

    tag = data.get("tag_name") or ""
    if not tag or _parse_version(tag) <= _parse_version(APP_VERSION):
        return None

    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        raise UpdateError(f"La release {tag} no incluye '{ASSET_NAME}'.")

    return ReleaseInfo(
        version=tag.lstrip("vV"),
        notes=(data.get("body") or "").strip(),
        download_url=asset["browser_download_url"],
        size=int(asset.get("size") or 0),
    )


def _download(release: ReleaseInfo, dest: str) -> None:
    req = Request(release.download_url, headers=_headers("application/octet-stream"))
    try:
        with urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        if os.path.exists(dest):
            os.remove(dest)
        raise UpdateError(f"Fallo la descarga: {exc}") from exc

    if release.size and os.path.getsize(dest) != release.size:
        os.remove(dest)
        raise UpdateError("El archivo descargado está incompleto.")


def apply_update(release: ReleaseInfo) -> None:
    """Descarga el .exe nuevo, lo reemplaza cuando la app se cierra y la relanza."""
    if not is_frozen():
        raise UpdateError("Modo desarrollo: actualice el código con 'git pull'.")

    current_exe = sys.executable
    new_exe = os.path.join(EXE_DIR, f"{os.path.splitext(os.path.basename(current_exe))[0]}.new.exe")
    try:
        _download(release, new_exe)
    except PermissionError as exc:
        raise UpdateError(f"Sin permisos de escritura en {EXE_DIR}.") from exc

    pid = os.getpid()
    script = os.path.join(tempfile.gettempdir(), f"nexxo_update_{pid}.bat")
    # ping como espera: 'timeout' falla en procesos sin consola.
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && '
            "(ping -n 2 127.0.0.1 >nul & goto wait)\r\n"
            "set tries=0\r\n"
            ":move\r\n"
            f'move /y "{new_exe}" "{current_exe}" >nul 2>&1\r\n'
            "if not errorlevel 1 goto launch\r\n"
            "set /a tries+=1\r\n"
            "if %tries% geq 10 goto launch\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            "goto move\r\n"
            ":launch\r\n"
            f'start "" "{current_exe}"\r\n'
            'del "%~f0"\r\n'
        )

    subprocess.Popen(
        ["cmd", "/c", script],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    sys.exit(0)
