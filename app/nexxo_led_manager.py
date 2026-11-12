"""Gestión del Nexxo LED Manager (instalación y actualización)."""

from __future__ import annotations

from models import NexxoLedManagerStatus
from ssh_client import SSHClient


class NexxoLedManager:
    """Instalación y actualización del Nexxo LED Manager desde GitHub."""

    def __init__(self, ssh: SSHClient):
        self._ssh = ssh

    def get_status(self) -> NexxoLedManagerStatus:
        """Verifica si el Nexxo LED Manager parece estar instalado."""
        # Intentamos detectar si existe algún rastro del instalador o servicio
        # Como es una herramienta externa, buscamos en rutas comunes o el comando
        check = self._ssh.run(
            "command -v nexxo-led-manager >/dev/null 2>&1 && echo OK || "
            "test -d /opt/nexxo-led-manager && echo OK || echo NO"
        )
        installed = check.stdout.strip() == "OK"
        
        version = ""
        if installed:
            # Si tiene comando, intentamos sacar la versión
            ver = self._ssh.run("nexxo-led-manager --version 2>/dev/null || echo ''")
            version = ver.stdout.strip()

        return NexxoLedManagerStatus(
            installed=installed,
            version=version
        )

    def install_or_update(self) -> str:
        """Ejecuta el script de instalación/actualización desde GitHub."""
        cmd = "wget -qO- https://raw.githubusercontent.com/jse-che/nexxo-led-manager/main/install.sh | sh"
        # Usamos use_sudo=True porque suele requerir permisos para instalar en /usr/local/bin o /opt
        return self._ssh.run_or_raise(cmd, use_sudo=True)
