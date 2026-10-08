#!/usr/bin/env python3
"""Menú consola — Gestor Nexxo 800."""

from __future__ import annotations

import os
import sys


def _setup_working_dir() -> None:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)


_setup_working_dir()

if os.name == "nt":
    os.system("")

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError
from menus.backup import menu_backups
from menus.connect import clear_connect_memory, connect_flow, reconnect_or_prompt
from menus.diagnostics import menu_review_diagnostics, support_report_flow
from menus.ha import menu_ha_admin, menu_hostname
from menus.network import menu_ethernet, menu_network_status, menu_wifi
from menus.remote import menu_error_correction, menu_remote_connection
from paths import APP_VERSION
from updater import RELEASES_URL, UpdateError, apply_update, check_latest, is_frozen
from ui import (
    ask,
    banner,
    confirm,
    error,
    get_menu_panel,
    info,
    main_menu_layout,
    panel_health_check,
    panel_system_info,
    section,
    success,
    warning,
    working,
)


def update_flow(api: HasControllerAPI | None = None, silent: bool = False) -> None:
    """Busca una versión nueva en GitHub y ofrece instalarla."""
    if not silent:
        info("Consultando GitHub…")
    try:
        release = check_latest(timeout=5 if silent else 10)
    except UpdateError as exc:
        if not silent:
            error(str(exc))
        return

    if not release:
        if not silent:
            success(f"Ya tiene la última versión ({APP_VERSION}).")
        return

    warning(f"Nueva versión disponible: {release.version} (actual {APP_VERSION})")
    if release.notes:
        info(release.notes.splitlines()[0][:120])
    if not is_frozen():
        info(f"Modo desarrollo: actualice con 'git pull' o descargue desde {RELEASES_URL}")
        return
    if not confirm("¿Descargar e instalar ahora? La app se reiniciará.", default=True):
        return

    info("Descargando actualización…")
    try:
        if api:
            api.disconnect()
        apply_update(release)
    except UpdateError as exc:
        error(str(exc))


def _health_panel(api: HasControllerAPI):
    try:
        with working("Revisando estado del controlador…"):
            return panel_health_check(api.get_health_check())
    except NotConnectedError:
        raise
    except HasApiError as exc:
        warning(f"No se pudo calcular el semáforo: {exc}")
        return None


def main_menu(api: HasControllerAPI) -> str:
    """Bucle del menú. Devuelve: exit | next | lost."""
    system_info = api.get_system_info()
    try:
        health_panel = _health_panel(api)
    except NotConnectedError:
        return "lost"

    while True:
        banner()
        menu_panel = get_menu_panel(
            "Gestor Nexxo 800",
            [
                ("", "[dim]Red[/dim]"),
                ("1", "Ver estado de red"),
                ("2", "Configurar Ethernet"),
                ("3", "Configurar Wi-Fi"),
                ("4", "Conexión remota (ZeroTier / Cloudflare)"),
                ("", ""),
                ("", "[dim]Sistema[/dim]"),
                ("5", "Home Assistant (usuarios, configuración, integraciones)"),
                ("6", "Backups y espacio en disco"),
                ("7", "Hostname del controlador"),
                ("", ""),
                ("", "[dim]Soporte[/dim]"),
                ("8", "Diagnóstico (semáforo, logs, pruebas de red)"),
                ("9", "Modo Corrección de errores"),
                ("I", "Generar informe de soporte (ZIP + HTML)"),
                ("", ""),
                ("", "[dim]Opciones App[/dim]"),
                ("P", "Próximo (Cambiar controlador)"),
                ("R", "Actualizar info sistema y semáforo"),
                ("U", "Buscar actualizaciones de la app"),
                ("0", "Salir"),
            ],
        )
        sys_panel = panel_system_info(system_info)
        main_menu_layout(menu_panel, sys_panel, health_panel)

        op = ask("Opción").upper()
        try:
            if op == "0":
                return "exit"
            if op == "P":
                return "next"
            if op == "1":
                menu_network_status(api)
                ask("Pulse Enter para volver al menú")
            elif op == "2":
                menu_ethernet(api)
            elif op == "3":
                menu_wifi(api)
            elif op == "4":
                menu_remote_connection(api)
            elif op == "5":
                menu_ha_admin(api)
            elif op == "6":
                menu_backups(api)
            elif op == "7":
                menu_hostname(api)
                ask("Pulse Enter para volver al menú")
            elif op == "8":
                menu_review_diagnostics(api)
            elif op == "9":
                menu_error_correction(api)
            elif op == "I":
                support_report_flow(api)
            elif op == "R":
                info("Actualizando información del sistema…")
                system_info = api.refresh_system_info()
                health_panel = _health_panel(api)
                success("Información actualizada.")
            elif op == "U":
                update_flow(api)
                if not api.connected:
                    return "lost"
                ask("Pulse Enter para continuar")
            else:
                warning("Opción no válida.")
        except NotConnectedError:
            error("Sesión perdida.")
            return "lost"
        except HasApiError as exc:
            error(str(exc))
            ask("Pulse Enter para continuar")


def main() -> None:
    banner()
    update_flow(silent=True)
    api = HasControllerAPI()
    try:
        while True:
            if not api.connected:
                if not connect_flow(api):
                    if confirm("¿Reintentar conexión?", default=True):
                        continue
                    break
            result = main_menu(api)
            if result == "exit":
                break
            if result == "next":
                api.disconnect()
                clear_connect_memory()
                banner()
                if not connect_flow(api):
                    if confirm("¿Reintentar conexión?", default=True):
                        continue
                    break
                continue
            if result == "lost":
                api.disconnect()
                warning("La sesión SSH se cortó (p. ej. cambio de IP).")
                if not reconnect_or_prompt(api):
                    if confirm("¿Reintentar conexión?", default=True):
                        continue
                    break
    finally:
        api.disconnect()
        section("Fin de sesión")
        info("Desconectado.")


if __name__ == "__main__":
    main()
