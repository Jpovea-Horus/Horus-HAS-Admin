"""Gestión de cloudflared en el controlador remoto."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING
from models import CloudflareStatus
from exceptions import SSHCommandError

if TYPE_CHECKING:
    from ssh_client import SSHClient

class CloudflareManager:
    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> CloudflareStatus:
        """Verifica si cloudflared está instalado y si hay túneles activos."""
        check = self.ssh.run("cloudflared --version 2>/dev/null")
        installed = check.ok
        version = check.stdout.strip() if installed else ""
        
        tunnels = []
        if installed:
            # Intentar ver si hay procesos de cloudflared activos
            ps = self.ssh.run("ps aux | grep cloudflared | grep -v grep")
            if ps.ok and ps.stdout.strip():
                tunnels = [line.strip() for line in ps.stdout.splitlines()]

        return CloudflareStatus(
            installed=installed,
            version=version,
            running_tunnels=tunnels
        )

    def install(self) -> str:
        """Instala cloudflared en el controlador (Debian/Ubuntu)."""
        # 1. Agregar repo y clave GPG oficial de Cloudflare
        # Usamos sh -c para que sudo afecte a toda la cadena de comandos con pipes
        cmds = [
            "mkdir -p --mode=0755 /usr/share/keyrings",
            "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null",
            "echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | tee /etc/apt/sources.list.d/cloudflared.list",
            "apt-get update",
            "apt-get install -y cloudflared"
        ]
        
        for cmd in cmds:
            # Si el comando tiene un pipe, lo envolvemos en bash -c para que el sudo de ssh.run sea efectivo en todo
            if "|" in cmd:
                final_cmd = f"bash -c {shlex.quote(cmd)}"
            else:
                final_cmd = cmd
                
            res = self.ssh.run(final_cmd, use_sudo=True)
            if not res.ok:
                raise SSHCommandError(f"Error al ejecutar: {final_cmd}\n{res.stderr}")
        
        return "cloudflared instalado correctamente en el controlador."

    def remove(self) -> str:
        """Elimina cloudflared del controlador."""
        res = self.ssh.run("apt-get remove -y cloudflared && rm /etc/apt/sources.list.d/cloudflared.list", use_sudo=True)
        if not res.ok:
            raise SSHCommandError(f"Error al eliminar cloudflared: {res.stderr}")
        return "cloudflared eliminado correctamente del controlador."
