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

        # Esperar a que el proceso acepte conexiones (auth ya en memoria)
        self.ssh.run(
            f"for i in $(seq 1 {_RESTART_WAIT_SEC}); do "
            f"docker exec {shlex.quote(container)} true 2>/dev/null && break; "
            "sleep 1; done",
            timeout=_AUTH_TIMEOUT,
        )
        self.ssh.run("sleep 8", timeout=20)

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
    ]))

for pu in prov_users:
    uname = (pu.get("username") or "").strip()
    if uname and uname not in usernames_linked:
        print("\t".join(["", uname, "(solo password, sin id)", "0", "1", "0", "1"]))
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
