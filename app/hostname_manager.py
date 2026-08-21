"""Gestión de hostname del controlador."""

from __future__ import annotations

import re
import shlex

from exceptions import SSHCommandError, ValidationError
from models import HostnameInfo
from ssh_client import SSHClient

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")


class HostnameManager:
    def __init__(self, ssh: SSHClient):
        self._ssh = ssh

    def get(self) -> HostnameInfo:
        raw = self._ssh.run_or_raise("hostnamectl status 2>/dev/null || true", use_sudo=True)
        static = ""
        pretty = ""
        for line in raw.splitlines():
            lower = line.lower()
            if "static hostname" in lower:
                static = line.split(":", 1)[-1].strip()
            elif "pretty hostname" in lower:
                pretty = line.split(":", 1)[-1].strip()
        if not static:
            static = self._ssh.run_or_raise("hostname").strip()
        return HostnameInfo(static_hostname=static, pretty_hostname=pretty, raw=raw)

    def set(self, hostname: str) -> str:
        name = hostname.strip()
        if not _HOSTNAME_RE.match(name):
            raise ValidationError(
                "Hostname inválido. Use 1-63 caracteres: letras, números y guiones; "
                "no puede empezar ni terminar con guión."
            )
        q = shlex.quote(name)
        errors: list[str] = []

        # 1) hostnamectl --static (mejor por SSH, evita D-Bus transitorio)
        r1 = self._ssh.run(f"hostnamectl set-hostname {q} --static", use_sudo=True)
        if r1.ok:
            self._sync_hosts_entry(name)
            return "Hostname actualizado con hostnamectl."

        if r1.stderr or r1.stdout:
            errors.append(r1.stderr or r1.stdout)

        # 2) hostnamectl clásico
        r2 = self._ssh.run(f"hostnamectl set-hostname {q}", use_sudo=True)
        if r2.ok:
            self._sync_hosts_entry(name)
            return "Hostname actualizado con hostnamectl."

        if r2.stderr or r2.stdout:
            errors.append(r2.stderr or r2.stdout)

        # 3) Fallback para embebidos HAS (sin D-Bus / polkit)
        r3 = self._ssh.run(
            f"echo {q} > /etc/hostname && hostname {q}",
            use_sudo=True,
        )
        if r3.ok:
            self._sync_hosts_entry(name)
            return "Hostname actualizado (/etc/hostname)."

        if r3.stderr or r3.stdout:
            errors.append(r3.stderr or r3.stdout)

        detail = errors[-1] if errors else "Sin privilegios o sistema de solo lectura."
        raise SSHCommandError(
            f"No se pudo cambiar el hostname: {detail}",
            exit_code=r3.exit_code,
            stderr=r3.stderr,
        )

    def _sync_hosts_entry(self, name: str) -> None:
        """Actualiza 127.0.1.1 en /etc/hosts si existe la línea (no falla si no aplica)."""
        self._ssh.run(
            "grep -q '^127.0.1.1' /etc/hosts 2>/dev/null && "
            f"sed -i 's/^127.0.1.1.*/127.0.1.1\\t{name}/' /etc/hosts || true",
            use_sudo=True,
        )
