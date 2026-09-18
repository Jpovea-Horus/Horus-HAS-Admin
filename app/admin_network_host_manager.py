"""Instalación del servicio host admin_network en el controlador (fuera de Docker)."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from ha_integration_manager import SKIP_UPLOAD_DIRS
from models import AdminNetworkHostStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

INSTALL_DIR = "/opt/admin_network"
ENV_FILE = "/etc/admin_network.env"
SERVICE_NAME = "admin_network"
WATCHDOG_TIMER = "admin_network-wifi-watchdog.timer"
STAGING_DIR = "/tmp/horus_admin_network_host"
APT_RELAX_CONF = "/etc/apt/apt.conf.d/99horus-admin-network-apt"
DEFAULT_PORT = 8765
INSTALL_TIMEOUT = 600


class AdminNetworkHostManager:
    """Sube host/, ejecuta install.sh y verifica systemd + /health."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> AdminNetworkHostStatus:
        dir_ok = self.ssh.run(f"test -d {shlex.quote(INSTALL_DIR)} && echo OK")
        env_ok = self.ssh.run(f"test -f {shlex.quote(ENV_FILE)} && echo OK")
        active = self.ssh.run(f"systemctl is-active {shlex.quote(SERVICE_NAME)} 2>/dev/null")
        enabled = self.ssh.run(f"systemctl is-enabled {shlex.quote(SERVICE_NAME)} 2>/dev/null")
        wd_active = self.ssh.run(
            f"systemctl is-active {shlex.quote(WATCHDOG_TIMER)} 2>/dev/null"
        )
        wd_enabled = self.ssh.run(
            f"systemctl is-enabled {shlex.quote(WATCHDOG_TIMER)} 2>/dev/null"
        )

        port = DEFAULT_PORT
        api_key = ""
        env_exists = env_ok.stdout.strip() == "OK"
        if env_exists:
            env_txt = self.ssh.run(f"cat {shlex.quote(ENV_FILE)} 2>/dev/null", use_sudo=True)
            if env_txt.ok:
                for line in env_txt.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("ADMIN_NETWORK_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                    elif line.startswith("ADMIN_NETWORK_PORT="):
                        try:
                            port = int(line.split("=", 1)[1].strip())
                        except ValueError:
                            port = DEFAULT_PORT

        health = self.ssh.run(
            f"curl -sS --max-time 3 http://127.0.0.1:{port}/health 2>/dev/null || true"
        )
        health_body = (health.stdout or "").strip()
        health_ok = health.ok and ("ok" in health_body.lower() or health_body.startswith("{"))

        return AdminNetworkHostStatus(
            install_dir=INSTALL_DIR,
            env_file=ENV_FILE,
            service_name=SERVICE_NAME,
            dir_exists=dir_ok.stdout.strip() == "OK",
            env_exists=env_exists,
            service_active=active.stdout.strip() == "active",
            service_enabled=enabled.stdout.strip() == "enabled",
            health_ok=health_ok,
            health_detail=health_body[:200],
            api_key=api_key,
            port=port,
            wifi_watchdog_active=wd_active.stdout.strip() == "active",
            wifi_watchdog_enabled=wd_enabled.stdout.strip() == "enabled",
        )

    def read_api_key(self) -> str:
        status = self.get_status()
        if not status.api_key:
            raise SSHCommandError(
                f"No se leyó ADMIN_NETWORK_API_KEY en {ENV_FILE}. "
                "Instale el servicio host primero."
            )
        return status.api_key

    def install(self, local_path: str) -> str:
        host_dir = resolve_host_dir(Path(local_path).expanduser())
        self.ssh.run(f"rm -rf {shlex.quote(STAGING_DIR)}", use_sudo=True)
        count = self.ssh.upload_directory(
            str(host_dir), STAGING_DIR, skip_dirs=SKIP_UPLOAD_DIRS
        )
        if count == 0:
            raise SSHCommandError("No se subió ningún archivo del servicio host.")

        prepare = (
            f"find {shlex.quote(STAGING_DIR)} -type f "
            r"\( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.txt' \) "
            r"-exec sed -i 's/\r$//' {} + ; "
            f"chmod +x {shlex.quote(STAGING_DIR + '/install.sh')}"
        )
        prep_res = self.ssh.run(prepare, use_sudo=True)
        if not prep_res.ok:
            raise SSHCommandError(
                prep_res.stderr or prep_res.stdout or "No se pudo preparar install.sh."
            )

        cmd = (
            "export DEBIAN_FRONTEND=noninteractive; "
            f"bash {shlex.quote(STAGING_DIR)}/install.sh"
        )
        self._enable_apt_relax()
        try:
            result = self.ssh.run(cmd, use_sudo=True, timeout=INSTALL_TIMEOUT)
        finally:
            self._disable_apt_relax()
        if not result.ok:
            raise SSHCommandError(
                self._install_failure_detail(result),
                exit_code=result.exit_code,
                stderr=result.stderr,
            )

        self.ssh.run(f"rm -rf {shlex.quote(STAGING_DIR)}", use_sudo=True)
        status = self.get_status()
        if not status.service_active:
            raise SSHCommandError(
                "install.sh terminó pero admin_network.service no está activo. "
                f"{status.health_detail}"
            )

        key_txt = status.api_key or "(no leída)"
        if status.api_key:
            from local_config import mask_secret

            key_txt = mask_secret(status.api_key)
        health = "OK" if status.health_ok else (status.health_detail or "sin respuesta")
        return (
            f"Servicio host instalado ({count} archivo(s) subidos). "
            f"systemctl=active  health={health}  "
            f"API key: {key_txt}  "
            f"HA: host 127.0.0.1  puerto {status.port}"
        )

    def _enable_apt_relax(self) -> None:
        """Ignora InRelease caducado en mirrors (USTC/bullseye-security)."""
        conf = (
            'Acquire::Check-Valid-Until "false";\n'
            'Acquire::Check-Date "false";\n'
        )
        payload = f"printf %s {shlex.quote(conf)} > {shlex.quote(APT_RELAX_CONF)}"
        self.ssh.run(f"bash -c {shlex.quote(payload)}", use_sudo=True)

    def _disable_apt_relax(self) -> None:
        self.ssh.run(f"rm -f {shlex.quote(APT_RELAX_CONF)}", use_sudo=True)

    def _install_failure_detail(self, result) -> str:
        detail = (result.stderr or result.stdout or "install.sh falló.").strip()
        combined = f"{result.stdout or ''}\n{result.stderr or ''}"
        if "InRelease is expired" in combined or "Release file" in combined:
            detail += (
                "\n\napt rechazó un mirror con InRelease caducado "
                "(típico de mirrors.ustc.edu.cn/debian-security). "
                "Reintente la instalación; si persiste, en el BND: "
                "sudo apt-get -o Acquire::Check-Valid-Until=false update"
            )
            return detail
        logs = self.ssh.run(
            f"journalctl -u {shlex.quote(SERVICE_NAME)} -n 30 --no-pager 2>/dev/null || true"
        )
        extra = (logs.stdout or "").strip()
        if extra and "No entries" not in extra:
            detail = f"{detail}\n--- journalctl ---\n{extra[-1500:]}"
        return detail

    def remove(self, wipe_env: bool = False) -> str:
        self.ssh.run(
            f"systemctl stop {shlex.quote(WATCHDOG_TIMER)} 2>/dev/null || true",
            use_sudo=True,
        )
        self.ssh.run(
            f"systemctl disable {shlex.quote(WATCHDOG_TIMER)} 2>/dev/null || true",
            use_sudo=True,
        )
        self.ssh.run(
            "rm -f /etc/systemd/system/admin_network-wifi-watchdog.service "
            "/etc/systemd/system/admin_network-wifi-watchdog.timer",
            use_sudo=True,
        )
        self.ssh.run(f"systemctl stop {shlex.quote(SERVICE_NAME)} 2>/dev/null || true", use_sudo=True)
        self.ssh.run(
            f"systemctl disable {shlex.quote(SERVICE_NAME)} 2>/dev/null || true",
            use_sudo=True,
        )
        self.ssh.run(
            f"rm -f /etc/systemd/system/{SERVICE_NAME}.service",
            use_sudo=True,
        )
        self.ssh.run("systemctl daemon-reload", use_sudo=True)
        rm_dir = self.ssh.run(f"rm -rf {shlex.quote(INSTALL_DIR)}", use_sudo=True)
        if not rm_dir.ok:
            raise SSHCommandError(
                rm_dir.stderr or f"No se pudo eliminar {INSTALL_DIR}."
            )
        env_msg = f"{ENV_FILE} conservado."
        if wipe_env:
            self.ssh.run(f"rm -f {shlex.quote(ENV_FILE)}", use_sudo=True)
            env_msg = f"{ENV_FILE} eliminado."
        return f"Servicio host {SERVICE_NAME} eliminado (watchdog incluido). {env_msg}"


def resolve_host_dir(local: Path) -> Path:
    """Acepta raíz admin_network o la carpeta host/."""
    if not local.exists():
        raise ValidationError(f"No existe la carpeta local: {local}")
    if (local / "install.sh").is_file():
        return local
    nested = local / "host"
    if (nested / "install.sh").is_file():
        return nested
    raise ValidationError(
        f"No se encontró host/install.sh en {local}. "
        "Indique la carpeta integrations/admin_network o admin_network/host."
    )
