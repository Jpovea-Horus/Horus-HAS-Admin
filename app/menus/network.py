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
    working,
)


def menu_network_status(api: HasControllerAPI) -> None:
    section("Estado de red")
    with working("Leyendo interfaces, IPs y gateway…"):
        status = api.get_network_summary()
    table_devices(status.devices, status.default_gateway)
    panel_ip_output(status.raw_ip_addr)
    if confirm("¿Ejecutar ping a 8.8.8.8?", default=False):
        try:
            with working("Ping a 8.8.8.8…"):
                out = api.ping()
            panel_ping(out)
        except HasApiError as exc:
            error(f"Ping falló: {exc}")


def _select_profile(api: HasControllerAPI, device_filter: str) -> tuple[str, str] | None:
    with working("Buscando interfaces…"):
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

    with working(f"Leyendo perfil de {device}…"):
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
    with working(f"Leyendo configuración IP de '{profile}'…"):
        prof = api.network.get_connection_profile(profile)
    panel_profile(profile, prof)

    opts = [
        ("1", "IP dinámica (DHCP)"),
        ("2", "IP estática"),
        ("3", "Servidores DNS"),
    ]
    if device_filter == "ethernet":
        opts.append(("4", "Re-aplicar interfaz"))
    opts.append(("", ""))
    opts.append(("0", "Volver"))
    menu_options("Configuración IP", opts)
    op = ask("Opción")
    try:
        if op == "1":
            if confirm(f"¿Aplicar DHCP en '{profile}'?", default=False):
                with working("Aplicando DHCP (la conexión se re-activa)…"):
                    api.network.set_dhcp(profile)
                success("DHCP aplicado correctamente.")
                if prof.ipv4_dns and confirm(
                    f"El perfil tiene DNS manual ({prof.ipv4_dns}). ¿Usar los DNS del DHCP?",
                    default=True,
                ):
                    with working("Restaurando DNS automáticos…"):
                        api.network.reset_dns_auto(profile)
                    success("DNS automáticos (DHCP) restaurados.")
        elif op == "2":
            addr = ask("IP/máscara CIDR", default="192.168.1.50/24")
            gw = ask("Gateway", default="192.168.1.1")
            dns = ask("DNS separados por coma", default=prof.ipv4_dns or "8.8.8.8,1.1.1.1")
            warning("Si cambia la IP del host, la sesión SSH puede cortarse.")
            info("Use C en el menú principal para reconectar a la nueva IP.")
            if confirm("¿Aplicar IP estática?", default=False):
                with working("Aplicando IP estática (la conexión se re-activa)…"):
                    api.network.set_static_ip(profile, addr, gw, dns or None)
                success("IP estática aplicada.")
        elif op == "3":
            _menu_dns(api, profile, prof)
        elif op == "4" and device_filter == "ethernet":
            with working(f"Re-aplicando {device}…"):
                api.network.reapply_device(device)
            success("Interfaz re-aplicada.")
    except HasApiError as exc:
        error(str(exc))


def _menu_dns(api: HasControllerAPI, profile: str, prof) -> None:
    is_dhcp = prof.ipv4_method == "auto"
    opts = [("1", "DNS manuales (reemplazan a los del DHCP)" if is_dhcp else "DNS manuales")]
    if is_dhcp:
        opts.append(("2", "DNS manuales + DNS del DHCP"))
        opts.append(("3", "DNS automáticos (DHCP)"))
    else:
        opts.append(("3", "Quitar DNS manuales"))
    opts += [("", ""), ("0", "Volver")]
    menu_options("Servidores DNS IPv4", opts)
    op = ask("Opción")

    if op == "1" or (op == "2" and is_dhcp):
        dns = ask("DNS separados por coma", default=prof.ipv4_dns or "8.8.8.8,1.1.1.1")
        servers = api.network.parse_dns_servers(dns)
        info("La conexión se re-activa; la sesión SSH puede cortarse unos segundos.")
        if confirm(f"¿Aplicar DNS {', '.join(servers)} en '{profile}'?", default=False):
            with working("Aplicando DNS…"):
                api.network.set_dns(profile, dns, ignore_auto=(op == "1"))
            success("DNS aplicados.")
    elif op == "3":
        if not is_dhcp:
            warning("Con IP estática y sin DNS el controlador no resolverá nombres.")
        if confirm(f"¿Eliminar DNS manuales en '{profile}'?", default=False):
            with working("Eliminando DNS manuales…"):
                api.network.reset_dns_auto(profile)
            success("DNS manuales eliminados.")
    elif op != "0":
        warning("Opción no válida.")


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
                ("5", "Diagnosticar WiFi"),
                ("6", "Reparar WiFi (si scan vacío / unavailable)"),
                ("", ""),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                with working("Escaneando redes Wi-Fi…"):
                    networks = api.network.list_wifi()
                if networks:
                    table_wifi(networks)
                else:
                    warning("No se encontraron redes.")
                    with working("Diagnosticando Wi-Fi…"):
                        diag = api.diagnose_wifi()
                    if not diag.healthy:
                        warning(diag.detail)
                        if confirm("¿Intentar reparar WiFi ahora?", default=True):
                            with working("Reparando Wi-Fi (puede reiniciar NetworkManager)…"):
                                result = api.repair_wifi(force_nm_restart=True)
                            success(result)
            elif op == "2":
                ssid = ask("SSID")
                pwd = ask_password("Contraseña Wi-Fi")
                with working(f"Conectando a {ssid}…"):
                    api.network.connect_wifi(ssid, pwd)
                success(f"Conectado a [bold]{ssid}[/bold].")
            elif op == "3":
                with working("Buscando interfaces Wi-Fi…"):
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
                with working(f"Desconectando {device}…"):
                    api.network.disconnect_device(device)
                success("Wi-Fi desconectado.")
            elif op == "4":
                _menu_ip_profile(api, "wifi")
            elif op == "5":
                with working("Diagnosticando Wi-Fi…"):
                    diag = api.diagnose_wifi()
                if diag.healthy:
                    success(f"{diag.detail} (radio={diag.radio})")
                else:
                    warning(f"{diag.detail} (radio={diag.radio})")
                if diag.devices_raw:
                    info(diag.devices_raw)
            elif op == "6":
                if confirm("¿Reparar WiFi (puede reiniciar NetworkManager)?", default=True):
                    with working("Reparando Wi-Fi (puede reiniciar NetworkManager)…"):
                        result = api.repair_wifi(force_nm_restart=True)
                    success(result)
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))
