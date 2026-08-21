"""Interfaz de consola con colores y formato (Rich)."""

from __future__ import annotations

import getpass
import re
import sys

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.theme import Theme
from rich.text import Text

from rich.columns import Columns
from rich.layout import Layout
from rich.live import Live

from models import (
    BackupManagerStatus,
    CellularStatus,
    HaConfigurationStatus,
    HaUser,
    HaUsersStatus,
    MaintenanceStatus,
    MqttDiagnosticStatus,
    PluginServiceStatus,
    SystemHealthStatus,
    WifiNetwork,
    NetworkDevice,
    ConnectionProfile,
    CloudflareStatus,
)
from paths import APP_NAME, APP_VERSION

HAS_THEME = Theme(
    {
        "title": "bold bright_white on blue",
        "subtitle": "bold bright_cyan",
        "info": "bright_blue",
        "success": "bold bright_green",
        "warning": "bold bright_yellow",
        "error": "bold bright_red",
        "menu": "bright_white",
        "dim": "dim white",
        "accent": "bold bright_magenta",
        "connected": "bold green",
        "ethernet": "bright_blue",
        "wifi": "bright_magenta",
        "label": "bold cyan",
        "value": "bold bright_green",
    }
)

console = Console(theme=HAS_THEME)


def clear_screen() -> None:
    console.clear()


def banner() -> None:
    console.clear()
    console.print()
    console.print(
        Panel(
            Text.from_markup(
                f"[title]      {APP_NAME}      [/title]\n"
                "[subtitle]Instalación y configuración integral[/subtitle]\n"
                f"[dim]v{APP_VERSION} · Red · HA · ZeroTier · Diagnóstico[/dim]"
            ),
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 10),
            expand=False,
        ),
        justify="center"
    )
    console.print()


def section(title: str) -> None:
    console.print()
    console.rule(f"[subtitle] {title} [/subtitle]", style="bright_blue")


def _line(style: str, icon: str, msg: str) -> None: 
    console.print(Text.from_markup(f"[{style}]{icon}[/{style}] {msg}"))


def success(msg: str) -> None:
    _line("success", "✓", msg)


def error(msg: str) -> None:
    _line("error", "✗", msg)


def warning(msg: str) -> None:
    _line("warning", "!", msg)


def info(msg: str) -> None:
    _line("info", "›", msg)


def panel_hostname(static: str, pretty: str) -> None:
    body = f"[info]Actual:[/info] [bold]{static}[/bold]"
    if pretty:
        body += f"\n[info]Pretty:[/info] {pretty}"
    console.print(Panel(body, title="Hostname del controlador", border_style="cyan", box=box.ROUNDED))


def panel_ha_users(status: HaUsersStatus) -> None:
    """Panel de usuarios Home Assistant detectados."""
    meta = Text()
    meta.append("HOME ASSISTANT\n", style="bold underline")
    meta.append("------------------------\n")
    if status.container_name:
        meta.append(f"Contenedor: {status.container_name}\n", style="dim")
    if status.version:
        meta.append(f"Versión: {status.version}\n", style="dim")
    if status.config_path:
        meta.append(f"Config: {status.config_path}\n", style="dim")
    if status.error:
        meta.append(f"\n{status.error}\n", style="bold red")
        console.print(Panel(meta, border_style="red", box=box.ROUNDED, padding=(1, 2)))
        return

    console.print(Panel(meta, border_style="cyan", box=box.ROUNDED, padding=(0, 2)))
    table_ha_users(status.users)


def table_ha_users(users: list[HaUser]) -> None:
    t = Table(
        show_header=True,
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold cyan",
    )
    t.add_column("#", style="dim", width=3)
    t.add_column("Usuario", style="bold")
    t.add_column("Nombre")
    t.add_column("ID")
    t.add_column("Rol")
    t.add_column("Estado")
    if not users:
        t.add_row("-", "(ninguno)", "", "", "", "")
    else:
        for i, u in enumerate(users, 1):
            if u.is_owner:
                role = "Owner"
            elif u.is_admin:
                role = "Admin"
            else:
                role = "Usuario"
            if u.incomplete or not u.user_id:
                estado = "INCOMPLETO"
                id_txt = "—"
            else:
                estado = "OK" if u.is_active else "Inactivo"
                id_txt = u.user_id[:8] + "…" if len(u.user_id) > 10 else u.user_id
            t.add_row(
                str(i),
                u.username or "(sin login)",
                u.name or "—",
                id_txt,
                role,
                estado,
            )
    console.print(t)


def ask(prompt: str, default: str = "") -> str:
    try:
        return Prompt.ask(f"[accent]{prompt}[/accent]", default=default).strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        warning("Operación cancelada.")
        sys.exit(0)


def ask_password(prompt: str = "Contraseña") -> str:
    console.print(f"[accent]{prompt}[/accent] [dim](no se muestra en pantalla)[/dim]")
    return getpass.getpass("  › ")


def ask_int(prompt: str, default: str = "") -> int | None:
    raw = ask(prompt, default=default)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        warning("Indique un número válido.")
        return None


def confirm(message: str, default: bool = False) -> bool:
    try:
        return Confirm.ask(f"[warning]{message}[/warning]", default=default)
    except (EOFError, KeyboardInterrupt):
        return False


def menu_options(title: str, options: list[tuple[str, str]]) -> None:
    """options: lista de (clave, etiqueta)."""
    table = Table(
        show_header=False,
        box=box.ROUNDED,
        border_style="bright_blue",
        padding=(0, 1),
    )
    table.add_column("Op", style="bold bright_cyan", width=4)
    table.add_column("Descripción", style="menu")
    for key, label in options:
        table.add_row(key, label)
    console.print(Panel(table, title=f"[subtitle]{title}[/subtitle]", border_style="cyan"))


def table_devices(devices: list, gateway: str) -> None:
    t = Table(
        title="Interfaces de red",
        box=box.ROUNDED,
        header_style="bold bright_blue",
        border_style="blue",
        title_style="subtitle",
    )
    t.add_column("Tipo", style="dim")
    t.add_column("Interfaz")
    t.add_column("MAC", style="dim")
    t.add_column("Estado")
    t.add_column("Perfil")
    for d in devices:
        tipo_style = "ethernet" if d.device_type == "ethernet" else "wifi"
        mac = getattr(d, "mac", "") or ""
        show_mac = mac if d.device_type in ("ethernet", "wifi") else ""
        t.add_row(
            f"[{tipo_style}]{d.device_type}[/{tipo_style}]",
            d.device,
            show_mac or "[dim]—[/dim]",
            d.state,
            d.connection or "[dim](sin perfil)[/dim]",
        )
    console.print(t)
    console.print(
        f"  [info]Gateway:[/info] [bold]{gateway or '(ninguno)'}[/bold]"
    )


def table_wifi(networks: list) -> None:
    t = Table(
        title="Redes Wi-Fi disponibles",
        box=box.ROUNDED,
        header_style="bold magenta",
        border_style="magenta",
    )
    t.add_column("", width=3)
    t.add_column("SSID", style="bold")
    t.add_column("Señal", justify="right")
    t.add_column("Seguridad", style="dim")
    for n in networks:
        mark = "[success]●[/success]" if n.in_use else ""
        t.add_row(mark, n.ssid, f"{n.signal}%", n.security)
    console.print(t)


def main_menu_layout(menu_panel: Panel, system_panel: Panel) -> None:
    """Muestra la info del sistema y el menú en dos columnas (Info primero)."""
    columns = Columns([system_panel, menu_panel], expand=True, equal=True)
    console.print(columns)


def panel_profile(profile: str, prof) -> None:
    method = prof.ipv4_method or "desconocido"
    method_style = "success" if method == "auto" else "warning"
    console.print(
        Panel(
            f"[info]Perfil:[/info] [bold]{profile}[/bold]\n"
            f"[info]IPv4:[/info] [{method_style}]{method}[/{method_style}]\n"
            f"[info]IP:[/info] {prof.ipv4_addresses or '-'}\n"
            f"[info]Gateway:[/info] {prof.ipv4_gateway or '-'}\n"
            f"[info]DNS:[/info] {prof.ipv4_dns or '-'}",
            title="Perfil de conexión",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def panel_ip_output(raw: str) -> None:
    text = Text(raw)
    
    # Colorear interfaces (ej: 3: eth0:)
    text.highlight_regex(r"^\s*\d+: [^:]+:", "bold yellow")
    
    # Colorear IPs (ej: 10.0.5.116/24)
    text.highlight_regex(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}", "bold green")
    
    # Colorear estados
    text.highlight_regex(r"state UP", "bold green")
    text.highlight_regex(r"state DOWN", "bold red")
    text.highlight_regex(r"state UNKNOWN", "dim yellow")
    
    # Colorear flags entre <>
    text.highlight_regex(r"<[^>]+>", "cyan")

    console.print(
        Panel(
            text, 
            title="[subtitle]Direcciones IPv4 (Detalle)[/subtitle]", 
            border_style="blue", 
            box=box.ROUNDED,
            padding=(1, 2)
        )
    )


def panel_process_snapshot(output: str) -> None:
    console.print(
        Panel(
            output or "[dim]Sin datos[/dim]",
            title="[subtitle]Procesos (snapshot)[/subtitle]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def panel_ping(output: str) -> None:
    console.print(
        Panel(output, title="Resultado ping", border_style="green", box=box.ROUNDED)
    )


_MOTD_FIELDS = [
    ("System information as of", "Fecha y hora"),
    ("System load:", "Carga del sistema"),
    ("Up time:", "Tiempo activo"),
    ("Memory usage:", "Memoria"),
    ("IP:", "Dirección IP"),
    ("CPU temp:", "Temp. CPU"),
    ("GPU temp:", "Temp. GPU"),
    ("Usage of /:", "Disco /"),
    ("Local users:", "Usuarios"),
]

_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def _clean_motd_text(raw: str) -> str:
    clean = re.sub(r"\x1b\[[0-9;]*[mK]", "", raw)
    clean = re.sub(r"(\d+;)*\d+m", "", clean)
    return clean


def _format_motd_datetime(value: str) -> str:
    """Wed May 20 14:25:46 -05 2026 → 20/05/2026  14:25:46  (UTC-5)"""
    match = re.search(
        r"\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{1,2}:\d{2}:\d{2})\s+([+-]\d{2})\s+(\d{4})",
        value,
    )
    if not match:
        return value.strip()
    mon, day, time_part, tz, year = match.groups()
    month = _MONTHS.get(mon, mon)
    tz_label = f"UTC{tz}" if tz.startswith(("+", "-")) else tz
    return f"{day.zfill(2)}/{month}/{year}  {time_part}  ({tz_label})"


def _parse_motd(raw_info: str) -> list[tuple[str, str]]:
    """Convierte el MOTD en filas (etiqueta, valor) ordenadas."""
    clean = _clean_motd_text(raw_info)
    extra_ips: list[str] = []
    text_lines: list[str] = []

    for line in clean.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", stripped):
            extra_ips.append(stripped)
        else:
            text_lines.append(stripped)

    blob = " ".join(text_lines)
    label_keys = [key for key, _ in _MOTD_FIELDS]
    pattern = "|".join(re.escape(k) for k in label_keys)
    parts = re.split(f"({pattern})", blob)

    display_by_key = {key: label for key, label in _MOTD_FIELDS}
    order = [label for _, label in _MOTD_FIELDS]
    values: dict[str, str] = {}

    idx = 1
    while idx < len(parts):
        key = parts[idx].strip()
        value = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
        if key in display_by_key:
            label = display_by_key[key]
            if label in values and value:
                values[label] = f"{values[label]}\n{value}"
            elif value:
                values[label] = value
        idx += 2

    for ip in extra_ips:
        if "Dirección IP" in values:
            values["Dirección IP"] += f"\n{ip}"
        else:
            values["Dirección IP"] = ip

    if "Fecha y hora" in values:
        values["Fecha y hora"] = _format_motd_datetime(values["Fecha y hora"])

    return [(label, values[label]) for label in order if label in values]


def panel_system_info(raw_info: str) -> Panel:
    """Muestra el MOTD filtrado en tabla Campo / Valor."""
    rows = _parse_motd(raw_info)

    table = Table(
        show_header=True,
        header_style="bold bright_green",
        border_style="green",
        box=box.SIMPLE_HEAD,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Campo", style="label", width=18, no_wrap=True)
    table.add_column("Valor", style="value")

    if rows:
        for label, value in rows:
            for line in value.split("\n"):
                table.add_row(label, line)
                label = ""  # IPs adicionales sin repetir etiqueta
    else:
        table.add_row("[dim]Sin datos[/dim]", "[dim]—[/dim]")

    return Panel(
        table,
        title="[subtitle]Información del Sistema[/subtitle]",
        border_style="bright_green",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=True,
    )


def get_menu_panel(title: str, options: list[tuple[str, str]]) -> Panel:
    """Retorna el menú como un objeto Panel."""
    table = Table(
        show_header=False,
        box=box.SIMPLE,
        border_style="bright_blue",
        padding=(0, 1),
        expand=True
    )
    table.add_column("Op", style="bold bright_cyan", width=4)
    table.add_column("Descripción", style="menu")
    for key, label in options:
        table.add_row(key, label)
    return Panel(table, title=f"[subtitle]{title}[/subtitle]", border_style="bright_blue", box=box.ROUNDED, expand=True)


def panel_zerotier(status: any) -> None:
    if status.installed:
        body = (
            "[success]ZeroTier: INSTALADO[/success]\n"
            f"[info]Versión:[/info] {status.version or '-'}\n"
            f"[info]Servicio:[/info] "
            f"{'[success]activo[/success]' if status.service_active else '[warning]inactivo[/warning]'}"
        )
        style = "green"
        console.print(Panel(body, border_style=style, box=box.ROUNDED))
        
        if status.networks:
            t = Table(
                title="Redes ZeroTier",
                box=box.ROUNDED,
                header_style="bold green",
                border_style="green",
            )
            t.add_column("ID Red", style="bold")
            t.add_column("Nombre")
            t.add_column("Estado")
            t.add_column("Interfaz")
            t.add_column("IP")
            for n in status.networks:
                t.add_row(n.nwid, n.name, n.status, n.dev, n.ip)
            console.print(t)
        else:
            info("No se encontraron redes ZeroTier activas.")
    else:
        body = "[dim]ZeroTier: NO instalado[/dim]"
        style = "dim"
        console.print(Panel(body, border_style=style, box=box.ROUNDED))


def panel_cellular(status: CellularStatus) -> None:
    """Muestra el panel de estado del módulo celular."""
    # Determinar color basado en coherencia (servicio activo vs hardware presente)
    if status.is_active and not status.has_modem:
        style = "bold red"
        border = "red"
    elif status.is_active:
        style = "success"
        border = "green"
    else:
        style = "dim"
        border = "yellow"

    body = Text()
    body.append("MÓDULO CELULAR / LTE\n", style="bold underline")
    body.append("-----------------------\n")
    
    body.append("En ejecución: ", style="info")
    body.append("SÍ\n" if status.is_active else "NO\n", style=style)

    body.append("Arranque automático: ", style="info")
    boot_style = "warning" if status.is_enabled else "dim"
    body.append("SÍ\n" if status.is_enabled else "NO\n", style=boot_style)

    body.append("Módem detectado: ", style="info")
    body.append("SÍ\n" if status.has_modem else "NO\n", style="success" if status.has_modem else "warning")

    if status.modem_devices:
        body.append(f"Dispositivos: {', '.join(status.modem_devices)}\n", style="dim")

    if status.is_active or status.is_enabled:
        if not status.has_modem:
            body.append(
                "\n[bold red]⚠️ ALERTA:[/bold red] Sin módem 4G: cellular.sh puede consumir CPU en bucle.\n"
                "Use la opción [bold]Dar de baja[/bold] (stop + disable)."
            )
        elif status.is_active and status.has_modem:
            body.append(
                "\n[dim]Hay módem detectado. Solo dé de baja el servicio si no usa LTE en este equipo.[/dim]"
            )

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))


def panel_cloudflare(status: CloudflareStatus) -> None:
    """Panel de estado de cloudflared en el controlador."""
    if status.installed and status.ha_proxy_ok:
        style = "green"
    elif status.installed:
        style = "yellow"
    else:
        style = "dim"

    if status.installed:
        body = (
            "[success]Cloudflared: INSTALADO[/success]\n"
            f"[info]Versión:[/info] {status.version or '-'}\n"
        )
        if status.running_tunnels:
            body += "\n[info]Túneles/Procesos activos:[/info]\n"
            for t in status.running_tunnels:
                body += f"  [dim]• {t[:80]}[/dim]\n"
    else:
        body = "[dim]Cloudflared: NO instalado en el controlador[/dim]\n"

    body += "\n[info]HA trusted_proxies:[/info] "
    if status.ha_proxy_ok:
        body += "[success]OK[/success]\n"
    else:
        body += "[warning]FALTA (error 400 al entrar a HA por túnel)[/warning]\n"
    if status.ha_proxy_detail:
        body += f"[dim]{status.ha_proxy_detail}[/dim]\n"

    console.print(Panel(body, title="Cloudflare Tunnel (Remoto)", border_style=style, box=box.ROUNDED))


_ACTION_LABELS = {
    "none_already_disabled": ("OK", "green", "MQTT ya deshabilitado"),
    "none_no_errors": ("OK", "green", "Sin errores MQTT en log de hoy"),
    "disable_mqtt": ("ACCIÓN", "yellow", "Se recomienda deshabilitar MQTT"),
    "audit_auth": ("REVISAR", "bright_yellow", "Broker activo: auditar auth, no apagar"),
    "verify_ha_first": ("PRECAUCIÓN", "yellow", "Confirme zwave_js en HA antes de deshabilitar"),
    "restart_required": ("REINICIAR", "bright_yellow", "MQTT deshabilitado pero persisten errores"),
    "store_not_found": ("ERROR", "red", "Ruta zwave-js-ui-store no encontrada"),
    "review_manual": ("REVISAR", "dim", "Revisión manual requerida"),
}


def panel_mqtt_diagnostic(status: MqttDiagnosticStatus) -> None:
    """Panel de diagnóstico MQTT Z-Wave JS UI."""
    label, border, title = _ACTION_LABELS.get(
        status.recommended_action, ("?", "dim", "Estado desconocido")
    )

    body = Text()
    body.append("CONEXIÓN MQTT — Z-Wave JS UI\n", style="bold underline")
    body.append("----------------------------\n")

    if status.store_path:
        body.append(f"Ruta: {status.store_path}\n", style="dim")
        body.append(f"Servicio: {status.service_name}\n", style="dim")
    else:
        body.append("Ruta: ", style="info")
        body.append("no detectada\n", style="bold red")

    if status.settings_found:
        disabled_txt = (
            "SÍ" if status.mqtt_disabled else "NO" if status.mqtt_disabled is False else "?"
        )
        body.append("mqtt.disabled: ", style="info")
        body.append(f"{disabled_txt}\n")
        if status.mqtt_host:
            body.append(f"mqtt.host: {status.mqtt_host}:{status.mqtt_port or 1883}\n", style="dim")

    body.append("Errores en log hoy: ", style="info")
    err_style = "bold red" if status.has_mqtt_log_errors else "success"
    body.append("SÍ\n" if status.has_mqtt_log_errors else "NO\n", style=err_style)

    body.append("zwave-ui.service: ", style="info")
    body.append(
        "activo\n" if status.zwave_ui_service_active else "inactivo\n",
        style="success" if status.zwave_ui_service_active else "warning",
    )
    body.append(
        f"Puertos — 3000:{'✓' if status.port_3000_open else '✗'}  "
        f"8091:{'✓' if status.port_8091_open else '✗'}  "
        f"1883:{'✓' if status.port_1883_open else '✗'}\n",
        style="dim",
    )
    if status.broker_probe:
        body.append(f"Probe localhost:1883 → {status.broker_probe}\n", style="dim")

    if status.ha_container_found:
        body.append(f"HA Z-Wave URL: {status.ha_zwave_ws_url or '(no detectada)'}\n", style="dim")
        if status.ha_mqtt_integration:
            body.append(f"HA integración MQTT: {status.ha_mqtt_integration}\n", style="dim")

    body.append(f"\n[{label}] ", style=f"bold {border}")
    body.append(f"{title}\n", style=border)
    if status.action_detail:
        body.append(f"{status.action_detail}\n", style="dim")

    if status.mqtt_log_sample:
        body.append("\nÚltimas trazas MQTT:\n", style="info")
        for line in status.mqtt_log_sample:
            body.append(f"  {line[:100]}\n", style="dim")

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))


def panel_plugin_service(status: PluginServiceStatus) -> None:
    """Panel de verificación del custom component plugin_service."""
    if status.error:
        border = "red"
    elif status.plugin_exists:
        border = "green"
    else:
        border = "yellow"

    body = Text()
    body.append("CUSTOM COMPONENT — plugin_service\n", style="bold underline")
    body.append("--------------------------------\n")
    body.append(f"Ruta padre: {status.parent_dir}\n", style="dim")
    body.append(f"Ruta activa: {status.plugin_dir}\n", style="dim")
    body.append(
        "Nombres válidos: plugin_service* (plugin_service, v2, v0...)\n",
        style="dim",
    )

    body.append("custom_components/: ", style="info")
    body.append(
        "existe\n" if status.parent_exists else "NO existe\n",
        style="success" if status.parent_exists else "bold red",
    )

    body.append("plugin (service/_v2): ", style="info")
    body.append(
        "PRESENTE\n" if status.plugin_exists else "AUSENTE\n",
        style="success" if status.plugin_exists else "bold yellow",
    )
    if status.found_names:
        body.append("Carpetas halladas: ", style="info")
        body.append(", ".join(status.found_names) + "\n", style="success")

    if status.manifest_domain:
        ok_domain = status.manifest_domain.lower().startswith("plugin_service")
        body.append("manifest domain: ", style="info")
        body.append(
            f"{status.manifest_domain}\n",
            style="success" if ok_domain else "warning",
        )

    if status.error:
        body.append(f"\n{status.error}\n", style="bold red")

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))

    if status.components:
        t = Table(
            title="Contenido de custom_components/",
            box=box.ROUNDED,
            header_style="bold cyan",
            border_style="cyan",
        )
        t.add_column("#", style="dim", width=3)
        t.add_column("Carpeta / archivo")
        t.add_column("Tipo")
        for i, name in enumerate(status.components, 1):
            is_plugin = name.lower().startswith("plugin_service")
            kind = "plugin" if is_plugin else "otro"
            style = "bold green" if is_plugin else ""
            t.add_row(str(i), f"[{style}]{name}[/{style}]" if style else name, kind)
        console.print(t)

    if status.plugin_exists and status.plugin_entries:
        title = f"Contenido de {status.plugin_dir.split('/')[-1]}/"
        t2 = Table(
            title=title,
            box=box.ROUNDED,
            header_style="bold green",
            border_style="green",
        )
        t2.add_column("#", style="dim", width=3)
        t2.add_column("Entrada")
        for i, name in enumerate(status.plugin_entries, 1):
            t2.add_row(str(i), name)
        console.print(t2)


def panel_ha_configuration(status: HaConfigurationStatus) -> None:
    """Panel HTTP: YAML legado + .storage/http (trusted_proxies)."""
    if status.error:
        border = "red"
    elif status.proxy_ok and (not status.uses_storage_http or status.http_ok):
        border = "green"
    elif status.has_http_block or not status.proxy_ok:
        border = "yellow"
    else:
        border = "yellow"

    body = Text()
    body.append("HOME ASSISTANT — CONECTIVIDAD HTTP / PROXY\n", style="bold underline")
    body.append("------------------------------\n")
    if status.ha_version:
        body.append(f"HA: {status.ha_version}\n", style="dim")
    body.append(f"YAML: {status.path}\n", style="dim")
    if status.storage_path:
        body.append(f"Storage: {status.storage_path}\n", style="dim")

    def _flag(label: str, ok: bool, missing: str = "FALTA") -> None:
        body.append(f"{label}: ", style="info")
        body.append("OK\n" if ok else f"{missing}\n", style="success" if ok else "bold yellow")

    body.append("configuration.yaml: ", style="info")
    if not status.exists:
        body.append("no existe\n", style="dim")
    elif status.is_empty:
        body.append("vacío\n", style="bold yellow")
    else:
        body.append("presente\n", style="success")
    _flag("Bloque http legado", not status.has_http_block, "PRESENTE")

    body.append("\n")
    body.append(".storage/http: ", style="info")
    body.append("presente\n" if status.storage_exists else "no existe\n",
                style="success" if status.storage_exists else "bold yellow")
    _flag("use_x_forwarded_for (stable)", status.storage_use_x_forwarded_for)
    _flag("trusted_proxies 127.0.0.1/32", status.storage_has_proxy_ipv4)
    _flag("trusted_proxies ::1/128", status.storage_has_proxy_ipv6)
    _flag("pending=null (no revierte a 5 min)", not status.storage_pending, "HAY PENDING")
    _flag("yaml_migration_done", status.storage_yaml_migration_done)

    body.append("\nModo: ", style="info")
    body.append(
        "HAS 2026.8+ (.storage)\n" if status.uses_storage_http else "HAS legado (YAML)\n",
        style="success" if status.uses_storage_http else "dim",
    )
    body.append("Túnel HA (evita 400): ", style="info")
    body.append(
        "OK\n" if status.proxy_ok else "FALTA trusted_proxies\n",
        style="success" if status.proxy_ok else "bold yellow",
    )

    if status.error:
        body.append(f"\n{status.error}\n", style="bold red")

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))


def panel_system_health(status: SystemHealthStatus) -> None:
    border = "red" if status.has_failed or status.error else "cyan"
    body = Text()
    body.append("SALUD DEL SISTEMA\n", style="bold underline")
    body.append("-----------------\n")
    if status.uptime:
        body.append(f"Uptime: {status.uptime}\n", style="dim")
    if status.memory:
        body.append("Memoria:\n", style="info")
        body.append(f"{status.memory}\n", style="dim")
    if status.disk:
        body.append("Disco:\n", style="info")
        body.append(f"{status.disk}\n", style="dim")
    body.append("Unidades fallidas: ", style="info")
    if status.has_failed:
        body.append("SÍ\n", style="bold red")
        body.append(f"{status.failed_units}\n", style="warning")
    else:
        body.append("ninguna\n", style="success")
    if status.error:
        body.append(f"\n{status.error}\n", style="bold red")
    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))


def panel_backup_manager(status: BackupManagerStatus) -> None:
    """Panel de espacio en disco y listado de backups."""
    border = "red" if status.error or status.low_space else "cyan"
    body = Text()
    body.append("GESTIÓN DE BACKUPS\n", style="bold underline")
    body.append("------------------\n")

    body.append("Espacio libre (/): ", style="info")
    free_style = "bold red" if status.low_space else "success"
    body.append(
        f"{status.root_free or '-'}  (usado {status.root_used_pct or '-'})\n",
        style=free_style,
    )
    if status.low_space:
        body.append(
            "  ⚠ Poco espacio: se recomiendan ≥2.5 GB libres antes de actualizar HA.\n",
            style="bold yellow",
        )

    body.append(f"Config HA: {status.ha_config_path or '-'}\n", style="dim")
    body.append(f"Store Z-Wave: {status.zwave_store_path or '-'}\n", style="dim")

    if status.docker_summary:
        body.append("\nDocker system df:\n", style="info")
        for line in status.docker_summary.splitlines()[:6]:
            body.append(f"  {line}\n", style="dim")

    if status.error:
        body.append(f"\n{status.error}\n", style="bold red")

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))

    t = Table(
        title="Backups detectados",
        box=box.ROUNDED,
        header_style="bold cyan",
        border_style="cyan",
    )
    t.add_column("#", style="dim", width=3)
    t.add_column("Tipo", width=8)
    t.add_column("Fecha", width=12)
    t.add_column("Tamaño", justify="right", width=8)
    t.add_column("Ruta")

    if not status.backups:
        t.add_row("-", "-", "-", "-", "(ninguno)")
    else:
        for i, b in enumerate(status.backups, 1):
            kind = "HA" if b.kind == "ha" else "Z-Wave" if b.kind == "zwave" else b.kind
            t.add_row(str(i), kind, b.date_label or "-", b.size or "-", b.path)

    console.print(t)


def panel_maintenance(status: MaintenanceStatus) -> None:
    """Panel de mantenimiento preventivo y limpieza."""
    border = "yellow" if (status.nested_config_detected or status.ha_db_alert or status.old_archives) else "cyan"
    body = Text()
    body.append("MANTENIMIENTO Y LIMPIEZA\n", style="bold underline")
    body.append("------------------------\n")

    def _row(label: str, value: str, alert: bool = False) -> None:
        body.append(f"{label}: ", style="info")
        style = "bold red" if alert else "success"
        body.append(f"{value}\n", style=style)

    _row("Caché APT", status.apt_cache_size)
    _row("Caché NPM", status.npm_cache_size)
    _row("Logs Sistema", status.journal_size)

    db_alert = status.ha_db_alert
    _row("Base Datos HA", f"{status.ha_db_size_mb:.1f} MB", alert=db_alert)
    if db_alert:
        body.append("  ⚠ DB > 500MB: configure 'purge_keep_days' en HA.\n", style="warning")

    if status.nested_config_detected:
        body.append("\n[bold red]⚠ BASURA DETECTADA:[/bold red] Carpeta /config/config/ duplicada.\n", style="error")

    if status.old_archives:
        body.append(f"\n[bold yellow]⚠ ARCHIVOS ANTIGUOS:[/bold yellow] {len(status.old_archives)} backups .zip/.tar.gz (>30d).\n", style="warning")
        for arc in status.old_archives[:3]:
            body.append(f"  • {arc.split('/')[-1]}\n", style="dim")
        if len(status.old_archives) > 3:
            body.append(f"  ... y {len(status.old_archives)-3} más.\n", style="dim")

    if status.last_cleanup_summary:
        body.append(f"\n[info]Última acción:[/info] {status.last_cleanup_summary}\n", style="success")

    console.print(Panel(body, border_style=border, box=box.ROUNDED, padding=(1, 2)))
