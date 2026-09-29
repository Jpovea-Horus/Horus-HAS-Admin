"""Instala el panel lateral Z-Wave JS UI (www + panel_custom)."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from ha_config_manager import HaConfigManager
from models import ZwavePanelStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

JS_NAME = "zwave-panel.js"
PANEL_NAME = "zwave-ui-panel"
JS_VERSION = "2"

PANEL_ITEM = f"""  - name: zwave-ui-panel
    sidebar_title: Z-Wave JS UI
    sidebar_icon: mdi:z-wave
    js_url: /local/{JS_NAME}?v={JS_VERSION}
    embed_iframe: false
    require_admin: true
"""

PANEL_BLOCK = "panel_custom:\n" + PANEL_ITEM


class ZwavePanelManager:
    """Sube el JS a /config/www y registra panel_custom en configuration.yaml."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh
        self._ha = HaConfigManager(ssh)

    def get_status(self) -> ZwavePanelStatus:
        config_dir = self._ha._detect_config_dir()
        js_path = f"{config_dir}/www/{JS_NAME}"
        yaml_path = f"{config_dir}/configuration.yaml"
        js_ok = (
            self.ssh.run(f"test -f {shlex.quote(js_path)} && echo OK").stdout.strip()
            == "OK"
        )
        yaml_exists = (
            self.ssh.run(f"test -f {shlex.quote(yaml_path)} && echo OK").stdout.strip()
            == "OK"
        )
        yaml_raw = ""
        if yaml_exists:
            yaml_raw = self.ssh.run(f"cat {shlex.quote(yaml_path)} 2>/dev/null").stdout or ""
        yaml_ok = PANEL_NAME in yaml_raw
        return ZwavePanelStatus(
            config_dir=config_dir,
            js_path=js_path,
            yaml_path=yaml_path,
            js_exists=js_ok,
            yaml_exists=yaml_exists,
            yaml_ok=yaml_ok,
            has_iframe_zwave=_has_iframe_zwave(yaml_raw),
            installed=js_ok and yaml_ok,
        )

    def install(self, local_js: str, restart: bool = False) -> str:
        local = resolve_zwave_panel_js(local_js)

        status = self.get_status()
        if not status.yaml_exists:
            raise SSHCommandError(
                f"No existe {status.yaml_path}. No se puede registrar el panel."
            )

        www = f"{status.config_dir}/www"
        remote_tmp = f"/tmp/horus_{JS_NAME}"
        sftp = self.ssh.open_sftp()
        try:
            sftp.put(str(local), remote_tmp)
        except Exception as exc:
            raise SSHCommandError(f"No se pudo subir {JS_NAME}: {exc}") from exc
        finally:
            sftp.close()

        ref = shlex.quote(status.yaml_path)
        place = self.ssh.run(
            f"mkdir -p {shlex.quote(www)} && "
            f"cp {shlex.quote(remote_tmp)} {shlex.quote(status.js_path)} && "
            f"chown --reference={ref} {shlex.quote(www)} {shlex.quote(status.js_path)} && "
            f"chmod --reference={ref} {shlex.quote(status.js_path)} && "
            f"rm -f {shlex.quote(remote_tmp)}",
            use_sudo=True,
        )
        if not place.ok:
            self.ssh.run(f"rm -f {shlex.quote(remote_tmp)}", use_sudo=True)
            raise SSHCommandError(
                f"No se pudo instalar {status.js_path}: {place.stderr or place.stdout}"
            )

        msgs = [f"Subido {status.js_path}."]
        yaml_msg = self._ensure_yaml(status.yaml_path)
        msgs.append(yaml_msg)
        if restart:
            msgs.append(self._ha.restart_ha())
        return " ".join(msgs)

    def remove(self) -> str:
        status = self.get_status()
        msgs: list[str] = []
        if status.js_exists:
            self.ssh.run(f"rm -f {shlex.quote(status.js_path)}", use_sudo=True)
            msgs.append(f"Eliminado {status.js_path}.")
        if status.yaml_exists and (status.yaml_ok or status.has_iframe_zwave):
            raw = self.ssh.run(f"cat {shlex.quote(status.yaml_path)}").stdout
            backup = status.yaml_path + ".bak.horus"
            self.ssh.run(
                f"cp {shlex.quote(status.yaml_path)} {shlex.quote(backup)}",
                use_sudo=True,
            )
            updated = _strip_iframe_zwave(_strip_panel(raw))
            self._ha._write_remote_file(status.yaml_path, updated)
            msgs.append(f"Quitado panel Z-Wave de {status.yaml_path}.")
        return " ".join(msgs) or "No había panel Z-Wave instalado."

    def _ensure_yaml(self, path: str) -> str:
        raw = self.ssh.run(f"cat {shlex.quote(path)}").stdout
        had_iframe = _has_iframe_zwave(raw)
        had_panel = PANEL_NAME in raw
        base = _strip_iframe_zwave(_strip_panel(raw) if had_panel else raw)
        injected = _inject_panel(base)
        if _norm_yaml(injected) == _norm_yaml(raw) and not had_iframe:
            return "configuration.yaml ya tiene zwave-ui-panel."

        backup = path + ".bak.horus"
        self.ssh.run(f"cp {shlex.quote(path)} {shlex.quote(backup)}", use_sudo=True)
        self._ha._write_remote_file(path, injected)
        parts: list[str] = []
        if had_iframe:
            parts.append("Eliminado panel_iframe zwave (legado).")
        if had_panel:
            parts.append(f"Actualizado panel_custom ({JS_NAME}?v={JS_VERSION}).")
        elif not had_panel:
            parts.append(f"Añadido panel_custom en {path}.")
        return " ".join(parts) or f"Actualizado {path}."


def resolve_zwave_panel_js(local_js: str) -> Path:
    """Acepta zwave-panel.js o la carpeta panel_zwave_js_ui/."""
    local = Path(local_js).expanduser()
    if local.is_dir():
        candidate = local / JS_NAME
        if candidate.is_file():
            return candidate
        raise ValidationError(f"No existe {JS_NAME} en la carpeta: {local}")
    if local.is_file():
        return local
    raise ValidationError(f"No existe el JS local: {local}")


def _norm_yaml(raw: str) -> str:
    return raw.replace("\r\n", "\n").strip() + "\n"


def _has_iframe_zwave(raw: str) -> bool:
    in_iframe = False
    for line in raw.splitlines():
        if line.startswith("panel_iframe:"):
            in_iframe = True
            continue
        if in_iframe:
            if line.strip() and not line.startswith((" ", "\t")):
                in_iframe = False
            elif line.startswith("  zwave:") or line.startswith("  zwave :"):
                return True
    return False


def _inject_panel(raw: str) -> str:
    text = raw if raw.endswith("\n") else raw + "\n"
    lines = text.splitlines()
    pc_idx = next((i for i, line in enumerate(lines) if line.startswith("panel_custom:")), None)
    if pc_idx is None:
        return text.rstrip() + "\n\n" + PANEL_BLOCK
    end = len(lines)
    for j in range(pc_idx + 1, len(lines)):
        line = lines[j]
        if line.strip() and not line.startswith((" ", "\t")):
            end = j
            break
    item_lines = PANEL_ITEM.rstrip("\n").splitlines()
    return "\n".join(lines[:end] + item_lines + lines[end:]).rstrip() + "\n"


def _strip_panel(raw: str) -> str:
    lines = raw.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == f"- name: {PANEL_NAME}":
            i += 1
            while i < len(lines) and lines[i].startswith("    "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    cleaned: list[str] = []
    i = 0
    while i < len(out):
        if out[i].startswith("panel_custom:"):
            j = i + 1
            while j < len(out) and (not out[j].strip() or out[j].startswith((" ", "\t"))):
                j += 1
            children = [ln for ln in out[i + 1 : j] if ln.strip()]
            if not children:
                i = j
                continue
        cleaned.append(out[i])
        i += 1
    return "\n".join(cleaned).rstrip() + "\n"


def _strip_iframe_zwave(raw: str) -> str:
    lines = raw.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("panel_iframe:"):
            block = [lines[i]]
            i += 1
            while i < len(lines):
                ln = lines[i]
                if ln.strip() and not ln.startswith((" ", "\t")):
                    break
                block.append(ln)
                i += 1
            out.extend(_filter_iframe_block(block))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).rstrip() + "\n"


def _filter_iframe_block(block: list[str]) -> list[str]:
    result = [block[0]]
    i = 1
    while i < len(block):
        ln = block[i]
        if ln.startswith("  zwave:") or ln.startswith("  zwave :"):
            i += 1
            while i < len(block) and (block[i].startswith("    ") or not block[i].strip()):
                i += 1
            continue
        result.append(ln)
        i += 1
    children = [ln for ln in result[1:] if ln.strip()]
    if not children:
        return []
    return result
