"""Diagnóstico y autoreparación segura de HA / Z-Wave (self-heal)."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import SelfHealStatus

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from ssh_client import SSHClient

_PREFERRED_WS_URL = "ws://127.0.0.1:3000"
_SERVICE_CANDIDATES = (
    "zwave-ui.service",
    "zwave-js-ui.service",
    "zwavejs-ui.service",
)
_CPU_HIGH_THRESHOLD = 90.0
_DISK_LOW_THRESHOLD = 15.0


class SelfHealManager:
    """Detecta fallos HA/Z-Wave y aplica reparaciones de menor a mayor impacto."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager):
        self.ssh = ssh
        self.ha_config = ha_config
        self._service_name: str = ""

    def diagnose(self) -> SelfHealStatus:
        status = SelfHealStatus()
        try:
            status.ha_container = self.ha_config._detect_container(include_stopped=True)
            status.zwave_service_name = self._detect_service_name()
            self._check_ha_runtime(status)
            self._check_ha_logs(status)
            self._check_zwave(status)
            self._check_disk(status)
            self._set_recommendation(status)
        except Exception as exc:  # noqa: BLE001 — diagnóstico no debe tumbar el menú
            status.error = str(exc)
            status.severity = "s4"
            status.recommended_action = "review_manual"
            status.action_detail = f"Error durante diagnóstico: {exc}"
        return status

    def run_auto_repair(self) -> str:
        """Ejecuta la reparación mínima según diagnóstico actual."""
        before = self.diagnose()
        action = before.recommended_action
        if action in ("none", "review_manual"):
            return before.action_detail or "Sin acción automática recomendada."

        msgs: list[str] = []
        if action == "fix_zwave_url":
            msgs.append(self.fix_zwave_ws_url(restart=True))
        elif action == "restart_zwave":
            msgs.append(self.restart_zwave_service())
            after_svc = self.diagnose()
            if not after_svc.port_3000_open:
                msgs.append(self.ha_config.restart_ha())
        elif action == "repair_discovery_keys":
            msgs.append(self.ha_config.repair_config_entries_schema())
            msgs.append(self.ha_config.restart_ha())
        elif action == "clean_db_wal":
            msgs.append(self.clean_db_wal_shm(restart=True))
        elif action == "restart_ha":
            msgs.append(self.ha_config.restart_ha())
        elif action == "escalate_ha_then_schema":
            msgs.append(self.ha_config.restart_ha())
            self.ssh.run("sleep 20")
            mid = self.diagnose()
            if mid.log_discovery_keys_error or not mid.ha_port_8123_ok:
                msgs.append(self.ha_config.repair_config_entries_schema())
                msgs.append(self.ha_config.restart_ha())
        else:
            return f"Acción desconocida: {action}"

        after = self.diagnose()
        summary = " | ".join(msgs)
        return (
            f"{summary} | Post: severity={after.severity}, "
            f"8123={'OK' if after.ha_port_8123_ok else 'FAIL'}, "
            f"3000={'OK' if after.port_3000_open else 'FAIL'}, "
            f"ws={after.ha_zwave_ws_url or '-'}."
        )

    def restart_zwave_service(self) -> str:
        service = self._detect_service_name()
        result = self.ssh.run(f"systemctl restart {shlex.quote(service)}", use_sudo=True)
        if not result.ok:
            msg = result.stderr or result.stdout or f"No se pudo reiniciar {service}."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        self.ssh.run("sleep 12")
        return f"Servicio {service} reiniciado."

    def fix_zwave_ws_url(self, url: str = _PREFERRED_WS_URL, restart: bool = True) -> str:
        """Actualiza solo la URL de domain=zwave_js (sin borrar entries)."""
        path = f"{self.ha_config._detect_config_dir()}/.storage/core.config_entries"
        container = self.ha_config._detect_container(include_stopped=True)
        if container:
            self.ssh.run(f"docker stop {shlex.quote(container)}", timeout=60)

        script = f"""
import json, os
from datetime import datetime, timezone
path = {json.dumps(path)}
want = {json.dumps(url)}

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

if not os.path.isfile(path):
    print("ERR")
    print("ABSENT")
    raise SystemExit(1)

with open(path) as fh:
    doc = json.load(fh)
entries = (doc.get("data") or {{}}).get("entries") or []
changed = 0
found = 0
for entry in entries:
    if not isinstance(entry, dict) or entry.get("domain") != "zwave_js":
        continue
    found += 1
    data = entry.setdefault("data", {{}})
    if not isinstance(data, dict):
        data = {{}}
        entry["data"] = data
    if data.get("url") != want:
        data["url"] = want
        entry["modified_at"] = now()
        if "discovery_keys" not in entry or not isinstance(entry.get("discovery_keys"), dict):
            entry["discovery_keys"] = {{}}
        if "subentries" not in entry or entry.get("subentries") is None:
            entry["subentries"] = []
        changed += 1

if found == 0:
    print("ERR")
    print("NO_ENTRY")
    raise SystemExit(1)

if changed == 0:
    print("OK")
    print("UNCHANGED")
    print(want)
    raise SystemExit(0)

backup = path + ".bak.horus.zwave_ws"
with open(backup, "w") as fh:
    with open(path) as src:
        fh.write(src.read())

tmp = path + ".tmp_horus_zwave_ws"
with open(tmp, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\\n")
os.replace(tmp, path)
print("OK")
print("UPDATED")
print(want)
print(backup)
print(str(changed))
"""
        res = self.ssh.run(f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            if container:
                self.ssh.run(f"docker start {shlex.quote(container)}", timeout=60)
            raise SSHCommandError(
                f"No se pudo actualizar URL Z-Wave: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

        auth_path = f"{self.ha_config._detect_config_dir()}/.storage/auth"
        self.ssh.run(
            f"if test -f {shlex.quote(auth_path)}; then "
            f"chown --reference={shlex.quote(auth_path)} {shlex.quote(path)} && "
            f"chmod --reference={shlex.quote(auth_path)} {shlex.quote(path)}; "
            "fi",
            use_sudo=True,
        )

        action = lines[1] if len(lines) > 1 else "UNCHANGED"
        note = f"URL Z-Wave → {url} ({action})."
        if action == "UPDATED" and len(lines) > 3:
            note = f"URL Z-Wave → {url}. Backup: {lines[3]}."

        if restart and container:
            start = self.ssh.run(f"docker start {shlex.quote(container)}", timeout=60)
            if not start.ok:
                raise SSHCommandError(
                    f"{note} Pero falló docker start: {start.stderr}",
                    exit_code=start.exit_code,
                    stderr=start.stderr,
                )
            note += f" Contenedor '{container}' iniciado."
        return note

    def clean_db_wal_shm(self, restart: bool = True) -> str:
        """Elimina solo -shm/-wal tras backup; no borra la DB principal."""
        config = self.ha_config._detect_config_dir()
        container = self.ha_config._detect_container(include_stopped=True)
        if not config:
            raise SSHCommandError("No se detectó ruta de config de Home Assistant.")

        if container:
            self.ssh.run(f"docker stop {shlex.quote(container)}", timeout=60)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        db = f"{config}/home-assistant_v2.db"
        shm = f"{db}-shm"
        wal = f"{db}-wal"
        backup_dir = f"{config}/.horus_db_rescue_{stamp}"

        script = f"""
import os, shutil
db = {json.dumps(db)}
shm = {json.dumps(shm)}
wal = {json.dumps(wal)}
bdir = {json.dumps(backup_dir)}
os.makedirs(bdir, exist_ok=True)
moved = []
for path in (shm, wal):
    if os.path.isfile(path):
        dest = os.path.join(bdir, os.path.basename(path))
        shutil.move(path, dest)
        moved.append(os.path.basename(path))
print("OK")
print(",".join(moved) if moved else "NONE")
print(bdir)
"""
        res = self.ssh.run(f"python3 -c {shlex.quote(script)}", timeout=60, use_sudo=True)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if not res.ok or not lines or lines[0] != "OK":
            detail = (res.stderr or res.stdout or "sin respuesta").strip()
            if container:
                self.ssh.run(f"docker start {shlex.quote(container)}", timeout=60)
            raise SSHCommandError(
                f"No se pudo limpiar shm/wal: {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

        moved = lines[1] if len(lines) > 1 else "NONE"
        bdir = lines[2] if len(lines) > 2 else backup_dir
        note = (
            f"Residuos DB movidos a {bdir} ({moved})."
            if moved != "NONE"
            else f"No había -shm/-wal. Backup dir: {bdir}."
        )

        if restart and container:
            start = self.ssh.run(f"docker start {shlex.quote(container)}", timeout=60)
            if not start.ok:
                raise SSHCommandError(
                    f"{note} Pero falló docker start: {start.stderr}",
                    exit_code=start.exit_code,
                    stderr=start.stderr,
                )
            note += f" Contenedor '{container}' iniciado."
        return note

    # --- checks ---

    def _detect_service_name(self) -> str:
        if self._service_name:
            return self._service_name
        for candidate in _SERVICE_CANDIDATES:
            check = self.ssh.run(
                f"systemctl list-unit-files {shlex.quote(candidate)} 2>/dev/null "
                f"| grep -q {shlex.quote(candidate)} && echo OK"
            )
            if check.stdout.strip() == "OK":
                self._service_name = candidate
                return candidate
        listed = self.ssh.run(
            "systemctl list-unit-files --type=service 2>/dev/null "
            "| grep -iE 'zwave.*\\.service' | awk '{print $1}' | head -1"
        )
        name = listed.stdout.strip()
        self._service_name = name or _SERVICE_CANDIDATES[0]
        return self._service_name

    def _check_ha_runtime(self, status: SelfHealStatus) -> None:
        if not status.ha_container:
            return
        running = self.ssh.run(
            f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(status.ha_container)} 2>/dev/null"
        )
        status.ha_running = running.stdout.strip().lower() == "true"

        probe = self.ssh.run(
            "nc -zv localhost 8123 2>&1 || curl -s -o /dev/null -w '%{http_code}' "
            "--connect-timeout 3 http://127.0.0.1:8123/ || true"
        )
        combined = (probe.stdout + probe.stderr).lower()
        status.ha_port_8123_ok = (
            "succeeded" in combined
            or "open" in combined
            or any(code in combined for code in ("200", "302", "401", "403"))
        )

        if status.ha_running:
            stats = self.ssh.run(
                "docker stats --no-stream --format '{{.CPUPerc}}' "
                f"{shlex.quote(status.ha_container)} 2>/dev/null"
            )
            raw = stats.stdout.strip().replace("%", "")
            try:
                status.ha_cpu_percent = float(raw)
            except ValueError:
                status.ha_cpu_percent = -1.0
            status.ha_cpu_high = status.ha_cpu_percent >= _CPU_HIGH_THRESHOLD

    def _check_ha_logs(self, status: SelfHealStatus) -> None:
        if not status.ha_container:
            return
        logs = self.ssh.run(
            f"docker logs --tail 120 {shlex.quote(status.ha_container)} 2>&1"
        )
        text = logs.stdout or ""
        status.log_discovery_keys_error = "KeyError: 'discovery_keys'" in text or (
            "discovery_keys" in text and "KeyError" in text
        )
        status.log_db_corrupt = "not shutdown cleanly" in text.lower()
        status.log_zwave_ws_error = bool(
            re.search(r"zwave.?js|websocket.*(fail|error|refused)", text, re.I)
        )
        interesting = []
        for line in text.splitlines():
            low = line.lower()
            if any(
                k in low
                for k in (
                    "discovery_keys",
                    "not shutdown cleanly",
                    "zwave",
                    "keyerror",
                    "traceback",
                )
            ):
                interesting.append(line.strip()[:140])
        status.log_sample = interesting[-8:]

    def _check_zwave(self, status: SelfHealStatus) -> None:
        svc = status.zwave_service_name or self._detect_service_name()
        status.zwave_service_name = svc
        active = self.ssh.run(f"systemctl is-active {shlex.quote(svc)} 2>/dev/null")
        status.zwave_service_active = active.stdout.strip() in ("active", "activating")

        proc = self.ssh.run("ps aux | grep -E 'zwave-js-ui|zwavejs' | grep -v grep")
        status.zwave_process_running = bool(proc.stdout.strip())

        ports = self.ssh.run("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null")
        status.port_3000_open = ":3000" in (ports.stdout or "")

        if not status.ha_container:
            return
        script = """
import json
try:
    with open('/config/.storage/core.config_entries') as f:
        data = json.load(f)
    for entry in data.get('data', {}).get('entries', []):
        if entry.get('domain') == 'zwave_js':
            print(entry.get('data', {}).get('url', ''))
            break
except Exception as exc:
    print('ERR', exc)
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(status.ha_container)} python3 -c {shlex.quote(script)}"
        )
        url = (result.stdout or "").strip().splitlines()
        status.ha_zwave_ws_url = url[0].strip() if url and not url[0].startswith("ERR") else ""
        status.zwave_ws_url_ok = self._is_ws_url_ok(status.ha_zwave_ws_url)

    def _check_disk(self, status: SelfHealStatus) -> None:
        df = self.ssh.run("df -P / | awk 'NR==2 {print $5}'")
        raw = df.stdout.strip().replace("%", "")
        try:
            used = float(raw)
            status.disk_free_pct = 100.0 - used
            status.disk_low = status.disk_free_pct < _DISK_LOW_THRESHOLD
        except ValueError:
            status.disk_free_pct = -1.0

    @staticmethod
    def _is_ws_url_ok(url: str) -> bool:
        if not url or "3000" not in url:
            return False
        return any(h in url for h in ("127.0.0.1", "localhost", "172.17.0.1"))

    def _set_recommendation(self, status: SelfHealStatus) -> None:
        if status.disk_low:
            status.severity = "s3"
            status.recommended_action = "review_manual"
            status.action_detail = (
                f"Disco bajo ({status.disk_free_pct:.0f}% libre). "
                "Libere espacio antes de reparar (menú Espacios / limpieza)."
            )
            return

        if status.log_discovery_keys_error:
            status.severity = "s2"
            status.recommended_action = "repair_discovery_keys"
            status.action_detail = (
                "Log con KeyError discovery_keys. "
                "Reparar schema de core.config_entries y reiniciar HA."
            )
            return

        if status.log_db_corrupt and not status.ha_port_8123_ok:
            status.severity = "s3"
            status.recommended_action = "clean_db_wal"
            status.action_detail = (
                "DB no cerró limpiamente y HA no responde en :8123. "
                "Limpiar solo -shm/-wal (con backup) e iniciar HA."
            )
            return

        if (not status.ha_port_8123_ok) and status.ha_cpu_high:
            status.severity = "s4"
            status.recommended_action = "escalate_ha_then_schema"
            status.action_detail = (
                f"HA no responde en :8123 y CPU alta ({status.ha_cpu_percent:.0f}%). "
                "Reiniciar contenedor; si persiste, reparar discovery_keys."
            )
            return

        if not status.ha_port_8123_ok:
            status.severity = "s4"
            status.recommended_action = "restart_ha"
            status.action_detail = (
                "Puerto 8123 no responde. Reiniciar contenedor Home Assistant."
            )
            return

        if not status.port_3000_open or not status.zwave_service_active:
            status.severity = "s1"
            status.recommended_action = "restart_zwave"
            status.action_detail = (
                f"Z-Wave caído (3000={'abierto' if status.port_3000_open else 'cerrado'}, "
                f"servicio={'activo' if status.zwave_service_active else 'inactivo'}). "
                f"Reiniciar {status.zwave_service_name}; si falla, reiniciar HA."
            )
            return

        if status.ha_zwave_ws_url and not status.zwave_ws_url_ok:
            status.severity = "s0"
            status.recommended_action = "fix_zwave_url"
            status.action_detail = (
                f"URL Z-Wave incorrecta ({status.ha_zwave_ws_url}). "
                f"Corregir a {_PREFERRED_WS_URL}."
            )
            return

        if not status.ha_zwave_ws_url and status.port_3000_open:
            status.severity = "s0"
            status.recommended_action = "review_manual"
            status.action_detail = (
                "Puerto 3000 OK pero no hay entry zwave_js en HA. "
                "Configure la integración manualmente en Home Assistant."
            )
            return

        status.severity = "ok"
        status.recommended_action = "none"
        status.action_detail = "HA y Z-Wave responden correctamente. Sin reparación necesaria."
