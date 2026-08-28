"""Menús Home Assistant: usuarios, backups, plugin, config."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError, ValidationError
from ui import (
    ask,
    ask_confirmed_path,
    ask_int,
    ask_password,
    confirm,
    error,
    info,
    menu_options,
    panel_backup_manager,
    panel_ha_configuration,
    panel_ha_users,
    panel_helper_manager,
    panel_hostname,
    panel_maintenance,
    panel_plugin_service,
    panel_process_snapshot,
    panel_admin_network,
    panel_zwave_panel,
    section,
    success,
    warning,
)


def menu_hostname(api: HasControllerAPI) -> None:
    section("Hostname")
    info_data = api.get_hostname()
    panel_hostname(info_data.static_hostname, info_data.pretty_hostname)
    nuevo = ask("Nuevo hostname (vacío = cancelar)", default="")
    if not nuevo:
        info("Sin cambios.")
        return
    if confirm(f"¿Cambiar hostname a '{nuevo}'?", default=False):
        try:
            api.set_hostname(nuevo)
            success(f"Hostname actualizado a [bold]{nuevo}[/bold].")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))
            if "denied" in str(exc).lower():
                warning("Pruebe con usuario root o verifique permisos en el controlador.")


def menu_ha_users(api: HasControllerAPI) -> None:
    while True:
        section("Usuarios Home Assistant")
        try:
            status = api.get_ha_users_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_ha_users(status)
        if status.error and not status.users:
            ask("Pulse Enter para volver")
            break

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar listado"),
                ("2", "Cambiar / resetear contraseña"),
                ("3", "Crear usuario"),
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
                username = ask("Usuario (login HA)").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                known = {u.username for u in status.users if u.username}
                if known and username not in known:
                    warning(f"'{username}' no aparece en el listado.")
                    if not confirm("¿Continuar de todos modos?", default=False):
                        continue
                target = next((u for u in status.users if u.username == username), None)
                if target and (target.incomplete or not target.user_id):
                    error(
                        f"'{username}' está incompleto (sin id). "
                        "No se puede resetear; elimínelo o créelo de nuevo."
                    )
                    continue
                pwd = ask_password("Nueva contraseña")
                pwd2 = ask_password("Confirmar contraseña")
                if pwd != pwd2:
                    error("Las contraseñas no coinciden.")
                    continue
                if confirm(f"¿Resetear contraseña de '{username}'?", default=False):
                    warning("Home Assistant se reiniciará para aplicar la nueva contraseña.")
                    if not confirm("¿Continuar con el reinicio de HA?", default=True):
                        continue
                    info("Aplicando cambio y reiniciando Home Assistant…")
                    success(api.change_ha_user_password(username, pwd))
            elif op == "3":
                info(
                    "Se creará usuario completo en .storage/auth "
                    "(con id UUID + credencial + contraseña)."
                )
                username = ask("Nuevo usuario (minúsculas)").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                pwd = ask_password("Contraseña")
                pwd2 = ask_password("Confirmar contraseña")
                if pwd != pwd2:
                    error("Las contraseñas no coinciden.")
                    continue
                is_admin = confirm("¿Usuario administrador?", default=False)
                role_txt = "administrador" if is_admin else "estándar"
                warning(
                    "Home Assistant se reiniciará unos segundos para cargar el usuario "
                    "(sin reinicio el login no funciona)."
                )
                if confirm(
                    f"¿Crear usuario '{username}' como {role_txt} y reiniciar HA?",
                    default=False,
                ):
                    info("Creando usuario, persona y reiniciando Home Assistant…")
                    success(api.add_ha_user(username, pwd, is_admin=is_admin))
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


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
            else:
                warning("Opción no válida.")
        except HasApiError as exc:
            error(str(exc))
            summary = f"Error: {exc}"


def menu_backup_manager(api: HasControllerAPI) -> None:
    while True:
        section("Gestión de backups")
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
                ("5", "Eliminar backup (por #)"),
                ("6", "Limpiar antiguos (mantener N más recientes)"),
                ("7", "Liberar espacio Docker (prune -a)"),
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


def menu_plugin_service(api: HasControllerAPI) -> None:
    from paths import get_local_plugin_source
    from plugin_service_manager import (
        GITHUB_DEFAULT_REF,
        GITHUB_REPO_URL,
    )

    default_local = get_local_plugin_source()
    while True:
        section("plugin_service (custom component)")
        try:
            status = api.get_plugin_service_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_plugin_service(status)
        if status.plugin_exists:
            names = ", ".join(status.found_names) or status.plugin_dir
            success(f"Plugin instalado ({names}).")
        elif status.parent_exists:
            warning("No se encontró ninguna carpeta plugin_service* en custom_components/.")
        else:
            error("No se encontró custom_components/ en la ruta esperada.")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Eliminar plugin_service"),
                ("3", "Subir / instalar desde carpeta local"),
                ("4", "Instalar desde GitHub (horus-integration-nexxo)"),
                ("5", "Reiniciar Home Assistant"),
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
                if not status.plugin_exists:
                    info("No hay nada que eliminar.")
                    continue
                warning(f"Se ejecutará: rm -rf {status.plugin_dir}")
                if confirm("¿Eliminar plugin_service del controlador?", default=False):
                    info("Eliminando…")
                    success(api.remove_plugin_service())
            elif op == "3":
                if not status.parent_exists:
                    error("No se puede instalar: falta custom_components/.")
                    continue
                info("La carpeta local puede llamarse plugin_serviceV2; en remoto será plugin_service.")
                local = ask_confirmed_path("plugin_service", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                if status.plugin_exists:
                    warning("Ya existe plugin_service en el controlador; se reemplazará.")
                    if not confirm("¿Eliminar la versión remota y subir la nueva?", default=True):
                        continue
                else:
                    if not confirm(f"¿Subir '{local}' → plugin_service?", default=True):
                        continue
                info("Subiendo por SFTP (puede tardar)…")
                success(api.install_plugin_service(local, replace=True))
                if confirm("¿Desea reiniciar Home Assistant ahora para aplicar cambios?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            elif op == "4":
                if not status.parent_exists:
                    error("No se puede instalar: falta custom_components/.")
                    continue
                info(f"Fuente: {GITHUB_REPO_URL}")
                info("Si el repo es privado, use token (env GITHUB_TOKEN / GH_TOKEN) o péguelo aquí.")
                ref = ask("Rama o tag", default=GITHUB_DEFAULT_REF)
                token = ask("GitHub token (vacío = usar variable de entorno)", default="")
                if status.plugin_exists:
                    warning("Ya existe plugin_service; se reemplazará con la versión de GitHub.")
                if not confirm(
                    f"¿Descargar {GITHUB_REPO_URL}@{ref or 'main'} e instalar como plugin_service?",
                    default=True,
                ):
                    continue
                info("Descargando de GitHub y subiendo por SFTP…")
                success(
                    api.install_plugin_service_from_github(
                        ref=ref or GITHUB_DEFAULT_REF,
                        token=token or None,
                        replace=True,
                    )
                )
                if confirm("¿Desea reiniciar Home Assistant ahora para aplicar cambios?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            elif op == "5":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_admin_network(api: HasControllerAPI) -> None:
    from paths import get_local_admin_network_source

    default_local = get_local_admin_network_source()
    while True:
        section("Admin Network (admin de red)")
        try:
            status = api.get_admin_network_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_admin_network(status)
        if status.host.service_active and status.ha.component_exists:
            success("Host e integración HA presentes.")
        elif status.host.service_active:
            warning("Servicio host OK; falta copiar la integración a custom_components.")
        elif status.ha.component_exists:
            warning("Integración HA copiada; falta el servicio host (nmcli).")
        else:
            warning("Admin Network no está instalado en este controlador.")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Instalar todo (host + integración HA)"),
                ("3", "Instalar solo servicio host"),
                ("4", "Instalar solo integración HA"),
                ("5", "Mostrar API key"),
                ("6", "Eliminar integración HA"),
                ("7", "Eliminar servicio host"),
                ("8", "Eliminar TODO (integración + servicio host)"),
                ("9", "Reiniciar Home Assistant"),
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
                local = ask_confirmed_path("admin_network", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                warning(
                    "Se instalará el servicio en el SO (/opt/admin_network) y "
                    "se copiará custom_components/admin_network."
                )
                if not confirm("¿Instalar Admin Network completo?", default=True):
                    continue
                info("Subiendo host e integración (pip/venv puede tardar)…")
                success(api.install_admin_network(local, replace=True))
                if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
                info(
                    "En HA: Añadir integración 'Admin Network' → "
                    "127.0.0.1 / 8765 / API key mostrada arriba."
                )
            elif op == "3":
                local = ask_confirmed_path("admin_network (o host/)", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                if confirm("¿Instalar solo el servicio host?", default=True):
                    info("Ejecutando install.sh en el controlador…")
                    success(api.install_admin_network_host(local))
            elif op == "4":
                local = ask_confirmed_path("admin_network", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                if confirm("¿Subir integración HA (reemplaza si existe)?", default=True):
                    info("Subiendo custom_components/admin_network…")
                    success(api.install_admin_network_ha(local, replace=True))
                    if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
            elif op == "5":
                success(f"API key: {api.get_admin_network_api_key()}")
            elif op == "6":
                if not status.ha.component_exists:
                    info("No hay integración HA que eliminar.")
                    continue
                if confirm("¿Eliminar custom_components/admin_network?", default=False):
                    success(api.remove_admin_network_ha())
            elif op == "7":
                if not status.host.dir_exists and not status.host.service_active:
                    info("No hay servicio host que eliminar.")
                    continue
                wipe = confirm("¿Borrar también /etc/admin_network.env (API key)?", default=False)
                if confirm("¿Eliminar servicio host admin_network?", default=False):
                    success(api.remove_admin_network_host(wipe_env=wipe))
            elif op == "8":
                if not status.ha.component_exists and not status.host.dir_exists:
                    info("No hay nada que eliminar.")
                    continue
                if confirm("¿Eliminar Admin Network COMPLETO (HA + Host)?", default=False):
                    wipe = confirm("¿Borrar también /etc/admin_network.env (API key)?", default=False)
                    info("Eliminando integración HA…")
                    try:
                        success(api.remove_admin_network_ha())
                    except Exception as e:
                        error(f"Error HA: {e}")

                    info("Eliminando servicio host…")
                    try:
                        success(api.remove_admin_network_host(wipe_env=wipe))
                    except Exception as e:
                        error(f"Error Host: {e}")
            elif op == "9":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_helper_manager(api: HasControllerAPI) -> None:
    from paths import get_local_helper_manager_source

    default_local = get_local_helper_manager_source()
    while True:
        section("Helper Manager (admin auxiliares)")
        try:
            status = api.get_helper_manager_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_helper_manager(status)
        if status.component_exists:
            success("Integración presente en custom_components/.")
        elif status.parent_exists:
            warning("Falta helper_manager en custom_components/.")
        else:
            error("No se encontró custom_components/ en la ruta esperada.")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Subir / instalar desde carpeta local"),
                ("3", "Eliminar helper_manager"),
                ("4", "Reiniciar Home Assistant"),
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
                local = ask_confirmed_path("helper_manager", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                if status.component_exists:
                    warning("Ya existe; se reemplazará.")
                if not confirm(f"¿Subir '{local}' → helper_manager?", default=True):
                    continue
                info("Subiendo por SFTP…")
                success(api.install_helper_manager(local, replace=True))
                if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
                info(
                    "En HA: Ajustes > Dispositivos y Servicios > Añadir > "
                    "Horus Helper Manager."
                )
            elif op == "3":
                if not status.component_exists:
                    info("No hay nada que eliminar.")
                    continue
                if confirm("¿Eliminar custom_components/helper_manager?", default=False):
                    success(api.remove_helper_manager())
            elif op == "4":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_zwave_panel(api: HasControllerAPI) -> None:
    from paths import get_local_zwave_panel_source

    default_local = get_local_zwave_panel_source()
    while True:
        section("Z-Wave JS UI (panel lateral)")
        try:
            status = api.get_zwave_panel_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_zwave_panel(status)
        if status.installed:
            success("Panel Z-Wave JS UI presente (JS + panel_custom).")
        elif status.js_exists or status.yaml_ok:
            warning("Instalación incompleta: falta JS o panel_custom.")
        else:
            warning("El panel Z-Wave JS UI no está instalado en este controlador.")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Instalar / actualizar panel"),
                ("3", "Eliminar panel Z-Wave"),
                ("4", "Reiniciar Home Assistant"),
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
                local = ask_confirmed_path(
                    "panel_zwave_js_ui (o zwave-panel.js)",
                    default_local,
                )
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                if status.installed:
                    warning("Ya existe; se reemplazará el JS y se verificará el YAML.")
                if not confirm(f"¿Subir '{local}' y registrar panel_custom?", default=True):
                    continue
                info("Subiendo JS y parcheando configuration.yaml…")
                success(api.install_zwave_panel(local, restart=False))
                if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            elif op == "3":
                if not status.js_exists and not status.yaml_ok and not status.has_iframe_zwave:
                    info("No hay nada que eliminar.")
                    continue
                if confirm("¿Eliminar el panel Z-Wave (JS + YAML)?", default=False):
                    success(api.remove_zwave_panel())
            elif op == "4":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_ha_configuration(api: HasControllerAPI) -> None:
    while True:
        section("Actualizar Conectividad HTTP y Reverse Proxy")
        try:
            status = api.get_ha_configuration_status()
        except HasApiError as exc:
            error(str(exc))
            break

        panel_ha_configuration(status)
        if status.proxy_ok:
            success("trusted_proxies listos: el túnel no debería devolver 400.")
        else:
            warning("Faltan trusted_proxies. Al entrar por Cloudflare, HA responderá 400.")
        if status.uses_storage_http and status.has_http_block:
            warning("Queda bloque http: en YAML; en HAS nuevas se ignora, conviene quitarlo.")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Eliminar bloque http legado (YAML)"),
                ("3", "Aplicar trusted_proxies (.storage/http stable)"),
                ("4", "Reiniciar Home Assistant"),
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
                if status.http_ok and status.exists and not status.is_empty:
                    info("No hay bloque http legado. No se requieren cambios.")
                    continue
                if status.has_http_block:
                    warning("Se eliminará la sección 'http:' y se creará backup .bak.horus.")
                else:
                    warning("Se escribirá plantilla base de configuration.yaml sin bloque http.")
                if confirm("¿Aplicar limpieza de configuration.yaml?", default=False):
                    info("Escribiendo configuration.yaml…")
                    success(api.ensure_ha_http_config())
                    if confirm("¿Desea reiniciar Home Assistant ahora para aplicar cambios?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
            elif op == "3":
                warning(
                    "Se escribe en stable (no pending). Si nadie confirma en la UI, "
                    "pending se revierte a los 5 minutos y vuelve el 400."
                )
                if confirm("¿Aplicar trusted_proxies y reiniciar HA?", default=True):
                    info("Parcheando .storage/http y reiniciando HA…")
                    success(api.ensure_ha_trusted_proxies(restart=True))
            elif op == "4":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_ha_spaces(api: HasControllerAPI) -> None:
    while True:
        menu_options(
            "Gestión de Espacios",
            [
                ("1", "Gestión de backups / espacio en disco"),
                ("2", "Mantenimiento y limpieza sistemática"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_backup_manager(api)
        elif op == "2":
            menu_maintenance(api)
        else:
            warning("Opción no válida.")


def menu_ha_integrations(api: HasControllerAPI) -> None:
    while True:
        menu_options(
            "Gestor de Integraciones",
            [
                ("1", "plugin_service (conexion energy)"),
                ("2", "Admin Network (administrador de Redes)"),
                ("3", "Helper Manager (administrador de Auxiliares)"),
                ("4", "Z-Wave JS UI (panel lateral :8091)"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_plugin_service(api)
        elif op == "2":
            menu_admin_network(api)
        elif op == "3":
            menu_helper_manager(api)
        elif op == "4":
            menu_zwave_panel(api)
        else:
            warning("Opción no válida.")


def menu_ha_admin(api: HasControllerAPI) -> None:
    while True:
        menu_options(
            "Administrar Home Assistant",
            [
                ("1", "Usuarios (crear / resetear contraseña)"),
                ("2", "Gestión de Espacios (backups, limpieza, disco)"),
                ("3", "Configuración HTTP y Reverse Proxy (configuration.yaml)"),
                ("4", "Gestor de Integraciones (energy, red, auxiliares, zwave)"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_ha_users(api)
        elif op == "2":
            menu_ha_spaces(api)
        elif op == "3":
            menu_ha_configuration(api)
        elif op == "4":
            menu_ha_integrations(api)
        else:
            warning("Opción no válida.")


def menu_administrative(api: HasControllerAPI) -> None:
    while True:
        section("Configuración administrativa")
        menu_options(
            "Administrativa",
            [
                ("1", "Configurar hostname"),
                ("2", "Administrar Home Assistant"),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_hostname(api)
        elif op == "2":
            menu_ha_admin(api)
        else:
            warning("Opción no válida.")
