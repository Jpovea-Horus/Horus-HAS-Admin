"""Gestión HTTP de Home Assistant: YAML legado y .storage/http (trusted_proxies)."""

from __future__ import annotations

import json
import re
import shlex
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
        else:
            status.is_empty = True
            status.http_ok = True

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
