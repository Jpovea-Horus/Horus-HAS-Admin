"""Menús de backups, espacio en disco y mantenimiento."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError, ValidationError
from menus.horus_backup import menu_horus_backup
from ui import (
    ask,
    ask_int,
    confirm,
    console,
    error,
    info,
    menu_options,
    panel_backup_manager,
    panel_maintenance,
    section,
    success,
    warning,
)


def menu_backups(api: HasControllerAPI) -> None:
    while True:
        section("Backups y espacio en disco")
        menu_options(
            "Backups y espacio",
            [
                ("1", "Backup remoto estándar HORUS (.tar.gz → este PC, restaurar, reset)"),
                ("2", "Backups locales en el controlador (copias de carpeta, avanzado)"),
                ("3", "Mantenimiento y limpieza del sistema (APT, NPM, logs, Docker)"),
                ("", ""),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_horus_backup(api)
        elif op == "2":
            menu_backup_manager(api)
        elif op == "3":
            menu_maintenance(api)
        else:
            warning("Opción no válida.")


def menu_maintenance(api: HasControllerAPI) -> None:
    summary = ""
    while True:
        section("Mantenimiento y Limpieza")
        try:
            status = api.get_maintenance_status()
            if summary:
                status.last_cleanup_summary = summary
        except HasApiError as exc:
            error(str(exc))
            break

        panel_maintenance(status)
        opts = [
            ("1", "Actualizar estado"),
            ("2", "Limpieza sistemática segura (APT, NPM, Logs, Docker prune)"),
            ("0", "Volver"),
        ]
        if status.nested_config_detected:
            opts.insert(2, ("3", "Eliminar carpeta anidada basura (/config/config/)"))
        if status.old_archives:
            opts.insert(len(opts) - 1, ("4", "Eliminar archivos .zip/.tar.gz antiguos (>30 días)"))
        if status.custom_components:
            opts.insert(len(opts) - 1, ("5", "Eliminar carpeta en custom_components (limpieza)"))

        menu_options("Acciones de Mantenimiento", opts)
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                summary = ""
                continue
            if op == "2":
                info("Ejecutando limpieza sistemática (esto puede tardar unos segundos)...")
                summary = api.safe_cleanup()
                success("Limpieza completada.")
            elif op == "3" and status.nested_config_detected:
                if confirm("¿Eliminar carpeta /home/cat/config/config/ ?", default=False):
                    summary = api.delete_nested_config()
                    success(summary)
            elif op == "4" and status.old_archives:
                if confirm(f"¿Eliminar {len(status.old_archives)} archivos antiguos?", default=False):
                    summary = api.delete_old_archives()
                    success(summary)
            elif op == "5" and status.custom_components:
                info("Carpetas en custom_components:")
                for cc in status.custom_components:
                    console.print(f"  • [bold]{cc}[/bold]")
                name = ask("Nombre de la carpeta a eliminar (vacío para cancelar)").strip()
                if not name:
                    info("Cancelado.")
                    continue
                if name not in status.custom_components:
                    error(f"'{name}' no es una carpeta válida en custom_components.")
                    continue
                if confirm(f"¿ELIMINAR PERMANENTEMENTE '{name}'?", default=False):
                    info(f"Eliminando '{name}'...")
                    summary = api.delete_custom_component(name)
                    success(summary)
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))
            summary = f"Error: {exc}"


def menu_backup_manager(api: HasControllerAPI) -> None:
    while True:
        section("Backups locales en el controlador")
        try:
            status = api.get_backup_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_backup_manager(status)
        menu_options(
            "Acciones",
            [
                ("1", "Actualizar listado / espacio"),
                ("2", "Crear backup HA + Z-Wave"),
                ("3", "Crear solo backup HA"),
                ("4", "Crear solo backup Z-Wave"),
                ("", ""),
                ("5", "Eliminar backup (por #)"),
                ("6", "Limpiar antiguos (mantener N más recientes)"),
                ("", ""),
                ("7", "Liberar espacio Docker (prune -a)"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                continue
            if op == "2":
                if status.low_space:
                    warning("Poco espacio libre; el backup puede fallar o llenar el disco.")
                if confirm("¿Crear backups HA + Z-Wave ahora?", default=True):
                    info("Creando backups…")
                    success(api.backup_before_update())
            elif op == "3":
                if confirm("¿Crear backup de config HA?", default=True):
                    success(api.backup_ha_config())
            elif op == "4":
                if confirm("¿Crear backup del store Z-Wave?", default=True):
                    success(api.backup_zwave_store())
            elif op == "5":
                if not status.backups:
                    info("No hay backups para eliminar.")
                    continue
                idx = ask_int("Número de backup a eliminar")
                if idx is None or idx < 1 or idx > len(status.backups):
                    warning("Número fuera de rango.")
                    continue
                target = status.backups[idx - 1]
                warning(f"Se eliminará: {target.path} ({target.size})")
                if confirm("¿Eliminar este backup de forma permanente?", default=False):
                    success(api.delete_backup(target.path))
            elif op == "6":
                keep = ask_int("¿Cuántos backups recientes conservar por tipo?", default="2")
                if keep is None or keep < 0:
                    warning("Número inválido.")
                    continue
                warning(
                    f"Se eliminarán backups antiguos dejando los {keep} más recientes "
                    "de HA y de Z-Wave."
                )
                if confirm("¿Continuar con la limpieza?", default=False):
                    success(api.cleanup_old_backups(keep=keep))
            elif op == "7":
                warning(
                    "docker system prune -a -f elimina imágenes y contenedores no usados. "
                    "La imagen actual de HA en uso se conserva; capas huérfanas se borran."
                )
                if confirm("¿Ejecutar Docker prune ahora?", default=False):
                    info("Ejecutando docker system prune…")
                    success(api.docker_prune())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))
