"""Soporte: semáforo de estado, visor de logs, pruebas de red e informe exportable."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import time
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from exceptions import NotConnectedError, SSHCommandError, ValidationError
from models import (
    HealthCheckItem,
    HealthCheckStatus,
    LogSource,
    NetworkTestItem,
    NetworkTestReport,
    SessionInfo,
    SupportReportResult,
)
from paths import APP_NAME, APP_VERSION, get_local_support_dir

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from self_heal_manager import SelfHealManager
    from ssh_client import SSHClient

_DISK_WARN_PCT = 85
_DISK_FAIL_PCT = 95
_CLOCK_DRIFT_FAIL_S = 120
_HA_HTTP_OK = {"200", "302", "401", "403", "404"}
_MAX_TAIL_FETCH = 5000  # paramiko puede bloquearse con salidas > ventana del canal
_REPORT_HA_LINES = 500
_REPORT_JOURNAL_LINES = 300
_REPORT_ERROR_EXCERPT = 25

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ERROR_UPPER_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|ERR)\b")
_WARN_UPPER_RE = re.compile(r"\b(WARNING|WARN|WRN)\b")
_INFO_UPPER_RE = re.compile(r"\b(INFO|DEBUG|INF|DBG)\b")
_ERROR_ANY_RE = re.compile(
    r"\b(error|critical|fatal|exception|traceback|failed)\b|\w+Error:", re.I
)
_WARN_ANY_RE = re.compile(r"\bwarn(ing)?\b", re.I)
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,253}$")

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|password|passwd|secret|token|authorization)", re.I)
_SECRET_PATTERNS = (
    (re.compile(r"(--token[=\s]+)\S+", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (
        re.compile(
            r"((?:api[_-]?key|password|passwd|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+",
            re.I,
        ),
        r"\1***",
    ),
)

_HEALTH_SCRIPT = r"""
port() { timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" >/dev/null 2>&1 && echo open || echo closed; }
echo "p8123=$(port 8123)"
echo "p3000=$(port 3000)"
echo "p8091=$(port 8091)"
echo "ha_http=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 http://127.0.0.1:8123/ 2>/dev/null)"
echo "disk=$(df -P / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
echo "epoch=$(date +%s)"
ntp=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
[ -z "$ntp" ] && ntp=$(timedatectl status 2>/dev/null | awk -F': ' 'tolower($0) ~ /synchronized/ {print $2; exit}')
echo "ntp=$ntp"
echo "tz=$(timedatectl show -p Timezone --value 2>/dev/null)"
echo "cf_installed=$(command -v cloudflared >/dev/null 2>&1 && echo yes || echo no)"
echo "cf_active=$(pgrep -x cloudflared >/dev/null 2>&1 && echo yes || echo no)"
echo "zt_installed=$(command -v zerotier-cli >/dev/null 2>&1 && echo yes || echo no)"
echo "zt_info=$(zerotier-cli info 2>/dev/null | head -1)"
echo "failed=$(systemctl --failed --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
"""

_NETWORK_SCRIPT = r"""
pingt() {
  out=$(ping -c 3 -W 2 "$1" 2>&1)
  loss=$(echo "$out" | grep -oE '[0-9.]+% packet loss' | cut -d% -f1)
  avg=$(echo "$out" | awk -F'/' '/^rtt|^round-trip/ {print $5}')
  echo "${loss:-100}|${avg}"
}
web() { curl -s -o /dev/null -w '%{http_code}|%{time_connect}|%{time_total}' --max-time 8 "$1" 2>/dev/null; }
gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
echo "gw=$gw"
[ -n "$gw" ] && echo "ping_gw=$(pingt "$gw")"
echo "ping_inet=$(pingt 8.8.8.8)"
echo "dns_servers=$(awk '/^nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null | tr '\n' ' ')"
for h in google.com rhorus.com region1.v2.argotunnel.com my.zerotier.com; do
  t0=$(date +%s%N); ip=$(timeout 6 getent hosts "$h" | awk '{print $1; exit}'); t1=$(date +%s%N)
  echo "dns|$h=${ip}|$(( (t1 - t0) / 1000000 ))"
done
echo "cf_https=$(web https://www.cloudflare.com/cdn-cgi/trace)"
echo "cf_7844=$(timeout 4 bash -c 'exec 3<>/dev/tcp/region1.v2.argotunnel.com/7844' >/dev/null 2>&1 && echo open || echo closed)"
echo "cf_active=$(pgrep -x cloudflared >/dev/null 2>&1 && echo yes || echo no)"
echo "zt_installed=$(command -v zerotier-cli >/dev/null 2>&1 && echo yes || echo no)"
echo "zt_info=$(zerotier-cli info 2>/dev/null | head -1)"
zerotier-cli peers 2>/dev/null | grep -E 'PLANET|MOON' | sed 's/^/zt_peer=/'
echo "zt_https=$(web https://my.zerotier.com)"
for p in 8123 3000 8091 8765 1883; do
  echo "lp_$p=$(timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$p" >/dev/null 2>&1 && echo open || echo closed)"
done
"""

_VERSIONS_SCRIPT = r"""
CT=__CT__; CFG=__CFG__; SVC=__SVC__
echo '== Sistema operativo'; cat /etc/os-release 2>/dev/null; uname -a
echo; echo '== Home Assistant'
if [ -n "$CT" ]; then
  docker exec "$CT" hass --version 2>/dev/null \
    || docker inspect -f '{{index .Config.Labels "io.hass.version"}}' "$CT" 2>/dev/null
else
  echo '(contenedor no detectado)'
fi
echo; echo '== Docker'; docker version --format '{{.Server.Version}}' 2>/dev/null || echo '(no disponible)'
echo; echo '== Contenedores'; docker ps -a --format '{{.Names}}  {{.Image}}  {{.Status}}' 2>/dev/null
echo; echo "== Z-Wave JS UI ($SVC)"
systemctl show -p ExecStart --value "$SVC" 2>/dev/null
for f in /opt/*/package.json /opt/*/*/package.json /home/*/zwave-js-ui/package.json /home/*/*/package.json \
         /usr/lib/node_modules/zwave-js-ui/package.json /usr/local/lib/node_modules/zwave-js-ui/package.json; do
  grep -qs '"name": *"zwave-js-ui"' "$f" && echo "$f -> $(grep -m1 '"version"' "$f" | tr -d ' ,')"
done
echo; echo '== cloudflared'; cloudflared --version 2>/dev/null || echo 'no instalado'
echo; echo '== ZeroTier'; zerotier-cli -v 2>/dev/null || echo 'no instalado'
echo; echo '== Python'; python3 --version 2>&1
echo; echo '== custom_components'
python3 - "$CFG" <<'PYEOF'
import glob, json, os, sys
root = os.path.join(sys.argv[1], "custom_components")
paths = sorted(glob.glob(os.path.join(root, "*", "manifest.json")))
if not paths:
    print("(ninguno)")
for m in paths:
    try:
        with open(m) as fh:
            d = json.load(fh)
        print(os.path.basename(os.path.dirname(m)) + ": " + str(d.get("version", "?")))
    except Exception as exc:
        print(m + ": error " + str(exc))
PYEOF
"""

_SYSTEM_SCRIPT = r"""
echo '== hostname'; hostname
echo; echo '== uptime'; uptime
echo; echo '== timedatectl'; timedatectl 2>/dev/null
echo; echo '== df -h'; df -h
echo; echo '== free -h'; free -h
echo; echo '== systemctl --failed'; systemctl --failed --no-pager 2>/dev/null
echo; echo '== docker ps -a'; docker ps -a 2>/dev/null
echo; echo '== top'; top -b -n1 2>/dev/null | head -n 30
"""

_NETWORK_INFO_SCRIPT = r"""
echo '== ip -br addr'; ip -br addr
echo; echo '== ip addr'; ip addr
echo; echo '== ip route'; ip route
echo; echo '== nmcli device'; nmcli device status 2>/dev/null
echo; echo '== nmcli connection'; nmcli connection show 2>/dev/null
echo; echo '== resolv.conf'; cat /etc/resolv.conf 2>/dev/null
"""


# --- utilidades de texto ---


def clean_log_line(raw: str) -> str:
    return _CTRL_RE.sub("", _ANSI_RE.sub("", raw)).rstrip()


def line_level(line: str) -> str:
    """Nivel aproximado de una línea de log: error | warn | ''."""
    if _ERROR_UPPER_RE.search(line):
        return "error"
    if _WARN_UPPER_RE.search(line):
        return "warn"
    if _INFO_UPPER_RE.search(line):
        return ""
    if _ERROR_ANY_RE.search(line):
        return "error"
    if _WARN_ANY_RE.search(line):
        return "warn"
    return ""


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if _SECRET_KEY_RE.search(str(k)) and v else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class LogFilter:
    """Filtra por nivel (all | warn | error) y texto; mantiene las líneas de traza indentadas."""

    def __init__(self, min_level: str = "all", text: str = ""):
        self.min_level = min_level
        self.text = text.strip().lower()
        self._last_level: Optional[str] = None

    def accept(self, raw: str) -> Optional[tuple[str, str]]:
        line = clean_log_line(raw)
        if not line.strip():
            return None
        if self._last_level is not None and line[:1] in (" ", "\t"):
            return line, self._last_level
        level = line_level(line)
        if self.min_level == "error":
            ok = level == "error"
        elif self.min_level == "warn":
            ok = level in ("warn", "error")
        else:
            ok = True
        if ok and self.text and self.text not in line.lower():
            ok = False
        self._last_level = level if ok and level else None
        return (line, level) if ok else None


def _parse_kv(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _validate_host(host: str) -> str:
    host = (host or "").strip()
    if not _HOST_RE.match(host):
        raise ValidationError(f"Host inválido: {host!r}")
    return host


def _ping_item(group: str, label: str, raw: str) -> NetworkTestItem:
    loss_txt, _, avg = raw.partition("|")
    try:
        loss = float(loss_txt)
    except ValueError:
        loss = 100.0
    if loss >= 100:
        return NetworkTestItem(group, label, "fail", "Sin respuesta (100 % pérdida)")
    detail = f"{avg} ms promedio" if avg else "Responde"
    if loss > 0:
        return NetworkTestItem(group, label, "warn", f"{detail} · {loss:g} % pérdida")
    return NetworkTestItem(group, label, "ok", detail)


def _seconds_to_ms(raw: str) -> str:
    try:
        return f"{float(raw) * 1000:.0f} ms"
    except ValueError:
        return "?"


def _web_item(group: str, label: str, raw: str) -> NetworkTestItem:
    code, connect, total = (raw.split("|") + ["", "", ""])[:3]
    if not code or code == "000":
        return NetworkTestItem(group, label, "fail", "Sin conexión HTTPS (timeout o bloqueado)")
    level = "ok" if code.startswith(("2", "3")) else "warn"
    detail = f"HTTP {code} · conexión {_seconds_to_ms(connect)} · total {_seconds_to_ms(total)}"
    return NetworkTestItem(group, label, level, detail)


def _zt_state(info: str) -> str:
    parts = info.split()
    return parts[-1].upper() if len(parts) >= 4 else ""


def _zt_latencies(peers: list[str]) -> list[int]:
    out: list[int] = []
    for line in peers:
        tokens = line.split()
        for i, tok in enumerate(tokens):
            if tok in ("PLANET", "MOON") and i + 1 < len(tokens):
                try:
                    lat = int(tokens[i + 1])
                except ValueError:
                    break
                if lat >= 0:
                    out.append(lat)
                break
    return out


def _to_data(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


class SupportManager:
    """Herramientas de soporte de solo lectura sobre el controlador."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager, self_heal: SelfHealManager):
        self.ssh = ssh
        self.ha_config = ha_config
        self.self_heal = self_heal

    # --- semáforo ---

    def health_check(self) -> HealthCheckStatus:
        status = HealthCheckStatus(checked_at=datetime.now().strftime("%H:%M:%S"))
        t0 = time.time()
        res = self.ssh.run(f"bash -c {shlex.quote(_HEALTH_SCRIPT)}", use_sudo=True, timeout=40)
        local_epoch = (t0 + time.time()) / 2
        values = _parse_kv((res.stdout or "").splitlines())
        if not values:
            status.error = (res.stderr or "Sin respuesta del controlador").strip()[:200]
            return status
        status.items = [
            self._eval_ha(values),
            self._eval_zwave(values),
            self._eval_disk(values),
            self._eval_clock(values, local_epoch),
            self._eval_tunnel(values),
            self._eval_failed_units(values),
        ]
        return status

    @staticmethod
    def _eval_ha(v: dict[str, str]) -> HealthCheckItem:
        label = "Home Assistant :8123"
        code = v.get("ha_http", "")
        if v.get("p8123") != "open":
            return HealthCheckItem("ha", label, "fail", "No responde (caído o arrancando)")
        if code in _HA_HTTP_OK:
            return HealthCheckItem("ha", label, "ok", f"Responde (HTTP {code})")
        return HealthCheckItem("ha", label, "warn", f"Puerto abierto, HTTP {code or 'sin respuesta'}")

    @staticmethod
    def _eval_zwave(v: dict[str, str]) -> HealthCheckItem:
        label = "Z-Wave :3000 / :8091"
        ws = v.get("p3000") == "open"
        ui = v.get("p8091") == "open"
        if ws and ui:
            return HealthCheckItem("zwave", label, "ok", "WebSocket y UI responden")
        if not ws and not ui:
            return HealthCheckItem("zwave", label, "fail", "Servicio caído (3000 y 8091 cerrados)")
        missing = ":3000 (WebSocket para HA)" if not ws else ":8091 (interfaz web)"
        return HealthCheckItem("zwave", label, "warn", f"No responde {missing}")

    @staticmethod
    def _eval_disk(v: dict[str, str]) -> HealthCheckItem:
        label = "Disco /"
        try:
            used = int(v.get("disk", ""))
        except ValueError:
            return HealthCheckItem("disk", label, "unknown", "No se pudo leer df")
        if used >= _DISK_FAIL_PCT:
            return HealthCheckItem("disk", label, "fail", f"{used} % usado (crítico)")
        if used >= _DISK_WARN_PCT:
            return HealthCheckItem("disk", label, "warn", f"{used} % usado (liberar espacio)")
        return HealthCheckItem("disk", label, "ok", f"{used} % usado")

    @staticmethod
    def _eval_clock(v: dict[str, str], local_epoch: float) -> HealthCheckItem:
        label = "Reloj / NTP"
        try:
            drift = int(round(int(v.get("epoch", "")) - local_epoch))
        except ValueError:
            return HealthCheckItem("clock", label, "unknown", "No se pudo leer la hora")
        ntp = v.get("ntp", "").lower()
        tz = v.get("tz", "")
        if abs(drift) > _CLOCK_DRIFT_FAIL_S:
            return HealthCheckItem(
                "clock", label, "fail", f"Desfase {drift:+d} s respecto a este PC (NTP: {ntp or '?'})"
            )
        if ntp == "yes":
            return HealthCheckItem("clock", label, "ok", f"Sincronizado{f' · {tz}' if tz else ''}")
        if ntp == "no":
            return HealthCheckItem("clock", label, "warn", f"NTP no sincronizado (desfase {drift:+d} s)")
        return HealthCheckItem("clock", label, "warn", f"Estado NTP desconocido (desfase {drift:+d} s)")

    @staticmethod
    def _eval_tunnel(v: dict[str, str]) -> HealthCheckItem:
        label = "Túnel remoto"
        cf_installed = v.get("cf_installed") == "yes"
        cf_active = v.get("cf_active") == "yes"
        zt_installed = v.get("zt_installed") == "yes"
        zt_state = _zt_state(v.get("zt_info", ""))
        zt_online = zt_state in ("ONLINE", "TUNNELED")
        parts: list[str] = []
        if cf_installed:
            parts.append(f"Cloudflare {'activo' if cf_active else 'DETENIDO'}")
        if zt_installed:
            parts.append(f"ZeroTier {zt_state or 'sin estado'}")
        if cf_active or zt_online:
            return HealthCheckItem("tunnel", label, "ok", " · ".join(parts))
        if cf_installed or zt_installed:
            return HealthCheckItem("tunnel", label, "fail", " · ".join(parts))
        return HealthCheckItem("tunnel", label, "warn", "Sin túnel instalado (solo LAN)")

    @staticmethod
    def _eval_failed_units(v: dict[str, str]) -> HealthCheckItem:
        label = "Unidades systemd"
        names = v.get("failed", "").split()
        if not names:
            return HealthCheckItem("systemd", label, "ok", "Ninguna fallida")
        shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        return HealthCheckItem("systemd", label, "warn", f"{len(names)} fallida(s): {shown}")

    # --- logs ---

    def log_sources(self) -> list[LogSource]:
        container = self.ha_config._detect_container(include_stopped=True)
        zwave = self.self_heal._detect_service_name()
        sources: list[LogSource] = []
        if container:
            c = shlex.quote(container)
            sources.append(
                LogSource(
                    "ha",
                    "Home Assistant",
                    f"docker logs --tail __N__ {c} 2>&1",
                    f"docker logs -f --tail __N__ {c} 2>&1",
                )
            )
            sources.append(
                LogSource(
                    "plugin_service",
                    "plugin_service (dentro del log de HA)",
                    f"docker logs --tail 20000 {c} 2>&1 | grep -i plugin_service | tail -n __N__",
                    f"docker logs -f --tail 2000 {c} 2>&1 | grep --line-buffered -i plugin_service",
                )
            )
        else:
            sources.append(
                LogSource("ha", "Home Assistant", available=False, detail="contenedor no detectado")
            )
        sources.extend(
            [
                self._journal_source("zwave", f"Z-Wave JS UI ({zwave})", zwave),
                self._journal_source("cloudflared", "cloudflared (túnel Cloudflare)", "cloudflared*"),
                self._journal_source("zerotier", "ZeroTier", "zerotier-one"),
                self._journal_source("admin_network", "Admin Network (servicio host)", "admin_network"),
                LogSource(
                    "system",
                    "Sistema (journal, prioridad warning o mayor)",
                    "journalctl -p warning -n __N__ --no-pager -o short-iso 2>&1",
                    "journalctl -p warning -f -n __N__ --no-pager -o short-iso 2>&1",
                    level_in_text=False,
                ),
            ]
        )
        return sources

    @staticmethod
    def _journal_source(key: str, label: str, unit: str) -> LogSource:
        u = shlex.quote(unit)
        return LogSource(
            key,
            label,
            f"journalctl -u {u} -n __N__ --no-pager -o short-iso 2>&1",
            f"journalctl -u {u} -f -n __N__ --no-pager -o short-iso 2>&1",
        )

    def read_log(
        self, source: LogSource, lines: int = 200, min_level: str = "warn", text: str = ""
    ) -> list[tuple[str, str]]:
        """Últimas líneas filtradas: lista de (línea, nivel)."""
        level = min_level if source.level_in_text else "all"
        fetch = lines if level == "all" and not text else min(max(lines * 10, 2000), _MAX_TAIL_FETCH)
        res = self.ssh.run(source.tail_command(min(fetch, _MAX_TAIL_FETCH)), use_sudo=True, timeout=120)
        if not res.ok and not res.stdout.strip():
            raise SSHCommandError(
                res.stderr or f"No se pudo leer el log de {source.label}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        flt = LogFilter(level, text)
        hits = [hit for raw in res.stdout.splitlines() if (hit := flt.accept(raw))]
        return hits[-lines:]

    def follow_log(
        self,
        source: LogSource,
        min_level: str = "warn",
        text: str = "",
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[tuple[str, str]]:
        """Sigue el log en vivo; emite (línea, nivel) que pasan el filtro."""
        flt = LogFilter(min_level if source.level_in_text else "all", text)
        for raw in self.ssh.stream_lines(
            source.follow_command(50), use_sudo=True, should_stop=should_stop
        ):
            hit = flt.accept(raw)
            if hit:
                yield hit

    # --- pruebas de red ---

    def run_network_tests(self) -> NetworkTestReport:
        res = self.ssh.run(f"bash -c {shlex.quote(_NETWORK_SCRIPT)}", use_sudo=True, timeout=150)
        lines = (res.stdout or "").splitlines()
        v = _parse_kv(lines)
        report = NetworkTestReport()
        if not v:
            report.error = (res.stderr or "Sin respuesta del controlador").strip()[:200]
            return report
        add = report.items.append

        gw = v.get("gw", "")
        if gw:
            add(_ping_item("Conectividad", f"Gateway {gw}", v.get("ping_gw", "")))
        else:
            add(NetworkTestItem("Conectividad", "Gateway", "fail", "Sin ruta por defecto"))
        inet = _ping_item("Conectividad", "Internet (ping 8.8.8.8)", v.get("ping_inet", ""))
        add(inet)

        add(NetworkTestItem("DNS", "Servidores", "info", v.get("dns_servers") or "(ninguno en resolv.conf)"))
        dns_failed = False
        for key, value in v.items():
            if not key.startswith("dns|"):
                continue
            ip, _, ms = value.partition("|")
            if ip:
                add(NetworkTestItem("DNS", key[4:], "ok", f"{ip} ({ms} ms)"))
            else:
                dns_failed = True
                add(NetworkTestItem("DNS", key[4:], "fail", "No resuelve"))
        if dns_failed and inet.level != "fail":
            add(NetworkTestItem(
                "DNS", "Diagnóstico", "warn",
                "Hay Internet pero el DNS falla: revise los DNS del perfil de red",
            ))

        add(_web_item("Cloudflare", "HTTPS cloudflare.com", v.get("cf_https", "")))
        edge_open = v.get("cf_7844") == "open"
        add(NetworkTestItem(
            "Cloudflare", "Edge del túnel TCP 7844",
            "ok" if edge_open else "warn",
            "Abierto" if edge_open else "Bloqueado: el túnel solo funcionará si UDP 7844 (QUIC) está permitido",
        ))
        cf_active = v.get("cf_active") == "yes"
        add(NetworkTestItem(
            "Cloudflare", "cloudflared en ejecución", "ok" if cf_active else "info", "Sí" if cf_active else "No"
        ))

        if v.get("zt_installed") != "yes":
            add(NetworkTestItem("ZeroTier", "Servicio", "info", "No instalado"))
        else:
            state = _zt_state(v.get("zt_info", ""))
            if state == "ONLINE":
                add(NetworkTestItem("ZeroTier", "Servicio", "ok", "ONLINE"))
            elif state == "TUNNELED":
                add(NetworkTestItem(
                    "ZeroTier", "Servicio", "warn",
                    "TUNNELED: UDP 9993 bloqueado, usa relay TCP (más lento)",
                ))
            else:
                add(NetworkTestItem("ZeroTier", "Servicio", "fail", state or "Sin respuesta de zerotier-cli"))
            peers = [ln.split("=", 1)[1] for ln in lines if ln.startswith("zt_peer=")]
            lats = _zt_latencies(peers)
            if lats:
                best = min(lats)
                level = "ok" if best < 250 else "warn"
                add(NetworkTestItem(
                    "ZeroTier", "Latencia a raíces (PLANET)", level,
                    ", ".join(f"{x} ms" for x in sorted(lats)),
                ))
            else:
                add(NetworkTestItem("ZeroTier", "Latencia a raíces (PLANET)", "warn", "Sin raíces alcanzables"))
        add(_web_item("ZeroTier", "HTTPS my.zerotier.com", v.get("zt_https", "")))

        for port, label, required in (
            (8123, "Home Assistant", True),
            (3000, "Z-Wave JS WebSocket", True),
            (8091, "Z-Wave JS UI", True),
            (8765, "Admin Network API", False),
            (1883, "Broker MQTT", False),
        ):
            is_open = v.get(f"lp_{port}") == "open"
            level = "ok" if is_open else ("fail" if required else "info")
            add(NetworkTestItem(
                "Servicios locales", f"{label} :{port}", level, "Abierto" if is_open else "Cerrado"
            ))
        return report

    def traceroute(self, host: str = "8.8.8.8") -> str:
        q = shlex.quote(_validate_host(host))
        cmd = (
            f"if command -v traceroute >/dev/null 2>&1; then traceroute -n -w 2 -q 1 -m 20 {q}; "
            f"elif command -v tracepath >/dev/null 2>&1; then tracepath -n -m 20 {q}; "
            "else echo NO_TOOL; fi"
        )
        res = self.ssh.run(cmd, timeout=120)
        out = (res.stdout or res.stderr or "").strip()
        if out == "NO_TOOL":
            return "No hay traceroute ni tracepath en el controlador (apt-get install traceroute)."
        return out or "Sin salida."

    def check_port(self, host: str, port: int) -> NetworkTestItem:
        host = _validate_host(host)
        if not 1 <= int(port) <= 65535:
            raise ValidationError(f"Puerto inválido: {port}")
        cmd = (
            "t0=$(date +%s%N); "
            f"if timeout 5 bash -c 'exec 3<>/dev/tcp/\"$0\"/\"$1\"' {shlex.quote(host)} {int(port)} "
            ">/dev/null 2>&1; then r=open; else r=closed; fi; "
            't1=$(date +%s%N); echo "$r|$(( (t1 - t0) / 1000000 ))"'
        )
        res = self.ssh.run(cmd, timeout=20)
        state, _, ms = (res.stdout or "").strip().partition("|")
        label = f"{host}:{port}"
        if state == "open":
            return NetworkTestItem("Puerto", label, "ok", f"Abierto ({ms} ms)")
        return NetworkTestItem("Puerto", label, "fail", f"Cerrado o filtrado ({ms or '?'} ms)")

    # --- informe de soporte ---

    def build_report(
        self,
        collectors: dict[str, Callable[[], Any]],
        session: Optional[SessionInfo] = None,
        on_step: Optional[Callable[[str], None]] = None,
    ) -> SupportReportResult:
        step = on_step or (lambda _msg: None)
        warnings: list[str] = []

        step("Semáforo de estado")
        health = self._safe(self.health_check, "Semáforo", warnings)
        statuses: dict[str, Any] = {}
        for title, fn in collectors.items():
            step(title)
            statuses[title] = self._safe(fn, title, warnings)
        step("Pruebas de red")
        network = self._safe(self.run_network_tests, "Pruebas de red", warnings)
        step("Versiones, sistema y logs")
        files = self._collect_files(warnings)
        hostname = self._safe(lambda: self.ssh.run("hostname").stdout.strip(), "hostname", warnings) or ""

        generated = datetime.now()
        host_label = hostname or (session.host if session else "controlador")
        data = redact_obj(
            {
                "generado": generated.isoformat(timespec="seconds"),
                "app": f"{APP_NAME} {APP_VERSION}",
                "host": session.host if session else "",
                "hostname": hostname,
                "semaforo": _to_data(health),
                "semaforo_global": health.overall if health else "",
                "estados": {k: _to_data(v) for k, v in statuses.items()},
                "pruebas_red": _to_data(network),
                "advertencias": warnings,
            }
        )
        files = {name: redact(text) for name, text in files.items()}
        excerpts = {
            name: self._error_excerpt(text) for name, text in files.items() if name.startswith("logs/")
        }
        html_doc = _render_html(data, files.get("sistema/versiones.txt", ""), excerpts)

        out_dir = get_local_support_dir()
        os.makedirs(out_dir, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", host_label).strip("-") or "controlador"
        base = f"soporte_{slug}_{generated.strftime('%Y%m%d_%H%M%S')}"
        zip_path = os.path.join(out_dir, f"{base}.zip")
        html_path = os.path.join(out_dir, f"{base}.html")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("resumen.html", html_doc)
            zf.writestr("diagnostico.json", json.dumps(data, ensure_ascii=False, indent=2, default=str))
            for name, text in files.items():
                zf.writestr(name, text)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html_doc)
        return SupportReportResult(zip_path=zip_path, html_path=html_path, warnings=warnings)

    @staticmethod
    def _safe(fn: Callable[[], Any], label: str, warnings: list[str]) -> Any:
        try:
            return fn()
        except NotConnectedError:
            raise
        except Exception as exc:  # noqa: BLE001 — un apartado fallido no debe abortar el informe
            warnings.append(f"{label}: {exc}")
            return None

    @staticmethod
    def _error_excerpt(text: str) -> list[str]:
        flt = LogFilter("error")
        hits = [hit[0] for raw in text.splitlines() if (hit := flt.accept(raw))]
        return hits[-_REPORT_ERROR_EXCERPT:]

    def _collect_files(self, warnings: list[str]) -> dict[str, str]:
        container = self._safe(
            lambda: self.ha_config._detect_container(include_stopped=True), "Contenedor HA", warnings
        ) or ""
        config = self._safe(self.ha_config._detect_config_dir, "Config HA", warnings) or ""
        zwave = self._safe(self.self_heal._detect_service_name, "Servicio Z-Wave", warnings) or ""
        q = shlex.quote

        def journal(unit: str, lines: int = _REPORT_JOURNAL_LINES) -> str:
            return f"journalctl -u {q(unit)} -n {lines} --no-pager -o short-iso 2>&1"

        versions = (
            _VERSIONS_SCRIPT.replace("__CT__", q(container))
            .replace("__CFG__", q(config))
            .replace("__SVC__", q(zwave))
        )
        commands: dict[str, str] = {
            "sistema/versiones.txt": f"bash -c {q(versions)}",
            "sistema/sistema.txt": f"bash -c {q(_SYSTEM_SCRIPT)}",
            "sistema/red.txt": f"bash -c {q(_NETWORK_INFO_SCRIPT)}",
        }
        if container:
            commands["logs/home-assistant.log"] = (
                f"docker logs --tail {_REPORT_HA_LINES} {q(container)} 2>&1"
            )
        if config:
            prev = f"{config}/home-assistant.log.1"
            commands["logs/home-assistant.previo.log"] = (
                f"tail -n 300 {q(prev)} 2>/dev/null || echo '(no existe home-assistant.log.1)'"
            )
        if zwave:
            commands["logs/zwave-js-ui.log"] = journal(zwave)
        commands.update(
            {
                "logs/cloudflared.log": journal("cloudflared*"),
                "logs/zerotier.log": journal("zerotier-one", 200),
                "logs/admin_network.log": journal("admin_network", 200),
                "logs/sistema_errores.log": "journalctl -p err -b -n 300 --no-pager -o short-iso 2>&1",
                "logs/kernel.log": "dmesg -T 2>/dev/null | tail -n 200",
            }
        )

        files: dict[str, str] = {}
        for name, cmd in commands.items():
            try:
                res = self.ssh.run(cmd, use_sudo=True, timeout=120)
                files[name] = (res.stdout or res.stderr or "(vacío)") + "\n"
            except NotConnectedError:
                raise
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{name}: {exc}")
        return files


# --- HTML ---

_LEVEL_HTML = {
    "ok": ("OK", "ok"),
    "warn": ("REVISAR", "warn"),
    "fail": ("FALLA", "fail"),
    "info": ("INFO", "info"),
    "unknown": ("?", "info"),
}

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1d2330;background:#f5f7fa}
h1{margin:0 0 4px}h2{margin-top:28px;border-bottom:2px solid #2b6cb0;padding-bottom:4px}
.meta{color:#556;margin-bottom:16px}
table{border-collapse:collapse;width:100%;background:#fff;margin:8px 0}
td,th{border:1px solid #d5dbe3;padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}
th{background:#eef2f7}
pre{background:#0f1720;color:#d6e2f0;padding:10px;overflow:auto;font-size:12px;margin:0;white-space:pre-wrap}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:600;font-size:12px;color:#fff}
.ok{background:#2f855a}.warn{background:#c58a00}.fail{background:#c53030}.info{background:#718096}
.dim{color:#8a94a3}.k{width:28%;font-weight:600}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _badge(level: str) -> str:
    text, css = _LEVEL_HTML.get(level, ("?", "info"))
    return f"<span class='badge {css}'>{text}</span>"


def _html_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if value is None or value == "" or value == [] or value == {}:
        return "<span class='dim'>—</span>"
    if isinstance(value, (list, dict)):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return f"<pre>{_esc(chr(10).join(value))}</pre>"
        return f"<pre>{_esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"
    text = str(value)
    if "\n" in text or len(text) > 140:
        return f"<pre>{_esc(text)}</pre>"
    return _esc(text)


def _html_items_table(items: list[dict], with_group: bool = False) -> str:
    head = "<tr>" + ("<th>Grupo</th>" if with_group else "") + "<th>Chequeo</th><th>Estado</th><th>Detalle</th></tr>"
    rows = []
    for it in items:
        group = f"<td>{_esc(it.get('group', ''))}</td>" if with_group else ""
        rows.append(
            f"<tr>{group}<td>{_esc(it.get('label', ''))}</td><td>{_badge(it.get('level', ''))}</td>"
            f"<td>{_esc(it.get('detail', ''))}</td></tr>"
        )
    return f"<table>{head}{''.join(rows)}</table>"


def _render_html(data: dict, versions: str, excerpts: dict[str, list[str]]) -> str:
    parts: list[str] = []
    title = f"Informe de soporte — {data.get('hostname') or data.get('host') or 'controlador'}"
    parts.append(f"<h1>{_esc(title)}</h1>")
    parts.append(
        f"<div class='meta'>Generado: {_esc(data.get('generado'))} · Host: {_esc(data.get('host'))} · "
        f"{_esc(data.get('app'))} · Estado global: {_badge(data.get('semaforo_global') or 'unknown')}</div>"
    )

    health = data.get("semaforo") or {}
    parts.append("<h2>Semáforo de estado</h2>")
    if health.get("error"):
        parts.append(f"<p>{_badge('fail')} {_esc(health['error'])}</p>")
    parts.append(_html_items_table(health.get("items") or []))

    network = data.get("pruebas_red") or {}
    parts.append("<h2>Pruebas de red</h2>")
    if network.get("error"):
        parts.append(f"<p>{_badge('fail')} {_esc(network['error'])}</p>")
    parts.append(_html_items_table(network.get("items") or [], with_group=True))

    errors_any = {k: v for k, v in excerpts.items() if v}
    parts.append("<h2>Errores recientes en logs</h2>")
    if not errors_any:
        parts.append("<p class='dim'>No se detectaron líneas de error en los logs recolectados.</p>")
    for name, lines in errors_any.items():
        parts.append(f"<h3>{_esc(name)}</h3><pre>{_esc(chr(10).join(lines))}</pre>")

    parts.append("<h2>Versiones instaladas</h2>")
    parts.append(f"<pre>{_esc(versions or '(no disponible)')}</pre>")

    for title_s, status in (data.get("estados") or {}).items():
        parts.append(f"<h2>{_esc(title_s)}</h2>")
        if not isinstance(status, dict):
            parts.append(f"<p class='dim'>{_esc(status if status is not None else 'No disponible')}</p>")
            continue
        rows = "".join(
            f"<tr><td class='k'>{_esc(k)}</td><td>{_html_value(v)}</td></tr>" for k, v in status.items()
        )
        parts.append(f"<table>{rows}</table>")

    warnings = data.get("advertencias") or []
    if warnings:
        parts.append("<h2>Apartados que no se pudieron recolectar</h2><ul>")
        parts.extend(f"<li>{_esc(w)}</li>" for w in warnings)
        parts.append("</ul>")

    parts.append(
        "<p class='dim'>Los logs completos, la información de red/sistema y el diagnóstico en JSON "
        "están dentro del ZIP. Los tokens y contraseñas detectados se reemplazan por ***.</p>"
    )
    body = "\n".join(parts)
    return (
        "<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>{body}</body></html>"
    )
