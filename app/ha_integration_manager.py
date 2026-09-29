"""Instalación genérica de custom components en Home Assistant."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from models import HaIntegrationStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

from paths import REMOTE_CUSTOM_COMPONENTS

CUSTOM_COMPONENTS_DIR = REMOTE_CUSTOM_COMPONENTS
SKIP_UPLOAD_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "__MACOSX",
    "venv",
    ".venv",
    "node_modules",
}


class HaIntegrationManager:
    """Verifica, elimina e instala un custom component por dominio."""

    def __init__(self, ssh: SSHClient, domain: str):
        self.ssh = ssh
        self.domain = domain.strip()
        if not self.domain:
            raise ValidationError("Dominio de integración vacío.")
        self.component_dir = f"{CUSTOM_COMPONENTS_DIR}/{self.domain}"

    def get_status(self) -> HaIntegrationStatus:
        parent = CUSTOM_COMPONENTS_DIR
        parent_check = self.ssh.run(f"test -d {shlex.quote(parent)} && echo OK")
        parent_exists = parent_check.stdout.strip() == "OK"

        if not parent_exists:
            return HaIntegrationStatus(
                domain=self.domain,
                parent_dir=parent,
                component_dir=self.component_dir,
                parent_exists=False,
                component_exists=False,
                error=f"No existe el directorio {parent}",
            )

        listed = self.ssh.run(f"ls -1 {shlex.quote(parent)} 2>/dev/null")
        components: list[str] = []
        if listed.ok and listed.stdout.strip():
            components = [line.strip() for line in listed.stdout.splitlines() if line.strip()]

        exists = self.domain in components
        entries: list[str] = []
        manifest_domain = ""
        manifest_version = ""
        if exists:
            inside = self.ssh.run(f"ls -1 {shlex.quote(self.component_dir)} 2>/dev/null")
            if inside.ok and inside.stdout.strip():
                entries = [line.strip() for line in inside.stdout.splitlines() if line.strip()]
            info = self._read_remote_manifest()
            manifest_domain = info.get("domain", "")
            manifest_version = info.get("version", "")

        return HaIntegrationStatus(
            domain=self.domain,
            parent_dir=parent,
            component_dir=self.component_dir,
            parent_exists=True,
            component_exists=exists,
            components=components,
            entries=entries,
            manifest_domain=manifest_domain,
            manifest_version=manifest_version,
        )

    def remove(self) -> str:
        status = self.get_status()
        if not status.parent_exists:
            raise SSHCommandError(status.error or f"No existe {CUSTOM_COMPONENTS_DIR}")
        if not status.component_exists:
            return f"No hay carpeta {self.domain} que eliminar."

        target = self.component_dir
        result = self.ssh.run(f"rm -rf {shlex.quote(target)}", use_sudo=True)
        if not result.ok:
            msg = result.stderr or result.stdout or f"No se pudo eliminar {target}."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        still = self.ssh.run(f"test -d {shlex.quote(target)} && echo EXISTS")
        if still.stdout.strip() == "EXISTS":
            raise SSHCommandError(f"rm -rf ejecutado pero {target} sigue existiendo.")
        return f"Eliminado: {target}"

    def install(self, local_path: str, replace: bool = True) -> str:
        local = resolve_component_dir(Path(local_path).expanduser(), self.domain)
        domain = self._read_local_manifest_domain(local)
        if domain and domain != self.domain:
            raise ValidationError(
                f"manifest.json domain='{domain}' (esperado '{self.domain}'). "
                "No se sube para evitar romper HA."
            )
        if not (local / "manifest.json").is_file() or not (local / "__init__.py").is_file():
            raise ValidationError(
                f"Faltan manifest.json o __init__.py en {local}."
            )

        status = self.get_status()
        if not status.parent_exists:
            mkdir = self.ssh.run(f"mkdir -p {shlex.quote(CUSTOM_COMPONENTS_DIR)}")
            if not mkdir.ok:
                mkdir = self.ssh.run(
                    f"mkdir -p {shlex.quote(CUSTOM_COMPONENTS_DIR)}", use_sudo=True
                )
            if not mkdir.ok:
                raise SSHCommandError(
                    status.error
                    or f"No existe {CUSTOM_COMPONENTS_DIR} y no se pudo crear."
                )

        if status.component_exists:
            if not replace:
                raise ValidationError(
                    f"{self.domain} ya existe. Elimínelo antes o use reemplazo."
                )
            self.remove()

        count = self.ssh.upload_directory(
            str(local), self.component_dir, skip_dirs=SKIP_UPLOAD_DIRS
        )
        if count == 0:
            raise SSHCommandError(
                "No se subió ningún archivo (carpeta local vacía o solo ignorados)."
            )

        verify = self.get_status()
        if not verify.component_exists:
            raise SSHCommandError(
                f"Subida terminó pero no aparece {self.domain} en remoto."
            )

        version = verify.manifest_version or "(sin versión)"
        domain_txt = verify.manifest_domain or domain or self.domain
        return (
            f"Instalado {count} archivo(s): {local} → {self.component_dir} "
            f"(domain={domain_txt}, v{version})"
        )

    def _read_remote_manifest(self) -> dict[str, str]:
        script = (
            "import json;"
            f"p={self.component_dir!r}+'/manifest.json';"
            "d=json.load(open(p));"
            "print(d.get('domain',''));"
            "print(d.get('version',''))"
        )
        res = self.ssh.run(f"python3 -c {shlex.quote(script)} 2>/dev/null")
        if not res.ok or not res.stdout.strip():
            return {}
        lines = [ln.strip() for ln in res.stdout.splitlines()]
        return {
            "domain": lines[0] if lines else "",
            "version": lines[1] if len(lines) > 1 else "",
        }

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


def resolve_component_dir(local: Path, domain: str) -> Path:
    """Acepta raíz de la integración o la carpeta del custom component."""
    if not local.exists():
        raise ValidationError(f"No existe la carpeta local: {local}")
    if not local.is_dir():
        raise ValidationError(f"No es una carpeta: {local}")

    direct = local / "manifest.json"
    if direct.is_file():
        return local

    nested = local / "custom_components" / domain
    if (nested / "manifest.json").is_file():
        return nested

    named = local / domain
    if (named / "manifest.json").is_file():
        return named

    raise ValidationError(
        f"No se encontró custom component '{domain}' (manifest.json) en {local}."
    )
