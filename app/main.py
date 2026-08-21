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

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError
from menus.connect import connect_flow, reconnect_or_prompt
from menus.diagnostics import menu_review_diagnostics
from menus.ha import menu_administrative
from menus.network import menu_ethernet, menu_network_status, menu_wifi
from menus.remote import menu_error_correction, menu_remote_connection
from ui import (
    ask,
    banner,
    confirm,
    error,
    get_menu_panel,
    info,
    main_menu_layout,
    panel_system_info,
    section,
    success,
    warning,
)


def main_menu(api: HasControllerAPI) -> str:
    """Bucle del menú. Devuelve: exit | reconnect | lost."""
    system_info = api.get_system_info()

    while True:
        banner()
        menu_panel = get_menu_panel(
            "Gestor Nexxo 800",
            [
                ("1", "Ver estado de red"),
                ("2", "Configurar Ethernet"),
                ("3", "Configurar Wi-Fi"),
                ("4", "Configuración administrativa"),
                ("5", "Consultar Conexión Remota"),
                ("6", "Diagnóstico y revisión"),
                ("7", "Modo Corrección de errores"),
                ("C", "Cambiar / reconectar controlador"),
                ("R", "Actualizar info sistema"),
                ("0", "Salir"),
            ],
        )
        sys_panel = panel_system_info(system_info)
        main_menu_layout(menu_panel, sys_panel)

        op = ask("Opción").upper()
        try:
            if op == "0":
                return "exit"
            if op == "C":
                return "reconnect"
            if op == "1":
                menu_network_status(api)
                ask("Pulse Enter para volver al menú")
            elif op == "2":
                menu_ethernet(api)
            elif op == "3":
                menu_wifi(api)
            elif op == "4":
                menu_administrative(api)
            elif op == "5":
                menu_remote_connection(api)
            elif op == "6":
                menu_review_diagnostics(api)
            elif op == "7":
                menu_error_correction(api)
            elif op == "R":
                info("Actualizando información del sistema…")
                system_info = api.refresh_system_info()
                success("Información actualizada.")
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
            if result in ("reconnect", "lost"):
                api.disconnect()
                if result == "lost":
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
