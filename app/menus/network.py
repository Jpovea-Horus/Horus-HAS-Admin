"""Menús de red Ethernet / Wi-Fi."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError
from ui import (
    ask,
    ask_int,
    ask_password,
    confirm,
    error,
    info,
    menu_options,
    panel_ip_output,
    panel_ping,
    panel_profile,
    section,
    success,
    table_devices,
    table_wifi,
    warning,
)


def menu_network_status(api: HasControllerAPI) -> None:
    section("Estado de red")
    status = api.get_network_summary()
    table_devices(status.devices, status.default_gateway)
    panel_ip_output(status.raw_ip_addr)
    if confirm("¿Ejecutar ping a 8.8.8.8?", default=False):
        try:
            panel_ping(api.ping())
        except HasApiError as exc:
            error(f"Ping falló: {exc}")


def _select_profile(api: HasControllerAPI, device_filter: str) -> tuple[str, str] | None:
    if device_filter == "ethernet":
        devs = api.network.get_ethernet_devices()
    else:
        devs = api.network.get_wifi_devices()

    if not devs:
        warning(f"No hay interfaces {device_filter}.")
        return None

    if len(devs) == 1:
        device = devs[0].device
    else:
        menu_options(
            "Seleccione interfaz",
            [(str(i), f"{d.device} ({d.state})") for i, d in enumerate(devs, 1)],
        )
        idx = ask_int("Número de interfaz")
        if idx is None or idx < 1 or idx > len(devs):
            warning("Selección inválida.")
            return None
        device = devs[idx - 1].device

    profile = api.network.get_profile_for_device(device)
    if not profile:
        warning("No hay perfil de conexión activo para esa interfaz.")
        return None
    return device, profile


def _menu_ip_profile(api: HasControllerAPI, device_filter: str) -> None:
    selected = _select_profile(api, device_filter)
    if not selected:
        return
    device, profile = selected
    prof = api.network.get_connection_profile(profile)
    panel_profile(profile, prof)

    opts = [
        ("1", "IP dinámica (DHCP)"),
        ("2", "IP estática"),
    ]
    if device_filter == "ethernet":
        opts.append(("3", "Re-aplicar interfaz"))
    opts.append(("0", "Volver"))
    menu_options("Configuración IP", opts)
    op = ask("Opción")
    try:
        if op == "1":
            if confirm(f"¿Aplicar DHCP en '{profile}'?", default=False):
                api.network.set_dhcp(profile)
                success("DHCP aplicado correctamente.")
        elif op == "2":
            addr = ask("IP/máscara CIDR", default="192.168.1.50/24")
            gw = ask("Gateway", default="192.168.1.1")
            dns = ask("DNS (opcional)", default="")
            warning("Si cambia la IP del host, la sesión SSH puede cortarse.")
            info("Use C en el menú principal para reconectar a la nueva IP.")
            if confirm("¿Aplicar IP estática?", default=False):
                api.network.set_static_ip(profile, addr, gw, dns or None)
                success("IP estática aplicada.")
        elif op == "3" and device_filter == "ethernet":
            api.network.reapply_device(device)
            success("Interfaz re-aplicada.")
    except HasApiError as exc:
        error(str(exc))


def menu_ethernet(api: HasControllerAPI) -> None:
    section("Ethernet")
    _menu_ip_profile(api, "ethernet")


def menu_wifi(api: HasControllerAPI) -> None:
    while True:
        section("Wi-Fi")
        menu_options(
            "Wi-Fi",
            [
                ("1", "Escanear redes"),
                ("2", "Conectar a red"),
                ("3", "Desconectar"),
                ("4", "Configurar IP (DHCP / estática)"),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                info("Escaneando redes…")
                networks = api.network.list_wifi()
                if networks:
                    table_wifi(networks)
                else:
                    warning("No se encontraron redes.")
            elif op == "2":
                ssid = ask("SSID")
                pwd = ask_password("Contraseña Wi-Fi")
                api.network.connect_wifi(ssid, pwd)
                success(f"Conectado a [bold]{ssid}[/bold].")
            elif op == "3":
                devs = api.network.get_wifi_devices()
                if not devs:
                    warning("Sin interfaz Wi-Fi.")
                    continue
                if len(devs) == 1:
                    device = devs[0].device
                else:
                    menu_options(
                        "Interfaz",
                        [(str(i), d.device) for i, d in enumerate(devs, 1)],
                    )
                    idx = ask_int("Número")
                    if idx is None or idx < 1 or idx > len(devs):
                        warning("Selección inválida.")
                        continue
                    device = devs[idx - 1].device
                api.network.disconnect_device(device)
                success("Wi-Fi desconectado.")
            elif op == "4":
                _menu_ip_profile(api, "wifi")
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))
