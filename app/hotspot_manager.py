"""Zona Wi-Fi de rescate nexxo-hotspot (servicio systemd en el Debian del controlador).

Si el controlador se queda sin Ethernet ni Wi-Fi cliente, el timer enciende un punto
de acceso «Nexxo-XXXX» (últimos 4 dígitos de la MAC Ethernet) en 10.42.0.1 para
entrar por SSH y configurarlo. No depende de Home Assistant.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from models import HotspotStatus

if TYPE_CHECKING:
    from network_manager import NetworkManager
    from ssh_client import SSHClient

PROFILE = "nexxo-hotspot"
SSID_PREFIX = "Nexxo"
HOTSPOT_IP = "10.42.0.1"
SCRIPT_PATH = "/usr/local/sbin/nexxo-hotspot.sh"
ENV_FILE = "/etc/nexxo-hotspot.env"
SERVICE_NAME = "nexxo-hotspot.service"
TIMER_NAME = "nexxo-hotspot.timer"
UNIT_DIR = "/etc/systemd/system"
FORCE_FLAG = "/run/nexxo-hotspot.force"
STATE_FILE = "/run/nexxo-hotspot.since"
# Leído por el dnsmasq que NetworkManager lanza en modo «shared».
SHARED_DNSMASQ_CONF = "/etc/NetworkManager/dnsmasq-shared.d/nexxo-hotspot.conf"
LOG_TAG = "nexxo-hotspot"
GRACE_S = 90
RETRY_S = 600
REQUIRED_PACKAGES = ("iw", "dnsmasq-base", "iptables")
APT_TIMEOUT = 300
ACTIVATION_TIMEOUT = 100
SHARED_PORTS = (53, 67)
DEFAULT_PASSWORD = "adminhorus26*"

HOTSPOT_SCRIPT = r"""#!/bin/bash
# Zona Wi-Fi de rescate Nexxo (instalado por Gestor Nexxo 800).
# Se enciende si no hay Ethernet ni Wi-Fi cliente; se apaga al volver la red.
PROFILE=nexxo-hotspot
HOTSPOT_IF=wlan0
GRACE_S=90
RETRY_S=600
[ -f /etc/nexxo-hotspot.env ] && . /etc/nexxo-hotspot.env
STATE=/run/nexxo-hotspot.since
FORCE=/run/nexxo-hotspot.force
now=$(date +%s)

log() { logger -t nexxo-hotspot "$*"; }

online() {
  nmcli -t -f TYPE,STATE,CONNECTION device status 2>/dev/null \
    | grep -v ":${PROFILE}\$" | grep -Eq '^(ethernet|wifi):connected' && return 0
  # Interfaces fuera de NetworkManager (ifupdown) con ruta por defecto viva.
  ip -4 route show default 2>/dev/null | grep -v linkdown \
    | grep -Ev "dev (${HOTSPOT_IF}|ppp|wwan|zt|tun|docker|br-)" | grep -q 'dev '
}
hotspot_up() {
  nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$PROFILE"
}
clients() {
  if command -v iw >/dev/null 2>&1; then
    iw dev "$HOTSPOT_IF" station dump 2>/dev/null | grep -c '^Station'
  else
    ip neigh show dev "$HOTSPOT_IF" 2>/dev/null | grep -cE 'REACHABLE|DELAY|PROBE'
  fi
}
has_client_wifi() {
  nmcli -t -f NAME,TYPE connection show 2>/dev/null \
    | grep -v "^${PROFILE}:" | grep -Eq ':(802-11-wireless|wifi)$'
}
start_hotspot() {
  nmcli radio wifi on >/dev/null 2>&1
  if nmcli connection up "$PROFILE" >/dev/null 2>&1; then
    log "$1: zona Wi-Fi activada"
  else
    log "$1: no se pudo activar la zona Wi-Fi"
  fi
}

if [ -f "$FORCE" ]; then
  hotspot_up || start_hotspot "Modo forzado"
  exit 0
fi

if online; then
  if hotspot_up && nmcli connection down "$PROFILE" >/dev/null 2>&1; then
    log "Red disponible: zona Wi-Fi apagada"
  fi
  rm -f "$STATE"
  exit 0
fi

[ -f "$STATE" ] || echo "$now" > "$STATE"
since=$(cat "$STATE" 2>/dev/null || echo "$now")
elapsed=$(( now - since ))

if ! hotspot_up; then
  if (( elapsed >= GRACE_S )); then
    start_hotspot "Sin red ${elapsed}s"
    echo "$now" > "$STATE"
  fi
elif (( $(clients) > 0 )); then
  echo "$now" > "$STATE"
elif has_client_wifi && (( elapsed >= RETRY_S )); then
  # Con una sola radio el AP impide reconectar como cliente: se libera un rato.
  nmcli connection down "$PROFILE" >/dev/null 2>&1
  log "Zona Wi-Fi sin clientes ${elapsed}s: reintentando Wi-Fi cliente"
  echo "$now" > "$STATE"
fi
exit 0
"""

SERVICE_UNIT = """[Unit]
Description=Nexxo - zona Wi-Fi de rescate
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nexxo-hotspot.sh
"""

TIMER_UNIT = """[Unit]
Description=Nexxo - revisa conectividad para la zona Wi-Fi de rescate

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
"""


class HotspotManager:
    """Instala, consulta y controla nexxo-hotspot en el controlador."""

    def __init__(self, ssh: SSHClient, network: NetworkManager):
        self.ssh = ssh
        self.network = network

    def expected_ssid(self) -> str:
        mac = self.network.get_primary_ethernet_mac()
        return f"{SSID_PREFIX}-{mac.replace(':', '')[-4:].upper()}"

    def get_status(self) -> HotspotStatus:
        status = HotspotStatus(address=HOTSPOT_IP)
        try:
            status.expected_ssid = self.expected_ssid()
        except SSHCommandError as exc:
            status.error = str(exc)

        q = shlex.quote(PROFILE)
        prof = self.ssh.run(
            f"nmcli -g 802-11-wireless.ssid,connection.interface-name connection show {q} 2>/dev/null",
            use_sudo=True,
        )
        lines = [ln.strip() for ln in (prof.stdout or "").splitlines()]
        if prof.ok and lines and lines[0]:
            status.profile_exists = True
            status.ssid = lines[0]
            status.interface = lines[1] if len(lines) > 1 else ""

        status.script_exists = self.ssh.run(f"test -x {SCRIPT_PATH}").ok
        enabled = self.ssh.run(f"systemctl is-enabled {TIMER_NAME} 2>/dev/null")
        active = self.ssh.run(f"systemctl is-active {TIMER_NAME} 2>/dev/null")
        status.timer_enabled = enabled.stdout.strip() == "enabled"
        status.timer_active = active.stdout.strip() == "active"

        conns = self.ssh.run("nmcli -t -f NAME connection show --active 2>/dev/null")
        status.hotspot_active = PROFILE in (conns.stdout or "").splitlines()
        status.forced = self.ssh.run(f"test -f {FORCE_FLAG}").ok

        if status.hotspot_active and status.interface:
            count = self.ssh.run(
                f"iw dev {shlex.quote(status.interface)} station dump 2>/dev/null "
                "| grep -c '^Station' || true",
                use_sudo=True,
            )
            try:
                status.clients = int((count.stdout or "0").strip() or 0)
            except ValueError:
                status.clients = 0
        elif status.profile_exists:
            status.port_conflicts = self._port_conflicts(status.interface)
        status.dns_disabled = self._dns_disabled()

        log = self.ssh.run(
            f"journalctl -t {LOG_TAG} -n 8 --no-pager -o short 2>/dev/null || true",
            use_sudo=True,
        )
        log_txt = (log.stdout or "").strip()
        if log_txt and "No entries" not in log_txt:
            status.recent_log = log_txt
        return status

    def get_password(self) -> str:
        res = self.ssh.run(
            f"nmcli -s -g 802-11-wireless-security.psk connection show {shlex.quote(PROFILE)}",
            use_sudo=True,
        )
        if not res.ok or not res.stdout.strip():
            raise SSHCommandError("No se pudo leer la contraseña de la zona Wi-Fi (¿está instalada?).")
        return res.stdout.strip()

    def install(self, password: str, wifi_device: str | None = None) -> str:
        _validate_password(password)
        iface = wifi_device or self._default_wifi_device()
        ssid = self.expected_ssid()

        self._ensure_packages()
        self._check_ap_support()
        self.ssh.run(f"rm -f {FORCE_FLAG} {STATE_FILE}", use_sudo=True)
        self._create_profile(ssid, password, iface)

        dns_owners = _parse_port_conflicts(self._udp_listeners(), iface, ports=(53,))
        if dns_owners:
            self.ssh.run_or_raise(f"mkdir -p {SHARED_DNSMASQ_CONF.rsplit('/', 1)[0]}", use_sudo=True)
            self._write_file(SHARED_DNSMASQ_CONF, "port=0\n", "644")
        else:
            self.ssh.run(f"rm -f {SHARED_DNSMASQ_CONF}", use_sudo=True)

        env = f"HOTSPOT_IF={iface}\nGRACE_S={GRACE_S}\nRETRY_S={RETRY_S}\n"
        self._write_file(ENV_FILE, env, "644")
        self._write_file(SCRIPT_PATH, HOTSPOT_SCRIPT, "755")
        self._write_file(f"{UNIT_DIR}/{SERVICE_NAME}", SERVICE_UNIT, "644")
        self._write_file(f"{UNIT_DIR}/{TIMER_NAME}", TIMER_UNIT, "644")
        self.ssh.run_or_raise("systemctl daemon-reload", use_sudo=True)
        self.ssh.run_or_raise(f"systemctl enable --now {TIMER_NAME}", use_sudo=True)

        msg = (
            f"Zona Wi-Fi instalada: SSID {ssid} en {iface}, IP {HOTSPOT_IP}. "
            f"Se enciende sola tras {GRACE_S}s sin red."
        )
        if dns_owners:
            msg += (
                f"\nDNS 53 ocupado por {', '.join(dns_owners)}: la zona Wi-Fi reparte IPs "
                "sin DNS propio (SSH por IP funciona igual)."
            )
        conflicts = self._port_conflicts(iface)
        if conflicts:
            msg += "\n" + _port_conflict_hint(conflicts)
        return msg

    def start(self) -> str:
        """Enciende la zona Wi-Fi y la mantiene encendida hasta apagarla o reiniciar."""
        self.ssh.run_or_raise(f"touch {FORCE_FLAG}", use_sudo=True)
        self.ssh.run("nmcli radio wifi on", use_sudo=True)
        res = self.ssh.run(
            f"nmcli --wait 90 connection up {shlex.quote(PROFILE)}",
            use_sudo=True,
            timeout=ACTIVATION_TIMEOUT,
        )
        if not res.ok:
            self.ssh.run(f"rm -f {FORCE_FLAG}", use_sudo=True)
            raise SSHCommandError(
                self._activation_failure_detail(res),
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        return f"Zona Wi-Fi encendida (forzada). SSH: root@{HOTSPOT_IP}"

    def stop(self) -> str:
        """Quita el modo forzado y apaga; el timer la vuelve a encender si no hay red."""
        self.ssh.run(f"rm -f {FORCE_FLAG} {STATE_FILE}", use_sudo=True)
        self.ssh.run(f"nmcli connection down {shlex.quote(PROFILE)} 2>/dev/null || true", use_sudo=True)
        return (
            "Zona Wi-Fi apagada. Modo automático: se encenderá sola si el "
            f"controlador queda {GRACE_S}s sin red."
        )

    def remove(self) -> str:
        self.ssh.run(f"systemctl disable --now {TIMER_NAME} 2>/dev/null || true", use_sudo=True)
        self.ssh.run(f"rm -f {FORCE_FLAG} {STATE_FILE}", use_sudo=True)
        q = shlex.quote(PROFILE)
        self.ssh.run(f"nmcli connection down {q} 2>/dev/null || true", use_sudo=True)
        self.ssh.run(f"nmcli connection delete {q} 2>/dev/null || true", use_sudo=True)
        self.ssh.run(
            f"rm -f {SCRIPT_PATH} {ENV_FILE} {SHARED_DNSMASQ_CONF} "
            f"{UNIT_DIR}/{SERVICE_NAME} {UNIT_DIR}/{TIMER_NAME}",
            use_sudo=True,
        )
        self.ssh.run("systemctl daemon-reload", use_sudo=True)
        return "Zona Wi-Fi de rescate eliminada (perfil, script y timer)."

    def _default_wifi_device(self) -> str:
        devs = self.network.get_wifi_devices()
        if not devs:
            raise ValidationError("No hay interfaz Wi-Fi en NetworkManager.")
        return devs[0].device

    def _missing_packages(self) -> list[str]:
        return [
            pkg for pkg in REQUIRED_PACKAGES
            if not self.ssh.run(f"dpkg -s {pkg} >/dev/null 2>&1").ok
        ]

    def _udp_listeners(self) -> str:
        return self.ssh.run("ss -lunpH 2>/dev/null", use_sudo=True).stdout or ""

    def _dns_disabled(self) -> bool:
        return self.ssh.run(f"grep -qx 'port=0' {SHARED_DNSMASQ_CONF} 2>/dev/null").ok

    def _port_conflicts(self, iface: str) -> list[str]:
        """Servicios que ocupan DNS/DHCP (53/67 UDP) donde el dnsmasq de NM los necesita."""
        ports = (67,) if self._dns_disabled() else SHARED_PORTS
        return _parse_port_conflicts(self._udp_listeners(), iface, ports=ports)

    def _activation_failure_detail(self, res) -> str:
        detail = (res.stderr or res.stdout or "nmcli connection up falló.").strip()
        prof = self.ssh.run(
            f"nmcli -g connection.interface-name connection show {shlex.quote(PROFILE)} 2>/dev/null",
            use_sudo=True,
        )
        iface = (prof.stdout or "").strip() or "wlan0"

        conflicts = self._port_conflicts(iface)
        if conflicts:
            detail += "\n\n" + _port_conflict_hint(conflicts)
        missing = self._missing_packages()
        if missing:
            detail += (
                f"\n\nFaltan paquetes: {', '.join(missing)}. "
                "Reinstale la zona Wi-Fi (opción 1) con el controlador conectado a internet."
            )

        log = self.ssh.run(
            "journalctl -u NetworkManager -n 120 --no-pager -o cat 2>/dev/null "
            rf"| grep -iE 'dnsmasq|shared|iptables|nft|{iface}|10\.42|{PROFILE}' "
            "| tail -n 15",
            use_sudo=True,
        )
        log_txt = (log.stdout or "").strip()
        if log_txt:
            detail += f"\n--- NetworkManager ---\n{log_txt}"
        detail += "\n\nModo forzado retirado: la zona Wi-Fi sigue en modo automático."
        return detail

    def _ensure_packages(self) -> None:
        missing = self._missing_packages()
        if not missing:
            return
        pkgs = " ".join(missing)
        install = f"apt-get install -y --no-install-recommends {pkgs}"
        cmd = (
            "export DEBIAN_FRONTEND=noninteractive; "
            f"{install} || (apt-get -o Acquire::Check-Valid-Until=false update && {install})"
        )
        res = self.ssh.run(f"bash -c {shlex.quote(cmd)}", use_sudo=True, timeout=APT_TIMEOUT)
        if not res.ok:
            raise SSHCommandError(
                f"No se pudieron instalar {pkgs} (necesarios para el punto de acceso). "
                "Verifique que el controlador tenga internet por Ethernet.",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

    def _check_ap_support(self) -> None:
        res = self.ssh.run("iw list 2>/dev/null", use_sudo=True)
        modes = _parse_interface_modes(res.stdout or "")
        if modes and "AP" not in modes:
            raise ValidationError(
                "El chip Wi-Fi no soporta modo punto de acceso (AP). "
                f"Modos disponibles: {', '.join(sorted(modes))}."
            )

    def _create_profile(self, ssid: str, password: str, iface: str) -> None:
        q = shlex.quote(PROFILE)
        self.ssh.run(f"nmcli connection delete {q} 2>/dev/null || true", use_sudo=True)
        self.ssh.run_or_raise(
            f"nmcli connection add type wifi ifname {shlex.quote(iface)} con-name {q} "
            f"autoconnect no ssid {shlex.quote(ssid)} "
            "802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 "
            "802-11-wireless.powersave 2 "
            f"ipv4.method shared ipv4.addresses {HOTSPOT_IP}/24 ipv6.method ignore "
            "wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn "
            "wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.pmf disable "
            f"wifi-sec.psk {shlex.quote(password)}",
            use_sudo=True,
        )

    def _write_file(self, path: str, content: str, mode: str) -> None:
        q = shlex.quote(path)
        payload = f"printf %s {shlex.quote(content)} > {q} && chmod {mode} {q}"
        self.ssh.run_or_raise(f"bash -c {shlex.quote(payload)}", use_sudo=True)


def _validate_password(password: str) -> None:
    if not 8 <= len(password) <= 63:
        raise ValidationError("La contraseña WPA2 debe tener entre 8 y 63 caracteres.")
    if not password.isascii() or not password.isprintable():
        raise ValidationError("La contraseña solo admite caracteres ASCII imprimibles.")


def _parse_port_conflicts(
    ss_output: str, iface: str, ports: tuple[int, ...] = SHARED_PORTS
) -> list[str]:
    """Listeners UDP en `ports` sobre todas las interfaces o la del hotspot (`ss -lunpH`)."""
    conflicts: list[str] = []
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[3]
        host, _, port = local.rpartition(":")
        if not port.isdigit() or int(port) not in ports:
            continue
        host, _, bound_iface = host.partition("%")
        if bound_iface:
            if bound_iface != iface:
                continue
        elif host not in ("0.0.0.0", "*", "[::]", "::"):
            continue
        match = re.search(r'users:\(\("([^"]+)"', line)
        name = match.group(1) if match else "proceso desconocido"
        entry = f"{name} en {local}"
        if entry not in conflicts:
            conflicts.append(entry)
    return conflicts


def _port_conflict_hint(conflicts: list[str]) -> str:
    return (
        f"AVISO: puertos DNS/DHCP ocupados por {', '.join(conflicts)}. "
        "NetworkManager necesita 53 y 67 UDP en la zona Wi-Fi para repartir IPs; "
        "detenga ese servicio o limítelo a otras interfaces (bind-interfaces)."
    )


def _parse_interface_modes(iw_list: str) -> set[str]:
    """Modos de la primera radio en `iw list` (sección Supported interface modes)."""
    modes: set[str] = set()
    inside = False
    for line in iw_list.splitlines():
        text = line.strip()
        if text.startswith("Supported interface modes"):
            inside = True
            continue
        if inside:
            if not text.startswith("* "):
                break
            modes.add(text[2:].strip())
    return modes
