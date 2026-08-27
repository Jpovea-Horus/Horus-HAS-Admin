"""Modelos de datos para respuestas de la API."""

from dataclasses import dataclass, field
from typing import Optional

from paths import REMOTE_CUSTOM_COMPONENTS, REMOTE_CONFIGURATION_YAML


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class NetworkDevice:
    device: str
    device_type: str
    state: str
    connection: str
    mac: str = ""


@dataclass
class ConnectionProfile:
    name: str
    device: str
    ipv4_method: str
    ipv4_addresses: str
    ipv4_gateway: str
    ipv4_dns: str


@dataclass
class NetworkStatus:
    devices: list[NetworkDevice] = field(default_factory=list)
    default_gateway: str = ""
    raw_device_status: str = ""
    raw_ip_addr: str = ""


@dataclass
class WifiNetwork:
    ssid: str
    signal: str
    security: str
    in_use: bool


@dataclass
class HostnameInfo:
    static_hostname: str
    pretty_hostname: str
    raw: str


@dataclass
class ZeroTierNetwork:
    nwid: str
    name: str
    status: str
    type: str
    dev: str
    ip: str


@dataclass
class ZeroTierStatus:
    installed: bool
    version: str = ""
    service_active: bool = False
    networks: list[ZeroTierNetwork] = field(default_factory=list)
    raw: str = ""


@dataclass
class SessionInfo:
    user: str
    host: str
    remote_user: str
    has_sudo: bool


@dataclass
class CellularStatus:
    is_active: bool
    is_enabled: bool
    has_modem: bool
    modem_devices: list[str] = field(default_factory=list)


@dataclass
class HaUser:
    """Usuario de Home Assistant (login en :8123)."""

    user_id: str
    username: str
    name: str
    is_owner: bool = False
    is_active: bool = True
    is_admin: bool = False
    incomplete: bool = False  # sin id o solo en auth_provider


@dataclass
class HaUsersStatus:
    """Estado de usuarios HA detectados en el controlador."""

    container_name: str = ""
    version: str = ""
    config_path: str = ""
    users: list[HaUser] = field(default_factory=list)
    error: str = ""


@dataclass
class MqttDiagnosticStatus:
    """Diagnóstico MQTT Z-Wave JS UI (runbook Horus)."""

    store_path: str = ""
    service_name: str = "zwave-ui.service"
    settings_found: bool = False
    mqtt_disabled: Optional[bool] = None  # True = MQTT deshabilitado en config
    mqtt_host: str = ""
    mqtt_port: int = 0
    has_mqtt_log_errors: bool = False
    mqtt_log_sample: list[str] = field(default_factory=list)
    zwave_ui_process_running: bool = False
    zwave_ui_service_active: bool = False
    port_3000_open: bool = False
    port_8091_open: bool = False
    port_1883_open: bool = False
    broker_probe: str = ""  # succeeded | refused | unknown
    mosquitto_running: bool = False
    ha_container_found: bool = False
    ha_zwave_ws_url: str = ""
    ha_mqtt_integration: str = ""
    recommended_action: str = "review_manual"
    action_detail: str = ""


@dataclass
class CloudflareStatus:
    """Estado de cloudflared en el controlador."""

    installed: bool
    version: str = ""
    running_tunnels: list[str] = field(default_factory=list)
    error: str = ""
    ha_proxy_ok: bool = False
    ha_proxy_detail: str = ""


@dataclass
class PluginServiceStatus:
    """Estado del custom component plugin_service en el controlador."""

    parent_dir: str = REMOTE_CUSTOM_COMPONENTS
    plugin_dir: str = f"{REMOTE_CUSTOM_COMPONENTS}/plugin_service"
    parent_exists: bool = False
    plugin_exists: bool = False
    found_names: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    plugin_entries: list[str] = field(default_factory=list)
    manifest_domain: str = ""
    error: str = ""


@dataclass
class HaConfigurationStatus:
    """Estado HTTP de HA: YAML legado + .storage/http (trusted_proxies)."""

    path: str = REMOTE_CONFIGURATION_YAML
    exists: bool = False
    is_empty: bool = True
    has_http_block: bool = False
    has_use_x_forwarded_for: bool = False
    has_trusted_proxy_ipv4: bool = False
    has_trusted_proxy_ipv6: bool = False
    has_all_cors_origins: bool = False
    missing_cors_origins: list[str] = field(default_factory=list)
    has_use_x_frame_options: bool = False
    http_ok: bool = False
    content_preview: str = ""
    error: str = ""
    ha_version: str = ""
    config_dir: str = ""
    storage_path: str = ""
    storage_exists: bool = False
    storage_use_x_forwarded_for: bool = False
    storage_has_proxy_ipv4: bool = False
    storage_has_proxy_ipv6: bool = False
    storage_pending: bool = False
    storage_yaml_migration_done: bool = False
    storage_proxy_ok: bool = False
    uses_storage_http: bool = False
    proxy_ok: bool = False


@dataclass
class BackupEntry:
    """Carpeta de backup detectada en el controlador."""

    path: str
    kind: str  # ha | zwave | other
    size: str = ""
    size_bytes: int = 0
    date_label: str = ""


@dataclass
class MaintenanceStatus:
    """Estado de limpieza y optimización del controlador."""
    apt_cache_size: str = ""
    npm_cache_size: str = ""
    journal_size: str = ""
    nested_config_detected: bool = False
    old_archives: list[str] = field(default_factory=list)
    ha_db_size_mb: float = 0.0
    ha_db_alert: bool = False
    last_cleanup_summary: str = ""
    error: str = ""

@dataclass
class SystemHealthStatus:
    """Disco, memoria y unidades systemd fallidas."""

    disk: str = ""
    memory: str = ""
    uptime: str = ""
    failed_units: str = ""
    has_failed: bool = False
    error: str = ""


@dataclass
class BackupManagerStatus:
    """Estado de disco y backups del controlador."""

    root_free: str = ""
    root_used_pct: str = ""
    root_avail_bytes: int = 0
    docker_summary: str = ""
    backups: list[BackupEntry] = field(default_factory=list)
    ha_config_path: str = ""
    zwave_store_path: str = ""
    low_space: bool = False
    error: str = ""


@dataclass
class HaIntegrationStatus:
    """Estado de un custom component en /config/custom_components."""

    domain: str
    parent_dir: str = REMOTE_CUSTOM_COMPONENTS
    component_dir: str = ""
    parent_exists: bool = False
    component_exists: bool = False
    components: list[str] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    manifest_domain: str = ""
    manifest_version: str = ""
    error: str = ""


@dataclass
class AdminNetworkHostStatus:
    """Estado del servicio host admin_network (systemd + API :8765)."""

    install_dir: str = "/opt/admin_network"
    env_file: str = "/etc/admin_network.env"
    service_name: str = "admin_network"
    dir_exists: bool = False
    env_exists: bool = False
    service_active: bool = False
    service_enabled: bool = False
    health_ok: bool = False
    health_detail: str = ""
    api_key: str = ""
    port: int = 8765
    error: str = ""


@dataclass
class AdminNetworkInstallStatus:
    """Estado combinado: servicio host + integración HA."""

    ha: HaIntegrationStatus
    host: AdminNetworkHostStatus
