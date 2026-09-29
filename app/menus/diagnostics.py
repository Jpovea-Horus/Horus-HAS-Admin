"""Diagnóstico: procesos y salud del sistema."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError
from ui import (
    ask,
    clear_screen,
    error,
    info,
    menu_options,
    panel_process_snapshot,
    panel_system_health,
    section,
    warning,
)


def menu_review_diagnostics(api: HasControllerAPI) -> None:
    while True:
        section("Diagnóstico y revisión")
        menu_options(
            "Diagnóstico",
            [
                ("1", "HTOP / monitor de procesos"),
                ("2", "Salud del sistema (disco, memoria, unidades fallidas)"),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                menu_options(
                    "HTOP",
                    [
                        ("1", "Abrir htop/top interactivo (salir con q)"),
                        ("2", "Snapshot de procesos (top)"),
                        ("0", "Volver"),
                    ],
                )
                sub = ask("Opción")
                if sub == "0":
                    continue
                if sub == "1":
                    info("Conectando monitor interactivo… (pulse 'q' para salir)")
                    clear_screen()
                    api.run_process_monitor()
                    ask("Pulse Enter para volver al menú")
                elif sub == "2":
                    panel_process_snapshot(api.get_top_snapshot())
                    ask("Pulse Enter para volver al menú")
                else:
                    warning("Opción no válida.")
            elif op == "2":
                panel_system_health(api.get_system_health())
                ask("Pulse Enter para volver al menú")
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))
        except NotConnectedError:
            error("Sesión perdida.")
            break
