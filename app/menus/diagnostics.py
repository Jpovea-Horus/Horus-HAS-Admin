"""Diagnóstico: procesos, salud, semáforo, logs, pruebas de red e informe de soporte."""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError
from models import LogSource
from ui import (
    ask,
    ask_int,
    clear_screen,
    confirm,
    console,
    error,
    info,
    log_line,
    menu_options,
    panel_health_check,
    panel_network_tests,
    panel_process_snapshot,
    panel_system_health,
    panel_traceroute,
    section,
    success,
    table_network_tests,
    warning,
    working,
)


def menu_review_diagnostics(api: HasControllerAPI) -> None:
    while True:
        section("Diagnóstico y revisión")
        menu_options(
            "Diagnóstico",
            [
                ("1", "HTOP / monitor de procesos"),
                ("2", "Salud del sistema (disco, memoria, unidades fallidas)"),
                ("3", "Semáforo de estado (HA, Z-Wave, disco, reloj, túnel, systemd)"),
                ("4", "Visor de logs (HA, Z-Wave, plugin_service, cloudflared…)"),
                ("5", "Pruebas de red (DNS, traceroute, puertos, Cloudflare / ZeroTier)"),
                ("6", "Generar informe de soporte (ZIP + HTML en este PC)"),
                ("", ""),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                _menu_process_monitor(api)
            elif op == "2":
                panel_system_health(api.get_system_health())
                ask("Pulse Enter para volver al menú")
            elif op == "3":
                with working("Revisando estado del controlador…"):
                    status = api.get_health_check()
                console.print(panel_health_check(status))
                ask("Pulse Enter para volver al menú")
            elif op == "4":
                menu_log_viewer(api)
            elif op == "5":
                menu_network_tests(api)
            elif op == "6":
                support_report_flow(api)
            else:
                warning("Opción no válida.")
        except NotConnectedError:
            error("Sesión perdida.")
            raise
        except HasApiError as exc:
            error(str(exc))


def _menu_process_monitor(api: HasControllerAPI) -> None:
    menu_options(
        "HTOP",
        [
            ("1", "Abrir htop/top interactivo (salir con q)"),
            ("2", "Snapshot de procesos (top)"),
            ("", ""),
            ("0", "Volver"),
        ],
    )
    sub = ask("Opción")
    if sub == "1":
        info("Conectando monitor interactivo… (pulse 'q' para salir)")
        clear_screen()
        api.run_process_monitor()
        ask("Pulse Enter para volver al menú")
    elif sub == "2":
        panel_process_snapshot(api.get_top_snapshot())
        ask("Pulse Enter para volver al menú")
    elif sub != "0":
        warning("Opción no válida.")


# --- visor de logs ---


def _quit_key_pressed() -> bool:
    if os.name != "nt":
        return False
    import msvcrt

    while msvcrt.kbhit():
        if msvcrt.getwch() in ("q", "Q", "\x1b"):
            return True
    return False


def _pick_source(sources: list[LogSource]) -> LogSource | None:
    opts = [
        (str(i), s.label if s.available else f"{s.label} [dim]({s.detail})[/dim]")
        for i, s in enumerate(sources, 1)
    ]
    opts += [("", ""), ("0", "Volver")]
    menu_options("Origen del log", opts)
    idx = ask_int("Opción")
    if not idx:
        return None
    if idx < 1 or idx > len(sources):
        warning("Selección inválida.")
        return None
    source = sources[idx - 1]
    if not source.available:
        warning(f"{source.label}: {source.detail}.")
        return None
    return source


def menu_log_viewer(api: HasControllerAPI) -> None:
    with working("Detectando servicios…"):
        sources = api.get_log_sources()
    while True:
        section("Visor de logs")
        source = _pick_source(sources)
        if source is None:
            return

        level = "all"
        if source.level_in_text:
            menu_options(
                "Filtro de nivel",
                [("1", "Solo ERROR y WARNING"), ("2", "Solo ERROR"), ("3", "Todo")],
            )
            level = {"1": "warn", "2": "error", "3": "all"}.get(ask("Opción", default="1"), "warn")
        text = ask("Texto adicional a buscar (Enter = ninguno)", default="")

        menu_options(
            "Modo",
            [("1", "Últimas líneas"), ("2", "Seguir en vivo (salir con q o Ctrl+C)")],
        )
        if ask("Opción", default="1") == "2":
            _follow(api, source, level, text)
        else:
            lines = ask_int("Cantidad de líneas a mostrar", default="200") or 200
            with working(f"Leyendo log de {source.label}…"):
                hits = api.read_log(source, lines=lines, min_level=level, text=text)
            section(f"{source.label} — {len(hits)} línea(s)")
            if not hits:
                info("No hay líneas que coincidan con el filtro.")
            for line, lvl in hits:
                log_line(line, lvl)
        ask("Pulse Enter para continuar")


def _follow(api: HasControllerAPI, source: LogSource, level: str, text: str) -> None:
    section(f"{source.label} — en vivo")
    info("Pulse q o Ctrl+C para detener.")
    shown = 0
    stream = api.follow_log(source, min_level=level, text=text, should_stop=_quit_key_pressed)
    try:
        for line, lvl in stream:
            log_line(line, lvl)
            shown += 1
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
    info(f"Seguimiento detenido ({shown} línea(s) mostradas).")


# --- pruebas de red ---


def menu_network_tests(api: HasControllerAPI) -> None:
    while True:
        section("Pruebas de red (ejecutadas desde el controlador)")
        menu_options(
            "Pruebas de red",
            [
                ("1", "Prueba completa (gateway, Internet, DNS, Cloudflare, ZeroTier, puertos locales)"),
                ("2", "Traceroute"),
                ("3", "Comprobar un puerto (host:puerto)"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            return
        try:
            if op == "1":
                with working("Ejecutando pruebas de red (puede tardar ~30 s)…"):
                    report = api.run_network_tests()
                panel_network_tests(report)
            elif op == "2":
                host = ask("Destino", default="8.8.8.8")
                with working(f"Traceroute a {host}…"):
                    out = api.traceroute(host)
                panel_traceroute(out, host)
            elif op == "3":
                raw = ask("Host:puerto", default="127.0.0.1:8123")
                host, _, port_txt = raw.rpartition(":")
                if not host or not port_txt.isdigit():
                    warning("Formato esperado: host:puerto (ej. 192.168.1.10:8123).")
                    continue
                with working(f"Probando {raw}…"):
                    item = api.check_port(host, int(port_txt))
                table_network_tests([item], title="Comprobación de puerto")
            else:
                warning("Opción no válida.")
                continue
        except NotConnectedError:
            raise
        except HasApiError as exc:
            error(str(exc))
        ask("Pulse Enter para continuar")


# --- informe de soporte ---


def support_report_flow(api: HasControllerAPI) -> None:
    section("Informe de soporte")
    info("Recolecta estado, pruebas de red, versiones y logs. No modifica el controlador.")
    with working("Preparando…") as status:
        result = api.create_support_report(
            on_step=lambda msg: status.update(f"[info]Recolectando: {msg}…[/info]")
        )
    success("Informe generado.")
    info(f"ZIP:  {result.zip_path}")
    info(f"HTML: {result.html_path}")
    if result.warnings:
        warning(f"{len(result.warnings)} apartado(s) no se pudieron recolectar (ver el informe).")
    if confirm("¿Abrir el resumen HTML ahora?", default=True):
        webbrowser.open(Path(result.html_path).as_uri())
    ask("Pulse Enter para continuar")
