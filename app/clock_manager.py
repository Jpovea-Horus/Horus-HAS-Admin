"""Revisión y corrección de la hora del controlador (NTP, hora del PC, zona horaria)."""

from __future__ import annotations

import shlex
import time
from datetime import datetime
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import ClockStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

DRIFT_FIX_S = 30
NTP_WAIT_S = 30

_STATUS_SCRIPT = r"""
echo "epoch=$(date +%s)"
echo "now=$(date '+%Y-%m-%d %H:%M:%S %Z (%z)')"
echo "tz=$(timedatectl show -p Timezone --value 2>/dev/null)"
echo "ntp_enabled=$(timedatectl show -p NTP --value 2>/dev/null)"
echo "ntp_synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
svc=""
for s in systemd-timesyncd chrony chronyd ntp ntpd; do
  if systemctl is-active --quiet "$s" 2>/dev/null; then svc="$s"; break; fi
done
echo "svc=$svc"
if [ -e /dev/rtc0 ]; then
  echo "rtc=yes"
  echo "rtc_time=$(cat /sys/class/rtc/rtc0/date 2>/dev/null) $(cat /sys/class/rtc/rtc0/time 2>/dev/null) UTC"
else
  echo "rtc=no"
fi
"""

_NTP_SCRIPT = rf"""
timedatectl set-ntp true 2>&1 || true
if systemctl cat systemd-timesyncd >/dev/null 2>&1; then
  systemctl restart systemd-timesyncd 2>&1
elif systemctl cat chrony >/dev/null 2>&1; then
  systemctl restart chrony 2>&1; chronyc makestep 2>&1 || true
elif systemctl cat ntp >/dev/null 2>&1; then
  systemctl restart ntp 2>&1
else
  echo "NO_NTP_SERVICE"
fi
end=$(( $(date +%s) + {NTP_WAIT_S} ))
while [ "$(date +%s)" -lt "$end" ]; do
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
    echo "NTP_SYNC_OK"
    [ -e /dev/rtc0 ] && hwclock -w 2>&1
    date
    exit 0
  fi
  sleep 2
done
echo "NTP_SYNC_TIMEOUT"
timedatectl timesync-status 2>/dev/null | head -20
"""


class ClockManager:
    """Compara la hora del controlador con la del PC y la corrige."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> ClockStatus:
        status = ClockStatus()
        try:
            t0 = time.time()
            result = self.ssh.run(f"bash -c {shlex.quote(_STATUS_SCRIPT)}")
            local = (t0 + time.time()) / 2
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, val = line.split("=", 1)
                    values[key.strip()] = val.strip()

            status.local_epoch = int(round(local))
            status.pc_time = datetime.fromtimestamp(local).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S (%z)"
            )
            status.remote_time = values.get("now", "")
            status.timezone = values.get("tz", "")
            status.ntp_enabled = values.get("ntp_enabled", "").lower()
            status.ntp_synced = values.get("ntp_synced", "").lower()
            status.sync_service = values.get("svc", "")
            status.has_rtc = values.get("rtc") == "yes"
            status.rtc_time = values.get("rtc_time", "").strip()
            try:
                status.remote_epoch = int(values.get("epoch", ""))
            except ValueError:
                status.error = "No se pudo leer la hora del controlador."
                return status
            status.drift_s = int(round(status.remote_epoch - local))
            status.needs_fix = abs(status.drift_s) > DRIFT_FIX_S or status.ntp_synced != "yes"
        except Exception as exc:
            status.error = str(exc)
        return status

    def sync_ntp(self) -> tuple[bool, str]:
        """Activa NTP, reinicia el servicio y espera la sincronización."""
        result = self.ssh.run(
            f"bash -c {shlex.quote(_NTP_SCRIPT)}",
            use_sudo=True,
            timeout=NTP_WAIT_S + 30,
        )
        out = self._combine(result)
        return "NTP_SYNC_OK" in out, out

    def set_from_pc(self) -> str:
        """Fija la hora del controlador con la de este PC (UTC) y la guarda en el RTC."""
        epoch = int(round(time.time()))
        cmd = (
            f"date -u -s @{epoch} && "
            "{ [ -e /dev/rtc0 ] && hwclock -w 2>&1 || true; } && date"
        )
        result = self.ssh.run(f"bash -c {shlex.quote(cmd)}", use_sudo=True)
        out = self._combine(result)
        if not result.ok:
            raise SSHCommandError(
                f"No se pudo ajustar la hora.\n{out}",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return out

    def auto_fix(self) -> str:
        """Intenta NTP; si no sincroniza, usa la hora del PC."""
        ok, out = self.sync_ntp()
        logs = [f"=== NTP ===\n{out}"]
        if ok:
            logs.append("\nHora sincronizada por NTP.")
            return "\n".join(logs)
        logs.append("\nNTP no sincronizó (¿sin internet / puerto UDP 123 bloqueado?).")
        logs.append(f"\n=== Hora del PC ===\n{self.set_from_pc()}")
        logs.append("\nHora ajustada con la de este PC.")
        return "\n".join(logs)

    def set_timezone(self, tz: str) -> str:
        tz = tz.strip()
        check = self.ssh.run(f"timedatectl list-timezones | grep -qxF {shlex.quote(tz)}")
        if not check.ok:
            raise SSHCommandError(f"Zona horaria no válida: {tz}")
        result = self.ssh.run(f"timedatectl set-timezone {shlex.quote(tz)}", use_sudo=True)
        if not result.ok:
            raise SSHCommandError(
                self._combine(result) or "No se pudo cambiar la zona horaria.",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return f"Zona horaria del sistema: {tz}"

    @staticmethod
    def _combine(result) -> str:
        parts = []
        if result.stdout:
            parts.append(result.stdout.strip())
        if result.stderr:
            parts.append(result.stderr.strip())
        return "\n".join(parts) if parts else "(sin salida)"
