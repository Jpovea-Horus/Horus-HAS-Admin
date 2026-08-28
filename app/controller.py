"""
Fachada principal de la API — Horus HAS Admin.

Uso programático:
    api = HasControllerAPI()
    api.connect("root", "10.0.5.111", "password")
    # Remoto Cloudflare:
    # api.connect("root", "ssh-xx00", "password", use_cloudflare=True)
    status = api.network.get_status()
    api.disconnect()
"""

from __future__ import annotations

from typing import Callable, Optional

from backup_manager import BackupManager, MaintenanceManager
from cellular_manager import CellularManager
from cloudflare_manager import CloudflareManager
from exceptions import SSHCommandError
from ha_config_manager import HaConfigManager
from ha_users_manager import HaUsersManager
from hostname_manager import HostnameManager
from mqtt_manager import MqttManager
from plugin_service_manager import PluginServiceManager
from ha_integration_manager import HaIntegrationManager
from admin_network_host_manager import AdminNetworkHostManager
from zwave_panel_manager import ZwavePanelManager
from models import (
    BackupEntry,
    BackupManagerStatus,
    CellularStatus,
    SystemHealthStatus,
    HaConfigurationStatus,
    HaUser,
    HaUsersStatus,
    HostnameInfo,
    MaintenanceStatus,
    MqttDiagnosticStatus,
    NetworkStatus,
    PluginServiceStatus,
    SessionInfo,
    AdminNetworkInstallStatus,
    HaIntegrationStatus,
    ZwavePanelStatus,
    ZeroTierStatus,
    CloudflareStatus,
)
from network_manager import NetworkManager
from ssh_client import SSHClient
from zerotier_check import ZeroTierCheck


class HasControllerAPI:
    """API unificada para administración de red del controlador HAS."""

    def __init__(self, timeout: int = 15):
        self.ssh = SSHClient(timeout=timeout)
        self.network = NetworkManager(self.ssh)
        self.hostname = HostnameManager(self.ssh)
        self.zerotier = ZeroTierCheck(self.ssh)
        self.cellular = CellularManager(self.ssh)
        self.mqtt = MqttManager(self.ssh)
        self.ha_users = HaUsersManager(self.ssh)
        self.plugin_service = PluginServiceManager(self.ssh)
        self.admin_network_ha = HaIntegrationManager(self.ssh, "admin_network")
        self.helper_manager = HaIntegrationManager(self.ssh, "helper_manager")
        self.zwave_panel = ZwavePanelManager(self.ssh)
        self.admin_network_host = AdminNetworkHostManager(self.ssh)
        self.ha_config = HaConfigManager(self.ssh)
        self.cloudflare = CloudflareManager(self.ssh)
        self.backups = BackupManager(self.ssh)
        self.maintenance = MaintenanceManager(self.ssh)
        self._session: Optional[SessionInfo] = None

    @property
    def connected(self) -> bool:
        return self.ssh.is_connected

    @property
    def session(self) -> Optional[SessionInfo]:
        return self._session

    def connect(
        self,
        user: str,
        host: str,
        password: str,
        port: int = 22,
        use_cloudflare: bool = False,
    ) -> SessionInfo:
        self._session = self.ssh.connect(
            user, host, password, port=port, use_cloudflare=use_cloudflare
        )
        return self._session

    def disconnect(self) -> None:
        self.ssh.disconnect()
        self._session = None

    def get_network_summary(self) -> NetworkStatus:
        return self.network.get_status()

    def get_hostname(self) -> HostnameInfo:
        return self.hostname.get()

    def set_hostname(self, name: str) -> str:
        return self.hostname.set(name)

    def get_ha_users_status(self) -> HaUsersStatus:
        return self.ha_users.get_status()

    def list_ha_users(self) -> list[HaUser]:
        return self.ha_users.list_users()

    def change_ha_user_password(self, username: str, new_password: str) -> str:
        return self.ha_users.change_password(username, new_password)

    def add_ha_user(self, username: str, password: str, is_admin: bool = False) -> str:
        return self.ha_users.add_user(username, password, is_admin=is_admin)

    def check_zerotier(self) -> ZeroTierStatus:
        return self.zerotier.status()

    def install_zerotier(self) -> str:
        return self.zerotier.install()

    def join_zerotier_network(self, network_id: str) -> str:
        return self.zerotier.join(network_id)

    def get_cellular_status(self) -> CellularStatus:
        return self.cellular.get_status()

    def take_down_cellular_service(self) -> str:
        return self.cellular.take_down_service()

    def bring_up_cellular_service(self) -> str:
        return self.cellular.bring_up_service()

    def leave_zerotier_network(self, network_id: str) -> str:
        return self.zerotier.leave(network_id)

    def get_system_health(self) -> SystemHealthStatus:
        return self.ssh.get_system_health()

    def get_mqtt_diagnostic(self) -> MqttDiagnosticStatus:
        return self.mqtt.diagnose()

    def get_plugin_service_status(self) -> PluginServiceStatus:
        return self.plugin_service.get_status()

    def remove_plugin_service(self) -> str:
        return self.plugin_service.remove()

    def install_plugin_service(self, local_path: str, replace: bool = True) -> str:
        return self.plugin_service.install(local_path, replace=replace)

    def install_plugin_service_from_github(
        self,
        ref: str = "main",
        token: str | None = None,
        replace: bool = True,
    ) -> str:
        return self.plugin_service.install_from_github(
            ref=ref, token=token, replace=replace
        )

    def get_admin_network_status(self) -> AdminNetworkInstallStatus:
        return AdminNetworkInstallStatus(
            ha=self.admin_network_ha.get_status(),
            host=self.admin_network_host.get_status(),
        )

    def install_admin_network(self, local_path: str, replace: bool = True) -> str:
        host_msg = self.admin_network_host.install(local_path)
        ha_msg = self.admin_network_ha.install(local_path, replace=replace)
        return f"{host_msg}\n{ha_msg}"

    def install_admin_network_host(self, local_path: str) -> str:
        return self.admin_network_host.install(local_path)

    def install_admin_network_ha(self, local_path: str, replace: bool = True) -> str:
        return self.admin_network_ha.install(local_path, replace=replace)

    def remove_admin_network_ha(self) -> str:
        return self.admin_network_ha.remove()

    def remove_admin_network_host(self, wipe_env: bool = False) -> str:
        return self.admin_network_host.remove(wipe_env=wipe_env)

    def get_admin_network_api_key(self) -> str:
        return self.admin_network_host.read_api_key()

    def get_helper_manager_status(self) -> HaIntegrationStatus:
        return self.helper_manager.get_status()

    def install_helper_manager(self, local_path: str, replace: bool = True) -> str:
        return self.helper_manager.install(local_path, replace=replace)

    def remove_helper_manager(self) -> str:
        return self.helper_manager.remove()

    def get_zwave_panel_status(self) -> ZwavePanelStatus:
        return self.zwave_panel.get_status()

    def install_zwave_panel(self, local_js: str, restart: bool = False) -> str:
        return self.zwave_panel.install(local_js, restart=restart)

    def remove_zwave_panel(self) -> str:
        return self.zwave_panel.remove()

    def get_ha_configuration_status(self) -> HaConfigurationStatus:
        return self.ha_config.get_status()

    def ensure_ha_http_config(self, force: bool = False) -> str:
        return self.ha_config.ensure_http_config(force=force)

    def ensure_ha_trusted_proxies(self, restart: bool = True, force: bool = False) -> str:
        return self.ha_config.ensure_trusted_proxies(restart=restart, force=force)

    def restart_ha(self) -> str:
        return self.ha_config.restart_ha()

    def disable_mqtt_zwave(self) -> str:
        return self.mqtt.disable_mqtt()

    def restart_mqtt_zwave_service(self) -> str:
        return self.mqtt.restart_service()

    def get_top_snapshot(self) -> str:
        return self.ssh.get_top_snapshot()

    def run_process_monitor(self) -> None:
        """Abre htop interactivo o top si htop no está instalado."""
        check = self.ssh.run("command -v htop 2>/dev/null")
        cmd = "htop" if check.stdout.strip() else "top"
        self.ssh.run_interactive_tty(cmd)

    def get_system_info(self) -> str:
        return self.ssh.get_motd()

    def refresh_system_info(self) -> str:
        return self.ssh.refresh_motd()

    def get_cloudflare_status(self) -> CloudflareStatus:
        status = self.cloudflare.get_status()
        try:
            ha = self.ha_config.get_status()
            status.ha_proxy_ok = ha.proxy_ok
            if ha.uses_storage_http:
                if ha.storage_proxy_ok:
                    status.ha_proxy_detail = ".storage/http stable (trusted_proxies OK)"
                elif ha.storage_pending:
                    status.ha_proxy_detail = (
                        ".storage/http tiene pending: en 5 min HA puede revertir (error 400)"
                    )
                elif not ha.storage_exists:
                    status.ha_proxy_detail = (
                        "Falta .storage/http con trusted_proxies (error 400 al entrar por túnel)"
                    )
                else:
                    status.ha_proxy_detail = (
                        "Falta use_x_forwarded_for / trusted_proxies en stable"
                    )
            elif ha.proxy_ok:
                status.ha_proxy_detail = "configuration.yaml (HAS legado) con trusted_proxies"
            else:
                status.ha_proxy_detail = (
                    "HAS legado sin trusted_proxies en YAML (error 400 al entrar por túnel)"
                )
        except Exception as exc:
            status.ha_proxy_detail = f"No se pudo leer HTTP de HA: {exc}"
        return status

    def install_cloudflare(self) -> str:
        installed = self.cloudflare.install()
        try:
            proxies = self.ha_config.ensure_trusted_proxies(restart=True)
        except SSHCommandError as exc:
            return (
                f"{installed} Cloudflare quedó instalado, pero no se aplicaron "
                f"trusted_proxies (error 400 al entrar a HA): {exc}"
            )
        return f"{installed} {proxies}"

    def remove_cloudflare(self) -> str:
        return self.cloudflare.remove()

    def get_backup_status(self) -> BackupManagerStatus:
        return self.backups.get_status()

    def list_backups(self) -> list[BackupEntry]:
        return self.backups.list_backups()

    def backup_before_update(self) -> str:
        return self.backups.backup_all()

    def backup_ha_config(self) -> str:
        return self.backups.backup_ha_config()

    def backup_zwave_store(self) -> str:
        return self.backups.backup_zwave_store()

    def delete_backup(self, path: str) -> str:
        return self.backups.delete_backup(path)

    def cleanup_old_backups(self, keep: int = 2, kind: str = "all") -> str:
        return self.backups.cleanup_keep_recent(keep=keep, kind=kind)

    def ping(self, host: str = "8.8.8.8") -> str:
        return self.network.ping(host)

    def get_maintenance_status(self) -> MaintenanceStatus:
        return self.maintenance.get_status()

    def safe_cleanup(self) -> str:
        return self.maintenance.safe_cleanup()

    def delete_nested_config(self) -> str:
        return self.maintenance.delete_nested_config()

    def delete_old_archives(self) -> str:
        return self.maintenance.delete_old_archives()
