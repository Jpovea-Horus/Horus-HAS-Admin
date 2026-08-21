"""Flujo de conexión SSH."""

from __future__ import annotations

from dataclasses import dataclass

from controller import HasControllerAPI
from exceptions import SSHConnectionError
from session_store import load_hosts, remember_host
from ui import ask, ask_password, confirm, error, info, menu_options, section, success, warning


@dataclass
class ConnectMemory:
    host_input: str = ""
    user: str = "root"
    password: str = ""
    use_cloudflare: bool = False


memory = ConnectMemory()


def connect_flow(api: HasControllerAPI) -> bool:
    section("Conexión SSH al controlador")
    recent = load_hosts()
    opts = [
        ("1", "Local (LAN / IP directa)"),
        ("2", "Remota (Cloudflare Tunnel)"),
    ]
    if recent:
        opts.append(("H", "Elegir host reciente"))
    if memory.host_input and memory.password:
        opts.append(("C", "Reconectar último (misma contraseña)"))
    opts.extend([("R", "Refrescar"), ("0", "Salir")])
    menu_options("Tipo de conexión", opts)
    conn_type = ask("Opción", default="1").upper()

    if conn_type == "0":
        return False
    if conn_type == "R":
        return connect_flow(api)

    if conn_type == "C" and memory.host_input and memory.password:
        return _do_connect(
            api,
            memory.user,
            memory.host_input,
            memory.password,
            memory.use_cloudflare,
        )

    use_cloudflare = False
    host = ""
    user = "root"

    if conn_type == "H" and recent:
        menu_options(
            "Hosts recientes",
            [
                (
                    str(i),
                    f"{h.get('user', 'root')}@{h['host']} "
                    f"({'CF' if h.get('use_cloudflare') else 'LAN'})",
                )
                for i, h in enumerate(recent, 1)
            ],
        )
        raw = ask("Número")
        try:
            idx = int(raw) - 1
        except ValueError:
            error("Número inválido.")
            return False
        if idx < 0 or idx >= len(recent):
            error("Fuera de rango.")
            return False
        picked = recent[idx]
        host = str(picked["host"])
        user = str(picked.get("user") or "root")
        use_cloudflare = bool(picked.get("use_cloudflare"))
        info(f"Host: {user}@{host}")
    elif conn_type == "2":
        use_cloudflare = True
        info("Requiere cloudflared en el PATH (Access SSH).")
        info("Hostname completo: ssh-xx00.rhorus.com")
        host = ask("Hostname / ID del túnel SSH")
        if not host:
            error("Debe indicar un hostname o ID.")
            return False
        user = ask("Usuario", default="root")
    else:
        host = ask("IP del controlador", default=memory.host_input if not memory.use_cloudflare else "")
        if not host:
            error("Debe indicar la IP del controlador.")
            return False
        user = ask("Usuario", default=memory.user or "root")

    password = ask_password("Contraseña SSH")
    if not password:
        error("Contraseña vacía.")
        return False
    return _do_connect(api, user, host, password, use_cloudflare)


def _do_connect(
    api: HasControllerAPI,
    user: str,
    host: str,
    password: str,
    use_cloudflare: bool,
) -> bool:
    try:
        if use_cloudflare:
            info("Conectando vía Cloudflare Access… (puede abrir el navegador para login)")
        session = api.connect(user, host, password, use_cloudflare=use_cloudflare)
        mode = "Cloudflare" if use_cloudflare else "LAN"
        success(
            f"Conectado ({mode}) como [bold]{session.remote_user}[/bold] en {session.host}"
        )
        if not session.has_sudo and session.remote_user != "root":
            warning("Sin privilegios sudo. Algunas operaciones de red pueden fallar.")
        memory.host_input = host
        memory.user = user
        memory.password = password
        memory.use_cloudflare = use_cloudflare
        remember_host(host, user, use_cloudflare)
        return True
    except SSHConnectionError as exc:
        error(str(exc))
        return False


def reconnect_or_prompt(api: HasControllerAPI) -> bool:
    if memory.host_input and memory.password:
        info("Reconectando al último controlador…")
        if _do_connect(
            api,
            memory.user,
            memory.host_input,
            memory.password,
            memory.use_cloudflare,
        ):
            return True
        warning("Falló la reconexión automática.")
    if confirm("¿Abrir flujo de conexión?", default=True):
        return connect_flow(api)
    return False
