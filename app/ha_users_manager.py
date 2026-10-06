"""Gestión de usuarios de Home Assistant (Docker) vía SSH."""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from models import HaUser, HaUsersStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

from paths import REMOTE_CONFIG_DIR

_CONFIG_CANDIDATES = (
    REMOTE_CONFIG_DIR,
    "/opt/homeassistant/config",
    "/srv/homeassistant/config",
    "/srv/homeassistant",
)
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_AUTH_TIMEOUT = 180
_RESTART_WAIT_SEC = 25


class HaUsersManager:
    """Lista y administra logins de Home Assistant en contenedor Docker."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh
        self._container: str = ""
        self._config_path: str = ""

    def _detect_container(self) -> str:
        if self._container:
            return self._container

        result = self.ssh.run(
            "docker ps -a --format '{{.Names}}' 2>/dev/null "
            "| grep -iE 'homeassistant|home-assistant' | head -1"
        )
        name = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if name:
            self._container = name
        return self._container

    def _detect_version(self, container: str) -> str:
        """Obtiene la versión de Home Assistant ejecutando hass --version en el contenedor."""
        if not container:
            return ""
        result = self.ssh.run(f"docker exec {shlex.quote(container)} hass --version")
        if result.ok:
            return result.stdout.strip()
        return ""

    def _require_container(self) -> str:
        container = self._detect_container()
        if not container:
            raise SSHCommandError(
                "No se encontró el contenedor Docker de Home Assistant.",
                exit_code=1,
                stderr="",
            )
        return container

    def _detect_config_path(self) -> str:
        if self._config_path:
            return self._config_path

        container = self._detect_container()
        if container:
            inspect = self.ssh.run(
                f"docker inspect {shlex.quote(container)} "
                "--format '{{range .Mounts}}{{.Destination}}|{{.Source}}{{\"\\n\"}}{{end}}'"
            )
            for line in inspect.stdout.splitlines():
                if "|" not in line:
                    continue
                dest, source = line.split("|", 1)
                if dest.strip() == "/config" and source.strip():
                    self._config_path = source.strip()
                    return self._config_path

        for candidate in _CONFIG_CANDIDATES:
            check = self.ssh.run(
                f"test -f {shlex.quote(candidate)}/.storage/auth && echo OK"
            )
            if check.stdout.strip() == "OK":
                self._config_path = candidate
                return candidate

        if container:
            self._config_path = "/config"
            return self._config_path

        return ""

    def _restart_homeassistant(self, container: str) -> None:
        """Reinicia HA para cargar auth en memoria (obligatorio tras editar .storage)."""
        stop = self.ssh.run(
            f"docker restart {shlex.quote(container)}", timeout=_AUTH_TIMEOUT
        )
        if not stop.ok:
            msg = stop.stderr or stop.stdout or "No se pudo reiniciar Home Assistant."
            raise SSHCommandError(msg, exit_code=stop.exit_code, stderr=stop.stderr)

        self._wait_homeassistant(container)

    def _wait_homeassistant(self, container: str) -> None:
        # Esperar a que el proceso acepte conexiones (auth ya en memoria)
        self.ssh.run(
            f"for i in $(seq 1 {_RESTART_WAIT_SEC}); do "
            f"docker exec {shlex.quote(container)} true 2>/dev/null && break; "
            "sleep 1; done",
            timeout=_AUTH_TIMEOUT,
        )
        self.ssh.run("sleep 8", timeout=20)

    def _host_auth_path(self) -> str:
        """Ruta de .storage/auth en el host (necesaria para editar con HA parado)."""
        config = self._detect_config_path()
        if not config.startswith("/"):
            return ""
        path = f"{config}/.storage/auth"
        check = self.ssh.run(f"test -f {shlex.quote(path)} && echo OK", use_sudo=True)
        return path if check.stdout.strip() == "OK" else ""

    def _run_with_ha_stopped(self, container: str, script: str) -> list[str]:
        """Para HA, ejecuta el script en el host y vuelve a arrancar HA.

        Con HA encendido, al apagarse vuelca su auth en memoria sobre
        .storage/auth y puede deshacer la edición.
        """
        stop = self.ssh.run(f"docker stop {shlex.quote(container)}", timeout=_AUTH_TIMEOUT)
        if not stop.ok:
            msg = stop.stderr or stop.stdout or "No se pudo detener Home Assistant."
            raise SSHCommandError(msg, exit_code=stop.exit_code, stderr=stop.stderr)

        result = self.ssh.run(
            f"python3 -c {shlex.quote(script)}", timeout=_AUTH_TIMEOUT, use_sudo=True
        )
        start = self.ssh.run(f"docker start {shlex.quote(container)}", timeout=_AUTH_TIMEOUT)
        if not start.ok:
            msg = start.stderr or start.stdout or "No se pudo arrancar Home Assistant."
            raise SSHCommandError(
                f"{msg} (ejecute 'docker start {container}' manualmente)",
                exit_code=start.exit_code,
                stderr=start.stderr,
            )
        self._wait_homeassistant(container)

        lines = result.stdout.splitlines()
        if not lines:
            msg = result.stderr or "Sin respuesta del script en el host."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        return lines

    def get_status(self) -> HaUsersStatus:
        status = HaUsersStatus()
        container = self._detect_container()
        config = self._detect_config_path()
        status.container_name = container
        status.version = self._detect_version(container)
        status.config_path = config

        if not container:
            status.error = "No se encontró el contenedor Docker de Home Assistant."
            return status
        if not config:
            status.error = "No se encontró la ruta de configuración de Home Assistant."
            return status

        try:
            status.users = self.list_users()
            orphans = [u for u in status.users if not u.user_id or u.incomplete]
            if orphans:
                names = ", ".join(u.username or "?" for u in orphans)
                status.error = (
                    f"Hay logins incompletos (sin id de usuario HA): {names}. "
                    "No servirán en la UI hasta recrearlos correctamente."
                )
        except SSHCommandError as exc:
            status.error = str(exc)
        return status

    def list_users(self) -> list[HaUser]:
        container = self._require_container()

        script = r"""
import json
auth_path = "/config/.storage/auth"
prov_path = "/config/.storage/auth_provider.homeassistant"
try:
    with open(auth_path) as f:
        auth = json.load(f)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

prov_users = []
try:
    with open(prov_path) as f:
        prov = json.load(f)
    prov_users = (prov.get("data") or {}).get("users") or []
except Exception:
    pass

person_uids = set()
try:
    with open("/config/.storage/person") as f:
        person = json.load(f)
    for it in (person.get("data") or {}).get("items") or []:
        if it.get("user_id"):
            person_uids.add(it["user_id"])
except Exception:
    pass

users = (auth.get("data") or {}).get("users") or []
creds = (auth.get("data") or {}).get("credentials") or []
by_id = {}
usernames_linked = set()
for c in creds:
    if c.get("auth_provider_type") != "homeassistant":
        continue
    uid = (c.get("user_id") or "").strip()
    uname = ((c.get("data") or {}).get("username") or "").strip()
    if uid and uname:
        by_id[uid] = uname
        usernames_linked.add(uname)

print("OK")
for u in users:
    if u.get("system_generated"):
        continue
    uid = (u.get("id") or "").strip()
    username = by_id.get(uid, "")
    name = (u.get("name") or "").replace("\t", " ").replace("\n", " ")
    is_owner = bool(u.get("is_owner"))
    is_active = bool(u.get("is_active", True))
    groups = u.get("group_ids") or []
    is_admin = "system-admin" in groups or is_owner
    incomplete = "0" if uid and username else "1"
    print("\t".join([
        uid,
        username,
        name,
        "1" if is_owner else "0",
        "1" if is_active else "0",
        "1" if is_admin else "0",
        incomplete,
        "1" if uid in person_uids else "0",
    ]))

for pu in prov_users:
    uname = (pu.get("username") or "").strip()
    if uname and uname not in usernames_linked:
        print("\t".join(["", uname, "(solo password, sin id)", "0", "1", "0", "1", "0"]))
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
            timeout=_AUTH_TIMEOUT,
        )
        # Si el contenedor está reiniciando, reintentar una vez
        if not result.ok and "is not running" in (result.stderr or "").lower():
            self.ssh.run("sleep 5", timeout=10)
            result = self.ssh.run(
                f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
                timeout=_AUTH_TIMEOUT,
            )

        lines = result.stdout.splitlines()
        if not lines or lines[0] != "OK":
            detail = result.stderr or result.stdout or "No se pudo leer .storage/auth"
            raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)

        users: list[HaUser] = []
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            users.append(
                HaUser(
                    user_id=parts[0],
                    username=parts[1],
                    name=parts[2],
                    is_owner=parts[3] == "1",
                    is_active=parts[4] == "1",
                    is_admin=parts[5] == "1",
                    incomplete=parts[6] == "1",
                    in_people=len(parts) > 7 and parts[7] == "1",
                )
            )
        return users

    def _validate_username(self, username: str) -> str:
        name = username.strip().lower()
        if not _USERNAME_RE.match(name):
            raise ValidationError(
                "Usuario inválido. Use minúsculas, números, punto, guion o guion bajo "
                "(sin espacios; máx. 32 caracteres)."
            )
        return name

    def _validate_password(self, password: str) -> str:
        if len(password) < 6:
            raise ValidationError("La contraseña debe tener al menos 6 caracteres.")
        return password

    def find_user(self, username: str) -> HaUser | None:
        name = username.strip().lower()
        for user in self.list_users():
            if user.username == name:
                return user
        return None

    def change_password(self, username: str, new_password: str) -> str:
        user = self._validate_username(username)
        pwd = self._validate_password(new_password)
        container = self._require_container()

        existing = self.find_user(user)
        if existing and existing.incomplete:
            raise ValidationError(
                f"El login '{user}' está incompleto (sin id). "
                "Elimínelo/recréelo; no se puede resetear de forma segura."
            )

        cmd = (
            f"docker exec {shlex.quote(container)} "
            f"hass --script auth --config /config change_password "
            f"{shlex.quote(user)} {shlex.quote(pwd)}"
        )
        result = self.ssh.run(cmd, timeout=_AUTH_TIMEOUT)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if not result.ok or "User not found" in out or "User not found" in err:
            msg = err or out or "No se pudo cambiar la contraseña."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)

        # HA en memoria no ve el cambio hasta reiniciar
        self._restart_homeassistant(container)
        return (
            f"Contraseña actualizada para '{user}'. "
            "Home Assistant reiniciado para aplicar el cambio."
        )

    def update_user(
        self,
        username: str,
        *,
        new_name: str | None = None,
        is_admin: bool | None = None,
    ) -> str:
        """Edita nombre y/o rol (Admin/Usuario). Owner: solo nombre."""
        user = self._validate_username(username)
        container = self._require_container()

        existing = self.find_user(user)
        if not existing:
            raise ValidationError(f"El usuario '{user}' no existe.")
        if existing.incomplete or not existing.user_id:
            raise ValidationError(
                f"El login '{user}' está incompleto (sin id). No se puede editar."
            )
        if new_name is None and is_admin is None:
            raise ValidationError("Indique al menos un cambio: nombre o rol.")

        name_to_set: str | None = None
        if new_name is not None:
            name_to_set = new_name.strip()
            if not name_to_set:
                raise ValidationError("El nombre no puede quedar vacío.")
            if len(name_to_set) > 64:
                raise ValidationError("El nombre no puede superar 64 caracteres.")

        if is_admin is not None and existing.is_owner:
            raise ValidationError(
                "No se puede cambiar el rol del usuario Owner. "
                "Solo se permite editar su nombre o contraseña."
            )

        payload = json.dumps(
            {
                "username": user,
                "user_id": existing.user_id,
                "new_name": name_to_set,
                "is_admin": is_admin,
            }
        )
        script = f"""
import json, os
req = json.loads({json.dumps(payload)})
username = req["username"]
user_id = req["user_id"]
new_name = req.get("new_name")
is_admin = req.get("is_admin")

auth_path = "/config/.storage/auth"
person_path = "/config/.storage/person"

try:
    with open(auth_path) as f:
        auth = json.load(f)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

users = (auth.get("data") or {{}}).get("users") or []
target = None
for u in users:
    if (u.get("id") or "").strip() == user_id:
        target = u
        break
if target is None:
    print("ERR")
    print("user_id not found in auth")
    raise SystemExit(0)

if target.get("is_owner") and is_admin is not None:
    print("ERR_OWNER")
    print("cannot change owner role")
    raise SystemExit(0)

changed = []
if new_name is not None:
    target["name"] = new_name
    changed.append("name")
if is_admin is not None:
    target["group_ids"] = ["system-admin"] if is_admin else ["system-users"]
    changed.append("role")

person = None
person_changed = False
if new_name is not None and os.path.isfile(person_path):
    try:
        with open(person_path) as f:
            person = json.load(f)
    except Exception:
        person = None
    if person is not None:
        items = (person.get("data") or {{}}).get("items") or []
        for it in items:
            if it.get("user_id") == user_id:
                it["name"] = new_name
                person_changed = True
                break

def atomic_write(path, data):
    tmp = path + ".tmp_horus"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\\n")
    os.replace(tmp, path)

try:
    atomic_write(auth_path, auth)
    if person is not None and person_changed:
        atomic_write(person_path, person)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

print("OK")
print(",".join(changed) if changed else "none")
print("admin" if "system-admin" in (target.get("group_ids") or []) else "user")
print(target.get("name") or "")
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
            timeout=_AUTH_TIMEOUT,
        )
        lines = result.stdout.splitlines()
        if not lines:
            msg = result.stderr or "Sin respuesta al editar el usuario."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        if lines[0] == "ERR_OWNER":
            raise ValidationError("No se puede cambiar el rol del usuario Owner.")
        if lines[0] != "OK":
            detail = result.stderr or "\n".join(lines) or "Fallo al escribir .storage"
            raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)

        self._restart_homeassistant(container)

        verified = self.find_user(user)
        if not verified:
            raise SSHCommandError(
                f"Tras reiniciar HA, '{user}' no aparece. Revise .storage/auth.",
                exit_code=1,
                stderr="",
            )
        parts: list[str] = []
        if name_to_set is not None:
            parts.append(f"nombre='{verified.name}'")
        if is_admin is not None:
            role = "administrador" if verified.is_admin else "usuario estándar"
            parts.append(f"rol={role}")
        detail = ", ".join(parts) if parts else "sin cambios"
        return (
            f"Usuario '{user}' actualizado ({detail}). "
            "Home Assistant reiniciado para aplicar el cambio."
        )

    def delete_user(self, username: str) -> str:
        """Elimina usuario (auth + credenciales + password + persona). Bloquea Owner."""
        user = self._validate_username(username)
        container = self._require_container()

        existing = self.find_user(user)
        if not existing:
            raise ValidationError(f"El usuario '{user}' no existe.")
        if existing.is_owner:
            raise ValidationError(
                "No se puede eliminar al usuario Owner. "
                "Es el propietario de la instancia Home Assistant."
            )
        if existing.incomplete and not existing.user_id:
            # Solo entrada huérfana en auth_provider: limpiar password
            payload = json.dumps({"username": user, "user_id": "", "orphan_only": True})
        else:
            if not existing.user_id:
                raise ValidationError(
                    f"El login '{user}' no tiene id. No se puede eliminar de forma segura."
                )
            payload = json.dumps(
                {"username": user, "user_id": existing.user_id, "orphan_only": False}
            )

        script = f"""
import json, os
req = json.loads({json.dumps(payload)})
username = req["username"]
user_id = (req.get("user_id") or "").strip()
orphan_only = bool(req.get("orphan_only"))

auth_path = "/config/.storage/auth"
prov_path = "/config/.storage/auth_provider.homeassistant"
person_path = "/config/.storage/person"

try:
    with open(auth_path) as f:
        auth = json.load(f)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

prov = None
try:
    with open(prov_path) as f:
        prov = json.load(f)
except Exception:
    prov = None

auth.setdefault("data", {{}})
users = auth["data"].setdefault("users", [])
creds = auth["data"].setdefault("credentials", [])

if not orphan_only:
    for u in users:
        if (u.get("id") or "").strip() == user_id and u.get("is_owner"):
            print("ERR_OWNER")
            print("cannot delete owner")
            raise SystemExit(0)
    auth["data"]["users"] = [
        u for u in users if (u.get("id") or "").strip() != user_id
    ]
    auth["data"]["credentials"] = [
        c for c in creds
        if not (
            c.get("auth_provider_type") == "homeassistant"
            and (
                (c.get("user_id") or "").strip() == user_id
                or ((c.get("data") or {{}}).get("username") or "").strip() == username
            )
        )
    ]
else:
    auth["data"]["credentials"] = [
        c for c in creds
        if not (
            c.get("auth_provider_type") == "homeassistant"
            and ((c.get("data") or {{}}).get("username") or "").strip() == username
        )
    ]

if prov is not None:
    prov.setdefault("data", {{}})
    prov_users = prov["data"].setdefault("users", [])
    prov["data"]["users"] = [
        pu for pu in prov_users
        if (pu.get("username") or "").strip() != username
    ]

person = None
if user_id and os.path.isfile(person_path):
    try:
        with open(person_path) as f:
            person = json.load(f)
    except Exception:
        person = None
    if person is not None:
        person.setdefault("data", {{}})
        items = person["data"].setdefault("items", [])
        person["data"]["items"] = [
            it for it in items if it.get("user_id") != user_id
        ]

def atomic_write(path, data):
    tmp = path + ".tmp_horus"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\\n")
    os.replace(tmp, path)

try:
    atomic_write(auth_path, auth)
    if prov is not None:
        atomic_write(prov_path, prov)
    if person is not None:
        atomic_write(person_path, person)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

print("OK")
print(user_id or "orphan")
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
            timeout=_AUTH_TIMEOUT,
        )
        lines = result.stdout.splitlines()
        if not lines:
            msg = result.stderr or "Sin respuesta al eliminar el usuario."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        if lines[0] == "ERR_OWNER":
            raise ValidationError("No se puede eliminar al usuario Owner.")
        if lines[0] != "OK":
            detail = result.stderr or "\n".join(lines) or "Fallo al escribir .storage"
            raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)

        self._restart_homeassistant(container)

        still = self.find_user(user)
        if still and not still.incomplete:
            raise SSHCommandError(
                f"Tras reiniciar HA, '{user}' sigue apareciendo. Revise .storage/auth.",
                exit_code=1,
                stderr="",
            )
        return (
            f"Usuario '{user}' eliminado. "
            "Home Assistant reiniciado para aplicar el cambio."
        )

    def set_people_visibility(self, username: str, visible: bool) -> str:
        """Oculta/muestra el usuario en Ajustes → Personas (el login no cambia)."""
        user = self._validate_username(username)
        container = self._require_container()

        existing = self.find_user(user)
        if not existing:
            raise ValidationError(f"El usuario '{user}' no existe.")
        if existing.incomplete or not existing.user_id:
            raise ValidationError(
                f"El login '{user}' está incompleto (sin id). No se puede modificar."
            )
        if existing.in_people == visible:
            estado = "visible" if visible else "oculto"
            return f"'{user}' ya está {estado} en Personas. No se requieren cambios."

        payload = json.dumps(
            {
                "username": user,
                "user_id": existing.user_id,
                "name": existing.name or user,
                "visible": bool(visible),
            }
        )
        script = f"""
import json, os, re
req = json.loads({json.dumps(payload)})
username = req["username"]
user_id = req["user_id"]
name = req["name"]
visible = bool(req["visible"])

person_path = "/config/.storage/person"
person = None
if os.path.isfile(person_path):
    try:
        with open(person_path) as f:
            person = json.load(f)
    except Exception as exc:
        print("ERR")
        print(exc)
        raise SystemExit(0)
if person is None:
    person = {{
        "version": 2,
        "minor_version": 1,
        "key": "person",
        "data": {{"items": []}},
    }}
person.setdefault("data", {{}})
items = person["data"].setdefault("items", [])

if visible:
    if any(it.get("user_id") == user_id for it in items):
        print("OK")
        print("none")
        raise SystemExit(0)
    slug = re.sub(r"[^a-z0-9_]+", "_", username)[:32] or user_id[:8]
    existing_ids = {{(it.get("id") or "") for it in items}}
    base_slug = slug
    n = 1
    while slug in existing_ids:
        slug = f"{{base_slug}}_{{n}}"
        n += 1
    items.append({{
        "id": slug,
        "name": name,
        "user_id": user_id,
        "device_trackers": [],
        "picture": None,
    }})
else:
    person["data"]["items"] = [it for it in items if it.get("user_id") != user_id]

tmp = person_path + ".tmp_horus"
try:
    with open(tmp, "w") as f:
        json.dump(person, f, indent=2)
        f.write("\\n")
    os.replace(tmp, person_path)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

print("OK")
print("shown" if visible else "hidden")
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
            timeout=_AUTH_TIMEOUT,
        )
        lines = result.stdout.splitlines()
        if not lines:
            msg = result.stderr or "Sin respuesta al modificar Personas."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        if lines[0] != "OK":
            detail = result.stderr or "\n".join(lines) or "Fallo al escribir .storage/person"
            raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)

        self._restart_homeassistant(container)

        verified = self.find_user(user)
        if not verified or verified.in_people != visible:
            raise SSHCommandError(
                f"Tras reiniciar HA, la visibilidad de '{user}' en Personas no cambió. "
                "Revise .storage/person.",
                exit_code=1,
                stderr="",
            )
        if visible:
            return (
                f"'{user}' vuelve a aparecer en Ajustes → Personas. "
                "Home Assistant reiniciado para aplicar el cambio."
            )
        return (
            f"'{user}' oculto de Ajustes → Personas (el login sigue funcionando). "
            "Home Assistant reiniciado para aplicar el cambio."
        )

    def set_owner(self, username: str) -> str:
        """Designa un usuario como único Owner (is_owner + admin)."""
        user = self._validate_username(username)
        container = self._require_container()

        existing = self.find_user(user)
        if not existing:
            raise ValidationError(f"El usuario '{user}' no existe.")
        if existing.incomplete or not existing.user_id:
            raise ValidationError(
                f"El login '{user}' está incompleto (sin id). "
                "No se puede designar Owner; elimínelo o créelo de nuevo."
            )
        if existing.is_owner:
            return f"'{user}' ya es el Owner. No se requieren cambios."

        previous_owners = [
            u.username for u in self.list_users() if u.is_owner and u.username
        ]

        host_auth = self._host_auth_path()
        payload = json.dumps(
            {
                "username": user,
                "user_id": existing.user_id,
                "auth_path": host_auth or "/config/.storage/auth",
            }
        )
        script = f"""
import json, os
req = json.loads({json.dumps(payload)})
username = req["username"]
user_id = req["user_id"]

auth_path = req["auth_path"]
try:
    with open(auth_path) as f:
        auth = json.load(f)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

users = (auth.get("data") or {{}}).get("users") or []
found = False
prev = []
for u in users:
    if u.get("system_generated"):
        continue
    uid = (u.get("id") or "").strip()
    if uid == user_id:
        u["is_owner"] = True
        u["group_ids"] = ["system-admin"]
        u["is_active"] = True
        found = True
    else:
        if u.get("is_owner"):
            prev.append(uid)
        u["is_owner"] = False

if not found:
    print("ERR")
    print("user_id not found in auth")
    raise SystemExit(0)

def atomic_write(path, data):
    st = os.stat(path)
    tmp = path + ".tmp_horus"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\\n")
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)

try:
    atomic_write(auth_path, auth)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

print("OK")
print(user_id)
print(",".join(prev) if prev else "-")
"""
        if host_auth:
            lines = self._run_with_ha_stopped(container, script)
            if lines[0] != "OK":
                detail = "\n".join(lines) or "Fallo al escribir .storage/auth"
                raise SSHCommandError(detail, exit_code=1, stderr="")
        else:
            result = self.ssh.run(
                f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
                timeout=_AUTH_TIMEOUT,
            )
            lines = result.stdout.splitlines()
            if not lines:
                msg = result.stderr or "Sin respuesta al designar Owner."
                raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
            if lines[0] != "OK":
                detail = result.stderr or "\n".join(lines) or "Fallo al escribir .storage"
                raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)
            self._restart_homeassistant(container)

        owners = [u for u in self.list_users() if u.is_owner]
        verified = self.find_user(user)
        if not verified or not verified.is_owner:
            raise SSHCommandError(
                f"Tras reiniciar HA, '{user}' no quedó como Owner. "
                "Revise .storage/auth.",
                exit_code=1,
                stderr="",
            )
        if len(owners) != 1:
            raise SSHCommandError(
                f"Se esperaba un solo Owner; hay {len(owners)}. Revise .storage/auth.",
                exit_code=1,
                stderr="",
            )

        prev_txt = ", ".join(previous_owners) if previous_owners else "(ninguno)"
        return (
            f"'{user}' es ahora el único Owner (también administrador). "
            f"Owner anterior: {prev_txt}. "
            "Home Assistant reiniciado para aplicar el cambio."
        )

    def add_user(self, username: str, password: str, is_admin: bool = False) -> str:
        """Crea usuario + persona en .storage y reinicia HA para cargar auth."""
        user = self._validate_username(username)
        pwd = self._validate_password(password)
        container = self._require_container()

        existing = self.find_user(user)
        if existing and not existing.incomplete:
            raise ValidationError(f"El usuario '{user}' ya existe (id: {existing.user_id}).")
        if existing and existing.incomplete:
            raise ValidationError(
                f"Ya existe un login incompleto '{user}' sin id. "
                "Elimine la entrada huérfana en auth_provider o use otro nombre."
            )

        payload = json.dumps(
            {
                "username": user,
                "password": pwd,
                "is_admin": bool(is_admin),
                "name": user,
            }
        )
        # Escribe auth + auth_provider + person (para que aparezca en Personas)
        script = f"""
import base64, json, os, re, uuid
try:
    import bcrypt
except ImportError:
    print("ERR")
    print("bcrypt no disponible en el contenedor")
    raise SystemExit(0)

req = json.loads({json.dumps(payload)})
username = req["username"]
password = req["password"]
is_admin = bool(req.get("is_admin"))
name = (req.get("name") or username).strip() or username

auth_path = "/config/.storage/auth"
prov_path = "/config/.storage/auth_provider.homeassistant"
person_path = "/config/.storage/person"

try:
    with open(auth_path) as f:
        auth = json.load(f)
    with open(prov_path) as f:
        prov = json.load(f)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

auth.setdefault("data", {{}})
prov.setdefault("data", {{}})
users = auth["data"].setdefault("users", [])
creds = auth["data"].setdefault("credentials", [])
prov_users = prov["data"].setdefault("users", [])

for pu in prov_users:
    if (pu.get("username") or "").strip() == username:
        print("ERR_EXISTS")
        print("username already in auth_provider")
        raise SystemExit(0)

for c in creds:
    if c.get("auth_provider_type") != "homeassistant":
        continue
    if ((c.get("data") or {{}}).get("username") or "").strip() == username:
        print("ERR_EXISTS")
        print("username already in credentials")
        raise SystemExit(0)

user_id = uuid.uuid4().hex
cred_id = uuid.uuid4().hex
group_ids = ["system-admin"] if is_admin else ["system-users"]
# Mismo formato que Home Assistant (truncate 72 bytes + base64 bcrypt)
hashed = base64.b64encode(
    bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12))
).decode("ascii")

users.append({{
    "id": user_id,
    "group_ids": group_ids,
    "is_owner": False,
    "is_active": True,
    "name": name,
    "system_generated": False,
    "local_only": False,
}})
creds.append({{
    "id": cred_id,
    "user_id": user_id,
    "auth_provider_type": "homeassistant",
    "auth_provider_id": None,
    "data": {{"username": username}},
}})
prov_users.append({{"username": username, "password": hashed}})

# Persona vinculada (Ajustes → Personas)
person = None
if os.path.isfile(person_path):
    try:
        with open(person_path) as f:
            person = json.load(f)
    except Exception:
        person = None
if person is None:
    person = {{
        "version": 2,
        "minor_version": 1,
        "key": "person",
        "data": {{"items": []}},
    }}
person.setdefault("data", {{}})
items = person["data"].setdefault("items", [])
# id de persona: slug seguro
slug = re.sub(r"[^a-z0-9_]+", "_", username)[:32] or user_id[:8]
existing_ids = {{(it.get("id") or "") for it in items}}
base_slug = slug
n = 1
while slug in existing_ids:
    slug = f"{{base_slug}}_{{n}}"
    n += 1
# Evitar user_id ya ligado a otra persona
for it in items:
    if it.get("user_id") == user_id:
        print("ERR")
        print("user_id already linked to a person")
        raise SystemExit(0)
items.append({{
    "id": slug,
    "name": name,
    "user_id": user_id,
    "device_trackers": [],
    "picture": None,
}})

def atomic_write(path, data):
    tmp = path + ".tmp_horus"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\\n")
    os.replace(tmp, path)

try:
    atomic_write(auth_path, auth)
    atomic_write(prov_path, prov)
    atomic_write(person_path, person)
except Exception as exc:
    print("ERR")
    print(exc)
    raise SystemExit(0)

print("OK")
print(user_id)
print("admin" if is_admin else "user")
print(slug)
"""
        cmd = (
            f"docker exec {shlex.quote(container)} "
            f"python3 -c {shlex.quote(script)}"
        )
        result = self.ssh.run(cmd, timeout=_AUTH_TIMEOUT)
        lines = result.stdout.splitlines()
        if not lines:
            msg = result.stderr or "Sin respuesta al crear el usuario."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)

        if lines[0] == "ERR_EXISTS":
            detail = lines[1] if len(lines) > 1 else "ya existe"
            raise ValidationError(f"No se pudo crear '{user}': {detail}")
        if lines[0] != "OK" or len(lines) < 2:
            detail = result.stderr or "\n".join(lines) or "Fallo al escribir .storage"
            raise SSHCommandError(detail, exit_code=result.exit_code, stderr=result.stderr)

        created_id = lines[1].strip()
        if not created_id:
            raise SSHCommandError(
                "El usuario se escribió pero no se obtuvo id. Verifique .storage/auth.",
                exit_code=1,
                stderr="",
            )

        # Crítico: HA solo carga auth al arrancar
        self._restart_homeassistant(container)

        verified = self.find_user(user)
        if not verified or not verified.user_id:
            raise SSHCommandError(
                f"Tras reiniciar HA, '{user}' no aparece con id. "
                "Revise .storage/auth (posible sobrescritura).",
                exit_code=1,
                stderr="",
            )
        if verified.user_id != created_id:
            raise SSHCommandError(
                f"Id inconsistente: creado={created_id}, listado={verified.user_id}.",
                exit_code=1,
                stderr="",
            )
        if verified.incomplete:
            raise SSHCommandError(
                f"Usuario '{user}' quedó incompleto tras el reinicio.",
                exit_code=1,
                stderr="",
            )

        role = "administrador" if verified.is_admin else "usuario estándar"
        return (
            f"Usuario '{user}' creado (id={verified.user_id}, rol={role}). "
            "Persona vinculada en Ajustes → Personas. "
            "Home Assistant reiniciado; ya puede iniciar sesión."
        )
