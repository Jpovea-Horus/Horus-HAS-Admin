"""Menú: actualizar Home Assistant + Z-Wave JS UI con perfiles de versión validados."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError, NotConnectedError, ValidationError
from ha_profiles import delete_local_profile, load_profiles, save_local_profile
from ha_update_manager import (
    MAX_DB_MB,
    MAX_DISK_USED_PCT,
    MIN_FREE_START_MB,
    ZWAVE_SERVICE,
)
from menus.horus_backup import _check_health, _phase, _report_failure
from models import HaUpdatePlan, HaUpdateProfile, HaUpdateStatus
from ui import ask, confirm, error, info, menu_options, section, success, warning, working


def _show_status(st: HaUpdateStatus) -> None:
    info(
        f"Actual: HA {st.ha_version or '—'} · Z-Wave JS UI {st.zwave_version or '—'} "
        f"({'activo' if st.zwave_active else 'inactivo'}) · Node {st.node_version or '—'} · {st.arch or '?'}"
    )
    info(
        f"Compose: {st.compose_file or 'no encontrado'} · imagen {st.compose_image or '—'} "
        f"({st.ha_image_mb} MB) · {st.compose_cmd or 'sin docker compose'}"
    )
    (success if st.used_pct < MAX_DISK_USED_PCT else warning)(
        f"Disco: {st.free_mb} MB libres ({st.used_pct}% usado) · DB HA {st.db_mb} MB"
    )


def _profile_tag(p: HaUpdateProfile) -> str:
    if p.source == "latest":
        return "[warning]NO validada[/warning]"
    tags = ["VALIDADA"]
    if p.recommended:
        tags.append("recomendada")
    if p.source == "local":
        tags.append("registrada en este PC")
    return "[success]" + " · ".join(tags) + "[/success]"


def _show_plan(plan: HaUpdatePlan) -> None:
    st, p = plan.status, plan.profile
    section(f"Plan: {p.title}")
    if plan.update_ha:
        info(f"Home Assistant: {st.ha_version or '—'} → {p.ha}  ({plan.new_image})")
    else:
        success(f"Home Assistant ya está en {st.ha_version}.")
    if plan.update_zwave:
        info(f"Z-Wave JS UI: {st.zwave_version or '—'} → {p.zwave_js_ui}")
        if plan.zwave_node_req:
            info(f"Node requerido: {plan.zwave_node_req} (actual {st.node_version or '—'})")
    elif st.zwave_version == p.zwave_js_ui:
        success(f"Z-Wave JS UI ya está en {st.zwave_version}.")
    else:
        info(f"Z-Wave JS UI se mantiene en {st.zwave_version or '—'}.")
    for w in plan.warnings:
        (success if w.startswith("Compatibles") else warning)(w)
    for b in plan.blockers:
        error(b)


def _free_space_with_backups(api: HasControllerAPI) -> bool:
    """Procedimiento: con < 500 MB libres, borrar config_backup_* si existen."""
    try:
        backups = [b for b in api.list_backups() if b.kind == "ha"]
    except HasApiError:
        backups = []
    if not backups:
        return False
    total = sum(b.size_bytes for b in backups) // (1024 * 1024)
    warning(f"Hay {len(backups)} backup(s) config_backup_* ocupando ~{total} MB.")
    if not confirm("¿Eliminarlos para liberar espacio?", default=True):
        return False
    for b in backups:
        success(api.delete_backup(b.path))
    return True


def _verify_after(api: HasControllerAPI, plan: HaUpdatePlan) -> None:
    _check_health(api, need_ha=True, need_zwave=plan.update_zwave or plan.status.zwave_active)
    with working("Verificación final (versiones, disco, DB, log Z-Wave)…"):
        new = api.get_ha_update_status()
        issues = api.get_zwave_log_issues(new.ha_container)
    p = plan.profile
    section("Resultado")
    (success if new.ha_version == p.ha else warning)(
        f"Home Assistant: {plan.status.ha_version or '—'} → {new.ha_version or '—'}"
    )
    (success if new.zwave_version == p.zwave_js_ui or not plan.update_zwave else warning)(
        f"Z-Wave JS UI: {plan.status.zwave_version or '—'} → {new.zwave_version or '—'}"
    )
    (success if new.zwave_active else error)(
        f"{ZWAVE_SERVICE}: {'active' if new.zwave_active else 'NO activo'}"
    )
    (success if new.used_pct < MAX_DISK_USED_PCT else warning)(
        f"Disco: {new.used_pct}% usado ({new.free_mb} MB libres)"
    )
    if new.db_mb > MAX_DB_MB:
        warning(f"DB {new.db_mb} MB > {MAX_DB_MB} MB: recorte purge_keep_days en Recorder.")
    if issues:
        error("La integración Z-Wave JS reporta problemas en el log de HA:")
        for line in issues:
            warning(f"  {line[-180:]}")
    else:
        info("Sin errores de compatibilidad zwave_js en el log de HA (puede tardar 1–2 min en cargar).")


def _run_profile(api: HasControllerAPI, profile: HaUpdateProfile, st: HaUpdateStatus) -> None:
    update_zwave = True
    if st.zwave_version and st.zwave_version != profile.zwave_js_ui:
        update_zwave = confirm(
            f"¿Actualizar también Z-Wave JS UI {st.zwave_version} → {profile.zwave_js_ui}?",
            default=True,
        )

    with working("Verificando versión, arquitectura, espacio y compatibilidad…"):
        plan = api.plan_ha_update(profile, update_zwave=update_zwave)
    _show_plan(plan)
    if plan.nothing_to_do:
        success("El controlador ya está en estas versiones. No hay nada que hacer.")
        return
    if plan.blockers and not any("MB" in b for b in plan.blockers):
        return

    if not confirm("¿Ejecutar limpieza de espacio (APT, NPM, logs, /config/config, *.zip)?", default=True):
        return
    with working("Limpiando…"):
        success(api.ha_update_cleanup())
        st = api.get_ha_update_status()
    if st.free_mb < MIN_FREE_START_MB:
        warning(f"Solo {st.free_mb} MB libres (mínimo {MIN_FREE_START_MB}).")
        if not _free_space_with_backups(api):
            error("No hay espacio suficiente. Libere espacio en Backups y espacio en disco.")
            return
    with working("Recalculando plan…"):
        plan = api.plan_ha_update(profile, update_zwave=update_zwave)
    if plan.blockers:
        _show_plan(plan)
        return
    info(f"Libre: {plan.status.free_mb} MB · tras borrar HA vieja: ~{plan.status.free_after_rm_mb} MB")

    if plan.update_ha:
        warning(
            "La imagen actual de HA se borra ANTES de descargar la nueva (no caben las dos). "
            "HA estará caída ~10–15 min. El trabajo sigue en el controlador aunque se corte el SSH."
        )
    if plan.update_zwave:
        warning("Z-Wave JS UI se detendrá durante el npm install (2–5 min).")
    if not profile.is_validated:
        warning("Este par NO está validado en Nexxo 800. Úselo solo en equipos de prueba.")
        if ask("Escriba NO VALIDADO para continuar").strip().upper() != "NO VALIDADO":
            info("Cancelado.")
            return
    if plan.is_downgrade:
        if ask("Es un DOWNGRADE de HA. Escriba DOWNGRADE para continuar").strip().upper() != "DOWNGRADE":
            info("Cancelado.")
            return
    if ask("Escriba ACTUALIZAR para iniciar").strip().upper() != "ACTUALIZAR":
        info("Cancelado.")
        return

    result = api.run_ha_update(plan, on_phase=_phase)
    if not result.ok:
        _report_failure(result)
        return
    success("Trabajo remoto completado.")
    _verify_after(api, plan)


def _register_profile(api: HasControllerAPI) -> None:
    with working("Leyendo versiones del controlador…"):
        profile = api.ha_update_profile_from_controller()
    info(f"Detectado: {profile.title}")
    if not confirm("¿Registrar este par como VALIDADO en este PC?", default=False):
        return
    profile.recommended = confirm("¿Marcarlo como recomendado por defecto?", default=False)
    profile.notes = ask("Notas (opcional)", default=profile.notes)
    success(f"Perfil guardado en {save_local_profile(profile)}")


def menu_ha_update(api: HasControllerAPI) -> None:
    while True:
        section("Actualizar Home Assistant + Z-Wave JS UI")
        try:
            with working("Revisando espacio, compose e imagen…"):
                st = api.get_ha_update_status()
        except NotConnectedError:
            raise
        except HasApiError as exc:
            error(str(exc))
            return
        _show_status(st)

        profiles = load_profiles()
        options: list[tuple[str, str]] = [("", "[dim]Versiones validadas (HA + Z-Wave compatibles)[/dim]")]
        for i, p in enumerate(profiles, start=1):
            current = " [dim](instalada)[/dim]" if p.ha == st.ha_version and p.zwave_js_ui == st.zwave_version else ""
            options.append((str(i), f"{p.title}  {_profile_tag(p)}{current}"))
        options += [
            ("", ""),
            ("L", "Última versión disponible (NO validada, solo equipos de prueba)"),
            ("G", "Registrar este controlador como perfil validado"),
        ]
        local = [p for p in profiles if p.source == "local"]
        if local:
            options.append(("B", "Borrar un perfil registrado en este PC"))
        options += [("R", "Actualizar estado"), ("0", "Volver")]
        menu_options("Versión a instalar", options)

        op = ask("Opción").upper()
        if op == "0":
            return
        try:
            if op == "R":
                continue
            if op == "L":
                with working("Consultando GitHub y npm…"):
                    profile = api.ha_update_latest_profile()
                _run_profile(api, profile, st)
            elif op == "G":
                _register_profile(api)
            elif op == "B" and local:
                for i, p in enumerate(local, start=1):
                    info(f"{i}. {p.title} ({p.id})")
                idx = ask("Número a borrar (vacío = cancelar)")
                if idx.isdigit() and 1 <= int(idx) <= len(local):
                    target = local[int(idx) - 1]
                    if confirm(f"¿Borrar '{target.title}'?", default=False) and delete_local_profile(target.id):
                        success("Perfil borrado.")
            elif op.isdigit() and 1 <= int(op) <= len(profiles):
                p = profiles[int(op) - 1]
                if p.notes:
                    info(p.notes)
                _run_profile(api, p, st)
            else:
                warning("Opción no válida.")
                continue
        except NotConnectedError:
            raise
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))
        ask("Pulse Enter para continuar")
