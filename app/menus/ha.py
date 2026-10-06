"""Menús Home Assistant: usuarios, plugin, integraciones, config."""

from __future__ import annotations

from controller import HasControllerAPI
from exceptions import HasApiError, ValidationError
from ui import (
    ask,
    ask_confirmed_path,
    ask_password,
    confirm,
    error,
    info,
    menu_options,
    panel_ha_configuration,
    panel_ha_users,
    panel_helper_manager,
    panel_hostname,
    panel_plugin_service,
    panel_admin_network,
    panel_yaml_content,
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
                ("2", "Crear usuario"),
                ("3", "Editar usuario (nombre / rol)"),
                ("4", "Ocultar / mostrar en Personas"),
                ("5", "Cambiar / resetear contraseña"),
                ("6", "Designar Owner"),
                ("7", "Eliminar usuario"),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                continue
            elif op == "2":
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
            elif op == "3":
                username = ask("Usuario (login HA)").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                target = next((u for u in status.users if u.username == username), None)
                if not target:
                    warning(f"'{username}' no aparece en el listado.")
                    continue
                if target.incomplete or not target.user_id:
                    error(
                        f"'{username}' está incompleto (sin id). "
                        "No se puede editar; elimínelo o créelo de nuevo."
                    )
                    continue
                info(
                    f"Actual: nombre='{target.name or '-'}', "
                    f"rol={'Owner' if target.is_owner else ('Admin' if target.is_admin else 'Usuario')}"
                )
                new_name_raw = ask(
                    "Nuevo nombre (vacío = no cambiar)",
                    default="",
                )
                new_name = new_name_raw.strip() if new_name_raw.strip() else None

                is_admin: bool | None = None
                if target.is_owner:
                    info("Owner: solo se puede cambiar el nombre (no el rol).")
                else:
                    if confirm("¿Cambiar el rol (Admin/Usuario)?", default=False):
                        is_admin = confirm(
                            "¿Dejarlo como administrador?",
                            default=target.is_admin,
                        )

                if new_name is None and is_admin is None:
                    info("Sin cambios.")
                    continue
                warning("Home Assistant se reiniciará para aplicar los cambios.")
                if confirm(f"¿Actualizar usuario '{username}' y reiniciar HA?", default=False):
                    info("Editando usuario y reiniciando Home Assistant…")
                    success(
                        api.update_ha_user(
                            username, new_name=new_name, is_admin=is_admin
                        )
                    )
            elif op == "4":
                username = ask("Usuario (login HA)").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                target = next((u for u in status.users if u.username == username), None)
                if not target:
                    warning(f"'{username}' no aparece en el listado.")
                    continue
                if target.incomplete or not target.user_id:
                    error(
                        f"'{username}' está incompleto (sin id). "
                        "No se puede ocultar ni mostrar."
                    )
                    continue
                visible = not target.in_people
                if visible:
                    info(f"'{username}' está oculto; se volverá a crear su persona.")
                    question = f"¿Mostrar '{username}' en Personas y reiniciar HA?"
                else:
                    warning(
                        f"Se quitará la persona de '{username}' (Ajustes → Personas). "
                        "El login sigue funcionando, pero se pierde la entidad person.* "
                        "(presencia, rastreadores y foto)."
                    )
                    question = f"¿Ocultar '{username}' de Personas y reiniciar HA?"
                if not confirm(question, default=False):
                    continue
                info("Aplicando cambio y reiniciando Home Assistant…")
                success(api.set_ha_user_people_visibility(username, visible))
            elif op == "5":
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
            elif op == "6":
                username = ask("Usuario a designar como Owner").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                target = next((u for u in status.users if u.username == username), None)
                if not target:
                    warning(f"'{username}' no aparece en el listado.")
                    continue
                if target.incomplete or not target.user_id:
                    error(
                        f"'{username}' está incompleto (sin id). "
                        "No se puede designar Owner."
                    )
                    continue
                if target.is_owner:
                    info(f"'{username}' ya es el Owner.")
                    continue
                current_owners = [
                    u.username for u in status.users if u.is_owner and u.username
                ]
                if current_owners:
                    warning(
                        f"Owner actual: {', '.join(current_owners)}. "
                        "Se le quitará el flag is_owner (seguirá existiendo como Admin/Usuario)."
                    )
                else:
                    warning("No hay Owner ahora; se designará uno.")
                warning(
                    f"'{username}' quedará como único Owner y administrador. "
                    "Home Assistant se reiniciará."
                )
                if not confirm(f"¿Designar Owner a '{username}'?", default=False):
                    continue
                typed = ask("Escriba OWNER para confirmar").strip()
                if typed != "OWNER":
                    info("Cancelado.")
                    continue
                info("Designando Owner y reiniciando Home Assistant…")
                success(api.set_ha_user_owner(username))
            elif op == "7":
                username = ask("Usuario a eliminar (login HA)").strip().lower()
                if not username:
                    warning("Debe indicar un usuario.")
                    continue
                target = next((u for u in status.users if u.username == username), None)
                if not target:
                    warning(f"'{username}' no aparece en el listado.")
                    continue
                if target.is_owner:
                    error(
                        "No se puede eliminar al usuario Owner. "
                        "Solo nombre o contraseña."
                    )
                    continue
                warning(
                    f"Se eliminará '{username}' "
                    f"(id={target.user_id or '—'}, persona vinculada si existe)."
                )
                if not confirm(f"¿ELIMINAR usuario '{username}'?", default=False):
                    continue
                typed = ask("Escriba ELIMINAR para confirmar").strip()
                if typed != "ELIMINAR":
                    info("Cancelado.")
                    continue
                warning("Home Assistant se reiniciará tras el borrado.")
                if not confirm("¿Continuar con el reinicio de HA?", default=True):
                    continue
                info("Eliminando usuario y reiniciando Home Assistant…")
                success(api.delete_ha_user(username))
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_plugin_service(api: HasControllerAPI) -> None:
    from paths import get_local_plugin_aws_credentials, get_local_plugin_source

    default_local = get_local_plugin_source()
    default_aws = get_local_plugin_aws_credentials()
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
        if not status.aws_credentials_exists:
            warning(
                "Falta plugin_service_aws_credentials en /config/ "
                "(v3 necesita el token de GitHub vía Secrets Manager)."
            )

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Subir / instalar desde carpeta local"),
                ("3", "Descargar e instalar desde GitHub (Recomendado)"),
                ("4", "Subir AWS credentials (1 vez)"),
                ("5", "Eliminar plugin_service"),
                ("6", "Reiniciar Home Assistant"),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                continue
            elif op == "2":
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
                _offer_aws_credentials_if_missing(api, default_aws)
                if confirm("¿Desea reiniciar Home Assistant ahora para aplicar cambios?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            elif op == "3":
                if not status.parent_exists:
                    error("No se puede instalar: falta custom_components/.")
                    continue
                if status.plugin_exists:
                    warning("Ya existe plugin_service; se reemplazará por la versión de GitHub.")
                    if not confirm("¿Reinstalar desde GitHub?", default=True):
                        continue
                
                info("Descargando desde GitHub y subiendo al controlador (puede tardar)…")
                try:
                    success(api.install_plugin_service_from_github(replace=True))
                    _offer_aws_credentials_if_missing(api, default_aws)
                    if confirm("¿Desea reiniciar Home Assistant ahora para aplicar cambios?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
                except Exception as exc:
                    error(f"Error al descargar de GitHub: {exc}")
            elif op == "4":
                local = ask_confirmed_path(
                    "plugin_service_aws_credentials", default_aws
                )
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_aws = local
                if status.aws_credentials_exists:
                    warning(
                        "Ya existe en /config/; por defecto no se reemplaza "
                        "(1 vez y para siempre)."
                    )
                    if not confirm("¿Reemplazar el archivo remoto?", default=False):
                        info("Sin cambios.")
                        continue
                    replace = True
                else:
                    if not confirm(
                        f"¿Subir '{local}' → /config/plugin_service_aws_credentials?",
                        default=True,
                    ):
                        continue
                    replace = False
                info("Subiendo AWS credentials…")
                success(api.ensure_plugin_aws_credentials(local, replace=replace))
            elif op == "5":
                if not status.plugin_exists:
                    info("No hay nada que eliminar.")
                    continue
                warning(f"Se ejecutará: rm -rf {status.plugin_dir}")
                if confirm("¿Eliminar plugin_service del controlador?", default=False):
                    info("Eliminando…")
                    success(api.remove_plugin_service())
            elif op == "6":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def _offer_aws_credentials_if_missing(api: HasControllerAPI, default_aws: str) -> None:
    """Tras instalar v3, avisa si faltan AWS credentials y ofrece subirlas."""
    try:
        st = api.get_plugin_service_status()
    except HasApiError:
        return
    if st.aws_credentials_exists:
        return
    warning(
        "Faltan AWS credentials en /config/. Sin ellas, v3 no puede leer el token "
        "de GitHub vía Secrets Manager."
    )
    if not confirm("¿Subir plugin_service_aws_credentials ahora?", default=True):
        return
    local = ask_confirmed_path("plugin_service_aws_credentials", default_aws)
    if not local:
        warning("Ruta vacía; se omite la subida.")
        return
    success(api.ensure_plugin_aws_credentials(local, replace=False))


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
            if status.ha_entry_configured:
                success("Host, integración HA y config entry presentes.")
            else:
                success("Host e integración HA presentes.")
                warning("Falta config entry en HA (use opción 6).")
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
                ("2", "Instalar TODO (host + integración HA + config entry) - Instalación completa del administrador"),
                ("3", "Instalar solo servicio host"),
                ("4", "Instalar solo integración HA"),
                ("5", "Mostrar API key (enmascarada / completa)"),
                ("6", "Configurar integración en HA (inyectar API key) - Vincula el host con Home Assistant"),
                ("7", "Reparar core.config_entries (KeyError discovery_keys) - Soluciona errores en la base de datos de HA"),
                ("8", "Diagnosticar WiFi (wlan unavailable) - Verifica el estado de la radio y la interfaz"),
                ("9", "Reparar WiFi ahora - Intenta recuperar la conexión inalámbrica"),
                ("10", "Eliminar TODO (integración + servicio host)"),
                ("11", "Eliminar solo servicio host"),
                ("12", "Eliminar solo integración HA"),
                ("13", "Reiniciar Home Assistant"),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                continue
            elif op == "2":
                local = ask_confirmed_path("admin_network", default_local)
                if not local:
                    warning("Ruta vacía.")
                    continue
                default_local = local
                warning(
                    "Se instalará el servicio en el SO (/opt/admin_network), "
                    "se copiará custom_components/admin_network y se inyectará "
                    "la API key en Home Assistant."
                )
                if not confirm("¿Instalar Admin Network completo?", default=True):
                    continue
                info("Subiendo host e integración (pip/venv puede tardar)…")
                success(api.install_admin_network(local, replace=True))
                if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
                info(
                    "Tras reiniciar, Admin Network debería aparecer ya configurado "
                    "en Dispositivos y Servicios (127.0.0.1:8765)."
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
                from local_config import mask_secret

                key = api.get_admin_network_api_key()
                info(f"API key (enmascarada): {mask_secret(key)}")
                if confirm("¿Mostrar la API key completa en pantalla?", default=False):
                    warning("Visible en pantalla / historial del terminal.")
                    success(f"API key: {key}")
            elif op == "6":
                if not status.host.env_exists and not status.host.api_key:
                    error("No hay API key en el host. Instale el servicio host primero.")
                    continue
                if confirm(
                    "¿Inyectar host/port/API key en core.config_entries de HA?",
                    default=True,
                ):
                    success(api.configure_admin_network_ha_entry(force=True))
                    if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
            elif op == "7":
                warning(
                    "NO borre core.config_entries completo (perdería todas las "
                    "integraciones). Esta reparación solo añade discovery_keys/subentries "
                    "faltantes a las entries existentes."
                )
                if confirm("¿Reparar schema de core.config_entries ahora?", default=True):
                    success(api.repair_ha_config_entries())
                    if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
            elif op == "8":
                info("Diagnosticando WiFi…")
                diag = api.diagnose_wifi()
                if diag.healthy:
                    success(f"{diag.detail} (radio={diag.radio})")
                else:
                    warning(f"{diag.detail} (radio={diag.radio})")
                if diag.devices_raw:
                    info(diag.devices_raw)
                if not status.host.wifi_watchdog_active:
                    warning(
                        "Watchdog WiFi no activo. Reinstale el servicio host "
                        "para prevenir caídas overnight."
                    )
            elif op == "9":
                warning(
                    "Puede reiniciar NetworkManager (eth/ZT se reconectan solos). "
                    "La sesión SSH por ZeroTier/LAN suele recuperarse."
                )
                if confirm("¿Reparar WiFi ahora?", default=True):
                    info("Aplicando recovery WiFi…")
                    success(api.repair_wifi(force_nm_restart=True))
            elif op == "10":
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
            elif op == "11":
                if not status.host.dir_exists and not status.host.service_active:
                    info("No hay servicio host que eliminar.")
                    continue
                wipe = confirm("¿Borrar también /etc/admin_network.env (API key)?", default=False)
                if confirm("¿Eliminar servicio host admin_network?", default=False):
                    success(api.remove_admin_network_host(wipe_env=wipe))
            elif op == "12":
                if not status.ha.component_exists and not status.ha_entry_configured:
                    info("No hay integración HA que eliminar.")
                    continue
                if confirm(
                    "¿Eliminar custom_components/admin_network y su config entry?",
                    default=False,
                ):
                    success(api.remove_admin_network_ha())
            elif op == "13":
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
        section("Configuración HTTP, Proxy y Discovery")
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
        if status.discovery_enabled:
            warning(
                "Discovery activo: HAS escanea la red (zeroconf/ssdp/dhcp) "
                "y puede llenar 'Discovered'."
            )
        else:
            success("Discovery desactivado: no se escanean dispositivos nuevos en red.")
        if status.core_missing:
            warning(
                f"Faltan integraciones core ({', '.join(status.core_missing)}). "
                "Restáurelas en Home Assistant → Gestor de "
                "Integraciones → 'Restaurar integraciones core' (no use la opción 5 "
                "de este menú: esa activa Discovery)."
            )
        if status.yaml_includes_ok:
            success("Includes automation/script/scene OK.")
        else:
            warning(
                "Includes incompletos: las automatizaciones pueden dar timeout al guardar. "
                "Use opción 9 para reparar."
            )
            for issue in status.yaml_issues[:4]:
                warning(f"  • {issue}")

        menu_options(
            "Acciones",
            [
                ("1", "Actualizar verificación"),
                ("2", "Eliminar bloque http legado (YAML) - Quita config antigua de red/proxy en configuration.yaml"),
                ("3", "Aplicar trusted_proxies (.storage/http stable) - Corrige error 400 al usar proxy o Cloudflare"),
                ("4", "Reiniciar Home Assistant"),
                ("", ""),
                (
                    "5",
                    "Desactivar Discovery (escaneo de red) - Detiene búsqueda automática de nuevos dispositivos"
                    if status.discovery_enabled
                    else "Activar Discovery (escaneo de red) - Permite detectar nuevos dispositivos automáticamente",
                ),
                ("6", "Ver configuration.yaml"),
                ("7", "Ver automations.yaml"),
                ("8", "Validar sintaxis (hass check_config) - Verifica errores en el YAML antes de reiniciar"),
                ("9", "Reparar includes (automation/script/scene) - Asegura que los archivos externos estén vinculados"),
                ("10", "Reparar registro HA (Fix KeyError discovery_keys) - Soluciona errores internos de la base de datos de HA"),
                ("", ""),
                ("11", "Eliminar YAML (submenú)"),
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
            elif op == "5":
                if status.discovery_enabled:
                    warning(
                        "Se reemplazará default_config por componentes sin "
                        "dhcp/ssdp/zeroconf/usb/bluetooth. "
                        "Se creará backup .bak.horus.discovery y se reiniciará HA."
                    )
                    if confirm("¿Desactivar Discovery (escaneo de red)?", default=False):
                        info("Desactivando Discovery y reiniciando HA…")
                        success(api.set_ha_discovery(enabled=False, restart=True))
                else:
                    warning(
                        "Se restaurará default_config (incluye escaneo de red). "
                        "Se reiniciará HA."
                    )
                    if confirm("¿Activar Discovery de nuevo?", default=False):
                        info("Activar Discovery de nuevo y reiniciar HA…")
                        success(api.set_ha_discovery(enabled=True, restart=True))
            elif op == "6":
                info("Leyendo configuration.yaml…")
                path, content = api.get_ha_configuration_yaml()
                panel_yaml_content(content, path)
                ask("Pulse Enter para continuar")
            elif op == "7":
                info("Leyendo automations.yaml…")
                path, content = api.get_ha_automations_yaml()
                panel_yaml_content(content, path)
                ask("Pulse Enter para continuar")
            elif op == "8":
                info(
                    "Validando con hass --script check_config "
                    "(puede tardar 1–3 min; útil tras timeout de automatizaciones)…"
                )
                result = api.check_ha_config()
                if result.startswith("Errores detectados"):
                    error(result)
                    warning(
                        "Corrija el YAML indicado, luego use opción 8 de nuevo "
                        "y recargue/reinicie HA para que las automatizaciones aparezcan."
                    )
                else:
                    success(result)
                if confirm("¿Reiniciar Home Assistant ahora?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
                ask("Pulse Enter para continuar")
            elif op == "9":
                warning(
                    "Se añadirán includes faltantes y se crearán "
                    "automations/scripts/scenes.yaml vacíos si no existen. "
                    "Backup: .bak.horus.includes"
                )
                if confirm("¿Reparar includes ahora?", default=True):
                    info("Reparando includes…")
                    success(api.ensure_ha_yaml_includes(restart=False))
                    if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                        info("Reiniciando HA…")
                        success(api.restart_ha())
            elif op == "10":
                warning(
                    "Esta reparación añade discovery_keys faltantes al registro de HA. "
                    "Soluciona integraciones que no aparecen."
                )
                if confirm("¿Reparar registro de integraciones y reiniciar HA?", default=True):
                    info("Reparando schema de core.config_entries…")
                    success(api.repair_ha_config_entries())
                    info("Reiniciando Home Assistant para aplicar cambios…")
                    success(api.restart_ha())
            elif op == "11":
                _menu_delete_ha_yaml(api)
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def _menu_delete_ha_yaml(api: HasControllerAPI) -> None:
    """Submenú: eliminar YAML de /config con backup."""
    while True:
        section("Eliminar YAML de Home Assistant")
        menu_options(
            "¿Qué archivo eliminar?",
            [
                ("1", "automations.yaml"),
                ("2", "scripts.yaml"),
                ("3", "scenes.yaml"),
                ("4", "configuration.yaml (peligroso)"),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        mapping = {
            "1": "automations.yaml",
            "2": "scripts.yaml",
            "3": "scenes.yaml",
            "4": "configuration.yaml",
        }
        filename = mapping.get(op)
        if not filename:
            warning("Opción no válida.")
            continue
        try:
            warning(f"Se creará backup .bak.horus.delete.* y se eliminará {filename}.")
            if filename == "configuration.yaml":
                warning(
                    "Sin configuration.yaml HA puede no arrancar bien. "
                    "Se recomienda recrear la plantilla base después."
                )
            if not confirm(f"¿Eliminar {filename}?", default=False):
                continue
            typed = ask("Escriba ELIMINAR para confirmar").strip()
            if typed != "ELIMINAR":
                info("Cancelado.")
                continue
            recreate = False
            if filename == "configuration.yaml":
                recreate = confirm(
                    "¿Recrear plantilla base de configuration.yaml con includes?",
                    default=True,
                )
            info(f"Eliminando {filename}…")
            success(api.delete_ha_yaml_file(filename, recreate_config=recreate))
            if confirm("¿Reiniciar Home Assistant ahora?", default=True):
                info("Reiniciando HA…")
                success(api.restart_ha())
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_ha_integrations(api: HasControllerAPI) -> None:
    while True:
        try:
            status = api.get_ha_configuration_status()
        except HasApiError as exc:
            error(str(exc))
            break

        section("Gestor de Integraciones")

        if status.core_missing:
            warning(
                "Faltan integraciones core (Historial, Logbook, Energía…): "
                "no aparecen en HA. Use opción 5 para restaurarlas sin Discovery."
            )

        menu_options(
            "Gestor de Integraciones",
            [
                ("1", "plugin_service (conexion energy)"),
                ("2", "Admin Network (administrador de Redes)"),
                ("3", "Helper Manager (administrador de Auxiliares)"),
                ("4", "Z-Wave JS UI (panel lateral :8091)"),
                ("", ""),
                ("5", "Restaurar integraciones core sin Discovery - Recupera Historial, Logbook, Energía y Backup"),
                ("6", "Reparar registro HA (Fix KeyError discovery_keys)"),
                ("", ""),
                ("7", "Reiniciar Home Assistant"),
                ("", ""),
                ("0", "Volver"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        try:
            if op == "1":
                menu_plugin_service(api)
            elif op == "2":
                menu_admin_network(api)
            elif op == "3":
                menu_helper_manager(api)
            elif op == "4":
                menu_zwave_panel(api)
            elif op == "5":
                if not status.core_missing:
                    info("Las integraciones core ya están cargadas. No se requieren cambios.")
                    continue
                warning(
                    f"Se añadirán: {', '.join(status.core_missing)} "
                    "(sin dhcp/ssdp/zeroconf/usb/bluetooth). "
                    "Backup .bak.horus.core y reinicio de HA."
                )
                if confirm("¿Restaurar integraciones core y reiniciar HA?", default=True):
                    info("Escribiendo configuration.yaml y reiniciando HA…")
                    success(api.restore_ha_core_integrations(restart=True))
            elif op == "6":
                warning(
                    "Esta reparación añade discovery_keys faltantes al registro de HA. "
                    "Soluciona integraciones que no aparecen."
                )
                if confirm("¿Reparar registro de integraciones y reiniciar HA?", default=True):
                    info("Reparando schema de core.config_entries…")
                    success(api.repair_ha_config_entries())
                    info("Reiniciando Home Assistant para aplicar cambios…")
                    success(api.restart_ha())
            elif op == "7":
                if confirm("¿Reiniciar Home Assistant?", default=False):
                    info("Reiniciando HA…")
                    success(api.restart_ha())
            else:
                warning("Opción no válida.")
        except ValidationError as exc:
            error(str(exc))
        except HasApiError as exc:
            error(str(exc))


def menu_ha_admin(api: HasControllerAPI) -> None:
    while True:
        section("Home Assistant")
        menu_options(
            "Administrar Home Assistant",
            [
                ("1", "Usuarios (crear / editar / eliminar / Owner / Personas)"),
                ("2", "Configuración HTTP, Proxy y Discovery"),
                ("3", "Gestor de Integraciones (energy, red, auxiliares, zwave)"),
                ("", ""),
                ("0", "Volver al menú principal"),
            ],
        )
        op = ask("Opción")
        if op == "0":
            break
        if op == "1":
            menu_ha_users(api)
        elif op == "2":
            menu_ha_configuration(api)
        elif op == "3":
            menu_ha_integrations(api)
        else:
            warning("Opción no válida.")
