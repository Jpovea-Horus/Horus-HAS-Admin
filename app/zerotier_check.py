"""Consulta de ZeroTier (solo lectura, v1)."""

from __future__ import annotations

import shlex
from models import ZeroTierStatus, ZeroTierNetwork
from ssh_client import SSHClient


class ZeroTierCheck:
    """Gestión de ZeroTier: consulta, instalación y redes."""

    def __init__(self, ssh: SSHClient):
        self._ssh = ssh

    def status(self) -> ZeroTierStatus:
        check = self._ssh.run(
            "command -v zerotier-cli >/dev/null 2>&1 && echo INSTALLED || echo MISSING"
        )
        installed = "INSTALLED" in check.stdout

        version = ""
        service_active = False
        networks = []
        raw_parts: list[str] = [check.stdout, check.stderr]

        if installed:
            ver = self._ssh.run("zerotier-cli -v 2>/dev/null || zerotier-cli -V 2>/dev/null")
            version = ver.stdout.strip() if ver.ok else ""
            raw_parts.append(ver.stdout)

            svc = self._ssh.run("systemctl is-active zerotier-one 2>/dev/null")
            service_active = svc.stdout.strip() == "active"
            raw_parts.append(svc.stdout)

            networks = self.list_networks()

        return ZeroTierStatus(
            installed=installed,
            version=version,
            service_active=service_active,
            networks=networks,
            raw="\n".join(p for p in raw_parts if p),
        )

    def list_networks(self) -> list[ZeroTierNetwork]:
        """Lista las redes ZeroTier a las que está unido el equipo."""
        result = self._ssh.run("sudo zerotier-cli listnetworks", use_sudo=True)
        if not result.ok:
            return []

        networks = []
        # Formato: <nwid> <name> <dev> <status> <type> <ztip>
        # Omitimos la primera línea (cabecera)
        lines = result.stdout.splitlines()
        if len(lines) <= 1:
            return []

        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 6:
                networks.append(
                    ZeroTierNetwork(
                        nwid=parts[0],
                        name=parts[1],
                        dev=parts[2],
                        status=parts[3],
                        type=parts[4],
                        ip=parts[5]
                    )
                )
        return networks

    def install(self) -> str:
        """Instala ZeroTier usando el script oficial."""
        return self._ssh.run_or_raise(
            "curl -s https://install.zerotier.com | sudo bash",
            use_sudo=True
        )

    def join(self, network_id: str) -> str:
        """Se une a una red ZeroTier."""
        q = shlex.quote(network_id)
        return self._ssh.run_or_raise(f"sudo zerotier-cli join {q}", use_sudo=True)

    def leave(self, network_id: str) -> str:
        """Sale de una red ZeroTier."""
        q = shlex.quote(network_id)
        return self._ssh.run_or_raise(f"sudo zerotier-cli leave {q}", use_sudo=True)
