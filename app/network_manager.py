"""Gestión de red Ethernet/Wi-Fi vía nmcli."""

from __future__ import annotations

import re
import shlex
from typing import Optional

from exceptions import SSHCommandError, ValidationError
from models import ConnectionProfile, NetworkDevice, NetworkStatus, WifiNetwork
from ssh_client import SSHClient


class NetworkManager:
    """Operaciones NetworkManager remotas (solo Ethernet y Wi-Fi)."""

    def __init__(self, ssh: SSHClient):
        self._ssh = ssh

    def get_status(self) -> NetworkStatus:
        device_raw = self._ssh.run_or_raise("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status")
        ip_raw = self._ssh.run_or_raise("ip -4 addr")
        gateway = ""
        gw_result = self._ssh.run("ip -4 route show default")
        if gw_result.ok and gw_result.stdout:
            match = re.search(r"via\s+(\S+)", gw_result.stdout)
            if match:
                gateway = match.group(1)

        macs = self._get_mac_addresses()
        devices = self._parse_device_status(device_raw, macs)
        return NetworkStatus(
            devices=devices,
            default_gateway=gateway,
            raw_device_status=device_raw,
            raw_ip_addr=ip_raw,
        )

    def list_connections(self) -> list[str]:
        raw = self._ssh.run_or_raise("nmcli -t -f NAME connection show", use_sudo=True)
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def get_connection_profile(self, profile_name: str) -> ConnectionProfile:
        q = shlex.quote(profile_name)
        raw = self._ssh.run_or_raise(f"nmcli -t connection show {q}", use_sudo=True)
        fields = self._parse_key_value_t(raw)
        return ConnectionProfile(
            name=profile_name,
            device=fields.get("connection.interface-name", fields.get("GENERAL.DEVICES", "")),
            ipv4_method=fields.get("ipv4.method", ""),
            ipv4_addresses=fields.get("ipv4.addresses", ""),
            ipv4_gateway=fields.get("ipv4.gateway", ""),
            ipv4_dns=fields.get("ipv4.dns", ""),
        )

    def get_profile_for_device(self, device: str) -> Optional[str]:
        q = shlex.quote(device)
        raw = self._ssh.run(f"nmcli -g GENERAL.CONNECTION device show {q}", use_sudo=True)
        if raw.ok and raw.stdout.strip() and raw.stdout.strip() != "--":
            return raw.stdout.strip()
        return None

    def list_wifi(self) -> list[WifiNetwork]:
        self._ssh.run_or_raise("nmcli dev wifi rescan 2>/dev/null || true", use_sudo=True)
        raw = self._ssh.run_or_raise("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list")
        networks: list[WifiNetwork] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            in_use, ssid, signal, security = parts[0], parts[1], parts[2], ":".join(parts[3:])
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            networks.append(
                WifiNetwork(
                    ssid=ssid,
                    signal=signal,
                    security=security,
                    in_use=in_use == "*",
                )
            )
        return networks

    def connect_wifi(self, ssid: str, password: str) -> str:
        if not ssid.strip():
            raise ValidationError("El SSID no puede estar vacío.")
        cmd = (
            f"nmcli dev wifi connect {shlex.quote(ssid)} "
            f"password {shlex.quote(password)}"
        )
        # 1. Conectar a la red (crea o actualiza el perfil)
        result = self._ssh.run_or_raise(cmd, use_sudo=True)

        # 2. Configurar prioridad alta y reintentos infinitos para persistencia
        q_ssid = shlex.quote(ssid)
        # Prioridad 100 (mayor que el 0 por defecto) para preferir esta red
        self._ssh.run(f"nmcli connection modify {q_ssid} connection.autoconnect-priority 100", use_sudo=True)
        # Retries 0 = reintentos infinitos si se pierde la señal
        self._ssh.run(f"nmcli connection modify {q_ssid} connection.autoconnect-retries 0", use_sudo=True)
        self._ssh.run(f"nmcli connection modify {q_ssid} connection.autoconnect yes", use_sudo=True)

        return result

    def disconnect_device(self, device: str) -> str:
        q = shlex.quote(device)
        return self._ssh.run_or_raise(f"nmcli device disconnect {q}", use_sudo=True)

    def set_dhcp(self, profile_name: str) -> str:
        q = shlex.quote(profile_name)
        self._ssh.run_or_raise(f"nmcli connection modify {q} ipv4.method auto", use_sudo=True)
        self._ssh.run_or_raise(f"nmcli connection modify {q} ipv4.addresses ''", use_sudo=True)
        self._ssh.run_or_raise(f"nmcli connection modify {q} ipv4.gateway ''", use_sudo=True)
        return self._ssh.run_or_raise(f"nmcli connection up {q}", use_sudo=True)

    def set_static_ip(
        self,
        profile_name: str,
        address_cidr: str,
        gateway: str,
        dns: Optional[str] = None,
    ) -> str:
        if "/" not in address_cidr:
            raise ValidationError("La dirección debe incluir máscara CIDR (ej. 192.168.1.50/24).")
        q = shlex.quote(profile_name)
        self._ssh.run_or_raise(
            f"nmcli connection modify {q} ipv4.method manual "
            f"ipv4.addresses {shlex.quote(address_cidr)} "
            f"ipv4.gateway {shlex.quote(gateway)}",
            use_sudo=True,
        )
        if dns:
            self._ssh.run_or_raise(
                f"nmcli connection modify {q} ipv4.dns {shlex.quote(dns)}",
                use_sudo=True,
            )
        return self._ssh.run_or_raise(f"nmcli connection up {q}", use_sudo=True)

    def reapply_device(self, device: str) -> str:
        q = shlex.quote(device)
        return self._ssh.run_or_raise(f"nmcli device reapply {q}", use_sudo=True)

    def activate_connection(self, profile_name: str) -> str:
        q = shlex.quote(profile_name)
        return self._ssh.run_or_raise(f"nmcli connection up {q}", use_sudo=True)

    def ping(self, host: str = "8.8.8.8", count: int = 4) -> str:
        q = shlex.quote(host)
        result = self._ssh.run(f"ping -c {count} {q}")
        if not result.ok:
            raise SSHCommandError(
                "Ping falló o sin conectividad.",
                exit_code=result.exit_code,
                stderr=result.stderr or result.stdout,
            )
        return result.stdout

    def get_ethernet_devices(self) -> list[NetworkDevice]:
        return [d for d in self.get_status().devices if d.device_type == "ethernet"]

    def get_wifi_devices(self) -> list[NetworkDevice]:
        return [d for d in self.get_status().devices if d.device_type == "wifi"]

    def _get_mac_addresses(self) -> dict[str, str]:
        """Obtiene MAC por interfaz (`ip -br link`)."""
        macs: dict[str, str] = {}
        result = self._ssh.run("ip -br link")
        if not result.ok or not result.stdout:
            return macs
        mac_re = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device = parts[0].split("@", 1)[0]
            for token in parts[2:]:
                if mac_re.match(token):
                    macs[device] = token.upper()
                    break
        return macs

    @staticmethod
    def _parse_device_status(raw: str, macs: Optional[dict[str, str]] = None) -> list[NetworkDevice]:
        macs = macs or {}
        devices = []
        for line in raw.splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            name = parts[0]
            devices.append(
                NetworkDevice(
                    device=name,
                    device_type=parts[1],
                    state=parts[2],
                    connection=parts[3] if parts[3] != "--" else "",
                    mac=macs.get(name, ""),
                )
            )
        return devices

    @staticmethod
    def _parse_key_value_t(raw: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value
        return fields
