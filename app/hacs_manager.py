"""Instalación de HACS (Home Assistant Community Store) en HA container."""

from __future__ import annotations

import re
import shlex
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from ha_integration_manager import HaIntegrationManager, resolve_component_dir
from models import HacsStatus
from paths import REMOTE_CONFIG_DIR, REMOTE_CUSTOM_COMPONENTS

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from ssh_client import SSHClient

HACS_DOMAIN = "hacs"
HACS_RELEASE_URL = "https://github.com/hacs/integration/releases/latest/download/hacs.zip"
HACS_MIN_HA_VERSION = "2024.4.1"
HACS_REMOTE_DIR = f"{REMOTE_CUSTOM_COMPONENTS}/{HACS_DOMAIN}"
_CONFIG_ENTRIES = f"{REMOTE_CONFIG_DIR}/.storage/core.config_entries"


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value or "")
    if not match:
        return ()
    return tuple(int(part or 0) for part in match.groups())


class HacsManager:
    """HACS vía descarga en el controlador (GitHub) o subida manual (carpeta / hacs.zip)."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager):
        self.ssh = ssh
        self.ha_config = ha_config
        self.integration = HaIntegrationManager(ssh, HACS_DOMAIN)

    def get_status(self) -> HacsStatus:
        integration = self.integration.get_status()
        ha_version = ""
        try:
            ha_version = self.ha_config._detect_version()
        except Exception:
            ha_version = ""
        current = _version_tuple(ha_version)
        compatible = (
            current >= _version_tuple(HACS_MIN_HA_VERSION) if current else None
        )
        return HacsStatus(
            integration=integration,
            ha_version=ha_version,
            min_ha_version=HACS_MIN_HA_VERSION,
            ha_compatible=compatible,
            entry_configured=self._entry_configured(),
        )

    def install_from_github(self, replace: bool = True) -> str:
        """El controlador descarga hacs.zip del último release y lo instala."""
        self._ensure_compatible()
        status = self.integration.get_status()
        if status.component_exists and not replace:
            raise ValidationError("HACS ya existe. Elimínelo antes o use reemplazo.")

        script = (
            "import io,os,sys,tempfile,urllib.request,zipfile\n"
            f"req=urllib.request.Request({HACS_RELEASE_URL!r},"
            "headers={'User-Agent':'Horus-HAS-Admin'})\n"
            "data=urllib.request.urlopen(req,timeout=120).read()\n"
            "tmp=tempfile.mkdtemp(prefix='horus_hacs_')\n"
            "zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)\n"
            "if not os.path.isfile(os.path.join(tmp,'manifest.json')):\n"
            "    sys.exit('hacs.zip sin manifest.json')\n"
            "print(tmp)\n"
        )
        download = self.ssh.run(f"python3 -c {shlex.quote(script)}", timeout=180)
        tmp_dir = download.stdout.strip().splitlines()[-1] if download.stdout.strip() else ""
        if not download.ok or not tmp_dir.startswith("/tmp/horus_hacs_"):
            detail = (download.stderr or download.stdout or "").strip()[-400:]
            raise SSHCommandError(
                "El controlador no pudo descargar HACS desde GitHub "
                f"(¿sin salida a internet/DNS?). Use la subida manual. {detail}",
                exit_code=download.exit_code,
                stderr=download.stderr,
            )

        target = shlex.quote(HACS_REMOTE_DIR)
        parent = shlex.quote(REMOTE_CUSTOM_COMPONENTS)
        src = shlex.quote(tmp_dir)
        place_cmd = (
            f"mkdir -p {parent} && rm -rf {target} && mkdir -p {target} && "
            f"cp -a {src}/. {target}/ && "
            f"chown -R --reference={parent} {target} && "
            f"rm -rf {src}"
        )
        place = self.ssh.run(f"sh -c {shlex.quote(place_cmd)}", use_sudo=True, timeout=120)
        if not place.ok:
            self.ssh.run(f"rm -rf {src}", use_sudo=True)
            raise SSHCommandError(
                f"No se pudo instalar HACS en {HACS_REMOTE_DIR}: "
                f"{place.stderr or place.stdout}",
                exit_code=place.exit_code,
                stderr=place.stderr,
            )

        verify = self.integration.get_status()
        if not verify.component_exists or verify.manifest_domain != HACS_DOMAIN:
            raise SSHCommandError(f"Descarga terminó pero {HACS_REMOTE_DIR} no es válido.")
        version = verify.manifest_version or "(sin versión)"
        return f"HACS v{version} instalado desde GitHub (controlador) → {HACS_REMOTE_DIR}"

    def install_from_local(self, local_path: str, replace: bool = True) -> str:
        """Sube HACS desde una carpeta local o desde el hacs.zip del release."""
        self._ensure_compatible()
        local = Path(local_path).expanduser()
        if local.is_file() and local.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory(prefix="has_hacs_") as tmp:
                try:
                    with zipfile.ZipFile(local, "r") as zf:
                        zf.extractall(tmp)
                except zipfile.BadZipFile as exc:
                    raise ValidationError(f"ZIP inválido: {local}") from exc
                component = resolve_component_dir(Path(tmp), HACS_DOMAIN)
                return self.integration.install(str(component), replace=replace)
        return self.integration.install(str(local), replace=replace)

    def remove(self) -> str:
        return self.integration.remove()

    def _ensure_compatible(self) -> None:
        version = ""
        try:
            version = self.ha_config._detect_version()
        except Exception:
            return
        current = _version_tuple(version)
        if current and current < _version_tuple(HACS_MIN_HA_VERSION):
            raise ValidationError(
                f"HACS requiere Home Assistant >= {HACS_MIN_HA_VERSION} "
                f"(detectado {version}). Actualice HA antes de instalar."
            )

    def _entry_configured(self) -> bool:
        script = (
            "import json;"
            f"d=json.load(open({_CONFIG_ENTRIES!r}));"
            "es=d.get('data',{}).get('entries',[]);"
            f"print('YES' if any(e.get('domain')=={HACS_DOMAIN!r} for e in es) else 'NO')"
        )
        res = self.ssh.run(f"python3 -c {shlex.quote(script)} 2>/dev/null", use_sudo=True)
        return res.stdout.strip() == "YES"
