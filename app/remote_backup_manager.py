"""Backup remoto estándar HORUS: .tar.gz ligero, descarga, restore con rollback y reset protegido.

Las operaciones que detienen servicios corren en el controlador como trabajo desacoplado
(systemd-run / setsid): si se corta el túnel SSH, el script igualmente reinicia HA y Z-Wave.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import tarfile
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from exceptions import SSHCommandError, ValidationError
from models import (
    CommandResult,
    HorusArchive,
    HorusArchiveInfo,
    HorusBackupStatus,
    HorusJobResult,
    HorusSnapshot,
)
from paths import (
    APP_NAME,
    APP_VERSION,
    REMOTE_CONFIG_DIR,
    REMOTE_HORUS_BACKUPS_DIR,
    REMOTE_ZWAVE_STORE,
    get_local_backups_dir,
)

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from self_heal_manager import SelfHealManager
    from ssh_client import SSHClient

ProgressFn = Callable[[int, int], None]
PhaseFn = Callable[[str, int], None]

MANIFEST_NAME = "horus_manifest.json"
MANIFEST_FORMAT = "horus-backup/1"
NVM_DIR = "horus_nvm"
JOBS_DIR = f"{REMOTE_HORUS_BACKUPS_DIR}/.jobs"

_ARCHIVE_RE = re.compile(
    r"^backup_horus_(?:(?P<id>[0-9a-z]{2,16})_)?(?P<stamp>\d{8}_\d{4})\.tar\.gz$"
)
_SNAPSHOT_RE = re.compile(
    r"^(?P<target>/.+)\.(?P<reason>pre_restore|pre_reset)_(?P<stamp>\d{8}_\d{6})$"
)
_STORE_CANDIDATES = (REMOTE_ZWAVE_STORE, "/opt/zwave-js-ui-store")
_SAFETY_MARGIN = 200 * 1024 * 1024
_JOB_TIMEOUT_S = 20 * 60
_HEALTH_TIMEOUT_S = 240

# Historial (DB + WAL/SHM), logs, cachés y respaldos internos: no son configuración.
_EXCLUDE_ANY = (
    "home-assistant_v2.db*",
    "home-assistant.log*",
    "__pycache__",
    "*.bak.horus*",
    ".horus_db_rescue_*",
)
_CFG_EXCLUDE_DIRS = ("backups", "tts", "deps")
_STORE_EXCLUDE_DIRS = ("backups", "logs")
_DU_EXCLUDES = _EXCLUDE_ANY + _CFG_EXCLUDE_DIRS + _STORE_EXCLUDE_DIRS + ("*.log",)

_PRELUDE = r"""#!/bin/bash
set -u
JOB="$1"
exec >>"$JOB/log" 2>&1
umask 077
FAIL_MSG=""
STOPPED=0
@VARS@
phase() { echo "$1" > "$JOB/phase"; echo "[$(date +%T)] $1"; }
fail() { FAIL_MSG="$2"; echo "ERROR: $2"; exit "$1"; }
ha_stop() { if [ -n "$HA_CT" ]; then docker stop -t 60 "$HA_CT" >/dev/null || true; fi; }
ha_start() { if [ -n "$HA_CT" ]; then docker start "$HA_CT" >/dev/null || true; fi; }
zw_stop() { if [ -n "$ZW_SVC" ]; then systemctl stop "$ZW_SVC" || true; fi; }
zw_start() { if [ -n "$ZW_SVC" ]; then systemctl start --no-block "$ZW_SVC" || true; fi; }
stop_all() {
  phase "Deteniendo servicios"
  STOPPED=1
  zw_stop
  ha_stop
  sleep 2
  if [ -n "$HA_CT" ] && [ "$(docker inspect -f '{{.State.Running}}' "$HA_CT" 2>/dev/null)" = "true" ]; then
    fail 7 "No se pudo detener Home Assistant ($HA_CT)"
  fi
  if [ -n "$ZW_SVC" ] && systemctl is-active --quiet "$ZW_SVC"; then
    fail 7 "No se pudo detener Z-Wave JS UI ($ZW_SVC)"
  fi
}
echo RUNNING > "$JOB/status"
"""

_BACKUP_BODY = r"""
HA_WAS=$(docker inspect -f '{{.State.Running}}' "$HA_CT" 2>/dev/null || echo false)
ZW_WAS=$(systemctl is-active "$ZW_SVC" 2>/dev/null || true)
RESTARTED=0
OUT=""
restore_state() {
  [ "$RESTARTED" = 1 ] && return 0
  RESTARTED=1
  [ "$STOPPED" = 1 ] || return 0
  phase "Reiniciando servicios"
  [ "$ZW_WAS" = "active" ] && zw_start
  [ "$HA_WAS" = "true" ] && ha_start
  return 0
}
finish() {
  rc=$?
  trap - EXIT
  restore_state
  if [ "$rc" -ne 0 ]; then
    [ -n "$OUT" ] && rm -f "$OUT.part"
    echo "FAIL|$rc|${FAIL_MSG:-error $rc}" > "$JOB/status"
  fi
}
trap finish EXIT
trap 'exit 130' INT TERM HUP

mkdir -p "$OUT_DIR" || fail 2 "No se pudo crear $OUT_DIR"
[ -n "$ARCHIVE_OWNER" ] && chown "$ARCHIVE_OWNER" "$OUT_DIR"
OUT="$OUT_DIR/backup_horus_${DEVICE_ID}_$(date +%Y%m%d_%H%M).tar.gz"
[ -e "$OUT" ] && fail 3 "Ya existe $OUT (espere un minuto y reintente)"

if [ -n "$NVM_FILE" ] && [ -f "$NVM_FILE" ]; then
  mkdir -p "$JOB/nvm/@NVM_DIR@" && cp -p "$NVM_FILE" "$JOB/nvm/@NVM_DIR@/" \
    && TAR_ARGS+=(-C "$JOB/nvm" "@NVM_DIR@")
fi

stop_all
T0=$(date +%s)
phase "Empaquetando"
tar -czf "$OUT.part" "${TAR_ARGS[@]}"
TAR_RC=$?
restore_state
DOWN=$(( $(date +%s) - T0 ))
# tar devuelve 1 si un archivo cambió durante la lectura (no crítico con servicios detenidos).
[ "$TAR_RC" -gt 1 ] && fail 10 "tar falló (código $TAR_RC)"

phase "Verificando integridad"
tar -tzf "$OUT.part" >/dev/null || fail 11 "El archivo generado está corrupto"
mv "$OUT.part" "$OUT" || fail 12 "No se pudo renombrar el archivo final"
SHA=$(sha256sum "$OUT" | awk '{print $1}')
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"
SIZE=$(stat -c %s "$OUT")
[ -n "$ARCHIVE_OWNER" ] && chown "$ARCHIVE_OWNER" "$OUT" "$OUT.sha256"
echo "OK|path=$OUT|sha256=$SHA|size=$SIZE|downtime=$DOWN" > "$JOB/status"
exit 0
"""

# Rollback común a restore y reset: deshace en orden inverso lo que se haya movido.
_SWAP_COMMON = r"""
MOVED_CFG=0; PLACED_CFG=0; MOVED_ST=0; PLACED_ST=0; DB_MOVED=0
rollback() {
  echo "Revirtiendo cambios..."
  if [ "$DB_MOVED" = 1 ]; then
    for f in "$CFG_TARGET"/home-assistant_v2.db*; do [ -e "$f" ] && mv "$f" "$CFG_TARGET.$SUFFIX/"; done
  fi
  if [ "$PLACED_ST" = 1 ]; then rm -rf "$ST_TARGET"; fi
  if [ "$MOVED_ST" = 1 ]; then mv "$ST_TARGET.$SUFFIX" "$ST_TARGET"; fi
  if [ "$PLACED_CFG" = 1 ]; then rm -rf "$CFG_TARGET"; fi
  if [ "$MOVED_CFG" = 1 ]; then mv "$CFG_TARGET.$SUFFIX" "$CFG_TARGET"; fi
}
set_aside() {
  local kind="$1" target="$2"
  OWNER=$(stat -c %u:%g "$target" 2>/dev/null || stat -c %u:%g "$(dirname "$target")")
  MODE=$(stat -c %a "$target" 2>/dev/null || echo 755)
  if [ -e "$target" ]; then
    mv "$target" "$target.$SUFFIX" || fail 20 "No se pudo apartar $target"
    eval "MOVED_$kind=1"
  fi
}
finish() {
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then rollback; fi
  [ -n "${STAGE:-}" ] && rm -rf "$STAGE"
  if [ "$STOPPED" = 1 ]; then phase "Iniciando servicios"; zw_start; ha_start; fi
  if [ "$rc" -eq 0 ]; then
    echo "OK|ts=$TS" > "$JOB/status"
  else
    echo "FAIL|$rc|${FAIL_MSG:-error $rc}" > "$JOB/status"
  fi
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
"""

_RESTORE_BODY = r"""
SUFFIX="pre_restore_$TS"
place() {
  local kind="$1" src="$2" target="$3"
  set_aside "$kind" "$target"
  mv "$STAGE/$src" "$target" || fail 21 "No se pudo colocar $target"
  eval "PLACED_$kind=1"
  chown -R "$OWNER" "$target" || true
  chmod "$MODE" "$target" || true
}

[ -f "$ARCHIVE" ] || fail 2 "No existe $ARCHIVE"
phase "Verificando archivo"
if [ -n "$EXPECTED_SHA" ]; then
  echo "$EXPECTED_SHA  $ARCHIVE" | sha256sum -c --status || fail 3 "SHA-256 no coincide: archivo dañado"
fi
phase "Extrayendo en zona temporal"
rm -rf "$STAGE"
mkdir -p "$STAGE" || fail 4 "No se pudo crear $STAGE"
tar -xzf "$ARCHIVE" -C "$STAGE" --no-same-owner || fail 5 "Fallo al extraer el backup"
[ -z "$SRC_CFG" ] || [ -d "$STAGE/$SRC_CFG" ] || fail 6 "El backup no contiene la config HA ($SRC_CFG)"
[ -z "$SRC_ST" ] || [ -d "$STAGE/$SRC_ST" ] || fail 6 "El backup no contiene el store Z-Wave ($SRC_ST)"

stop_all
phase "Colocando configuración restaurada"
[ -n "$SRC_CFG" ] && place CFG "$SRC_CFG" "$CFG_TARGET"
[ -n "$SRC_ST" ] && place ST "$SRC_ST" "$ST_TARGET"

if [ "$KEEP_DB" = 1 ] && [ -n "$SRC_CFG" ] && [ -f "$CFG_TARGET.$SUFFIX/home-assistant_v2.db" ] \
   && [ ! -e "$CFG_TARGET/home-assistant_v2.db" ]; then
  phase "Conservando historial (base de datos actual)"
  DB_MOVED=1
    for f in "$CFG_TARGET.$SUFFIX"/home-assistant_v2.db*; do
    mv "$f" "$CFG_TARGET/" || fail 22 "No se pudo conservar la base de datos"
  done
fi
exit 0
"""

_RESET_BODY = r"""
SUFFIX="pre_reset_$TS"
wipe() {
  local kind="$1" target="$2"
  set_aside "$kind" "$target"
  mkdir -p "$target" || fail 21 "No se pudo recrear $target"
  eval "PLACED_$kind=1"
  chown "$OWNER" "$target" || true
  chmod "$MODE" "$target" || true
}
stop_all
phase "Apartando configuración actual"
[ "$DO_CFG" = 1 ] && wipe CFG "$CFG_TARGET"
[ "$DO_ST" = 1 ] && wipe ST "$ST_TARGET"
exit 0
"""

_REVERT_BODY = r"""
SUFFIX="reverting_$TS"
MOVED_CFG=0; PLACED_CFG=0; MOVED_ST=0; PLACED_ST=0
rollback() {
  echo "Revirtiendo cambios..."
  if [ "$PLACED_ST" = 1 ]; then mv "$ST_TARGET" "$SNAP_ST"; fi
  if [ "$MOVED_ST" = 1 ]; then mv "$ST_TARGET.$SUFFIX" "$ST_TARGET"; fi
  if [ "$PLACED_CFG" = 1 ]; then mv "$CFG_TARGET" "$SNAP_CFG"; fi
  if [ "$MOVED_CFG" = 1 ]; then mv "$CFG_TARGET.$SUFFIX" "$CFG_TARGET"; fi
}
swap_in() {
  local kind="$1" snap="$2" target="$3"
  [ -d "$snap" ] || fail 2 "No existe $snap"
  if [ -e "$target" ]; then
    mv "$target" "$target.$SUFFIX" || fail 20 "No se pudo apartar $target"
    eval "MOVED_$kind=1"
  fi
  mv "$snap" "$target" || fail 21 "No se pudo restaurar $snap"
  eval "PLACED_$kind=1"
}
finish() {
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then
    rollback
  else
    rm -rf "$CFG_TARGET.$SUFFIX" "$ST_TARGET.$SUFFIX"
  fi
  if [ "$STOPPED" = 1 ]; then phase "Iniciando servicios"; zw_start; ha_start; fi
  if [ "$rc" -eq 0 ]; then echo "OK|ts=$TS" > "$JOB/status"; else echo "FAIL|$rc|${FAIL_MSG:-error $rc}" > "$JOB/status"; fi
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
stop_all
phase "Revirtiendo a la copia apartada"
[ -n "$SNAP_CFG" ] && swap_in CFG "$SNAP_CFG" "$CFG_TARGET"
[ -n "$SNAP_ST" ] && swap_in ST "$SNAP_ST" "$ST_TARGET"
exit 0
"""

_STATUS_SCRIPT = r"""
CFG=@CFG@; ST=@ST@; CT=@CT@; SVC=@SVC@; DIR=@DIR@
echo "hostname=$(hostname)"
[ -d "$CFG" ] && echo "cfg_exists=1"
[ -d "$ST" ] && echo "st_exists=1"
if [ -n "$CT" ]; then
  echo "ha_running=$(docker inspect -f '{{.State.Running}}' "$CT" 2>/dev/null)"
  echo "ha_version=$(docker inspect -f '{{index .Config.Labels "io.hass.version"}}' "$CT" 2>/dev/null)"
fi
echo "zw_active=$(systemctl is-active "$SVC" 2>/dev/null)"
P=""
[ -d "$CFG" ] && P="$CFG"
[ -d "$ST" ] && P="$P $ST"
[ -n "$P" ] && echo "estimate=$(du -sbc @DU_EXCLUDES@ $P 2>/dev/null | tail -1 | cut -f1)"
echo "free=$(df -B1 --output=avail "$(dirname "$DIR")" 2>/dev/null | tail -1 | tr -d ' ')"
NVM=$(ls -1t "$ST"/backups/nvm/*.bin 2>/dev/null | head -1)
if [ -n "$NVM" ]; then echo "nvm=$NVM"; echo "nvm_mtime=$(stat -c %Y "$NVM")"; fi
echo "now=$(date +%s)"
for f in "$DIR"/backup_horus_*.tar.gz; do
  [ -f "$f" ] && echo "archive=$f|$(stat -c %s "$f")|$(cut -d' ' -f1 "$f.sha256" 2>/dev/null)"
done
for d in "$CFG".pre_restore_* "$CFG".pre_reset_* "$ST".pre_restore_* "$ST".pre_reset_*; do
  [ -d "$d" ] && echo "snap=$d"
done
"""


def _bash_vars(**values: object) -> str:
    return "\n".join(f"{k}={shlex.quote(str(v))}" for k, v in values.items())


def _bash_array(name: str, items: list[str]) -> str:
    return f"{name}=(" + " ".join(shlex.quote(i) for i in items) + ")"


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", value or "")[:3])


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RemoteBackupManager:
    """Backups ligeros HORUS (config HA + store Z-Wave) con servicios detenidos."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager, self_heal: SelfHealManager):
        self.ssh = ssh
        self.ha_config = ha_config
        self.self_heal = self_heal
        self._device_id = ""
        self._remote_dir = ""
        self._store_path = ""

    # ------------------------------------------------------------------ detección

    def _root(self, script: str, timeout: Optional[int] = None) -> CommandResult:
        """Ejecuta un script bash completo con privilegios (sudo si no es root)."""
        return self.ssh.run(f"bash -c {shlex.quote(script)}", use_sudo=True, timeout=timeout)

    def device_id(self) -> str:
        """Últimos 4 hex de la MAC (igual que ssh-XXXX.rhorus.com)."""
        if self._device_id:
            return self._device_id
        match = re.match(r"^ssh-([0-9a-f]{4})\.", (self.ssh.host or "").lower())
        if match:
            self._device_id = match.group(1)
            return self._device_id
        res = self.ssh.run(
            "cat /sys/class/net/eth0/address 2>/dev/null "
            "|| cat /sys/class/net/end0/address 2>/dev/null "
            "|| cat $(ls -d /sys/class/net/e* 2>/dev/null | head -1)/address 2>/dev/null"
        )
        mac = re.sub(r"[^0-9a-f]", "", res.stdout.lower())
        if len(mac) >= 4:
            self._device_id = mac[-4:]
        else:
            host = re.sub(r"[^0-9a-z]", "", self.ssh.run("hostname").stdout.lower())
            self._device_id = (host or "equipo")[:16]
        return self._device_id

    def remote_dir(self) -> str:
        if self._remote_dir:
            return self._remote_dir
        if self.ssh.is_root:
            self._remote_dir = REMOTE_HORUS_BACKUPS_DIR
        else:
            home = self.ssh.run("echo $HOME").stdout.strip() or f"/home/{self.ssh.user}"
            self._remote_dir = f"{home}/horus_backups"
        return self._remote_dir

    def _config_path(self) -> str:
        return self.ha_config._detect_config_dir() or REMOTE_CONFIG_DIR

    def _store_path_detect(self) -> str:
        if self._store_path:
            return self._store_path
        for candidate in _STORE_CANDIDATES:
            check = self.ssh.run(f"test -d {shlex.quote(candidate)} && echo OK")
            if check.stdout.strip() == "OK":
                self._store_path = candidate
                return candidate
        return REMOTE_ZWAVE_STORE

    def local_dir(self, device_id: str = "") -> str:
        base = get_local_backups_dir()
        return os.path.join(base, device_id) if device_id else base

    # ------------------------------------------------------------------ estado

    def get_status(self) -> HorusBackupStatus:
        status = HorusBackupStatus(
            device_id=self.device_id(),
            ha_container=self.ha_config._detect_container(include_stopped=True),
            zwave_service=self.self_heal._detect_service_name(),
            config_path=self._config_path(),
            store_path=self._store_path_detect(),
        )
        script = (
            _STATUS_SCRIPT.replace("@CFG@", shlex.quote(status.config_path))
            .replace("@ST@", shlex.quote(status.store_path))
            .replace("@CT@", shlex.quote(status.ha_container))
            .replace("@SVC@", shlex.quote(status.zwave_service))
            .replace("@DIR@", shlex.quote(self.remote_dir()))
            .replace("@DU_EXCLUDES@", " ".join(shlex.quote(f"--exclude={p}") for p in _DU_EXCLUDES))
        )
        res = self._root(script, timeout=120)
        values: dict[str, str] = {}
        archives: list[HorusArchive] = []
        snaps: list[str] = []
        for line in res.stdout.splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "archive":
                archive = self._parse_remote_archive(value)
                if archive:
                    archives.append(archive)
            elif key == "snap":
                snaps.append(value.strip())
            else:
                values[key] = value.strip()

        status.hostname = values.get("hostname", "")
        status.config_exists = values.get("cfg_exists") == "1"
        status.store_exists = values.get("st_exists") == "1"
        status.ha_running = values.get("ha_running", "").lower() == "true"
        status.ha_version = values.get("ha_version", "").replace("<no value>", "")
        if not status.ha_version and status.ha_running:
            status.ha_version = self.ha_config._detect_version()
        status.zwave_active = values.get("zw_active") == "active"
        status.estimated_bytes = _to_int(values.get("estimate"))
        status.free_bytes = _to_int(values.get("free"))
        status.enough_space = (
            status.free_bytes <= 0
            or status.free_bytes >= status.estimated_bytes + _SAFETY_MARGIN
        )
        status.nvm_latest = values.get("nvm", "")
        if status.nvm_latest:
            age = _to_int(values.get("now")) - _to_int(values.get("nvm_mtime"))
            status.nvm_age_days = round(max(age, 0) / 86400, 1)
        status.remote_archives = sorted(archives, key=lambda a: a.stamp, reverse=True)
        status.snapshots = self._parse_snapshots(snaps, status.config_path, status.store_path)
        if not status.config_exists and not status.store_exists:
            status.error = "No se encontró ni la config HA ni el store Z-Wave en el controlador."
        return status

    def _parse_remote_archive(self, value: str) -> Optional[HorusArchive]:
        parts = value.split("|")
        path = parts[0].strip()
        name = path.rsplit("/", 1)[-1]
        match = _ARCHIVE_RE.match(name)
        if not match:
            return None
        return HorusArchive(
            path=path,
            name=name,
            device_id=match.group("id") or "",
            stamp=match.group("stamp"),
            size_bytes=_to_int(parts[1] if len(parts) > 1 else ""),
            sha256=parts[2].strip() if len(parts) > 2 else "",
            location="remote",
            legacy=not match.group("id"),
        )

    @staticmethod
    def _parse_snapshots(paths: list[str], cfg: str, store: str) -> list[HorusSnapshot]:
        result: list[HorusSnapshot] = []
        for path in paths:
            match = _SNAPSHOT_RE.match(path)
            if not match:
                continue
            target = match.group("target")
            if target == cfg:
                kind = "ha"
            elif target == store:
                kind = "zwave"
            else:
                continue
            result.append(
                HorusSnapshot(
                    path=path,
                    kind=kind,
                    reason=match.group("reason"),
                    stamp=match.group("stamp"),
                    target=target,
                )
            )
        result.sort(key=lambda s: (s.stamp, s.kind), reverse=True)
        return result

    # ------------------------------------------------------------------ trabajos remotos

    def _run_job(
        self,
        kind: str,
        script: str,
        files: Optional[dict[str, str]] = None,
        on_phase: Optional[PhaseFn] = None,
        timeout_s: int = _JOB_TIMEOUT_S,
    ) -> HorusJobResult:
        job_id = f"{kind}_{datetime.now():%Y%m%d_%H%M%S}"
        job_dir = f"{JOBS_DIR}/{job_id}"
        qdir = shlex.quote(job_dir)
        setup = [
            "umask 077",
            f"mkdir -p {qdir}",
            f"{{ find {shlex.quote(JOBS_DIR)} -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf {{}} + 2>/dev/null; true; }}",
        ]
        for name, content in {"job.sh": script, **(files or {})}.items():
            b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
            setup.append(f"echo {b64} | base64 -d > {qdir}/{shlex.quote(name)}")
        setup += [f"chmod 700 {qdir}/job.sh", f"echo PENDING > {qdir}/status", "echo READY"]
        res = self._root(" && ".join(setup), timeout=60)
        if "READY" not in res.stdout:
            raise SSHCommandError(
                f"No se pudo preparar el trabajo remoto: {res.stderr or res.stdout}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )

        unit = f"horus-{job_id.replace('_', '-')}"
        job_sh = f"{qdir}/job.sh"
        launch = (
            f"if command -v systemd-run >/dev/null 2>&1 && "
            f"systemd-run --unit={unit} --collect --quiet /bin/bash {job_sh} {qdir}; then echo LAUNCHED; "
            f"else setsid nohup /bin/bash {job_sh} {qdir} </dev/null >/dev/null 2>&1 & echo LAUNCHED; fi"
        )
        res = self._root(launch, timeout=60)
        if "LAUNCHED" not in res.stdout:
            raise SSHCommandError(
                f"No se pudo lanzar el trabajo remoto: {res.stderr or res.stdout}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        return self._poll_job(job_dir, on_phase, timeout_s)

    def _poll_job(self, job_dir: str, on_phase: Optional[PhaseFn], timeout_s: int) -> HorusJobResult:
        qdir = shlex.quote(job_dir)
        start = time.monotonic()
        failures = 0
        last_phase = ""
        while True:
            elapsed = int(time.monotonic() - start)
            if elapsed > timeout_s:
                return HorusJobResult(
                    ok=False,
                    status="TIMEOUT",
                    detail=f"El trabajo sigue en curso tras {timeout_s // 60} min. Revise {job_dir}/log.",
                    log_tail=self._log_tail(job_dir),
                )
            try:
                res = self._root(
                    f"cat {qdir}/status 2>/dev/null; echo '@@'; cat {qdir}/phase 2>/dev/null",
                    timeout=30,
                )
                failures = 0
            except Exception as exc:  # noqa: BLE001 — túnel inestable: reintentar
                failures += 1
                if failures >= 6:
                    raise SSHCommandError(
                        f"Se perdió la conexión mientras corría el trabajo ({exc}). "
                        f"El controlador lo termina solo y reinicia los servicios. "
                        f"Al reconectar, revise {job_dir}/status."
                    ) from exc
                time.sleep(5)
                continue

            status_txt, _, phase_txt = res.stdout.partition("@@")
            phase = phase_txt.strip()
            if on_phase and phase and phase != last_phase:
                on_phase(phase, elapsed)
                last_phase = phase
            lines = [l for l in status_txt.strip().splitlines() if l.strip()]
            line = lines[-1] if lines else ""
            if line.startswith("OK"):
                fields = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
                try:
                    self._root(f"rm -rf {qdir}", timeout=30)
                except Exception:  # noqa: BLE001 — limpieza opcional; se purga a los 7 días
                    pass
                return HorusJobResult(
                    ok=True,
                    status="OK",
                    archive_path=fields.get("path", ""),
                    sha256=fields.get("sha256", ""),
                    size_bytes=_to_int(fields.get("size")),
                    downtime_s=_to_int(fields.get("downtime")),
                    detail=fields.get("ts", ""),
                )
            if line.startswith("FAIL"):
                parts = line.split("|", 2)
                return HorusJobResult(
                    ok=False,
                    status="FAIL",
                    detail=parts[2] if len(parts) > 2 else line,
                    log_tail=self._log_tail(job_dir),
                )
            if line == "PENDING" and elapsed > 45:
                return HorusJobResult(
                    ok=False,
                    status="FAIL",
                    detail="El trabajo remoto no arrancó (systemd-run/setsid).",
                    log_tail=self._log_tail(job_dir),
                )
            time.sleep(3)

    def _log_tail(self, job_dir: str, lines: int = 25) -> str:
        try:
            return self._root(f"tail -n {lines} {shlex.quote(job_dir)}/log 2>/dev/null", timeout=30).stdout
        except Exception:  # noqa: BLE001
            return ""

    def wait_healthy(
        self,
        need_ha: bool = True,
        need_zwave: bool = True,
        timeout_s: int = _HEALTH_TIMEOUT_S,
        on_tick: Optional[Callable[[int], None]] = None,
    ) -> tuple[bool, bool]:
        """Espera a que respondan HA (:8123) y Z-Wave JS UI (:3000 o :8091)."""
        probe = (
            "timeout 3 bash -c '</dev/tcp/127.0.0.1/8123' 2>/dev/null && echo HA=1; "
            "(timeout 3 bash -c '</dev/tcp/127.0.0.1/3000' 2>/dev/null "
            "|| timeout 3 bash -c '</dev/tcp/127.0.0.1/8091' 2>/dev/null) && echo ZW=1; true"
        )
        start = time.monotonic()
        ha_ok = not need_ha
        zw_ok = not need_zwave
        while True:
            try:
                out = self.ssh.run(f"bash -c {shlex.quote(probe)}", timeout=20).stdout
                ha_ok = ha_ok or "HA=1" in out
                zw_ok = zw_ok or "ZW=1" in out
            except Exception:  # noqa: BLE001
                pass
            elapsed = int(time.monotonic() - start)
            if (ha_ok and zw_ok) or elapsed >= timeout_s:
                return ha_ok, zw_ok
            if on_tick:
                on_tick(elapsed)
            time.sleep(5)

    # ------------------------------------------------------------------ backup

    def create_archive(self, on_phase: Optional[PhaseFn] = None) -> HorusJobResult:
        """Detiene servicios, empaqueta config HA + store Z-Wave (sin DB) y los reinicia."""
        status = self.get_status()
        if status.error:
            raise ValidationError(status.error)
        if not status.enough_space:
            raise ValidationError(
                f"Espacio insuficiente: libres {_human(status.free_bytes)}, "
                f"se necesitan ~{_human(status.estimated_bytes + _SAFETY_MARGIN)}."
            )

        cfg_parent, cfg_name = _split(status.config_path) if status.config_exists else ("", "")
        st_parent, st_name = _split(status.store_path) if status.store_exists else ("", "")
        excludes = [f"--exclude={p}" for p in _EXCLUDE_ANY]
        operands: list[str] = []
        if cfg_name:
            excludes += [f"--exclude={cfg_name}/{d}" for d in _CFG_EXCLUDE_DIRS]
            operands += ["-C", cfg_parent, cfg_name]
        if st_name:
            excludes += [f"--exclude={st_name}/{d}" for d in _STORE_EXCLUDE_DIRS]
            excludes.append(f"--exclude={st_name}/*.log")
            operands += ["-C", st_parent, st_name]

        manifest = {
            "format": MANIFEST_FORMAT,
            "app": APP_NAME,
            "app_version": APP_VERSION,
            "created": datetime.now().isoformat(timespec="seconds"),
            "device_id": status.device_id,
            "hostname": status.hostname,
            "ha_version": status.ha_version,
            "ha_container": status.ha_container,
            "zwave_service": status.zwave_service,
            "config_path": status.config_path if cfg_name else "",
            "store_path": status.store_path if st_name else "",
            "config_name": cfg_name,
            "store_name": st_name,
            "nvm_file": status.nvm_latest.rsplit("/", 1)[-1] if status.nvm_latest else "",
            "excludes": list(_EXCLUDE_ANY),
        }
        job_dir_ref = '"$JOB"'
        tar_args = excludes + operands
        variables = _bash_vars(
            HA_CT=status.ha_container,
            ZW_SVC=status.zwave_service,
            OUT_DIR=self.remote_dir(),
            DEVICE_ID=status.device_id,
            ARCHIVE_OWNER="" if self.ssh.is_root else self.ssh.user,
            NVM_FILE=status.nvm_latest,
        )
        array = _bash_array("TAR_ARGS", tar_args) + f"\nTAR_ARGS+=(-C {job_dir_ref} {MANIFEST_NAME})"
        script = (
            _PRELUDE.replace("@VARS@", variables + "\n" + array)
            + _BACKUP_BODY.replace("@NVM_DIR@", NVM_DIR)
        )
        return self._run_job(
            "backup",
            script,
            files={MANIFEST_NAME: json.dumps(manifest, indent=2, ensure_ascii=False)},
            on_phase=on_phase,
        )

    def download_archive(
        self,
        remote_path: str,
        expected_sha: str = "",
        progress: Optional[ProgressFn] = None,
    ) -> tuple[str, str]:
        """Descarga y verifica SHA-256. Devuelve (ruta_local, ruta_nvm_local)."""
        name = self._validate_remote_archive(remote_path)
        match = _ARCHIVE_RE.match(name)
        device = (match.group("id") if match else "") or self.device_id()
        local_path = os.path.join(self.local_dir(device), name)

        if not expected_sha:
            res = self._root(
                f"cut -d' ' -f1 {shlex.quote(remote_path)}.sha256 2>/dev/null "
                f"|| sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1",
                timeout=120,
            )
            expected_sha = res.stdout.strip().split()[0] if res.stdout.strip() else ""

        self.ssh.download_file(remote_path, local_path, progress=progress)
        local_sha = sha256_file(local_path)
        if expected_sha and local_sha != expected_sha:
            os.remove(local_path)
            raise SSHCommandError(
                "La descarga no coincide con el SHA-256 del controlador (archivo dañado en tránsito). "
                "Reintente la descarga."
            )
        with open(f"{local_path}.sha256", "w", encoding="ascii") as fh:
            fh.write(f"{local_sha}  {name}\n")
        return local_path, self._extract_nvm_local(local_path)

    @staticmethod
    def _extract_nvm_local(local_path: str) -> str:
        """Deja el .bin de la antena junto al backup para subirlo en Z-Wave JS UI."""
        try:
            with tarfile.open(local_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if member.isfile() and member.name.startswith(f"{NVM_DIR}/") and member.name.endswith(".bin"):
                        fh = tf.extractfile(member)
                        if fh is None:
                            continue
                        out = local_path[: -len(".tar.gz")] + "_NVM.bin"
                        with open(out, "wb") as dst:
                            dst.write(fh.read())
                        return out
        except (tarfile.TarError, OSError):
            return ""
        return ""

    def delete_remote_archive(self, remote_path: str) -> str:
        self._validate_remote_archive(remote_path)
        q = shlex.quote(remote_path)
        res = self._root(f"rm -f {q} {q}.sha256 && echo OK", timeout=60)
        if "OK" not in res.stdout:
            raise SSHCommandError(f"No se pudo eliminar {remote_path}: {res.stderr}")
        return f"Eliminado del controlador: {remote_path}"

    def prune_remote(self, keep: int = 1) -> list[str]:
        """Conserva solo los `keep` backups más recientes en el controlador."""
        if keep < 1:
            raise ValidationError("Debe conservar al menos 1 backup en el controlador.")
        archives = self.get_status().remote_archives
        return [self.delete_remote_archive(a.path) for a in archives[keep:]]

    def _validate_remote_archive(self, remote_path: str) -> str:
        value = remote_path.strip()
        parent, _, name = value.rpartition("/")
        if parent != self.remote_dir() or not _ARCHIVE_RE.match(name):
            raise ValidationError(f"Ruta de backup remoto no válida: {remote_path}")
        return name

    # ------------------------------------------------------------------ locales

    def list_local_archives(self) -> list[HorusArchive]:
        base = get_local_backups_dir()
        found: list[HorusArchive] = []
        if not os.path.isdir(base):
            return found
        for root, dirs, files in os.walk(base):
            if os.path.relpath(root, base).count(os.sep) >= 1:
                dirs[:] = []
            for name in files:
                match = _ARCHIVE_RE.match(name)
                if not match:
                    continue
                path = os.path.join(root, name)
                sha = ""
                sidecar = f"{path}.sha256"
                if os.path.isfile(sidecar):
                    with open(sidecar, encoding="ascii", errors="ignore") as fh:
                        sha = (fh.read().split() or [""])[0]
                found.append(
                    HorusArchive(
                        path=path,
                        name=name,
                        device_id=match.group("id") or os.path.basename(root),
                        stamp=match.group("stamp"),
                        size_bytes=os.path.getsize(path),
                        sha256=sha,
                        location="local",
                        legacy=not match.group("id"),
                    )
                )
        found.sort(key=lambda a: a.stamp, reverse=True)
        return found

    def prune_local(self, device_id: str, keep: int) -> list[str]:
        if keep < 1:
            raise ValidationError("Debe conservar al menos 1 backup local.")
        archives = [a for a in self.list_local_archives() if a.device_id == device_id]
        removed: list[str] = []
        for archive in archives[keep:]:
            stem = archive.path[: -len(".tar.gz")]
            for path in (archive.path, f"{archive.path}.sha256", f"{stem}_NVM.bin"):
                if os.path.isfile(path):
                    os.remove(path)
            removed.append(archive.name)
        return removed

    def inspect_local_archive(self, local_path: str) -> HorusArchiveInfo:
        """Valida un .tar.gz local (integridad, rutas seguras, contenido) antes de subirlo."""
        if not os.path.isfile(local_path):
            raise ValidationError(f"No existe el archivo: {local_path}")
        name = os.path.basename(local_path)
        info = HorusArchiveInfo(path=local_path, name=name)
        info.sha256 = sha256_file(local_path)
        sidecar = f"{local_path}.sha256"
        if os.path.isfile(sidecar):
            with open(sidecar, encoding="ascii", errors="ignore") as fh:
                expected = (fh.read().split() or [""])[0]
            if expected and expected != info.sha256:
                raise ValidationError("El archivo no coincide con su .sha256: está dañado o fue modificado.")
        else:
            info.warnings.append("Sin archivo .sha256 junto al backup: no se pudo verificar el origen.")

        try:
            with tarfile.open(local_path, "r:gz") as tf:
                members = tf.getmembers()
                manifest = None
                if any(m.name == MANIFEST_NAME for m in members):
                    fh = tf.extractfile(MANIFEST_NAME)
                    if fh is not None:
                        manifest = json.loads(fh.read().decode("utf-8"))
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ValidationError(f"El archivo está dañado o no es un .tar.gz válido: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Manifest ilegible: {exc}") from exc

        for member in members:
            parts = member.name.split("/")
            if member.name.startswith("/") or ".." in parts:
                raise ValidationError(f"Ruta insegura dentro del backup: {member.name}")
        info.total_bytes = sum(m.size for m in members if m.isfile())
        dirs = {m.name.rstrip("/") for m in members if m.isdir()}
        files = {m.name for m in members if m.isfile()}

        if manifest:
            info.device_id = manifest.get("device_id", "")
            info.hostname = manifest.get("hostname", "")
            info.created = manifest.get("created", "")
            info.ha_version = manifest.get("ha_version", "")
            info.src_config = manifest.get("config_name", "")
            info.src_store = manifest.get("store_name", "")
        else:
            info.legacy = True
            info.warnings.append("Backup sin manifest (formato guía manual): se detectó el contenido por estructura.")
            match = _ARCHIVE_RE.match(name)
            info.device_id = (match.group("id") if match else "") or ""
            info.src_config = _find_parent(dirs | files, ".storage") or _find_parent(files, "configuration.yaml")
            info.src_store = next(
                (p.rsplit("/", 1)[0] for p in sorted(files, key=len) if p.endswith("/settings.json") and "zwave" in p.lower()),
                "",
            )
        if info.src_config and info.src_config not in dirs and not any(f.startswith(info.src_config + "/") for f in files):
            info.src_config = ""
        if info.src_store and info.src_store not in dirs and not any(f.startswith(info.src_store + "/") for f in files):
            info.src_store = ""
        info.nvm_member = next(
            (f for f in files if f.startswith(f"{NVM_DIR}/") and f.endswith(".bin")), ""
        )
        if not info.src_config and not info.src_store:
            raise ValidationError("El backup no contiene config HA ni store Z-Wave reconocibles.")
        if info.src_store and not info.nvm_member:
            info.warnings.append(
                "El backup no incluye respaldo NVM (.bin) de la antena Z-Wave: "
                "en un equipo nuevo habrá que cargar el .bin manualmente o re-emparejar."
            )
        return info

    # ------------------------------------------------------------------ restore / reset

    def restore(
        self,
        info: HorusArchiveInfo,
        restore_ha: bool = True,
        restore_zwave: bool = True,
        keep_db: bool = True,
        progress: Optional[ProgressFn] = None,
        on_phase: Optional[PhaseFn] = None,
    ) -> HorusJobResult:
        """Sube el backup y lo restaura apartando lo actual (*.pre_restore_TS) para poder revertir."""
        src_cfg = info.src_config if restore_ha else ""
        src_st = info.src_store if restore_zwave else ""
        if not src_cfg and not src_st:
            raise ValidationError("No hay nada seleccionado para restaurar.")

        status = self.get_status()
        if status.free_bytes > 0 and status.free_bytes < info.total_bytes + _SAFETY_MARGIN:
            raise ValidationError(
                f"Espacio insuficiente para extraer: libres {_human(status.free_bytes)}, "
                f"se necesitan ~{_human(info.total_bytes + _SAFETY_MARGIN)}."
            )

        remote_dir = self.remote_dir()
        remote_path = f"{remote_dir}/{info.name}"
        owner = "" if self.ssh.is_root else self.ssh.user
        qdir = shlex.quote(remote_dir)
        prep = f"mkdir -p {qdir} && chmod 700 {qdir}"
        if owner:
            prep += f" && chown {shlex.quote(owner)} {qdir}"
        prep += f"; sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1"
        remote_sha = self._root(prep, timeout=120).stdout.strip()
        if remote_sha != info.sha256:
            self.ssh.upload_file(info.path, remote_path, progress=progress)
            self._root(f"chmod 600 {shlex.quote(remote_path)}", timeout=30)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cfg_parent, _ = _split(status.config_path)
        variables = _bash_vars(
            HA_CT=status.ha_container,
            ZW_SVC=status.zwave_service,
            ARCHIVE=remote_path,
            EXPECTED_SHA=info.sha256,
            STAGE=f"{cfg_parent}/.horus_restore_{ts}",
            TS=ts,
            SRC_CFG=src_cfg,
            SRC_ST=src_st,
            CFG_TARGET=status.config_path,
            ST_TARGET=status.store_path,
            KEEP_DB="1" if keep_db else "0",
        )
        script = _PRELUDE.replace("@VARS@", variables) + _SWAP_COMMON + _RESTORE_BODY
        result = self._run_job("restore", script, on_phase=on_phase)
        self._store_path = ""
        return result

    def factory_reset(
        self,
        reset_ha: bool = True,
        reset_zwave: bool = True,
        on_phase: Optional[PhaseFn] = None,
    ) -> HorusJobResult:
        """Deja HA/Z-Wave en blanco; lo anterior queda en *.pre_reset_TS (reversible)."""
        if not reset_ha and not reset_zwave:
            raise ValidationError("No hay nada seleccionado para resetear.")
        status = self.get_status()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        variables = _bash_vars(
            HA_CT=status.ha_container,
            ZW_SVC=status.zwave_service,
            TS=ts,
            CFG_TARGET=status.config_path,
            ST_TARGET=status.store_path,
            DO_CFG="1" if reset_ha else "0",
            DO_ST="1" if reset_zwave else "0",
        )
        script = _PRELUDE.replace("@VARS@", variables) + _SWAP_COMMON + _RESET_BODY
        return self._run_job("reset", script, on_phase=on_phase)

    def revert_snapshot(self, stamp: str, on_phase: Optional[PhaseFn] = None) -> HorusJobResult:
        """Vuelve a poner las carpetas apartadas con ese sello; descarta las actuales."""
        status = self.get_status()
        snaps = [s for s in status.snapshots if s.stamp == stamp]
        if not snaps:
            raise ValidationError(f"No hay copias apartadas con sello {stamp}.")
        snap_cfg = next((s.path for s in snaps if s.kind == "ha"), "")
        snap_st = next((s.path for s in snaps if s.kind == "zwave"), "")
        variables = _bash_vars(
            HA_CT=status.ha_container,
            ZW_SVC=status.zwave_service,
            TS=datetime.now().strftime("%Y%m%d_%H%M%S"),
            SNAP_CFG=snap_cfg,
            SNAP_ST=snap_st,
            CFG_TARGET=status.config_path,
            ST_TARGET=status.store_path,
        )
        script = _PRELUDE.replace("@VARS@", variables) + _REVERT_BODY
        return self._run_job("revert", script, on_phase=on_phase)

    def delete_snapshots(self, stamp: str) -> str:
        status = self.get_status()
        snaps = [s for s in status.snapshots if s.stamp == stamp]
        if not snaps:
            raise ValidationError(f"No hay copias apartadas con sello {stamp}.")
        for snap in snaps:
            match = _SNAPSHOT_RE.match(snap.path)
            if not match or match.group("target") not in (status.config_path, status.store_path):
                raise ValidationError(f"Ruta no permitida: {snap.path}")
        paths = " ".join(shlex.quote(s.path) for s in snaps)
        res = self._root(f"rm -rf {paths} && echo OK", timeout=300)
        if "OK" not in res.stdout:
            raise SSHCommandError(f"No se pudieron eliminar las copias: {res.stderr}")
        return f"Eliminadas {len(snaps)} copia(s) apartada(s) del {stamp}."

    def reboot(self) -> str:
        self._root("systemd-run --on-active=3 --quiet systemctl reboot 2>/dev/null "
                   "|| (setsid nohup sh -c 'sleep 3; reboot' </dev/null >/dev/null 2>&1 &)")
        return "Reinicio programado en 3 s. Vuelva a conectar en 2-3 minutos."

    @staticmethod
    def version_warning(backup_version: str, current_version: str) -> str:
        """HA no soporta bajar de versión: restaurar .storage nuevo en HA viejo rompe la config."""
        b, c = _version_tuple(backup_version), _version_tuple(current_version)
        if b and c and b > c:
            return (
                f"El backup es de HA {backup_version} y el contenedor actual es {current_version}. "
                "HA no soporta bajar de versión: actualice HA antes de restaurar."
            )
        return ""


def _split(path: str) -> tuple[str, str]:
    parent, _, name = path.rstrip("/").rpartition("/")
    return parent or "/", name


def _find_parent(paths: set[str], leaf: str) -> str:
    candidates = [p.rsplit("/", 1)[0] for p in paths if p.endswith(f"/{leaf}") and "/" in p]
    return min(candidates, key=len) if candidates else ""


def _to_int(value: Optional[str]) -> int:
    try:
        return int((value or "").strip() or 0)
    except ValueError:
        return 0


def _human(n: int) -> str:
    return HorusArchive(path="", name="", size_bytes=n).size_human
