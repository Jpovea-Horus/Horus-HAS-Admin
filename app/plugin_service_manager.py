"""Gestión del custom component plugin_service en Home Assistant."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from exceptions import SSHCommandError, ValidationError
from models import PluginServiceStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

from paths import (
    DEFAULT_LOCAL_SOURCE as PATHS_LOCAL_SOURCE,
    REMOTE_CUSTOM_COMPONENTS,
    REMOTE_BASE_PATH
)

CUSTOM_COMPONENTS_DIR = REMOTE_CUSTOM_COMPONENTS
PLUGIN_SERVICE_NAME = "plugin_service"
# Nombres aceptados en el controlador (cualquiera cuenta como instalado)
PLUGIN_FOLDER_NAMES = ("plugin_service", "plugin_service_v2", "plugin_serviceV2")
PLUGIN_SERVICE_DIR = f"{CUSTOM_COMPONENTS_DIR}/{PLUGIN_SERVICE_NAME}"
DEFAULT_LOCAL_SOURCE = PATHS_LOCAL_SOURCE

# Repositorio fuente oficial (custom component)
GITHUB_OWNER = "horus-factory"
GITHUB_REPO = "horus-integration-nexxo"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_DEFAULT_REF = "main"
GITHUB_REF_FALLBACKS = ("main", "master")


class PluginServiceManager:
    """Verifica, elimina e instala plugin_service vía SSH/SFTP o GitHub."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> PluginServiceStatus:
        """Comprueba si existe plugin_service o plugin_service_v2 y lista vecinos."""
        parent = CUSTOM_COMPONENTS_DIR

        parent_check = self.ssh.run(f"test -d {shlex.quote(parent)} && echo OK")
        parent_exists = parent_check.stdout.strip() == "OK"

        if not parent_exists:
            return PluginServiceStatus(
                parent_dir=parent,
                plugin_dir=PLUGIN_SERVICE_DIR,
                parent_exists=False,
                plugin_exists=False,
                found_names=[],
                components=[],
                plugin_entries=[],
                error=f"No existe el directorio {parent}",
            )

        listed = self.ssh.run(f"ls -1 {shlex.quote(parent)} 2>/dev/null")
        components: list[str] = []
        if listed.ok and listed.stdout.strip():
            components = [line.strip() for line in listed.stdout.splitlines() if line.strip()]

        found_names = [n for n in PLUGIN_FOLDER_NAMES if n in components]
        # Preferir plugin_service canónico; si no, el primero encontrado
        active_name = (
            PLUGIN_SERVICE_NAME
            if PLUGIN_SERVICE_NAME in found_names
            else (found_names[0] if found_names else PLUGIN_SERVICE_NAME)
        )
        plugin = f"{parent}/{active_name}"
        plugin_exists = bool(found_names)

        plugin_entries: list[str] = []
        manifest_domain = ""
        if plugin_exists:
            inside = self.ssh.run(f"ls -1 {shlex.quote(plugin)} 2>/dev/null")
            if inside.ok and inside.stdout.strip():
                plugin_entries = [
                    line.strip() for line in inside.stdout.splitlines() if line.strip()
                ]
            domain_script = (
                "import json;"
                f"p={plugin!r}+'/manifest.json';"
                "print(json.load(open(p)).get('domain',''))"
            )
            domain_res = self.ssh.run(f"python3 -c {shlex.quote(domain_script)} 2>/dev/null")
            if domain_res.ok and domain_res.stdout.strip():
                manifest_domain = domain_res.stdout.strip()

        return PluginServiceStatus(
            parent_dir=parent,
            plugin_dir=plugin,
            parent_exists=True,
            plugin_exists=plugin_exists,
            found_names=found_names,
            components=components,
            plugin_entries=plugin_entries,
            manifest_domain=manifest_domain,
        )

    def remove(self) -> str:
        """Elimina carpetas aceptadas (plugin_service / plugin_service_v2)."""
        status = self.get_status()
        if not status.parent_exists:
            raise SSHCommandError(status.error or f"No existe {CUSTOM_COMPONENTS_DIR}")
        if not status.plugin_exists:
            return "No hay carpeta plugin_service / plugin_service_v2 que eliminar."

        removed: list[str] = []
        for name in status.found_names:
            target = f"{CUSTOM_COMPONENTS_DIR}/{name}"
            result = self.ssh.run(f"rm -rf {shlex.quote(target)}", use_sudo=True)
            if not result.ok:
                msg = result.stderr or result.stdout or f"No se pudo eliminar {target}."
                raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
            still = self.ssh.run(f"test -d {shlex.quote(target)} && echo EXISTS")
            if still.stdout.strip() == "EXISTS":
                raise SSHCommandError(f"rm -rf ejecutado pero {target} sigue existiendo.")
            removed.append(target)

        return "Eliminado: " + ", ".join(removed)

    def install(self, local_path: str, replace: bool = True) -> str:
        """
        Sube la carpeta local al remoto como plugin_service.
        El domain del manifest debe ser plugin_service (aunque la carpeta local
        se llame plugin_serviceV2).
        """
        local = Path(local_path).expanduser()
        if not local.is_dir():
            raise ValidationError(f"No existe la carpeta local: {local}")

        domain = self._read_local_manifest_domain(local)
        if domain and domain != PLUGIN_SERVICE_NAME:
            raise ValidationError(
                f"manifest.json domain='{domain}' (esperado '{PLUGIN_SERVICE_NAME}'). "
                "No se sube para evitar romper HA."
            )

        status = self.get_status()
        if not status.parent_exists:
            raise SSHCommandError(
                status.error
                or f"No existe {CUSTOM_COMPONENTS_DIR}. Cree custom_components en HA primero."
            )

        if status.plugin_exists:
            if not replace:
                raise ValidationError(
                    "plugin_service ya existe. Elimínelo antes o use reemplazo."
                )
            self.remove()

        count = self.ssh.upload_directory(str(local), PLUGIN_SERVICE_DIR)
        if count == 0:
            raise SSHCommandError(
                "No se subió ningún archivo (carpeta local vacía o solo ignorados)."
            )

        verify = self.get_status()
        if not verify.plugin_exists:
            raise SSHCommandError("Subida terminó pero no aparece plugin_service en remoto.")

        domain_txt = verify.manifest_domain or domain or "(sin manifest)"
        return (
            f"Instalado {count} archivo(s): {local} → {PLUGIN_SERVICE_DIR} "
            f"(domain={domain_txt})"
        )

    def install_from_github(
        self,
        ref: str = GITHUB_DEFAULT_REF,
        token: Optional[str] = None,
        replace: bool = True,
    ) -> str:
        """
        Descarga el repo desde GitHub al PC local (temp), localiza el componente
        y lo sube por SFTP a plugin_service.
        """
        token = (token or "").strip() or _resolve_github_token()
        refs_to_try = [ref] if ref else list(GITHUB_REF_FALLBACKS)
        for fallback in GITHUB_REF_FALLBACKS:
            if fallback not in refs_to_try:
                refs_to_try.append(fallback)

        last_error: Exception | None = None
        with tempfile.TemporaryDirectory(prefix="has_plugin_service_") as tmp:
            tmp_path = Path(tmp)
            component_dir: Path | None = None
            used_ref = ""
            for candidate in refs_to_try:
                try:
                    zip_path = tmp_path / f"{GITHUB_REPO}-{candidate}.zip"
                    self._download_github_zip(candidate, zip_path, token=token)
                    extract_dir = tmp_path / f"extract-{candidate}"
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(extract_dir)
                    component_dir = self._find_plugin_component_dir(extract_dir)
                    used_ref = candidate
                    break
                except ValidationError as exc:
                    last_error = exc
                    # Si el ZIP bajó pero no hay componente, no sirve otro branch
                    if "manifest" in str(exc).lower() or "plugin_service" in str(exc):
                        raise
                except Exception as exc:
                    last_error = exc
                    continue

            if component_dir is None:
                hint = (
                    " Si el repo es privado, configure GITHUB_TOKEN o GH_TOKEN "
                    "(classic con repo read) en el entorno."
                    if not token
                    else ""
                )
                raise ValidationError(
                    f"No se pudo obtener {GITHUB_REPO_URL} "
                    f"(refs: {', '.join(refs_to_try)}): {last_error}.{hint}"
                )

            msg = self.install(str(component_dir), replace=replace)
            return f"{msg} [GitHub {GITHUB_OWNER}/{GITHUB_REPO}@{used_ref}]"

    @staticmethod
    def _download_github_zip(ref: str, dest_zip: Path, token: Optional[str] = None) -> None:
        """Descarga zipball del repo (API GitHub; soporta privado con token)."""
        url = (
            f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/zipball/{ref}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Gestor-Nexxo-800",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=60) as resp:
                dest_zip.write_bytes(resp.read())
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            if exc.code == 404:
                raise ValidationError(
                    f"Repo/rama no encontrada ({ref}) o sin acceso (404). {body}"
                ) from exc
            if exc.code in (401, 403):
                raise ValidationError(
                    f"GitHub denegó acceso ({exc.code}). Use un token con lectura al repo. {body}"
                ) from exc
            raise ValidationError(f"Error HTTP {exc.code} al descargar de GitHub: {body}") from exc
        except URLError as exc:
            raise ValidationError(f"No se pudo conectar a GitHub: {exc.reason}") from exc

        if not dest_zip.is_file() or dest_zip.stat().st_size < 64:
            raise ValidationError("Descarga de GitHub vacía o inválida.")

    @classmethod
    def _find_plugin_component_dir(cls, extract_root: Path) -> Path:
        """
        Localiza la carpeta del custom component dentro del ZIP.
        Prioridad: domain=plugin_service en manifest.json; luego carpeta plugin_service/.
        """
        manifests = list(extract_root.rglob("manifest.json"))
        # Preferir domain == plugin_service
        for manifest in manifests:
            domain = cls._read_local_manifest_domain(manifest.parent)
            if domain == PLUGIN_SERVICE_NAME:
                return manifest.parent

        # Carpeta llamada plugin_service con algún manifest
        for manifest in manifests:
            if manifest.parent.name == PLUGIN_SERVICE_NAME:
                return manifest.parent

        # Un solo manifest en la raíz del repo extraído
        if len(manifests) == 1:
            return manifests[0].parent

        raise ValidationError(
            "En el ZIP de GitHub no se encontró un custom component "
            f"con domain '{PLUGIN_SERVICE_NAME}' (manifest.json)."
        )

    @staticmethod
    def _read_local_manifest_domain(local: Path) -> str:
        manifest = local / "manifest.json" if local.is_dir() else local
        if not manifest.is_file():
            return ""
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        domain = data.get("domain", "")
        return str(domain).strip() if domain else ""


def _resolve_github_token() -> str:
    return (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("HORUS_GITHUB_TOKEN")
        or ""
    ).strip()
