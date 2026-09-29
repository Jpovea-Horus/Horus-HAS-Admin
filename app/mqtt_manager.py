"""Diagnóstico y corrección MQTT en Z-Wave JS UI (runbook Horus v2)."""

from __future__ import annotations

import json
import shlex
from datetime import date, timedelta
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import MqttDiagnosticStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

from paths import REMOTE_ZWAVE_STORE

_STORE_CANDIDATES = (
    REMOTE_ZWAVE_STORE,
    "/opt/zwave-js-ui-store",
)
_SETTINGS_NAME = "settings.json"
_SERVICE_CANDIDATES = (
    "zwave-ui.service",
    "zwave-js-ui.service",
    "zwavejs-ui.service",
)
# Patrones alineados con logs reales (ERROR MQTT: Mqtt client error, AggregateError, etc.)
_LOG_ERROR_PATTERN = (
    r"ERROR MQTT:|Mqtt client error|mqtt client reconnecting|"
    r"MQTT client closed|AggregateError"
)


class MqttManager:
    """Diagnóstico MQTT según runbook_mqtt_horus_v2."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh
        self._store_path: str = ""
        self._service_name: str = ""

    def _detect_store_path(self) -> str:
        if self._store_path:
            return self._store_path

        for candidate in _STORE_CANDIDATES:
            path = f"{candidate}/{_SETTINGS_NAME}"
            check = self.ssh.run(f"test -f {shlex.quote(path)} && echo OK")
            if check.stdout.strip() == "OK":
                self._store_path = candidate
                return candidate

        found = self.ssh.run(
            "find /home/cat /opt /srv -maxdepth 5 "
            f"-path '*/zwave*/{_SETTINGS_NAME}' 2>/dev/null | head -1"
        )
        if found.stdout.strip():
            self._store_path = found.stdout.strip().rsplit("/", 1)[0]
            return self._store_path

        return ""

    def _detect_service_name(self) -> str:
        if self._service_name:
            return self._service_name

        for candidate in _SERVICE_CANDIDATES:
            check = self.ssh.run(
                f"systemctl list-unit-files {shlex.quote(candidate)} 2>/dev/null "
                "| grep -q {0} && echo OK".format(candidate)
            )
            if check.stdout.strip() == "OK":
                self._service_name = candidate
                return candidate

        listed = self.ssh.run(
            "systemctl list-unit-files --type=service 2>/dev/null "
            "| grep -iE 'zwave.*\\.service' | awk '{print $1}' | head -1"
        )
        name = listed.stdout.strip()
        if name:
            self._service_name = name
            return name

        return _SERVICE_CANDIDATES[0]

    @property
    def settings_path(self) -> str:
        base = self._detect_store_path()
        return f"{base}/{_SETTINGS_NAME}" if base else ""

    def diagnose(self) -> MqttDiagnosticStatus:
        store = self._detect_store_path()
        status = MqttDiagnosticStatus(store_path=store)
        status.service_name = self._detect_service_name()

        if not store:
            status.recommended_action = "store_not_found"
            status.action_detail = (
                "No se encontró zwave-js-ui-store/settings.json en el controlador."
            )
            return status

        status.settings_found = True
        self._read_mqtt_settings(status)
        self._check_mqtt_logs(status, store)
        self._check_zwave_service(status)
        self._check_ports_and_broker(status)
        self._check_home_assistant(status)
        self._set_recommendation(status)
        return status

    def _read_mqtt_settings(self, status: MqttDiagnosticStatus) -> None:
        script = f"""
import json
path = {json.dumps(self.settings_path)}
try:
    with open(path) as f:
        data = json.load(f)
    mqtt = data.get("mqtt") or {{}}
    disabled = bool(mqtt.get("disabled", False))
    enabled = mqtt.get("enabled")
    host = mqtt.get("host", "")
    port = mqtt.get("port", 1883)
    print("OK")
    print(disabled)
    print("" if enabled is None else enabled)
    print(host)
    print(port)
except Exception as exc:
    print("ERR")
    print(exc)
"""
        result = self.ssh.run(f"python3 -c {shlex.quote(script)}")
        lines = result.stdout.splitlines()
        if not lines or lines[0] != "OK" or len(lines) < 5:
            return

        disabled = lines[1].strip().lower() in ("true", "1")
        enabled_raw = lines[2].strip()
        if enabled_raw == "":
            mqtt_enabled = None
        else:
            mqtt_enabled = enabled_raw.lower() in ("true", "1")

        status.mqtt_host = lines[3].strip()
        try:
            status.mqtt_port = int(lines[4].strip())
        except ValueError:
            status.mqtt_port = 1883

        # MQTT activo en config: disabled=false y (enabled no definido o enabled=true)
        if mqtt_enabled is None:
            status.mqtt_disabled = disabled
        else:
            status.mqtt_disabled = disabled or not mqtt_enabled

    def _check_mqtt_logs(self, status: MqttDiagnosticStatus, store: str) -> None:
        logs_dir = shlex.quote(f"{store}/logs")
        pattern = shlex.quote(_LOG_ERROR_PATTERN)

        # Hoy, ayer y búsqueda amplia en zwavejs*.log
        dates = [
            date.today().strftime("%Y-%m-%d"),
            (date.today() - timedelta(days=1)).strftime("%Y-%m-%d"),
        ]
        chunks: list[str] = []
        for day in dates:
            log_file = shlex.quote(f"{store}/logs/zwavejs_{day}.log")
            cmd = f"grep -iE {pattern} {log_file} 2>/dev/null | tail -15"
            res = self.ssh.run(cmd)
            if res.stdout.strip():
                chunks.extend(res.stdout.splitlines())

        if not chunks:
            wide = self.ssh.run(
                f"grep -ihE {pattern} {logs_dir}/zwavejs*.log 2>/dev/null | tail -25"
            )
            chunks = [ln for ln in wide.stdout.splitlines() if ln.strip()]

        status.mqtt_log_sample = chunks[-10:] if len(chunks) > 10 else chunks
        status.has_mqtt_log_errors = len(chunks) > 0

    def _check_zwave_service(self, status: MqttDiagnosticStatus) -> None:
        proc = self.ssh.run("ps aux | grep -E 'zwave-js-ui|zwavejs' | grep -v grep")
        status.zwave_ui_process_running = bool(proc.stdout.strip())

        svc = self.ssh.run(f"systemctl is-active {shlex.quote(status.service_name)} 2>/dev/null")
        status.zwave_ui_service_active = svc.stdout.strip() in ("active", "activating")

    def _check_ports_and_broker(self, status: MqttDiagnosticStatus) -> None:
        ports = self.ssh.run("ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null")
        text = ports.stdout
        status.port_3000_open = ":3000" in text
        status.port_8091_open = ":8091" in text
        status.port_1883_open = ":1883" in text

        probe = self.ssh.run("nc -zv localhost 1883 2>&1 || true")
        combined = (probe.stdout + probe.stderr).lower()
        if "succeeded" in combined or "open" in combined:
            status.broker_probe = "succeeded"
        elif "refused" in combined:
            status.broker_probe = "refused"
        else:
            status.broker_probe = "unknown"

        mosq = self.ssh.run("pgrep -a mosquitto 2>/dev/null")
        status.mosquitto_running = bool(mosq.stdout.strip())

    def _check_home_assistant(self, status: MqttDiagnosticStatus) -> None:
        ha_ps = self.ssh.run(
            "docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'homeassistant|home-assistant' | head -1"
        )
        container = ha_ps.stdout.strip().splitlines()[0] if ha_ps.stdout.strip() else ""
        if not container:
            return

        status.ha_container_found = True
        script = """
import json
try:
    with open('/config/.storage/core.config_entries') as f:
        data = json.load(f)
    for entry in data.get('data', {}).get('entries', []):
        domain = entry.get('domain', '')
        if domain == 'zwave_js':
            print('ZWAVE', entry.get('data', {}).get('url', ''))
        elif domain == 'mqtt':
            print('MQTT', entry.get('title', ''))
except Exception as exc:
    print('ERR', exc)
"""
        result = self.ssh.run(
            f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(script)}"
        )
        for line in result.stdout.splitlines():
            if line.startswith("ZWAVE "):
                status.ha_zwave_ws_url = line[6:].strip()
            elif line.startswith("MQTT "):
                status.ha_mqtt_integration = line[5:].strip()

    def _mqtt_still_enabled(self, status: MqttDiagnosticStatus) -> bool:
        """True si el cliente MQTT sigue habilitado en settings.json."""
        return status.mqtt_disabled is False

    def _set_recommendation(self, status: MqttDiagnosticStatus) -> None:
        if status.mqtt_disabled is True:
            if status.has_mqtt_log_errors:
                status.recommended_action = "restart_required"
                status.action_detail = (
                    "mqtt.disabled=true pero aún hay errores en log. "
                    "Reinicie el servicio o verifique que no exista otro settings.json."
                )
            else:
                status.recommended_action = "none_already_disabled"
                status.action_detail = "MQTT ya está deshabilitado en settings.json."
            return

        broker_down = status.broker_probe == "refused" or (
            not status.port_1883_open and status.broker_probe != "succeeded"
        )

        if status.port_1883_open or status.broker_probe == "succeeded":
            status.recommended_action = "audit_auth"
            status.action_detail = (
                "Puerto 1883 activo: revise credenciales en UI :8091. No deshabilite MQTT aquí."
            )
            return

        ws_ok = "3000" in status.ha_zwave_ws_url and (
            "localhost" in status.ha_zwave_ws_url or "127.0.0.1" in status.ha_zwave_ws_url
        )

        if status.has_mqtt_log_errors or (self._mqtt_still_enabled(status) and broker_down):
            if ws_ok or status.port_3000_open:
                status.recommended_action = "disable_mqtt"
                status.action_detail = (
                    "Errores MQTT / broker ausente en :1883. Deshabilitar mqtt "
                    f"(disabled:true) y reiniciar {status.service_name}."
                )
            else:
                status.recommended_action = "verify_ha_first"
                status.action_detail = (
                    "Confirme zwave_js → ws://localhost:3000 en HA antes de deshabilitar MQTT."
                )
            return

        if self._mqtt_still_enabled(status) and broker_down:
            status.recommended_action = "disable_mqtt"
            status.action_detail = "MQTT habilitado sin broker en 1883. Se recomienda deshabilitar."
            return

        status.recommended_action = "none_no_errors"
        status.action_detail = "Sin indicios de problema MQTT. No modificar."

    def disable_mqtt(self) -> str:
        """Backup, disabled:true, enabled:false, reinicio y verificación."""
        store = self._detect_store_path()
        if not store:
            raise SSHCommandError(
                "No se encontró zwave-js-ui-store en el controlador.",
                exit_code=1,
                stderr="",
            )

        settings = self.settings_path
        service = self._detect_service_name()
        backup = f"{settings}.bak.{date.today().strftime('%Y%m%d')}"

        owner = self.ssh.run(f"stat -c '%U:%G' {shlex.quote(settings)} 2>/dev/null")
        owner_str = owner.stdout.strip()

        cp = self.ssh.run(f"cp {shlex.quote(settings)} {shlex.quote(backup)}", use_sudo=True)
        if not cp.ok:
            msg = cp.stderr or cp.stdout or "No se pudo crear el backup."
            raise SSHCommandError(msg, exit_code=cp.exit_code, stderr=cp.stderr)

        patch_script = f"""
import json
path = {json.dumps(settings)}
with open(path) as f:
    data = json.load(f)
mqtt = data.setdefault("mqtt", {{}})
mqtt["disabled"] = True
if "enabled" in mqtt:
    mqtt["enabled"] = False
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\\n")
print("OK")
"""
        patch = self.ssh.run(f"python3 -c {shlex.quote(patch_script)}", use_sudo=True)
        if not patch.ok or patch.stdout.strip() != "OK":
            msg = patch.stderr or patch.stdout or "No se pudo actualizar settings.json."
            raise SSHCommandError(msg, exit_code=patch.exit_code, stderr=patch.stderr)

        if owner_str and ":" in owner_str:
            self.ssh.run(
                f"chown {owner_str} {shlex.quote(settings)} {shlex.quote(backup)}",
                use_sudo=True,
            )

        verify = self._verify_settings_disabled()
        if not verify:
            raise SSHCommandError(
                "settings.json no refleja mqtt.disabled=true tras la escritura.",
                exit_code=1,
                stderr="",
            )

        restart = self.ssh.run(f"systemctl restart {shlex.quote(service)}", use_sudo=True)
        if not restart.ok:
            msg = restart.stderr or restart.stdout or f"No se pudo reiniciar {service}."
            raise SSHCommandError(msg, exit_code=restart.exit_code, stderr=restart.stderr)

        self.ssh.run("sleep 10")

        post = self.diagnose()
        still_errors = post.has_mqtt_log_errors and post.mqtt_disabled is True
        verify_note = (
            "Aún aparecen líneas MQTT antiguas en el log; espere 1 min y actualice diagnóstico."
            if still_errors
            else "Diagnóstico post-fix: mqtt deshabilitado correctamente."
        )

        return (
            f"Corrección aplicada en {settings}. Backup: {backup}. "
            f"Servicio reiniciado: {service}. {verify_note}"
        )

    def _verify_settings_disabled(self) -> bool:
        script = f"""
import json
with open({json.dumps(self.settings_path)}) as f:
    mqtt = json.load(f).get("mqtt") or {{}}
print(mqtt.get("disabled") is True)
"""
        result = self.ssh.run(f"python3 -c {shlex.quote(script)}")
        return result.stdout.strip().lower() == "true"

    def restart_service(self) -> str:
        service = self._detect_service_name()
        result = self.ssh.run(f"systemctl restart {shlex.quote(service)}", use_sudo=True)
        if not result.ok:
            msg = result.stderr or result.stdout or f"No se pudo reiniciar {service}."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        self.ssh.run("sleep 8")
        return f"Servicio {service} reiniciado."
