"""Menú de backup remoto estándar HORUS (.tar.gz ligero, restore y reset protegido)."""

from __future__ import annotations

import os
from typing import Callable, Optional, TypeVar

from rich import box
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError
from models import HorusArchive, HorusArchiveInfo, HorusBackupStatus, HorusJobResult
from remote_backup_manager import RemoteBackupManager
from ui import (
    ask,
    ask_int,
    confirm,
    console,
    error,
    info,
    menu_options,
    panel_horus_backup,
    section,
    success,
    table_horus_archives,
    table_horus_snapshots,
    warning,
)

T = TypeVar("T")


def _phase(phase: str, elapsed: int) -> None:
    info(f"[{elapsed:>3}s] {phase}")


def _with_progress(label: str, action: Callable[[Callable[[int, int], None]], T]) -> T:
    with Progress(
        TextColumn("[info]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as prog:
        task = prog.add_task(label, total=None)

        def _cb(done: int, total: int) -> None:
            prog.update(task, completed=done, total=total or None)

        return action(_cb)


def _report_failure(result: HorusJobResult) -> None:
    error(f"{result.status}: {result.detail}")
    if result.log_tail:
        console.print(Panel(result.log_tail.strip(), title="Log remoto", border_style="red", box=box.ROUNDED))


def _check_health(api: HasControllerAPI, need_ha: bool, need_zwave: bool) -> bool:
    with console.status("Esperando a que Home Assistant y Z-Wave respondan (hasta 4 min)…"):
        ha_ok, zw_ok = api.wait_services_healthy(need_ha=need_ha, need_zwave=need_zwave)
    if need_ha:
        (success if ha_ok else warning)(
            "Home Assistant responde en :8123." if ha_ok else "Home Assistant aún no responde en :8123."
        )
    if need_zwave:
        (success if zw_ok else warning)(
            "Z-Wave JS UI responde." if zw_ok else "Z-Wave JS UI aún no responde (:3000/:8091)."
        )
    return ha_ok and zw_ok


def _create_and_download(api: HasControllerAPI, status: HorusBackupStatus, ask_first: bool = True) -> Optional[str]:
    """Backup en frío + descarga verificada. Devuelve la ruta local o None."""
    if status.error:
        error(status.error)
        return None
    if not status.enough_space:
        error("Espacio insuficiente en el controlador para crear el backup. Libere espacio primero.")
        return None
    if not status.nvm_latest:
        warning("No hay respaldo NVM (.bin) de la antena: el backup no servirá para cambiar hardware sin re-emparejar.")
    warning("Home Assistant y Z-Wave se detendrán ~1 minuto mientras se empaqueta (backup en frío).")
    if ask_first and not confirm("¿Crear el backup ahora?", default=True):
        return None

    result = api.create_horus_backup(on_phase=_phase)
    if not result.ok:
        _report_failure(result)
        return None
    size = _human(result.size_bytes)
    success(
        f"Backup creado: {result.archive_path.rsplit('/', 1)[-1]} ({size}) · "
        f"servicios detenidos {result.downtime_s}s"
    )

    local_path, nvm_path = _with_progress(
        "Descargando",
        lambda cb: api.download_horus_backup(result.archive_path, result.sha256, progress=cb),
    )
    success(f"Descargado y verificado (SHA-256): {local_path}")
    if nvm_path:
        success(f"Respaldo NVM de la antena: {nvm_path}")

    if confirm("¿Conservar en el controlador solo este último backup (borrar los anteriores)?", default=True):
        removed = api.prune_remote_horus_backups(keep=1)
        if removed:
            info(f"Eliminados {len(removed)} backup(s) antiguos del controlador.")
    return local_path


def _human(n: int) -> str:
    return HorusArchive(path="", name="", size_bytes=n).size_human


def _pick_remote(status: HorusBackupStatus, verb: str) -> Optional[str]:
    if not status.remote_archives:
        info("No hay backups en el controlador.")
        return None
    idx = ask_int(f"Número de backup a {verb}")
    if idx is None or idx < 1 or idx > len(status.remote_archives):
        warning("Número fuera de rango.")
        return None
    return status.remote_archives[idx - 1].path


def _download_existing(api: HasControllerAPI, status: HorusBackupStatus) -> None:
    path = _pick_remote(status, "descargar")
    if not path:
        return
    archive = next(a for a in status.remote_archives if a.path == path)
    local_path, nvm_path = _with_progress(
        "Descargando",
        lambda cb: api.download_horus_backup(path, archive.sha256, progress=cb),
    )
    success(f"Descargado y verificado: {local_path}")
    if nvm_path:
        success(f"Respaldo NVM: {nvm_path}")


def _delete_remote(api: HasControllerAPI, status: HorusBackupStatus) -> None:
    path = _pick_remote(status, "eliminar del controlador")
    if path and confirm(f"¿Eliminar {path.rsplit('/', 1)[-1]} del controlador?", default=False):
        success(api.delete_remote_horus_backup(path))


def _local_backups(api: HasControllerAPI) -> None:
    archives = api.list_local_horus_backups()
    folder = api.horus_backup.local_dir()
    table_horus_archives(archives, title="Backups en este PC")
    info(f"Carpeta: {folder}")
    menu_options(
        "Backups locales",
        [("1", "Abrir carpeta en el explorador"), ("2", "Limpiar antiguos de un equipo (mantener N)"), ("0", "Volver")],
    )
    op = ask("Opción", default="0")
    if op == "1":
        os.makedirs(folder, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            info(folder)
    elif op == "2":
        device = ask("ID del equipo (4 caracteres)", default=api.horus_backup.device_id()).lower()
        keep = ask_int("¿Cuántos backups recientes conservar?", default="3")
        if keep is None or keep < 1:
            warning("Número inválido.")
            return
        removed = api.prune_local_horus_backups(device, keep)
        success(f"Eliminados {len(removed)} backup(s) locales de {device}." if removed else "No había antiguos.")


def _show_archive_info(archive: HorusArchiveInfo, status: HorusBackupStatus) -> bool:
    """Muestra el contenido y advertencias. Devuelve False si el usuario cancela."""
    lines = [
        f"[info]Archivo:[/info] {archive.name}",
        f"[info]Equipo de origen:[/info] {archive.device_id or '?'} ({archive.hostname or '?'})",
        f"[info]Creado:[/info] {archive.created or '?'}",
        f"[info]Versión HA:[/info] {archive.ha_version or '?'}  ·  [info]actual:[/info] {status.ha_version or '?'}",
        f"[info]Contiene:[/info] "
        + ", ".join(
            x for x in (
                "config HA" if archive.src_config else "",
                "store Z-Wave" if archive.src_store else "",
                "NVM antena" if archive.nvm_member else "",
            ) if x
        ),
        f"[info]Tamaño descomprimido:[/info] {_human(archive.total_bytes)}",
    ]
    console.print(Panel("\n".join(lines), title="Backup a restaurar", border_style="cyan", box=box.ROUNDED))
    for w in archive.warnings:
        warning(w)

    if archive.device_id and status.device_id and archive.device_id != status.device_id:
        warning(
            f"El backup es del equipo {archive.device_id} y está conectado a {status.device_id}. "
            "Continúe solo si es un reemplazo de hardware."
        )
        if not confirm("¿Restaurar en un equipo distinto al de origen?", default=False):
            return False
    version_msg = RemoteBackupManager.version_warning(archive.ha_version, status.ha_version)
    if version_msg:
        warning(version_msg)
        if not confirm("¿Continuar de todos modos (no recomendado)?", default=False):
            return False
    return True


def _restore(api: HasControllerAPI, status: HorusBackupStatus) -> None:
    archives = api.list_local_horus_backups()
    table_horus_archives(archives, title="Backups en este PC")
    idx = ask_int("Número de backup a restaurar (0 = indicar otra ruta)", default="1" if archives else "0")
    if idx is None:
        return
    if idx == 0:
        path = ask("Ruta completa del .tar.gz").strip().strip('"').strip("'")
    elif 1 <= idx <= len(archives):
        path = archives[idx - 1].path
    else:
        warning("Número fuera de rango.")
        return
    if not path:
        return

    info("Verificando archivo local…")
    archive = api.inspect_horus_backup(path)
    if not _show_archive_info(archive, status):
        info("Cancelado.")
        return

    restore_ha = bool(archive.src_config) and confirm("¿Restaurar la configuración de Home Assistant?", default=True)
    restore_zw = bool(archive.src_store) and confirm(
        "¿Restaurar el store Z-Wave (llaves de seguridad y nombres de nodos)?", default=True
    )
    if not restore_ha and not restore_zw:
        info("Nada seleccionado.")
        return
    keep_db = restore_ha and confirm("¿Conservar el historial actual (base de datos) del controlador?", default=True)

    warning(
        "Se detendrán los servicios. Lo actual quedará apartado como *.pre_restore_<fecha> "
        "y podrá revertirse desde 'Copias apartadas'."
    )
    if not confirm("¿Restaurar ahora?", default=False):
        return

    result = _with_progress(
        "Subiendo",
        lambda cb: api.restore_horus_backup(
            archive, restore_ha=restore_ha, restore_zwave=restore_zw, keep_db=keep_db,
            progress=cb, on_phase=_phase,
        ),
    )
    if not result.ok:
        _report_failure(result)
        warning("El controlador revirtió automáticamente al estado anterior.")
        return
    success("Restauración aplicada.")
    healthy = _check_health(api, need_ha=True, need_zwave=restore_zw)
    if archive.nvm_member and archive.device_id != status.device_id:
        warning(
            "Equipo distinto al de origen: cargue el respaldo NVM (.bin) en Z-Wave JS UI › "
            "Configuración › Restaurar NVM para clonar la antena."
        )
    if not healthy and result.detail and confirm(
        "Los servicios no respondieron a tiempo. ¿Revertir al estado anterior ahora?", default=False
    ):
        _run_revert(api, result.detail)
        return
    if confirm("¿Reiniciar el controlador ahora? (opcional)", default=False):
        _reboot(api)


def _run_revert(api: HasControllerAPI, stamp: str) -> None:
    result = api.revert_horus_snapshot(stamp, on_phase=_phase)
    if not result.ok:
        _report_failure(result)
        return
    success("Revertido al estado anterior.")
    _check_health(api, need_ha=True, need_zwave=True)


def _snapshots(api: HasControllerAPI, status: HorusBackupStatus) -> None:
    stamps = table_horus_snapshots(status.snapshots)
    if not stamps:
        return
    info("Ocupan espacio en disco: elimínelas cuando confirme que el equipo funciona bien.")
    menu_options("Copias apartadas", [("1", "Revertir a una copia (por #)"), ("2", "Eliminar una copia (por #)"), ("0", "Volver")])
    op = ask("Opción", default="0")
    if op not in ("1", "2"):
        return
    idx = ask_int("Número de copia")
    if idx is None or idx < 1 or idx > len(stamps):
        warning("Número fuera de rango.")
        return
    stamp = stamps[idx - 1]
    if op == "1":
        warning("La configuración actual se descartará y se pondrá la copia apartada.")
        if confirm("¿Revertir ahora?", default=False):
            _run_revert(api, stamp)
    elif confirm("¿Eliminar definitivamente esta copia apartada?", default=False):
        success(api.delete_horus_snapshots(stamp))


def _factory_reset(api: HasControllerAPI, status: HorusBackupStatus) -> None:
    section("Reset de fábrica protegido")
    warning("Deja Home Assistant en la pantalla de creación de cuenta.")
    warning("Se pierden usuarios, integraciones, plugin_service, credenciales AWS, admin_network y panel Z-Wave.")
    reset_ha = confirm("¿Resetear Home Assistant?", default=True)
    reset_zw = confirm("¿Resetear también Z-Wave (llaves de seguridad y nodos)?", default=False)
    if not reset_ha and not reset_zw:
        info("Nada seleccionado.")
        return
    if reset_zw:
        warning(
            "La antena conserva los dispositivos emparejados pero sin llaves S2: las cerraduras quedarán "
            "inaccesibles hasta excluirlas físicamente o restaurar este backup."
        )

    info("Paso obligatorio: backup previo descargado a este PC.")
    local = _create_and_download(api, status, ask_first=False)
    if not local:
        error("Reset cancelado: no se pudo asegurar un backup previo.")
        return

    device = status.device_id
    typed = ask(f"Escriba el ID del equipo ([bold]{device}[/bold]) para confirmar el reset").strip().lower()
    if typed != device.lower():
        info("ID no coincide. Reset cancelado.")
        return

    result = api.factory_reset_horus(reset_ha=reset_ha, reset_zwave=reset_zw, on_phase=_phase)
    if not result.ok:
        _report_failure(result)
        warning("El controlador revirtió automáticamente al estado anterior.")
        return
    success("Reset aplicado. La configuración anterior quedó apartada (revertible).")
    _check_health(api, need_ha=reset_ha, need_zwave=reset_zw)
    info("Home Assistant puede tardar ~2 minutos en mostrar la pantalla de creación de cuenta.")
    if result.detail and confirm(
        "¿Eliminar definitivamente la configuración anterior del controlador (para reasignar el equipo)?",
        default=False,
    ):
        success(api.delete_horus_snapshots(result.detail))


def _reboot(api: HasControllerAPI) -> None:
    success(api.reboot_controller())
    api.disconnect()
    raise NotConnectedError("Controlador reiniciando.")


def menu_horus_backup(api: HasControllerAPI) -> None:
    while True:
        section("Backup remoto estándar HORUS")
        try:
            with console.status("Analizando controlador…"):
                status = api.get_horus_backup_status()
        except NotConnectedError:
            raise
        except HasApiError as exc:
            error(str(exc))
            return

        panel_horus_backup(status)
        opts = [
            ("1", "Actualizar estado"),
            ("2", "Crear backup y descargar a este PC"),
            ("3", "Descargar un backup del controlador (por #)"),
            ("4", "Eliminar un backup del controlador (por #)"),
            ("5", "Backups guardados en este PC"),
            ("", ""),
            ("6", "Restaurar desde un backup de este PC"),
        ]
        if status.snapshots:
            opts.append(("7", "Copias apartadas (revertir / eliminar)"))
        opts += [("8", "Reset de fábrica protegido"), ("", ""), ("0", "Volver")]
        menu_options("Acciones", opts)

        op = ask("Opción")
        if op == "0":
            return
        try:
            if op == "1":
                continue
            if op == "2":
                _create_and_download(api, status)
            elif op == "3":
                _download_existing(api, status)
            elif op == "4":
                _delete_remote(api, status)
            elif op == "5":
                _local_backups(api)
            elif op == "6":
                _restore(api, status)
            elif op == "7" and status.snapshots:
                _snapshots(api, status)
            elif op == "8":
                _factory_reset(api, status)
            else:
                warning("Opción no válida.")
                continue
        except NotConnectedError:
            raise
        except HasApiError as exc:
            error(str(exc))
        ask("Pulse Enter para continuar")
