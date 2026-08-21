"""ZeroTier, Cloudflare, celular y MQTT."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError
from ui import (
    ask,
    confirm,
    error,
    info,
    menu_options,
    panel_cellular,
    panel_cloudflare,
    panel_mqtt_diagnostic,
    panel_zerotier,
    section,
    success,
    warning,
)


def menu_cloudflare_remote(api: HasControllerAPI) -> None:
    while True:
        section("Cloudflare Tunnel (Remoto)")
        status = api.get_cloudflare_status()
        panel_cloudflare(status)

        opts = [("1", "Actualizar estado")]
        if not status.installed:
            opts.append(("2", "Instalar cloudflared"))
        else:
            opts.append(("3", "Eliminar cloudflared (Uninstall)"))
        opts.append(("4", "Aplicar trusted_proxies en HA (evitar error 400)"))
        opts.append(("0", "Volver"))

        menu_options("Acciones Cloudflare", opts)
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                continue
            if op == "2" and not status.installed:
                warning(
                    "Se instalará cloudflared y se escribirán trusted_proxies en "
                    ".storage/http (stable, no pending). Home Assistant se reiniciará."
                )
                if confirm("¿Desea instalar cloudflared en el controlador?", default=False):
                    info("Instalando cloudflared y aplicando trusted_proxies…")
                    success(api.install_cloudflare())
            elif op == "3" and status.installed:
                if confirm("¿Desea eliminar cloudflared del controlador?", default=False):
                    info("Eliminando cloudflared...")
                    success(api.remove_cloudflare())
            elif op == "4":
                warning(
                    "Se escribirá use_x_forwarded_for + 127.0.0.1/::1 en stable "
                    "y se reiniciará HA. No hace falta entrar a la UI."
                )
                if confirm("¿Aplicar trusted_proxies y reiniciar Home Assistant?", default=True):
                    info("Escribiendo .storage/http y reiniciando HA…")
                    success(api.ensure_ha_trusted_proxies(restart=True))
            else:
                warning("Opción no válida.")
        except Exception as exc:
            error(str(exc))


def menu_zerotier(api: HasControllerAPI) -> None:
    section("ZeroTier")
    zt = api.check_zerotier()
    panel_zerotier(zt)

    if not zt.installed:
        if confirm("¿Desea instalar ZeroTier?", default=False):
            try:
                info("Iniciando instalación de ZeroTier…")
                api.install_zerotier()
                success("ZeroTier instalado correctamente.")
                zt = api.check_zerotier()
                panel_zerotier(zt)
            except HasApiError as exc:
                error(f"Fallo en la instalación: {exc}")
                return

    if not zt.installed:
        return

    menu_options(
        "Opciones ZeroTier",
        [
            ("1", "Unirse a una red"),
            ("2", "Salir de una red"),
            ("3", "Ver información de redes"),
            ("0", "Volver"),
        ],
    )
    op = ask("Opción")
    try:
        if op == "1":
            nwid = ask("Ingrese el Network ID (16 caracteres)")
            if len(nwid) != 16:
                warning("El Network ID debe tener exactamente 16 caracteres.")
                return
            info(f"Uniéndose a la red {nwid}…")
            api.join_zerotier_network(nwid)
            success("Petición de unión enviada. Recuerde autorizar el nodo en el panel de ZeroTier.")
        elif op == "2":
            if zt.networks:
                menu_options(
                    "Redes unidas",
                    [(str(i), f"{n.nwid} {n.name} {n.ip}") for i, n in enumerate(zt.networks, 1)],
                )
                raw = ask("Número o Network ID")
                nwid = raw
                if raw.isdigit():
                    idx = int(raw) - 1
                    if 0 <= idx < len(zt.networks):
                        nwid = zt.networks[idx].nwid
                if len(nwid) != 16:
                    warning("Network ID inválido.")
                    return
            else:
                nwid = ask("Network ID (16 caracteres)")
                if len(nwid) != 16:
                    warning("El Network ID debe tener exactamente 16 caracteres.")
                    return
            if confirm(f"¿Salir de la red {nwid}?", default=False):
                success(api.leave_zerotier_network(nwid))
        elif op == "3":
            zt = api.check_zerotier()
            panel_zerotier(zt)
    except HasApiError as exc:
        error(str(exc))


def menu_cellular(api: HasControllerAPI) -> None:
    while True:
        section("MÓDULO CELULAR / LTE")
        status = api.get_cellular_status()
        panel_cellular(status)

        menu_options(
            "Acciones Celular",
            [
                ("1", "Dar de baja al servicio (stop + disable)"),
                ("2", "Reactivar servicio (enable + start)"),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                if not status.is_active and not status.is_enabled:
                    info("El servicio ya está de baja. No se requiere acción.")
                    continue
                warning(
                    "Se detendrá cellular.service y se quitará del arranque automático "
                    "(como en el informe ZW855)."
                )
                if confirm("¿Dar de baja al servicio celular?", default=False):
                    info("Dando de baja cellular.service…")
                    success(api.take_down_cellular_service())
            elif op == "2":
                if status.is_active and status.is_enabled:
                    info("El servicio ya está activo y habilitado.")
                    continue
                if confirm("¿Habilitar e iniciar cellular.service?", default=False):
                    info("Reactivando cellular.service…")
                    success(api.bring_up_cellular_service())
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))


def menu_mqtt(api: HasControllerAPI) -> None:
    while True:
        section("Conexión MQTT — Z-Wave JS UI")
        info("Ejecutando diagnóstico (runbook Horus)…")
        try:
            status = api.get_mqtt_diagnostic()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_mqtt_diagnostic(status)
        menu_options(
            "Acciones MQTT",
            [
                ("1", "Actualizar diagnóstico"),
                ("2", "Deshabilitar módulo MQTT (backup + reinicio)"),
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
                if status.recommended_action == "none_already_disabled":
                    info(status.action_detail or "MQTT ya está deshabilitado.")
                    continue
                if (
                    status.recommended_action == "none_no_errors"
                    and status.mqtt_disabled is not False
                ):
                    info(status.action_detail or "No se requiere acción.")
                    continue
                if status.recommended_action == "restart_required":
                    warning(status.action_detail)
                    if confirm(f"¿Reiniciar {status.service_name} ahora?", default=True):
                        info("Reiniciando servicio…")
                        success(api.restart_mqtt_zwave_service())
                        status = api.get_mqtt_diagnostic()
                        panel_mqtt_diagnostic(status)
                    continue
                if status.recommended_action == "audit_auth":
                    error(status.action_detail)
                    warning("El runbook indica NO apagar MQTT si el puerto 1883 está activo.")
                    continue
                if status.recommended_action == "verify_ha_first":
                    warning(status.action_detail)
                    if not confirm(
                        "¿Continuar de todos modos con la deshabilitación de MQTT?",
                        default=False,
                    ):
                        continue
                elif status.recommended_action != "disable_mqtt":
                    warning(status.action_detail or "Revise el diagnóstico antes de continuar.")
                    if not confirm("¿Aplicar deshabilitación de MQTT de todos modos?", default=False):
                        continue
                else:
                    warning(
                        "Se creará backup de settings.json, se pondrá mqtt.disabled=true "
                        "y se reiniciará zwave-ui.service."
                    )
                    if not confirm("¿Deshabilitar módulo MQTT?", default=False):
                        continue

                info("Aplicando corrección…")
                success(api.disable_mqtt_zwave())
                info("Actualizando diagnóstico post-corrección…")
                status = api.get_mqtt_diagnostic()
                panel_mqtt_diagnostic(status)
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))


def menu_remote_connection(api: HasControllerAPI) -> None:
    while True:
        section("Consultar Conexión Remota")
        menu_options(
            "Conexión Remota",
            [
                ("1", "Consultar ZeroTier"),
                ("2", "Consultar Cloudflare (Remoto)"),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_zerotier(api)
            ask("Pulse Enter para volver")
        elif op == "2":
            menu_cloudflare_remote(api)
        else:
            warning("Opción no válida.")


def menu_error_correction(api: HasControllerAPI) -> None:
    while True:
        section("Modo Corrección de errores")
        menu_options(
            "Corrección",
            [
                ("1", "Service cellular"),
                ("2", "Conexión MQTT (Z-Wave JS UI)"),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_cellular(api)
        elif op == "2":
            menu_mqtt(api)
        else:
            warning("Opción no válida.")
