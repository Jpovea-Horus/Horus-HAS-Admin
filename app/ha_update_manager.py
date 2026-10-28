"""Actualización de Home Assistant (docker compose) + Z-Wave JS UI (npm) en Nexxo 800.

Procedimiento para disco ~7 GB: sin cp -a de config; la imagen vieja de HA se borra
antes del pull porque no caben las dos. Pasos 3–5 corren como trabajo remoto
desacoplado (sobrevive a cortes de SSH).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING, Optional

from exceptions import ValidationError
from ha_compat import (
    CompatError,
    check_pair,
    ha_tag_exists,
    latest_ha_release,
    latest_zwave_js_ui,
    node_satisfies,
    version_tuple,
    zwave_node_requirement,
    zwave_version_exists,
)
from models import CommandResult, HaUpdatePlan, HaUpdateProfile, HaUpdateStatus, HorusJobResult
from paths import REMOTE_BASE_PATH, REMOTE_CONFIG_DIR
from remote_backup_manager import _PRELUDE, PhaseFn, _bash_vars, _to_int

if TYPE_CHECKING:
    from backup_manager import MaintenanceManager
    from remote_backup_manager import RemoteBackupManager
    from ssh_client import SSHClient

ZWAVE_SERVICE = "zwave-ui.service"
MIN_FREE_START_MB = 500
MIN_FREE_AFTER_RM_MB = 2048
MAX_DISK_USED_PCT = 85
MAX_DB_MB = 500
_JOB_TIMEOUT_S = 45 * 60

_ARCH_MAP = {
    "x86_64": ("amd64", ""),
    "amd64": ("amd64", ""),
    "aarch64": ("arm64", ""),
    "arm64": ("arm64", ""),
    "armv7l": ("arm", "v7"),
    "armv6l": ("arm", "v6"),
    "i686": ("386", ""),
    "i386": ("386", ""),
}

_IMAGE_LINE_RE = re.compile(
    r"^(?P<pre>[ \t]*image:[ \t]*)(?P<q>['\"]?)(?P<ref>[^'\"\s#]+)(?P=q)", re.M
)

_STATUS_SCRIPT = r"""
BASE=@BASE@
for f in "$BASE/docker-compose.yaml" "$BASE/docker-compose.yml"; do
  [ -f "$f" ] && { echo "COMPOSE=$f"; break; }
done
if docker compose version >/dev/null 2>&1; then echo "CMD=docker compose"
elif command -v docker-compose >/dev/null 2>&1; then echo "CMD=docker-compose"; fi
CT=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -iE 'homeassistant|home-assistant' | head -1)
echo "CT=$CT"
if [ -n "$CT" ]; then
  ID=$(docker inspect -f '{{.Image}}' "$CT" 2>/dev/null)
  echo "IMG_ID=$ID"
  echo "IMG_BYTES=$(docker image inspect -f '{{.Size}}' "$ID" 2>/dev/null)"
  VER=$(docker inspect -f '{{index .Config.Labels "io.hass.version"}}' "$CT" 2>/dev/null)
  if [ -z "$VER" ] || [ "$VER" = "<no value>" ]; then
    VER=$(timeout 60 docker exec "$CT" hass --version 2>/dev/null)
  fi
  echo "HA_VER=$VER"
fi
echo "ARCH=$(uname -m)"
echo "NODE=$(node -v 2>/dev/null)"
NPM_ROOT=$(npm root -g 2>/dev/null)
PKG="$NPM_ROOT/zwave-js-ui/package.json"
if [ -n "$NPM_ROOT" ] && [ -f "$PKG" ]; then
  echo "ZW_VER=$(grep -m1 '"version"' "$PKG" | sed -E 's/.*"version" *: *"([^"]+)".*/\1/')"
fi
echo "ZW_ACTIVE=$(systemctl is-active @SVC@ 2>/dev/null)"
ZP=$(systemctl show -p ExecStart --value @SVC@ 2>/dev/null | grep -o 'argv\[\]=[^;]*' | head -1 | sed 's/^argv\[\]=//')
echo "ZW_EXEC=$ZP"
GLOBAL=0
[ -z "$ZP" ] && GLOBAL=1
for w in $ZP; do
  r=$(readlink -f "$(command -v "$w" 2>/dev/null || echo "$w")" 2>/dev/null)
  case "$r" in "$NPM_ROOT"/zwave-js-ui/*) GLOBAL=1 ;; esac
done
WD=$(systemctl show -p WorkingDirectory --value @SVC@ 2>/dev/null)
case "$(readlink -f "$WD" 2>/dev/null)" in "$NPM_ROOT"/zwave-js-ui*) GLOBAL=1 ;; esac
echo "ZW_GLOBAL=$GLOBAL"
df -Pm / | awk 'NR==2{gsub("%","",$5); print "FREE_MB="$4; print "USED_PCT="$5}'
echo "DB_MB=$(du -m @CFG@/home-assistant_v2.db 2>/dev/null | cut -f1)"
"""

_UPDATE_BODY = r"""
HA_GONE=0
COMPOSE_BAK="$COMPOSE_FILE.bak.horus.update"
free_mb() { df -Pm / | awk 'NR==2{print $4}'; }
ha_running() { [ "$(docker inspect -f '{{.State.Running}}' "$HA_CT" 2>/dev/null)" = "true" ]; }
finish() {
  rc=$?
  trap - EXIT
  [ "$rc" -eq 0 ] && return 0
  if [ "$HA_GONE" = 1 ] && ! ha_running; then
    phase "Recuperación: levantando Home Assistant"
    $COMPOSE -f "$COMPOSE_FILE" up -d \
      || { [ -f "$COMPOSE_BAK" ] && cat "$COMPOSE_BAK" > "$COMPOSE_FILE" && $COMPOSE -f "$COMPOSE_FILE" up -d; } \
      || true
  fi
  if [ "$DO_ZW" = 1 ] && ! systemctl is-active --quiet "$ZW_SVC"; then zw_start; fi
  echo "FAIL|$rc|${FAIL_MSG:-error $rc}" > "$JOB/status"
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
cd "$(dirname "$COMPOSE_FILE")" 2>/dev/null || cd /

if [ "$DO_HA" = 1 ]; then
  if [ "$CHECK_MANIFEST" = 1 ]; then
    phase "Verificando $NEW_IMG en el registro"
    timeout 90 docker manifest inspect "$NEW_IMG" >/dev/null 2>&1 \
      || fail 3 "No se alcanza $NEW_IMG en el registro; HA actual intacta"
  fi
  phase "Deteniendo y borrando HA vieja"
  HA_GONE=1
  docker stop -t 60 "$HA_CT" >/dev/null 2>&1 || true
  docker rm "$HA_CT" >/dev/null 2>&1 || true
  if [ -n "$OLD_IMG" ]; then docker rmi -f "$OLD_IMG" >/dev/null 2>&1 || true; fi
  phase "Liberando espacio Docker"
  docker system prune -f >/dev/null 2>&1 || true
  docker builder prune -af >/dev/null 2>&1 || true
  FREE=$(free_mb)
  echo "Libre tras borrar HA: ${FREE} MB"
  [ "$FREE" -ge "$MIN_MB" ] || fail 4 "Solo ${FREE} MB libres tras borrar HA (mínimo $MIN_MB)"
  phase "Fijando $NEW_IMG en $(basename "$COMPOSE_FILE")"
  cp -p "$COMPOSE_FILE" "$COMPOSE_BAK" || fail 5 "No se pudo respaldar el compose"
  cat "$JOB/compose.new" > "$COMPOSE_FILE" || fail 5 "No se pudo escribir el compose"
  phase "Descargando HA nueva (10-15 min)"
  $COMPOSE -f "$COMPOSE_FILE" pull || fail 5 "docker compose pull falló"
  phase "Levantando Home Assistant"
  $COMPOSE -f "$COMPOSE_FILE" up -d || fail 6 "docker compose up falló"
  for _ in $(seq 1 36); do ha_running && break; sleep 5; done
  ha_running || fail 6 "Home Assistant no quedó Up"
  HA_GONE=0
fi

if [ "$DO_ZW" = 1 ]; then
  phase "Actualizando Z-Wave JS UI a $ZW_VERSION (npm)"
  zw_stop
  npm install -g "zwave-js-ui@$ZW_VERSION" --unsafe-perm || fail 9 "npm install zwave-js-ui@$ZW_VERSION falló"
  npm cache clean --force >/dev/null 2>&1 || true
  rm -rf /root/.npm/_cacache
  phase "Arrancando Z-Wave JS UI"
  systemctl start "$ZW_SVC" || fail 9 "No arrancó $ZW_SVC"
  sleep 5
  systemctl is-active --quiet "$ZW_SVC" || fail 9 "$ZW_SVC no quedó activo"
fi
echo "OK|ts=$(date +%F_%T)" > "$JOB/status"
"""


def _split_image_ref(ref: str) -> tuple[str, str]:
    """('ghcr.io/home-assistant/home-assistant', 'stable'); tag vacío si no tiene."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        cut = ref.rfind(":")
        return ref[:cut], ref[cut + 1 :]
    return ref, ""


def _find_ha_image(compose: str) -> Optional[re.Match[str]]:
    for match in _IMAGE_LINE_RE.finditer(compose):
        ref = match.group("ref").lower()
        if "homeassistant" in ref or "home-assistant" in ref:
            return match
    return None


def pin_compose_tag(compose: str, tag: str) -> tuple[str, str]:
    """Devuelve (compose nuevo, imagen nueva) con el tag de HA fijado."""
    match = _find_ha_image(compose)
    if not match:
        raise ValidationError("El compose no tiene una línea image: de Home Assistant.")
    repo, _ = _split_image_ref(match.group("ref"))
    image = f"{repo}:{tag}"
    quote = match.group("q")
    line = f"{match.group('pre')}{quote}{image}{quote}"
    return compose[: match.start()] + line + compose[match.end() :], image


class HaUpdateManager:
    def __init__(
        self, ssh: SSHClient, maintenance: MaintenanceManager, jobs: RemoteBackupManager
    ):
        self.ssh = ssh
        self.maintenance = maintenance
        self._jobs = jobs

    def _root(self, script: str, timeout: Optional[int] = 120) -> CommandResult:
        return self.ssh.run(f"bash -c {shlex.quote(script)}", use_sudo=True, timeout=timeout)

    # ------------------------------------------------------------------ estado

    def get_status(self) -> HaUpdateStatus:
        script = (
            _STATUS_SCRIPT.replace("@BASE@", shlex.quote(REMOTE_BASE_PATH))
            .replace("@CFG@", shlex.quote(REMOTE_CONFIG_DIR))
            .replace("@SVC@", ZWAVE_SERVICE)
        )
        out = self._root(script, timeout=180).stdout
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        st = HaUpdateStatus(
            compose_file=kv.get("COMPOSE", ""),
            compose_cmd=kv.get("CMD", ""),
            ha_container=kv.get("CT", ""),
            ha_image_id=kv.get("IMG_ID", ""),
            ha_image_mb=_to_int(kv.get("IMG_BYTES")) // (1024 * 1024),
            ha_version=kv.get("HA_VER", "").strip(),
            arch=kv.get("ARCH", "").strip(),
            zwave_version=kv.get("ZW_VER", "").strip(),
            zwave_active=kv.get("ZW_ACTIVE", "").strip() == "active",
            zwave_exec=kv.get("ZW_EXEC", "").strip(),
            zwave_global=kv.get("ZW_GLOBAL", "1").strip() == "1",
            node_version=kv.get("NODE", "").strip(),
            free_mb=_to_int(kv.get("FREE_MB")),
            used_pct=_to_int(kv.get("USED_PCT")),
            db_mb=_to_int(kv.get("DB_MB")),
        )
        if st.compose_file:
            match = _find_ha_image(self._read_compose(st.compose_file))
            if match:
                st.compose_image = match.group("ref")
        return st

    def _read_compose(self, path: str) -> str:
        return self.ssh.run(f"cat {shlex.quote(path)}", use_sudo=True).stdout

    def profile_from_controller(self) -> HaUpdateProfile:
        st = self.get_status()
        if not st.ha_version or not st.zwave_version:
            raise ValidationError(
                "No se pudieron leer las versiones de HA y Z-Wave JS UI de este controlador."
            )
        return HaUpdateProfile(
            id=f"local-{st.ha_version}-{st.zwave_version}",
            ha=st.ha_version,
            zwave_js_ui=st.zwave_version,
            notes=f"Registrado desde controlador ({self.ssh.host}) · Node {st.node_version}",
            source="local",
        )

    @staticmethod
    def latest_profile() -> HaUpdateProfile:
        try:
            ha = latest_ha_release()
            zw = latest_zwave_js_ui()
        except CompatError as exc:
            raise ValidationError(f"No se pudo consultar la última versión: {exc}") from exc
        return HaUpdateProfile(
            id="latest",
            ha=ha,
            zwave_js_ui=zw,
            notes="Última versión publicada. NO validada en Nexxo 800.",
            source="latest",
        )

    # ------------------------------------------------------------------ plan

    def _check_manifest(self, image: str, arch: str) -> tuple[Optional[bool], str]:
        res = self._root(f"timeout 90 docker manifest inspect {shlex.quote(image)} 2>&1", timeout=120)
        out = res.stdout or res.stderr
        low = out.lower()
        if res.ok:
            try:
                data = json.loads(out)
            except ValueError:
                return True, ""
            platforms = [m.get("platform") or {} for m in data.get("manifests") or []]
            want = _ARCH_MAP.get(arch)
            if not platforms or not want:
                return True, ""
            found = {(p.get("architecture", ""), p.get("variant", "")) for p in platforms}
            if any(a == want[0] and (not want[1] or v in ("", want[1])) for a, v in found):
                return True, ""
            names = ", ".join(sorted(f"{a}/{v}" if v else a for a, v in found))
            return False, f"{image} no existe para {arch} (disponible: {names})."
        if "not a docker command" in low or "experimental" in low:
            return None, "docker manifest no disponible en el controlador; se validará con el pull."
        if "no such manifest" in low or "manifest unknown" in low or "not found" in low:
            return False, f"No existe la imagen {image} en el registro."
        return None, f"No se pudo consultar el registro desde el controlador: {out.strip()[-160:]}"

    def plan(self, profile: HaUpdateProfile, update_zwave: bool = True) -> HaUpdatePlan:
        st = self.get_status()
        plan = HaUpdatePlan(profile=profile, status=st)
        cur_ha = version_tuple(st.ha_version)
        target_ha = version_tuple(profile.ha)

        plan.update_ha = not (cur_ha and cur_ha == target_ha)
        plan.update_zwave = update_zwave and st.zwave_version != profile.zwave_js_ui
        plan.is_downgrade = bool(cur_ha and target_ha and target_ha < cur_ha)
        if plan.nothing_to_do:
            return plan

        if plan.update_ha:
            self._plan_ha(plan)
        if plan.update_zwave:
            self._plan_zwave(plan)

        ha_eff = profile.ha if plan.update_ha else st.ha_version
        zw_eff = profile.zwave_js_ui if plan.update_zwave else st.zwave_version
        if ha_eff and zw_eff:
            compat = check_pair(ha_eff, zw_eff)
            if compat.compatible is False:
                plan.blockers.append(f"Par incompatible: {compat.detail}.")
            elif compat.compatible is None:
                plan.warnings.append(compat.detail)
            else:
                plan.warnings.insert(0, f"Compatibles: {compat.detail}.")
        if plan.is_downgrade:
            plan.warnings.append(
                f"DOWNGRADE de HA {st.ha_version} → {profile.ha}: la DB y .storage pueden "
                "haber migrado y no cargar en la versión vieja."
            )
        return plan

    def _plan_ha(self, plan: HaUpdatePlan) -> None:
        st, profile = plan.status, plan.profile
        if not st.compose_file:
            plan.blockers.append(f"No existe {REMOTE_BASE_PATH}/docker-compose.yaml ni .yml.")
            return
        if not st.compose_cmd:
            plan.blockers.append("docker compose no está disponible en el controlador.")
            return
        try:
            plan.new_compose, plan.new_image = pin_compose_tag(
                self._read_compose(st.compose_file), profile.ha
            )
        except ValidationError as exc:
            plan.blockers.append(str(exc))
            return
        try:
            if not ha_tag_exists(profile.ha):
                plan.blockers.append(f"Home Assistant {profile.ha} no existe.")
                return
        except CompatError as exc:
            plan.warnings.append(f"No se pudo confirmar el tag {profile.ha} en GitHub: {exc}")

        ok, detail = self._check_manifest(plan.new_image, st.arch)
        if ok is False:
            plan.blockers.append(detail)
            return
        if ok is None:
            plan.warnings.append(detail)

        if st.free_after_rm_mb < MIN_FREE_AFTER_RM_MB:
            plan.blockers.append(
                f"Tras borrar la HA vieja quedarían ~{st.free_after_rm_mb} MB "
                f"(mínimo {MIN_FREE_AFTER_RM_MB} MB para el pull)."
            )
        if not st.ha_container:
            plan.warnings.append("No hay contenedor de HA: se instalará desde cero con el compose.")

    def _plan_zwave(self, plan: HaUpdatePlan) -> None:
        st, profile = plan.status, plan.profile
        if not st.zwave_global:
            plan.blockers.append(
                f"{ZWAVE_SERVICE} no usa la instalación global de npm "
                f"(ExecStart: {st.zwave_exec or '—'}); npm install -g no la actualizaría."
            )
            return
        try:
            if not zwave_version_exists(profile.zwave_js_ui):
                plan.blockers.append(f"zwave-js-ui {profile.zwave_js_ui} no existe en npm.")
                return
            plan.zwave_node_req = zwave_node_requirement(profile.zwave_js_ui)
        except CompatError as exc:
            plan.warnings.append(f"No se pudo consultar zwave-js-ui en npm: {exc}")
            return
        node_ok = node_satisfies(st.node_version, plan.zwave_node_req)
        if node_ok is False:
            plan.blockers.append(
                f"Node {st.node_version} no cumple '{plan.zwave_node_req}' "
                f"(requerido por zwave-js-ui {profile.zwave_js_ui})."
            )
        elif node_ok is None and not st.node_version:
            plan.blockers.append("Node no está instalado en el controlador.")

    # ------------------------------------------------------------------ ejecución

    def cleanup(self) -> str:
        """Paso 1: APT, NPM, journal, imágenes huérfanas, /config/config y *.zip."""
        summary = self.maintenance.safe_cleanup()
        base = shlex.quote(REMOTE_BASE_PATH)
        cfg = shlex.quote(REMOTE_CONFIG_DIR)
        self._root(
            f"if [ -f {cfg}/configuration.yaml ]; then rm -rf {cfg}/config; fi; "
            f"rm -f {base}/*.zip; true",
            timeout=120,
        )
        return f"{summary} | /config/config y *.zip de {REMOTE_BASE_PATH} eliminados"

    def run(self, plan: HaUpdatePlan, on_phase: Optional[PhaseFn] = None) -> HorusJobResult:
        if not plan.ok:
            raise ValidationError("El plan tiene bloqueos: " + " | ".join(plan.blockers))
        if plan.nothing_to_do:
            raise ValidationError("No hay nada que actualizar.")
        st = plan.status
        variables = _bash_vars(
            HA_CT=st.ha_container or "homeassistant",
            ZW_SVC=ZWAVE_SERVICE,
            DO_HA="1" if plan.update_ha else "0",
            DO_ZW="1" if plan.update_zwave else "0",
            OLD_IMG=st.ha_image_id,
            NEW_IMG=plan.new_image,
            COMPOSE_FILE=st.compose_file,
            COMPOSE=st.compose_cmd or "docker compose",
            ZW_VERSION=plan.profile.zwave_js_ui,
            MIN_MB=MIN_FREE_AFTER_RM_MB,
            CHECK_MANIFEST="0" if any("docker manifest no disponible" in w for w in plan.warnings) else "1",
        )
        files = {"compose.new": plan.new_compose} if plan.update_ha else {}
        script = _PRELUDE.replace("@VARS@", variables) + _UPDATE_BODY
        return self._jobs._run_job(
            "haupdate", script, files=files, on_phase=on_phase, timeout_s=_JOB_TIMEOUT_S
        )

    def zwave_log_issues(self, container: str = "") -> list[str]:
        """Errores de la integración zwave_js en el log de HA (últimos 20 min)."""
        ct = container or "homeassistant"
        res = self._root(
            f"docker logs --since 20m {shlex.quote(ct)} 2>&1 "
            "| grep -iE 'zwave_js' "
            "| grep -iE 'incompatib|schema|unable to connect|failed to connect|cannot connect' "
            "| tail -n 5",
            timeout=60,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
