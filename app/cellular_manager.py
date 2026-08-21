"""Gestión del servicio celular (LTE) y detección de hardware."""

from __future__ import annotations

from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import CellularStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient


class CellularManager:
    """Administra el servicio cellular.service y detecta módems USB."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> CellularStatus:
        """Verifica si el servicio está activo/habilitado y si hay hardware."""
        # 1. Verificar hardware (módem USB)
        ls_result = self.ssh.run("ls /dev/ttyUSB* 2>/dev/null")
        modem_devices = ls_result.stdout.split() if ls_result.stdout else []
        has_modem = len(modem_devices) > 0

        # 2. Estado del servicio (active)
        active_result = self.ssh.run("systemctl is-active cellular.service 2>/dev/null")
        is_active = active_result.stdout.strip() == "active"

        # 3. Estado del servicio (enabled)
        enabled_result = self.ssh.run("systemctl is-enabled cellular.service 2>/dev/null")
        is_enabled = enabled_result.stdout.strip() == "enabled"

        return CellularStatus(
            is_active=is_active,
            is_enabled=is_enabled,
            has_modem=has_modem,
            modem_devices=modem_devices,
        )

    def take_down_service(self) -> str:
        """Detiene y deshabilita cellular.service (acción de campo ZW855)."""
        status = self.get_status()
        if not status.is_active and not status.is_enabled:
            return "El servicio ya está de baja (inactivo y sin arranque automático)."

        stop = self.ssh.run("systemctl stop cellular.service", use_sudo=True)
        if not stop.ok:
            err = (stop.stderr or stop.stdout or "").lower()
            if "not loaded" not in err and "could not be found" not in err:
                msg = stop.stderr or stop.stdout or "No se pudo detener cellular.service."
                raise SSHCommandError(msg, exit_code=stop.exit_code, stderr=stop.stderr)

        disable = self.ssh.run("systemctl disable cellular.service", use_sudo=True)
        if not disable.ok:
            msg = disable.stderr or disable.stdout or "No se pudo deshabilitar cellular.service."
            raise SSHCommandError(msg, exit_code=disable.exit_code, stderr=disable.stderr)

        return (
            "Servicio cellular dado de baja: detenido y deshabilitado. "
            "No iniciará al reiniciar el controlador."
        )

    def bring_up_service(self) -> str:
        """Habilita e inicia cellular.service."""
        enable = self.ssh.run("systemctl enable cellular.service", use_sudo=True)
        if not enable.ok:
            msg = enable.stderr or enable.stdout or "No se pudo habilitar cellular.service."
            raise SSHCommandError(msg, exit_code=enable.exit_code, stderr=enable.stderr)
        start = self.ssh.run("systemctl start cellular.service", use_sudo=True)
        if not start.ok:
            msg = start.stderr or start.stdout or "No se pudo iniciar cellular.service."
            raise SSHCommandError(msg, exit_code=start.exit_code, stderr=start.stderr)
        return "Servicio cellular habilitado e iniciado."
