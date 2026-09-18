"""Gestión HTTP de Home Assistant: YAML legado y .storage/http (trusted_proxies)."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import HaConfigurationStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

from paths import REMOTE_CONFIG_DIR, REMOTE_CONFIGURATION_YAML

CONFIG_YAML_PATH = REMOTE_CONFIGURATION_YAML
_STORAGE_VERSION = 2
_STORAGE_MINOR = 2
_HA_STORAGE_HTTP_SINCE = (2026, 8)
_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")
_HORUS_CORS = (
    "https://cast.home-assistant.io",
    "https://www.horussmartenergyapp.com",
    "https://staging.horussmartenergyapp.com",
    "https://develop.horussmartenergyapp.com",
)

# Componentes de discovery incluidos en default_config (escaneo de red).
_DISCOVERY_COMPONENTS = frozenset({"dhcp", "ssdp", "zeroconf", "usb", "bluetooth"})

# default_config (core) sin los de discovery — lista alineada con manifest HA.
_DEFAULT_CONFIG_NO_DISCOVERY = (
    "assist_pipeline",
    "cloud",
    "conversation",
    "energy",
    "file",
    "go2rtc",
    "history",
    "homeassistant_alerts",
    "logbook",
    "media_source",
    "mobile_app",
    "my",
    "stream",
    "sun",
    "usage_prediction",
    "webhook",
)

_DISCOVERY_OFF_START = "# --- HORUS_DISCOVERY_OFF_START ---"
_DISCOVERY_OFF_END = "# --- HORUS_DISCOVERY_OFF_END ---"
_DISCOVERY_OFF_BLOCK = (
    f"{_DISCOVERY_OFF_START}\n"
    "# Escaneo de red desactivado (sin dhcp/ssdp/zeroconf/usb/bluetooth).\n"
    + "\n".join(f"{name}:" for name in _DEFAULT_CONFIG_NO_DISCOVERY)
    + f"\n{_DISCOVERY_OFF_END}\n"
)

_DEFAULT_CONFIG_RE = re.compile(r"(?m)^default_config:\s*(?:#.*)?$")
_DISCOVERY_OFF_RE = re.compile(
    rf"(?ms)^{re.escape(_DISCOVERY_OFF_START)}.*?^{re.escape(_DISCOVERY_OFF_END)}\s*\n?"
)

DEFAULT_CONFIGURATION_YAML = """# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml
"""

LEGACY_HTTP_YAML = """http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
  cors_allowed_origins:
    - https://cast.home-assistant.io
    - https://www.horussmartenergyapp.com
    - https://staging.horussmartenergyapp.com
    - https://develop.horussmartenergyapp.com
  use_x_frame_options: false
"""

_YAML_PROXY_V4 = re.compile(r"(?m)^\s*-\s*127\.0\.0\.1(?:/32)?\s*$")
_YAML_PROXY_V6 = re.compile(r"(?m)^\s*-\s*::1(?:/128)?\s*$")

_AUTOMATION_INCLUDE_RE = re.compile(
    r"(?m)^automation:\s*!include\s+automations\.yaml\s*(?:#.*)?$"
)
_SCRIPT_INCLUDE_RE = re.compile(
    r"(?m)^script:\s*!include\s+scripts\.yaml\s*(?:#.*)?$"
)
_SCENE_INCLUDE_RE = re.compile(
    r"(?m)^scene:\s*!include\s+scenes\.yaml\s*(?:#.*)?$"
)

_INCLUDE_BLOCK = (
    "automation: !include automations.yaml\n"
    "script: !include scripts.yaml\n"
    "scene: !include scenes.yaml\n"
)

_YAML_DELETE_TARGETS = frozenset(
    {
        "automations.yaml",
        "scripts.yaml",
        "scenes.yaml",
        "configuration.yaml",
    }
)


class HaConfigManager:
    """HTTP de HA: limpia YAML legado y deja trusted_proxies en .storage (stable)."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh
        self._container = ""
        self._config_dir = ""

    def get_status(self) -> HaConfigurationStatus:
        config_dir = self._detect_config_dir()
        yaml_path = f"{config_dir}/configuration.yaml" if config_dir else CONFIG_YAML_PATH
        storage_path = f"{config_dir}/.storage/http" if config_dir else ""
        version = self._detect_version()
        status = HaConfigurationStatus(
            path=yaml_path,
            config_dir=config_dir,
            storage_path=storage_path,
            ha_version=version,
        )

        exists = (
            self.ssh.run(f"test -f {shlex.quote(yaml_path)} && echo OK").stdout.strip()
            == "OK"
        )
        status.exists = exists
        if exists:
            raw = self.ssh.run(f"cat {shlex.quote(yaml_path)} 2>/dev/null")
            content = raw.stdout if raw.ok else ""
            stripped = content.strip()
            status.is_empty = not stripped
            status.content_preview = stripped[:1200]
            status.has_http_block = self._has_http_key(stripped)
            status.has_use_x_forwarded_for = bool(
                re.search(r"use_x_forwarded_for\s*:\s*true\b", stripped, re.IGNORECASE)
            )
            status.has_trusted_proxy_ipv4 = bool(_YAML_PROXY_V4.search(stripped))
            status.has_trusted_proxy_ipv6 = bool(_YAML_PROXY_V6.search(stripped))
            status.has_use_x_frame_options = bool(
                re.search(r"use_x_frame_options\s*:\s*false\b", stripped, re.IGNORECASE)
            )
            status.http_ok = not status.has_http_block
            self._fill_discovery_status(status, content)
            self._fill_includes_status(status, content, config_dir)
        else:
            status.is_empty = True
            status.http_ok = True
            status.discovery_enabled = True
            status.discovery_detail = "Sin configuration.yaml (se asumirá default_config al crear)."
            self._fill_includes_status(status, "", config_dir)

        self._fill_storage_status(status)
        parsed = self._parse_ha_version(version)
        if parsed:
            status.uses_storage_http = parsed >= _HA_STORAGE_HTTP_SINCE
        else:
            status.uses_storage_http = status.storage_exists
        if status.uses_storage_http:
            status.proxy_ok = status.storage_proxy_ok
        else:
            status.proxy_ok = (
                status.has_use_x_forwarded_for
                and status.has_trusted_proxy_ipv4
                and status.has_trusted_proxy_ipv6
            )
        return status

    def ensure_http_config(self, force: bool = False) -> str:
        """Compatibilidad: limpia el bloque http legado (HAS 2026.8+)."""
        _ = force
        return self.remove_legacy_http_config()

    def ensure_trusted_proxies(self, restart: bool = True, force: bool = False) -> str:
        """Deja trusted_proxies fijos sin UI (stable en .storage, YAML en HAS viejas)."""
        status = self.get_status()
        msgs: list[str] = []
        changed = False

        storage_changed, storage_msg = self._patch_http_storage(force=force)
        msgs.append(storage_msg)
        changed = changed or storage_changed

        if status.uses_storage_http:
            if status.has_http_block:
                msgs.append(self.remove_legacy_http_config())
                changed = True
        else:
            yaml_changed, yaml_msg = self._ensure_legacy_yaml_http()
            msgs.append(yaml_msg)
            changed = changed or yaml_changed

        if restart and (changed or force):
            msgs.append(self.restart_ha())
        elif restart:
            msgs.append("HA no se reinició: trusted_proxies ya estaba aplicado.")

        return " ".join(msgs)

    def remove_legacy_http_config(self) -> str:
        """Elimina bloque raíz `http:` para compatibilidad con HAS nuevas."""
        status = self.get_status()
        path = status.path or CONFIG_YAML_PATH

        if not status.exists or status.is_empty:
            self._write_remote_file(path, DEFAULT_CONFIGURATION_YAML)
            return f"Escrita plantilla base en {path} (sin bloque http legado)."

        if not status.has_http_block:
            return "No se detectó bloque http legado. No se realizaron cambios."

        backup_path = path + ".bak.horus"
        backup = self.ssh.run(
            f"cp {shlex.quote(path)} {shlex.quote(backup_path)} 2>/dev/null; echo OK",
            use_sudo=True,
        )
        if backup.stdout.strip() != "OK" and not backup.ok:
            raise SSHCommandError("No se pudo crear backup de configuration.yaml.")

        raw = self.ssh.run(f"cat {shlex.quote(path)}").stdout
        new_content = self._strip_http_block(raw)
        self._write_remote_file(path, new_content)
        verify = self.get_status()
        if verify.has_http_block:
            raise SSHCommandError(
                "Se intentó eliminar 'http:' pero todavía aparece en el archivo. "
                f"Revise {path} (backup: {backup_path})."
            )
        return (
            "Bloque http legado eliminado de configuration.yaml. "
            f"Backup: {backup_path}."
        )

    def restart_ha(self) -> str:
        """Reinicia el contenedor de Home Assistant."""
        container = self._detect_container(include_stopped=True)
        if not container:
            raise SSHCommandError(
                "No se detectó el contenedor de Home Assistant para reiniciar."
            )

        res = self.ssh.run(f"docker restart {shlex.quote(container)}", timeout=60)
        if res.ok:
            self._container = container
            return f"Contenedor '{container}' reiniciado correctamente."
        raise SSHCommandError(f"Error al reiniciar '{container}': {res.stderr}")

    def get_full_configuration_yaml(self) -> tuple[str, str]:
        """Lee configuration.yaml completo. Devuelve (path, content)."""
        status = self.get_status()
        path = status.path or CONFIG_YAML_PATH
        if not status.exists:
            raise SSHCommandError(f"No existe configuration.yaml en {path}.")
        res = self.ssh.run(f"cat {shlex.quote(path)} 2>/dev/null")
        if not res.ok:
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(f"No se pudo leer {path}: {detail}")
        return path, res.stdout

    def get_automations_yaml(self) -> tuple[str, str]:
        """Lee automations.yaml completo. Devuelve (path, content)."""
        config_dir = self._detect_config_dir()
        if not config_dir:
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")
        path = f"{config_dir}/automations.yaml"
        exists = (
            self.ssh.run(f"test -f {shlex.quote(path)} && echo OK").stdout.strip()
            == "OK"
        )
        if not exists:
            raise SSHCommandError(f"No existe automations.yaml en {path}.")
        res = self.ssh.run(f"cat {shlex.quote(path)} 2>/dev/null")
        if not res.ok:
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(f"No se pudo leer {path}: {detail}")
        return path, res.stdout

    def check_ha_config(self) -> str:
        """Valida YAML con hass --script check_config (útil tras timeout de automatizaciones)."""
        container = self._detect_container()
        if not container:
            raise SSHCommandError(
                "No se detectó el contenedor de Home Assistant en ejecución."
            )

        res = self.ssh.run(
            f"docker exec {shlex.quote(container)} "
            "hass --script check_config -c /config",
            timeout=180,
        )
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        combined = "\n".join(p for p in (out, err) if p)

        fatal = bool(
            re.search(
                r"Fatal error|ERROR|failed to|Invalid config|Integration error",
                combined,
                re.IGNORECASE,
            )
        )
        if res.ok and not fatal:
            return (
                "Configuración válida (check_config OK). "
                "Si la automatización no aparece, reinicie HA o recargue automatizaciones."
            )
        if not combined:
            raise SSHCommandError(
                "check_config no devolvió salida. "
                f"exit_code={res.exit_code}."
            )
        return f"Errores detectados en la configuración:\n{combined}"

    def ensure_yaml_includes(self, restart: bool = False) -> str:
        """Asegura automation/script/scene includes y crea archivos vacíos si faltan."""
        status = self.get_status()
        path = status.path or CONFIG_YAML_PATH
        config_dir = status.config_dir or self._detect_config_dir()
        if not config_dir:
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")

        msgs: list[str] = []
        changed = False

        if not status.exists or status.is_empty:
            content = DEFAULT_CONFIGURATION_YAML
            self._backup_remote(path, ".bak.horus.includes")
            self._write_remote_file(path, content)
            msgs.append(f"Escrita plantilla base en {path}.")
            changed = True

        raw = self.ssh.run(f"cat {shlex.quote(path)}").stdout
        new_content, include_msgs = self._patch_include_lines(raw)
        if include_msgs:
            msgs.extend(include_msgs)
        if new_content != raw:
            self._backup_remote(path, ".bak.horus.includes")
            self._write_remote_file(path, new_content)
            changed = True

        for name, default_body in (
            ("automations.yaml", "[]\n"),
            ("scripts.yaml", "{}\n"),
            ("scenes.yaml", "[]\n"),
        ):
            file_path = f"{config_dir}/{name}"
            exists = (
                self.ssh.run(
                    f"test -f {shlex.quote(file_path)} && echo OK"
                ).stdout.strip()
                == "OK"
            )
            if not exists:
                self._write_remote_file(file_path, default_body)
                msgs.append(f"Creado {file_path} vacío.")
                changed = True

        if not changed and not msgs:
            msg = "Includes automation/script/scene ya estaban OK."
        else:
            msg = " ".join(msgs) if msgs else "Includes reparados."

        verify = self.get_status()
        if not verify.yaml_includes_ok:
            raise SSHCommandError(
                f"{msg} Pero la verificación sigue fallando: "
                f"{'; '.join(verify.yaml_issues) or 'includes incompletos'}."
            )

        if restart and changed:
            msg = f"{msg} {self.restart_ha()}"
        return msg

    def delete_ha_yaml_file(self, filename: str, recreate_config: bool = False) -> str:
        """Elimina un YAML de /config tras backup. Solo nombres permitidos."""
        name = (filename or "").strip()
        if name not in _YAML_DELETE_TARGETS:
            raise SSHCommandError(
                f"Archivo no permitido: {name}. "
                f"Permitidos: {', '.join(sorted(_YAML_DELETE_TARGETS))}."
            )
        config_dir = self._detect_config_dir()
        if not config_dir:
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")
        path = f"{config_dir}/{name}"
        exists = (
            self.ssh.run(f"test -f {shlex.quote(path)} && echo OK").stdout.strip()
            == "OK"
        )
        if not exists:
            return f"No existe {path}; nada que eliminar."

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = f"{path}.bak.horus.delete.{stamp}"
        backup_res = self.ssh.run(
            f"cp {shlex.quote(path)} {shlex.quote(backup)} 2>/dev/null; echo OK",
            use_sudo=True,
        )
        if backup_res.stdout.strip() != "OK" and not backup_res.ok:
            raise SSHCommandError(f"No se pudo crear backup de {path}.")

        rm = self.ssh.run(f"rm -f {shlex.quote(path)}", use_sudo=True)
        if not rm.ok:
            raise SSHCommandError(
                f"No se pudo eliminar {path}: {rm.stderr or rm.stdout}",
                exit_code=rm.exit_code,
                stderr=rm.stderr,
            )

        msg = f"Eliminado {path}. Backup: {backup}."
        if name == "configuration.yaml" and recreate_config:
            self._write_remote_file(path, DEFAULT_CONFIGURATION_YAML)
            for fname, body in (
                ("automations.yaml", "[]\n"),
                ("scripts.yaml", "{}\n"),
                ("scenes.yaml", "[]\n"),
            ):
                fpath = f"{config_dir}/{fname}"
                if (
                    self.ssh.run(
                        f"test -f {shlex.quote(fpath)} && echo OK"
                    ).stdout.strip()
                    != "OK"
                ):
                    self._write_remote_file(fpath, body)
            msg += " Se escribió plantilla base de configuration.yaml + includes."
        return msg

    def get_admin_network_entry_status(
        self, host: str = "127.0.0.1", port: int = 8765
    ) -> tuple[bool, str]:
        """Devuelve (configurado, detalle) de la entry admin_network en HA."""
        path = f"{self._detect_config_dir()}/.storage/core.config_entries"
        script = f"""
import json
path = {json.dumps(path)}
want_host = {json.dumps(host)}
want_port = int({port})
try:
    with open(path) as fh:
        doc = json.load(fh)
except Exception as exc:
    print("MISSING")
    print(str(exc))
    raise SystemExit(0)
entries = (doc.get("data") or {{}}).get("entries") or []
found = None
for entry in entries:
    if entry.get("domain") != "admin_network":
        continue
    data = entry.get("data") or {{}}
    if str(data.get("host", "")).strip() == want_host and int(data.get("port") or 0) == want_port:
        found = entry
        break
if not found:
    for entry in entries:
        if entry.get("domain") == "admin_network":
            found = entry
            break
if not found:
    print("ABSENT")
    raise SystemExit(0)
data = found.get("data") or {{}}
key = str(data.get("api_key") or "")
masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("*" * len(key) if key else "(vacía)")
print("OK")
print(f"{{found.get('title') or 'Admin Network'}} host={{data.get('host')}} port={{data.get('port')}} key={{masked}}")
"""
        res = self.ssh.run(f"python3 -c {shlex.quote(script)}", use_sudo=True)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return False, "No se pudo leer core.config_entries"
        if lines[0] == "OK":
            return True, lines[1] if len(lines) > 1 else "configurada"
        if lines[0] == "ABSENT":
            return False, "sin config entry"
        return False, lines[1] if len(lines) > 1 else lines[0]

    def ensure_admin_network_entry(
        self,
        api_key: str,
        host: str = "127.0.0.1",
        port: int = 8765,
        force: bool = False,
    ) -> str:
        """Crea o actualiza la config entry de admin_network en .storage."""
        api_key = (api_key or "").strip()
        if not api_key:
            raise SSHCommandError(
                "API key vacía: no se puede configurar la integración en HA."
            )
        path = f"{self._detect_config_dir()}/.storage/core.config_entries"
        if not path.startswith("/"):
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")

        payload = json.dumps(
            {
                "path": path,
                "force": bool(force),
                "host": host,
                "port": int(port),
                "api_key": api_key,
                "title": f"Admin Network ({host})",
            }
        )
        script = f"""
import json, os, uuid
from datetime import datetime, timezone
req = json.loads({json.dumps(payload)})
path = req["path"]
force = bool(req["force"])
host = req["host"]
port = int(req["port"])
api_key = req["api_key"]
title = req["title"]

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

def normalize_entry(entry):
    # HA storage >= 1.4/1.5 exige discovery_keys (si falta → KeyError)
    if not isinstance(entry, dict):
        return False
    changed = False
    if "discovery_keys" not in entry or entry.get("discovery_keys") is None:
        entry["discovery_keys"] = {{}}
        changed = True
    elif not isinstance(entry.get("discovery_keys"), dict):
        entry["discovery_keys"] = {{}}
        changed = True
    if "subentries" not in entry or entry.get("subentries") is None:
        entry["subentries"] = []
        changed = True
    entry.setdefault("options", {{}})
    entry.setdefault("pref_disable_new_entities", False)
    entry.setdefault("pref_disable_polling", False)
    entry.setdefault("unique_id", None)
    entry.setdefault("disabled_by", None)
    entry.setdefault("minor_version", 1)
    if "created_at" not in entry:
        entry["created_at"] = now()
        changed = True
    if "modified_at" not in entry:
        entry["modified_at"] = now()
        changed = True
    return changed

os.makedirs(os.path.dirname(path), exist_ok=True)
doc = None
if os.path.isfile(path):
    with open(path) as fh:
        doc = json.load(fh)
if not isinstance(doc, dict):
    doc = {{"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {{"entries": []}}}}
doc.setdefault("version", 1)
# minor >= 5: HA ya no migra discovery_keys; hay que escribirlos nosotros
doc["minor_version"] = max(int(doc.get("minor_version") or 1), 5)
doc["key"] = "core.config_entries"
data = doc.get("data")
if not isinstance(data, dict):
    data = {{}}
entries = data.get("entries")
if not isinstance(entries, list):
    entries = []

repaired = 0
for entry in entries:
    if normalize_entry(entry):
        repaired += 1

match = None
for entry in entries:
    if entry.get("domain") != "admin_network":
        continue
    edata = entry.get("data") or {{}}
    if str(edata.get("host", "")).strip() == host and int(edata.get("port") or 0) == port:
        match = entry
        break
if match is None:
    for entry in entries:
        if entry.get("domain") == "admin_network":
            match = entry
            break

if match is not None:
    edata = match.get("data") if isinstance(match.get("data"), dict) else {{}}
    same = (
        str(edata.get("host", "")).strip() == host
        and int(edata.get("port") or 0) == port
        and str(edata.get("api_key") or "") == api_key
        and "discovery_keys" in match
        and match.get("subentries") is not None
    )
    if same and not force and repaired == 0:
        print("OK")
        print("UNCHANGED")
        print(match.get("entry_id", ""))
        raise SystemExit(0)
    match["title"] = title
    match["data"] = {{"host": host, "port": port, "api_key": api_key}}
    match["version"] = int(match.get("version") or 1)
    match["modified_at"] = now()
    normalize_entry(match)
    match.setdefault("source", "user")
    action = "UPDATED"
    entry_id = match.get("entry_id", "")
else:
    entry_id = uuid.uuid4().hex
    new_entry = {{
        "entry_id": entry_id,
        "version": 1,
        "minor_version": 1,
        "domain": "admin_network",
        "title": title,
        "data": {{"host": host, "port": port, "api_key": api_key}},
        "options": {{}},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "unique_id": None,
        "disabled_by": None,
        "created_at": now(),
        "modified_at": now(),
        "discovery_keys": {{}},
        "subentries": [],
    }}
    entries.append(new_entry)
    action = "CREATED"

data["entries"] = entries
doc["data"] = data
tmp = path + ".tmp_horus_admin_network"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print(action)
print(entry_id)
print(path)
print(str(repaired))
"""
        container = self._detect_container()
        if container:
            running = (
                self.ssh.run(
                    f"docker ps --format '{{{{.Names}}}}' | grep -Fx {shlex.quote(container)}"
                ).stdout.strip()
                == container
            )
            if running:
                # Preferir escribir desde el host (misma ruta montada); fallback docker exec
                pass

        res = self.ssh.run(
            f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(
                f"No se pudo escribir config entry admin_network: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

        action = lines[1] if len(lines) > 1 else "UPDATED"
        repaired = lines[4] if len(lines) > 4 else "0"
        auth_path = f"{self._detect_config_dir()}/.storage/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(path)}; "
            "fi",
            use_sudo=True,
        )
        repaired_txt = ""
        if repaired and repaired != "0":
            repaired_txt = f" Además se normalizaron {repaired} entry(ies) sin discovery_keys."
        if action == "UNCHANGED":
            return (
                f"Integración Admin Network ya estaba en HA "
                f"({host}:{port}). Reinicie HA si no aparece.{repaired_txt}"
            )
        if action == "CREATED":
            return (
                f"Integración Admin Network añadida a HA automáticamente "
                f"({host}:{port}). Reinicie HA para cargarla.{repaired_txt}"
            )
        return (
            f"Integración Admin Network actualizada en HA "
            f"({host}:{port}). Reinicie HA para aplicar.{repaired_txt}"
        )

    def repair_config_entries_schema(self) -> str:
        """Añade discovery_keys/subentries faltantes en TODAS las entries (KeyError safe)."""
        path = f"{self._detect_config_dir()}/.storage/core.config_entries"
        if not path.startswith("/"):
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")

        script = f"""
import json, os
from datetime import datetime, timezone
path = {json.dumps(path)}

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

if not os.path.isfile(path):
    print("OK")
    print("ABSENT")
    raise SystemExit(0)

backup = path + ".bak.horus.discovery_keys"
with open(path) as fh:
    doc = json.load(fh)
if not isinstance(doc, dict):
    print("ERR")
    print("JSON inválido")
    raise SystemExit(1)

data = doc.get("data")
if not isinstance(data, dict):
    print("OK")
    print("EMPTY")
    raise SystemExit(0)
entries = data.get("entries")
if not isinstance(entries, list):
    print("OK")
    print("EMPTY")
    raise SystemExit(0)

repaired = 0
missing_domains = []
for entry in entries:
    if not isinstance(entry, dict):
        continue
    changed = False
    if "discovery_keys" not in entry or not isinstance(entry.get("discovery_keys"), dict):
        entry["discovery_keys"] = {{}}
        changed = True
    if "subentries" not in entry or entry.get("subentries") is None:
        entry["subentries"] = []
        changed = True
    entry.setdefault("options", {{}})
    entry.setdefault("pref_disable_new_entities", False)
    entry.setdefault("pref_disable_polling", False)
    entry.setdefault("unique_id", None)
    entry.setdefault("disabled_by", None)
    entry.setdefault("minor_version", 1)
    if "created_at" not in entry:
        entry["created_at"] = now()
        changed = True
    if "modified_at" not in entry:
        entry["modified_at"] = now()
        changed = True
    if changed:
        repaired += 1
        domain = str(entry.get("domain") or "?")
        if domain not in missing_domains:
            missing_domains.append(domain)

if repaired == 0:
    print("OK")
    print("UNCHANGED")
    print("0")
    raise SystemExit(0)

doc["minor_version"] = max(int(doc.get("minor_version") or 1), 5)
data["entries"] = entries
doc["data"] = data

with open(backup, "w") as fh:
    with open(path) as src:
        fh.write(src.read())

tmp = path + ".tmp_horus_repair_discovery"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print("REPAIRED")
print(str(repaired))
print(",".join(missing_domains[:20]))
print(backup)
"""
        res = self.ssh.run(
            f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(
                f"No se pudo reparar core.config_entries: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        action = lines[1] if len(lines) > 1 else "UNCHANGED"
        if action in ("ABSENT", "EMPTY"):
            return "No hay core.config_entries que reparar."
        if action == "UNCHANGED":
            return "core.config_entries ya tiene discovery_keys/subentries en todas las entries."

        auth_path = f"{self._detect_config_dir()}/.storage/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(path)}; "
            "fi",
            use_sudo=True,
        )
        count = lines[2] if len(lines) > 2 else "?"
        domains = lines[3] if len(lines) > 3 else ""
        backup = lines[4] if len(lines) > 4 else ""
        return (
            f"Reparadas {count} entry(ies) (añadido discovery_keys={{}} / subentries=[]). "
            f"Dominios: {domains or '—'}. Backup: {backup}. "
            "Reinicie Home Assistant."
        )

    def remove_admin_network_entry(self) -> str:
        """Elimina entries domain=admin_network de core.config_entries."""
        path = f"{self._detect_config_dir()}/.storage/core.config_entries"
        script = f"""
import json, os
path = {json.dumps(path)}
if not os.path.isfile(path):
    print("OK")
    print("ABSENT")
    raise SystemExit(0)
with open(path) as fh:
    doc = json.load(fh)
data = doc.get("data") if isinstance(doc, dict) else None
entries = (data or {{}}).get("entries") if isinstance(data, dict) else None
if not isinstance(entries, list):
    print("OK")
    print("ABSENT")
    raise SystemExit(0)
kept = [e for e in entries if e.get("domain") != "admin_network"]
removed = len(entries) - len(kept)
if removed == 0:
    print("OK")
    print("ABSENT")
    raise SystemExit(0)
data["entries"] = kept
doc["data"] = data
tmp = path + ".tmp_horus_admin_network_rm"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print("REMOVED")
print(str(removed))
"""
        res = self.ssh.run(
            f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(
                f"No se pudo eliminar config entry admin_network: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        if len(lines) > 1 and lines[1] == "ABSENT":
            return "No había config entry admin_network en HA."
        count = lines[2] if len(lines) > 2 else "?"
        auth_path = f"{self._detect_config_dir()}/.storage/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(path)}; "
            "fi",
            use_sudo=True,
        )
        return f"Eliminada(s) {count} config entry(ies) admin_network de HA."

    def set_discovery(self, enabled: bool, restart: bool = True) -> str:
        """Activa o desactiva el escaneo de red (dhcp/ssdp/zeroconf/usb/bluetooth)."""
        status = self.get_status()
        path = status.path or CONFIG_YAML_PATH

        if not status.exists or status.is_empty:
            raise SSHCommandError(
                f"No hay configuration.yaml usable en {path}. "
                "Aplique primero la plantilla base o configure HA."
            )

        raw = self.ssh.run(f"cat {shlex.quote(path)}").stdout
        if not raw.strip():
            raise SSHCommandError(f"configuration.yaml vacío: {path}")

        if enabled:
            if status.discovery_enabled and not _DISCOVERY_OFF_RE.search(raw):
                msg = "Discovery ya está activo (default_config presente)."
                if restart:
                    return f"{msg} Reinicio no necesario."
                return msg
            new_content = self._enable_discovery_yaml(raw)
            action = "Discovery ACTIVADO (restaurado default_config)."
        else:
            if not status.discovery_enabled:
                msg = "Discovery ya está desactivado."
                if restart:
                    return f"{msg} Reinicio no necesario."
                return msg
            new_content = self._disable_discovery_yaml(raw)
            action = (
                "Discovery DESACTIVADO (sin dhcp/ssdp/zeroconf/usb/bluetooth)."
            )

        if new_content == raw:
            raise SSHCommandError(
                "No se pudo modificar configuration.yaml: "
                "no se encontró default_config ni el bloque Horus de discovery."
            )

        backup_path = path + ".bak.horus.discovery"
        backup = self.ssh.run(
            f"cp {shlex.quote(path)} {shlex.quote(backup_path)} 2>/dev/null; echo OK",
            use_sudo=True,
        )
        if backup.stdout.strip() != "OK" and not backup.ok:
            raise SSHCommandError("No se pudo crear backup de configuration.yaml.")

        self._write_remote_file(path, new_content)
        verify = self.get_status()
        if enabled and not verify.discovery_enabled:
            raise SSHCommandError(
                "Se escribió el YAML pero discovery sigue desactivado. "
                f"Revise {path} (backup: {backup_path})."
            )
        if not enabled and verify.discovery_enabled:
            raise SSHCommandError(
                "Se escribió el YAML pero discovery sigue activo. "
                f"Revise {path} (backup: {backup_path})."
            )

        msg = f"{action} Backup: {backup_path}."
        if restart:
            msg = f"{msg} {self.restart_ha()}"
        return msg

    @staticmethod
    def _fill_discovery_status(status: HaConfigurationStatus, content: str) -> None:
        has_default = bool(_DEFAULT_CONFIG_RE.search(content))
        has_horus_off = bool(_DISCOVERY_OFF_RE.search(content))
        if has_horus_off and not has_default:
            status.discovery_enabled = False
            status.discovery_detail = (
                "Bloque Horus sin dhcp/ssdp/zeroconf/usb/bluetooth"
            )
        elif has_default:
            status.discovery_enabled = True
            status.discovery_detail = "default_config activo"
        else:
            # Sin default_config: comprobar si discovery está explícito.
            explicit = [
                name
                for name in sorted(_DISCOVERY_COMPONENTS)
                if re.search(rf"(?m)^{name}:\s*(?:#.*)?$", content)
            ]
            if explicit:
                status.discovery_enabled = True
                status.discovery_detail = f"Explícito: {', '.join(explicit)}"
            else:
                status.discovery_enabled = False
                status.discovery_detail = "Sin default_config ni componentes de discovery"

    def _fill_includes_status(
        self, status: HaConfigurationStatus, content: str, config_dir: str
    ) -> None:
        status.has_automation_include = bool(_AUTOMATION_INCLUDE_RE.search(content or ""))
        status.has_script_include = bool(_SCRIPT_INCLUDE_RE.search(content or ""))
        status.has_scene_include = bool(_SCENE_INCLUDE_RE.search(content or ""))

        issues: list[str] = []
        if content and not status.has_automation_include:
            issues.append("Falta automation: !include automations.yaml")
        if content and not status.has_script_include:
            issues.append("Falta script: !include scripts.yaml")
        if content and not status.has_scene_include:
            issues.append("Falta scene: !include scenes.yaml")
        if not content:
            issues.append("Sin configuration.yaml usable")

        if config_dir:
            for attr, name in (
                ("automations_file_exists", "automations.yaml"),
                ("scripts_file_exists", "scripts.yaml"),
                ("scenes_file_exists", "scenes.yaml"),
            ):
                path = f"{config_dir}/{name}"
                ok = (
                    self.ssh.run(
                        f"test -f {shlex.quote(path)} && echo OK"
                    ).stdout.strip()
                    == "OK"
                )
                setattr(status, attr, ok)
                if not ok:
                    issues.append(f"No existe {name}")

        status.yaml_issues = issues
        status.yaml_includes_ok = (
            status.has_automation_include
            and status.has_script_include
            and status.has_scene_include
            and status.automations_file_exists
            and status.scripts_file_exists
            and status.scenes_file_exists
        )

    @staticmethod
    def _patch_include_lines(raw: str) -> tuple[str, list[str]]:
        """Inserta includes faltantes sin duplicar. Devuelve (nuevo_contenido, msgs)."""
        msgs: list[str] = []
        content = raw if raw.endswith("\n") or not raw else raw + "\n"
        missing: list[str] = []
        if not _AUTOMATION_INCLUDE_RE.search(content):
            missing.append("automation: !include automations.yaml")
        if not _SCRIPT_INCLUDE_RE.search(content):
            missing.append("script: !include scripts.yaml")
        if not _SCENE_INCLUDE_RE.search(content):
            missing.append("scene: !include scenes.yaml")
        if not missing:
            return content, msgs

        block = "\n".join(missing) + "\n"
        msgs.append(f"Añadido: {', '.join(missing)}.")

        # Preferir insertar tras bloque Horus discovery-off
        if _DISCOVERY_OFF_RE.search(content):
            def _after_horus(m: re.Match[str]) -> str:
                return m.group(0).rstrip("\n") + "\n\n" + block

            return _DISCOVERY_OFF_RE.sub(_after_horus, content, count=1), msgs

        # Si hay scene include parcial, insertar antes
        scene_m = re.search(r"(?m)^scene:\s*", content)
        if scene_m and "automation: !include automations.yaml" in missing:
            idx = scene_m.start()
            return content[:idx] + block + content[idx:], msgs

        # Insertar antes de http: si existe
        http_m = re.search(r"(?m)^http:\s*", content)
        if http_m:
            idx = http_m.start()
            return content[:idx] + block + "\n" + content[idx:], msgs

        return content.rstrip() + "\n\n" + block, msgs

    def _backup_remote(self, path: str, suffix: str) -> str:
        backup_path = path + suffix
        backup = self.ssh.run(
            f"cp {shlex.quote(path)} {shlex.quote(backup_path)} 2>/dev/null; echo OK",
            use_sudo=True,
        )
        if backup.stdout.strip() != "OK" and not backup.ok:
            # Si el archivo no existía aún, no fallar
            exists = (
                self.ssh.run(f"test -f {shlex.quote(path)} && echo OK").stdout.strip()
                == "OK"
            )
            if exists:
                raise SSHCommandError(f"No se pudo crear backup: {backup_path}")
        return backup_path

    @classmethod
    def _disable_discovery_yaml(cls, raw: str) -> str:
        if _DISCOVERY_OFF_RE.search(raw) and not _DEFAULT_CONFIG_RE.search(raw):
            return raw
        if not _DEFAULT_CONFIG_RE.search(raw):
            return raw
        return _DEFAULT_CONFIG_RE.sub(_DISCOVERY_OFF_BLOCK.rstrip("\n"), raw, count=1)

    @classmethod
    def _enable_discovery_yaml(cls, raw: str) -> str:
        if _DISCOVERY_OFF_RE.search(raw):
            return _DISCOVERY_OFF_RE.sub("default_config:\n", raw, count=1)
        if _DEFAULT_CONFIG_RE.search(raw):
            return raw
        # Sin bloque Horus ni default_config: insertar al inicio útil.
        lines = raw.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            if line.strip() and not line.lstrip().startswith("#"):
                insert_at = i
                break
        lines.insert(insert_at, "default_config:\n\n")
        return "".join(lines)

    def _ensure_legacy_yaml_http(self) -> tuple[bool, str]:
        """HAS pre-2026.8: trusted_proxies sigue viviendo en configuration.yaml."""
        status = self.get_status()
        path = status.path or CONFIG_YAML_PATH
        yaml_ok = (
            status.has_http_block
            and status.has_use_x_forwarded_for
            and status.has_trusted_proxy_ipv4
            and status.has_trusted_proxy_ipv6
        )
        if yaml_ok:
            return False, "configuration.yaml ya tiene trusted_proxies."

        if status.exists and not status.is_empty:
            backup_path = path + ".bak.horus"
            self.ssh.run(
                f"cp {shlex.quote(path)} {shlex.quote(backup_path)} 2>/dev/null; echo OK",
                use_sudo=True,
            )
            raw = self.ssh.run(f"cat {shlex.quote(path)}").stdout
            base = self._strip_http_block(raw)
        else:
            base = DEFAULT_CONFIGURATION_YAML

        new_content = base.rstrip() + "\n\n" + LEGACY_HTTP_YAML
        if not new_content.endswith("\n"):
            new_content += "\n"
        self._write_remote_file(path, new_content)
        return True, f"Bloque http (trusted_proxies) escrito en {path} (HAS legado)."

    def _patch_http_storage(self, force: bool = False) -> tuple[bool, str]:
        """Escribe use_x_forwarded_for + trusted_proxies en stable (pending=null)."""
        container = self._detect_container()
        host_path = f"{self._detect_config_dir()}/.storage/http"
        if not host_path.startswith("/"):
            raise SSHCommandError("No se detectó la ruta de config de Home Assistant.")

        if container:
            running = (
                self.ssh.run(
                    f"docker ps --format '{{{{.Names}}}}' | grep -Fx {shlex.quote(container)}"
                ).stdout.strip()
                == container
            )
            if running:
                script = self._storage_patch_script("/config/.storage/http", force)
                res = self.ssh.run(
                    f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}",
                    timeout=60,
                )
                parsed = self._parse_patch_result(res.stdout, res.stderr, res.ok)
                if parsed is not None:
                    return parsed

        script = self._storage_patch_script(host_path, force)
        res = self.ssh.run(
            f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True
        )
        parsed = self._parse_patch_result(res.stdout, res.stderr, res.ok)
        if parsed is None:
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(
                f"No se pudo escribir .storage/http: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

        auth_path = f"{self._detect_config_dir()}/.storage/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(host_path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(host_path)}; "
            "fi",
            use_sudo=True,
        )
        return parsed

    def _fill_storage_status(self, status: HaConfigurationStatus) -> None:
        path = status.storage_path
        if not path:
            return
        exists = (
            self.ssh.run(f"test -f {shlex.quote(path)} && echo OK").stdout.strip() == "OK"
        )
        status.storage_exists = exists
        if not exists:
            return
        raw = self.ssh.run(f"cat {shlex.quote(path)} 2>/dev/null")
        if not raw.ok or not raw.stdout.strip():
            return
        try:
            doc = json.loads(raw.stdout)
        except json.JSONDecodeError:
            return
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            return
        stable = data.get("stable") if isinstance(data.get("stable"), dict) else {}
        pending = data.get("pending")
        status.storage_pending = pending is not None
        status.storage_yaml_migration_done = bool(data.get("yaml_migration_done"))
        status.storage_use_x_forwarded_for = bool(stable.get("use_x_forwarded_for"))
        proxies = [self._normalize_proxy(p) for p in (stable.get("trusted_proxies") or [])]
        status.storage_has_proxy_ipv4 = "127.0.0.1/32" in proxies
        status.storage_has_proxy_ipv6 = "::1/128" in proxies
        status.storage_proxy_ok = (
            status.storage_use_x_forwarded_for
            and status.storage_has_proxy_ipv4
            and status.storage_has_proxy_ipv6
            and not status.storage_pending
        )

    def _detect_container(self, include_stopped: bool = False) -> str:
        if self._container and not include_stopped:
            return self._container
        cmd = (
            "docker ps -a --format '{{.Names}}'"
            if include_stopped
            else "docker ps --format '{{.Names}}'"
        )
        result = self.ssh.run(
            f"{cmd} 2>/dev/null | grep -iE 'homeassistant|home-assistant' | head -1"
        )
        name = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if name:
            self._container = name
        return name

    def _detect_config_dir(self) -> str:
        if self._config_dir:
            return self._config_dir
        container = self._detect_container(include_stopped=True)
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
                    self._config_dir = source.strip()
                    return self._config_dir
        self._config_dir = REMOTE_CONFIG_DIR
        return self._config_dir

    def _detect_version(self) -> str:
        container = self._detect_container(include_stopped=True)
        if not container:
            return ""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} hass --version"
        )
        return result.stdout.strip() if result.ok else ""

    def _write_remote_file(self, path: str, content: str) -> None:
        """Escribe archivo remoto vía SFTP (UTF-8)."""
        parent = path.rsplit("/", 1)[0]
        parent_ok = self.ssh.run(f"test -d {shlex.quote(parent)} && echo OK")
        if parent_ok.stdout.strip() != "OK":
            raise SSHCommandError(f"No existe el directorio {parent}")

        sftp = self.ssh.open_sftp()
        try:
            with sftp.open(path, "w") as fh:
                fh.write(content)
        except Exception as exc:
            raise SSHCommandError(f"No se pudo escribir {path}: {exc}") from exc
        finally:
            sftp.close()

        self.ssh.run(f"chmod 644 {shlex.quote(path)}", use_sudo=True)

    @staticmethod
    def _storage_patch_script(path: str, force: bool) -> str:
        payload = json.dumps(
            {
                "path": path,
                "force": bool(force),
                "proxies": list(_TRUSTED_PROXIES),
                "cors": list(_HORUS_CORS),
                "version": _STORAGE_VERSION,
                "minor_version": _STORAGE_MINOR,
            }
        )
        return f"""
import json, os
from datetime import datetime, timezone
req = json.loads({json.dumps(payload)})
path = req["path"]
force = bool(req["force"])
need_proxies = list(req["proxies"])
need_cors = list(req["cors"])

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

def norm(value):
    text = str(value).strip()
    if text == "127.0.0.1":
        return "127.0.0.1/32"
    if text == "::1":
        return "::1/128"
    return text

os.makedirs(os.path.dirname(path), exist_ok=True)
doc = None
if os.path.isfile(path):
    with open(path) as fh:
        doc = json.load(fh)
if not isinstance(doc, dict):
    doc = {{}}
doc["version"] = max(int(doc.get("version") or 0), int(req["version"]))
doc["minor_version"] = max(int(doc.get("minor_version") or 0), int(req["minor_version"]))
doc["key"] = "http"
data = doc.get("data")
if not isinstance(data, dict):
    data = {{}}
stable = data.get("stable")
if not isinstance(stable, dict):
    stable = {{}}
pending = data.get("pending") if isinstance(data.get("pending"), dict) else None

cors = [c for c in (stable.get("cors_allowed_origins") or []) if c]
for origin in need_cors:
    if origin not in cors:
        cors.append(origin)

proxies = [norm(p) for p in (stable.get("trusted_proxies") or [])]
if pending:
    for p in pending.get("trusted_proxies") or []:
        n = norm(p)
        if n not in proxies:
            proxies.append(n)
for p in need_proxies:
    if p not in proxies:
        proxies.append(p)

already = (
    stable.get("use_x_forwarded_for") is True
    and all(p in [norm(x) for x in (stable.get("trusted_proxies") or [])] for p in need_proxies)
    and data.get("pending") is None
    and data.get("yaml_migration_done") is True
)
if already and not force:
    print("OK")
    print("UNCHANGED")
    raise SystemExit(0)

stable["server_port"] = int(stable.get("server_port") or 8123)
stable["cors_allowed_origins"] = cors
stable["use_x_forwarded_for"] = True
stable["trusted_proxies"] = proxies
if "login_attempts_threshold" not in stable:
    stable["login_attempts_threshold"] = -1
if "ip_ban_enabled" not in stable:
    stable["ip_ban_enabled"] = True
if not stable.get("ssl_profile"):
    stable["ssl_profile"] = "modern"
stable["use_x_frame_options"] = False
if not stable.get("created_at"):
    stable["created_at"] = now()
stable["error"] = None
stable["error_message"] = None
data["stable"] = stable
data["pending"] = None
data["yaml_migration_done"] = True
doc["data"] = data

tmp = path + ".tmp_horus"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print("UPDATED")
print(path)
"""

    @staticmethod
    def _parse_patch_result(stdout: str, _stderr: str, ok: bool) -> tuple[bool, str] | None:
        lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
        if not ok or not lines or lines[0] != "OK":
            return None
        action = lines[1] if len(lines) > 1 else "UPDATED"
        path = lines[2] if len(lines) > 2 else ".storage/http"
        if action == "UNCHANGED":
            return False, f"{path}: trusted_proxies ya estaba en stable."
        return True, (
            f"{path}: trusted_proxies escrito en stable "
            "(127.0.0.1/32, ::1/128; pending=null)."
        )

    @staticmethod
    def _strip_http_block(raw: str) -> str:
        lines = raw.splitlines()
        new_lines: list[str] = []
        in_http = False
        for line in lines:
            if line.startswith("http:"):
                in_http = True
                continue
            if in_http:
                if line.startswith("  ") or not line.strip():
                    continue
                in_http = False
            new_lines.append(line)
        return "\n".join(new_lines).rstrip() + "\n"

    @staticmethod
    def _has_http_key(content: str) -> bool:
        for line in content.splitlines():
            if line.startswith("http:"):
                return True
        return False

    @staticmethod
    def _parse_ha_version(raw: str) -> tuple[int, int] | None:
        match = re.search(r"(\d{4})\.(\d{1,2})", raw or "")
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _normalize_proxy(value: object) -> str:
        text = str(value).strip()
        if text == "127.0.0.1":
            return "127.0.0.1/32"
        if text == "::1":
            return "::1/128"
        return text
