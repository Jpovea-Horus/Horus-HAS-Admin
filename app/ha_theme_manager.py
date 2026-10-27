"""Tema Horus: colores estándar del historial de estados de Home Assistant."""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import HaThemeStatus

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from ssh_client import SSHClient

THEME_NAME = "horus"
THEME_FILE = "horus_theme.yaml"
THEMES_INCLUDE = "themes: !include_dir_merge_named themes"

# (grupo, variable CSS del tema, color). HA solo colorea por dominio/device_class/estado.
HISTORY_COLORS: tuple[tuple[str, str, str], ...] = (
    ("Plugins / gráficos — color 1", "graph-color-1", "#8DC6E9"),
    ("Plugins / gráficos — color 2", "graph-color-2", "#98E98D"),
    ("Plugins / gráficos — color 3", "graph-color-3", "#E9B08D"),
    ("Plugins / gráficos — color 4", "graph-color-4", "#8D98E9"),
    ("Plugins / gráficos — color 5", "graph-color-5", "#E98DC6"),
    ("Plugins / gráficos — color 6", "graph-color-6", "#00BCD4"),
    ("Puerta abierta", "state-binary_sensor-door-on-color", "#FD3DB5"),
    ("Puerta cerrada", "state-binary_sensor-door-off-color", "#9E9E9E"),
    ("Movimiento detectado", "state-binary_sensor-motion-on-color", "#7B59C0"),
    ("Sin movimiento", "state-binary_sensor-motion-off-color", "#9E9E9E"),
    ("Luz encendida", "state-light-on-color", "#FEC335"),
    ("Luz apagada", "state-light-off-color", "#9E9E9E"),
    ("Interruptor encendido", "state-switch-on-color", "#FEC335"),
    ("Interruptor apagado", "state-switch-off-color", "#9E9E9E"),
    ("Modo / actuador activo", "state-input_boolean-on-color", "#FEC335"),
    ("Modo / actuador inactivo", "state-input_boolean-off-color", "#9E9E9E"),
)

_FRONTEND_KEY_RE = re.compile(r"^frontend\s*:")
_FRONTEND_PLAIN_RE = re.compile(r"^frontend:\s*(?:#.*)?$")
_THEMES_CHILD_RE = re.compile(r"^\s+themes\s*:(.*)$")
_INCLUDE_OK_RE = re.compile(r"^\s*!include_dir_merge_named\s+themes/?\s*(?:#.*)?$")
_COLOR_LINE_RE = re.compile(r'^\s+([a-z0-9_-]+)\s*:\s*"?(#[0-9A-Fa-f]{6})"?\s*$')


def build_theme_yaml() -> str:
    lines = [
        "# Generado por Horus HAS Admin. Se sobrescribe al reaplicar el tema.",
        f"{THEME_NAME}:",
        "  modes:",
    ]
    for mode in ("light", "dark"):
        lines.append(f"    {mode}:")
        lines.extend(f'      {var}: "{color}"' for _label, var, color in HISTORY_COLORS)
    return "\n".join(lines) + "\n"


class HaThemeManager:
    """Escribe themes/horus_theme.yaml y lo deja como tema por defecto de HA."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager):
        self.ssh = ssh
        self.ha_config = ha_config

    def get_status(self) -> HaThemeStatus:
        config_dir = self.ha_config._detect_config_dir()
        status = HaThemeStatus(
            config_dir=config_dir,
            themes_dir=f"{config_dir}/themes",
            theme_path=f"{config_dir}/themes/{THEME_FILE}",
            yaml_path=f"{config_dir}/configuration.yaml",
            storage_path=f"{config_dir}/.storage/frontend_theme",
        )
        status.themes_dir_exists = self._exists(status.themes_dir, "-d")
        status.theme_exists = self._exists(status.theme_path)
        if status.theme_exists:
            raw = self._cat(status.theme_path)
            status.colors = _parse_colors(raw)
            status.theme_up_to_date = _norm(raw) == build_theme_yaml()

        status.yaml_exists = self._exists(status.yaml_path)
        if status.yaml_exists:
            state, detail = _themes_include_state(self._cat(status.yaml_path))
            status.frontend_themes_ok = state == "ok"
            status.frontend_themes_conflict = state == "conflict"
            status.frontend_themes_detail = detail
        else:
            status.frontend_themes_detail = "No existe configuration.yaml"

        if self._exists(status.storage_path):
            try:
                doc = json.loads(self._cat(status.storage_path) or "{}")
            except json.JSONDecodeError:
                doc = {}
            data = doc.get("data") if isinstance(doc, dict) else None
            if isinstance(data, dict):
                status.default_theme = str(data.get("frontend_default_theme") or "")
                status.default_dark_theme = str(data.get("frontend_default_dark_theme") or "")
        status.default_theme_set = (
            status.default_theme == THEME_NAME and status.default_dark_theme == THEME_NAME
        )
        return status

    def apply(self, set_default: bool = True) -> str:
        status = self.get_status()
        if not status.yaml_exists:
            raise SSHCommandError(
                f"No existe {status.yaml_path}. Aplique primero la plantilla base de HA."
            )
        if status.frontend_themes_conflict:
            raise SSHCommandError(
                f"No se tocó configuration.yaml: {status.frontend_themes_detail}. "
                f"Debe quedar 'frontend:' con '  {THEMES_INCLUDE}'."
            )

        msgs: list[str] = []
        ref = shlex.quote(status.yaml_path)
        themes_dir = shlex.quote(status.themes_dir)
        mk = self.ssh.run(
            f"mkdir -p {themes_dir} && chown --reference={ref} {themes_dir}",
            use_sudo=True,
        )
        if not mk.ok:
            raise SSHCommandError(
                f"No se pudo crear {status.themes_dir}: {mk.stderr or mk.stdout}"
            )

        if status.theme_up_to_date:
            msgs.append(f"{status.theme_path} ya tenía los colores Horus.")
        else:
            if status.theme_exists:
                self.ha_config._backup_remote(status.theme_path, ".bak.horus")
            self.ha_config._write_remote_file(status.theme_path, build_theme_yaml())
            self.ssh.run(
                f"chown --reference={ref} {shlex.quote(status.theme_path)}",
                use_sudo=True,
            )
            msgs.append(f"Escrito {status.theme_path}.")

        if status.frontend_themes_ok:
            msgs.append("configuration.yaml ya carga la carpeta themes.")
        else:
            raw = self._cat(status.yaml_path)
            backup = self.ha_config._backup_remote(status.yaml_path, ".bak.horus.themes")
            self.ha_config._write_remote_file(status.yaml_path, _inject_themes_include(raw))
            msgs.append(f"Añadido '{THEMES_INCLUDE}' en configuration.yaml (backup: {backup}).")

        if set_default:
            msgs.append(self._set_default_theme(status.storage_path, enable=True))

        verify = self.get_status()
        if not (verify.theme_up_to_date and verify.frontend_themes_ok):
            raise SSHCommandError(
                " ".join(msgs) + " Pero la verificación falló: revise "
                f"{verify.theme_path} y {verify.yaml_path}."
            )
        msgs.append("Reinicie Home Assistant para cargar el tema.")
        return " ".join(msgs)

    def remove(self) -> str:
        status = self.get_status()
        msgs: list[str] = []
        if status.theme_exists:
            backup = self.ha_config._backup_remote(status.theme_path, ".bak.horus.delete")
            self.ssh.run(f"rm -f {shlex.quote(status.theme_path)}", use_sudo=True)
            msgs.append(f"Eliminado {status.theme_path} (backup: {backup}).")
        if status.default_theme == THEME_NAME or status.default_dark_theme == THEME_NAME:
            msgs.append(self._set_default_theme(status.storage_path, enable=False))
        if not msgs:
            return "No había tema Horus instalado."
        msgs.append("Reinicie Home Assistant para volver a los colores nativos.")
        return " ".join(msgs)

    def get_theme_yaml(self) -> tuple[str, str]:
        status = self.get_status()
        if not status.theme_exists:
            raise SSHCommandError(f"No existe {status.theme_path}.")
        return status.theme_path, self._cat(status.theme_path)

    def _set_default_theme(self, path: str, enable: bool) -> str:
        payload = json.dumps({"path": path, "name": THEME_NAME, "enable": enable})
        script = f"""
import json, os
req = json.loads({json.dumps(payload)})
path, name, enable = req["path"], req["name"], req["enable"]
keys = ("frontend_default_theme", "frontend_default_dark_theme")
doc = None
if os.path.isfile(path):
    with open(path) as fh:
        doc = json.load(fh)
if not isinstance(doc, dict):
    doc = {{"version": 1, "minor_version": 1, "key": "frontend_theme", "data": {{}}}}
data = doc.get("data") if isinstance(doc.get("data"), dict) else {{}}
before = dict(data)
for key in keys:
    if enable:
        data[key] = name
    elif data.get(key) == name:
        data.pop(key)
if data == before and os.path.isfile(path):
    print("OK")
    print("UNCHANGED")
    raise SystemExit(0)
doc["data"] = data
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp_horus_theme"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print("UPDATED")
"""
        res = self.ssh.run(f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            raise SSHCommandError(
                f"No se pudo escribir {path}: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        auth_path = path.rsplit("/", 1)[0] + "/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(path)}; "
            "fi",
            use_sudo=True,
        )
        unchanged = len(lines) > 1 and lines[1] == "UNCHANGED"
        if enable:
            return (
                f"Tema '{THEME_NAME}' ya era el predeterminado."
                if unchanged
                else f"Tema '{THEME_NAME}' fijado como predeterminado (claro y oscuro)."
            )
        return (
            "El tema predeterminado no era Horus."
            if unchanged
            else "Tema predeterminado restaurado al nativo de HA."
        )

    def _exists(self, path: str, flag: str = "-f") -> bool:
        res = self.ssh.run(f"test {flag} {shlex.quote(path)} && echo OK")
        return res.stdout.strip() == "OK"

    def _cat(self, path: str) -> str:
        return self.ssh.run(f"cat {shlex.quote(path)} 2>/dev/null").stdout or ""


def _norm(raw: str) -> str:
    return raw.replace("\r\n", "\n").strip() + "\n"


def _parse_colors(raw: str) -> dict[str, str]:
    colors: dict[str, str] = {}
    for line in raw.splitlines():
        match = _COLOR_LINE_RE.match(line)
        if match:
            colors.setdefault(match.group(1), match.group(2).upper())
    return colors


def _frontend_block(lines: list[str]) -> tuple[int | None, int]:
    start = next((i for i, ln in enumerate(lines) if _FRONTEND_KEY_RE.match(ln)), None)
    if start is None:
        return None, 0
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and not ln.startswith((" ", "\t", "#")):
            end = j
            break
    return start, end


def _themes_include_state(raw: str) -> tuple[str, str]:
    """Devuelve (ok | missing | conflict, detalle)."""
    lines = raw.replace("\r\n", "\n").splitlines()
    start, end = _frontend_block(lines)
    if start is None:
        return "missing", "Sin bloque frontend: (se añadirá)"
    if not _FRONTEND_PLAIN_RE.match(lines[start]):
        return "conflict", f"frontend: con valor en línea ('{lines[start].strip()}')"
    for ln in lines[start + 1 : end]:
        match = _THEMES_CHILD_RE.match(ln)
        if not match:
            continue
        if _INCLUDE_OK_RE.match(match.group(1)):
            return "ok", "frontend.themes carga la carpeta themes"
        return "conflict", f"frontend.themes distinto ('{ln.strip()}')"
    children = [ln for ln in lines[start + 1 : end] if ln.strip() and not ln.lstrip().startswith("#")]
    if any(ln.startswith("\t") for ln in children):
        return "conflict", "frontend: usa tabulaciones"
    return "missing", "frontend: sin themes (se añadirá)"


def _inject_themes_include(raw: str) -> str:
    text = raw.replace("\r\n", "\n")
    text = text if text.endswith("\n") else text + "\n"
    lines = text.splitlines()
    start, end = _frontend_block(lines)
    if start is None:
        return text.rstrip() + f"\n\nfrontend:\n  {THEMES_INCLUDE}\n"
    indent = "  "
    for ln in lines[start + 1 : end]:
        if ln.strip() and not ln.lstrip().startswith("#"):
            indent = ln[: len(ln) - len(ln.lstrip())]
            break
    lines.insert(start + 1, f"{indent}{THEMES_INCLUDE}")
    return "\n".join(lines).rstrip() + "\n"
