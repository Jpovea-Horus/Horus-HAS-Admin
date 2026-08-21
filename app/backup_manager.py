"""Gestión de backups HA/Z-Wave y espacio en disco del controlador."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from models import BackupEntry, BackupManagerStatus, MaintenanceStatus
from paths import REMOTE_BASE_PATH, REMOTE_CONFIG_DIR, REMOTE_ZWAVE_STORE

if TYPE_CHECKING:
    from ssh_client import SSHClient

_MIN_FREE_BYTES_HA = 2_500_000_000
_BACKUP_NAME_RE = re.compile(
    r"^/[\w./-]+/(?:config_backup_\d{8}|zwave-js-ui-store_backup_\d{8})$"
)

class MaintenanceManager:
    """Mantenimiento preventivo: limpieza de caches, logs y detección de basura."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> MaintenanceStatus:
        status = MaintenanceStatus()
        
        # 1. APT Cache size
        res = self.ssh.run("du -sh /var/cache/apt/archives 2>/dev/null | cut -f1")
        status.apt_cache_size = res.stdout.strip() or "0B"

        # 2. NPM Cache size
        res = self.ssh.run("du -sh /root/.npm/_cacache 2>/dev/null | cut -f1")
        status.npm_cache_size = res.stdout.strip() or "0B"

        # 3. Journal size
        res = self.ssh.run("journalctl --disk-usage 2>/dev/null | grep -oP '(?<=is ).*'")
        status.journal_size = res.stdout.strip() or "0B"

        # 4. Nested config detection
        # /home/cat/config/config/configuration.yaml vs /home/cat/config/configuration.yaml
        check = self.ssh.run("test -f /home/cat/config/config/configuration.yaml && test -f /home/cat/config/configuration.yaml && echo YES")
        status.nested_config_detected = check.stdout.strip() == "YES"

        # 5. Old archives (>30 days)
        res = self.ssh.run("find /home/cat /root -maxdepth 2 -name '*.zip' -o -name '*.tar.gz' -mtime +30 2>/dev/null")
        status.old_archives = [l.strip() for l in res.stdout.splitlines() if l.strip()]

        # 6. HA DB size
        res = self.ssh.run("du -m /home/cat/config/home-assistant_v2.db 2>/dev/null | cut -f1")
        try:
            status.ha_db_size_mb = float(res.stdout.strip() or 0)
            status.ha_db_alert = status.ha_db_size_mb > 500
        except ValueError:
            status.ha_db_size_mb = 0.0

        return status

    def safe_cleanup(self) -> str:
        """Ejecuta la limpieza sistemática segura."""
        steps = []
        
        # APT clean
        self.ssh.run("apt-get clean", use_sudo=True)
        steps.append("Caché APT limpia")

        # NPM clean
        self.ssh.run("npm cache clean --force", use_sudo=True)
        self.ssh.run("rm -rf /root/.npm/_cacache/*", use_sudo=True)
        steps.append("Caché NPM limpia")

        # Journal vacuum
        self.ssh.run("journalctl --vacuum-time=1d", use_sudo=True)
        steps.append("Logs rotados (1 día)")

        # Docker prune (huérfanas)
        self.ssh.run("docker image prune -f", use_sudo=True)
        steps.append("Imágenes Docker huérfanas eliminadas")

        return " | ".join(steps)

    def delete_nested_config(self) -> str:
        """Borra la carpeta anidada basura /home/cat/config/config/."""
        if not self.get_status().nested_config_detected:
            return "No se detectó carpeta anidada basura."
        
        res = self.ssh.run("rm -rf /home/cat/config/config/", use_sudo=True)
        if res.ok:
            return "Carpeta anidada /home/cat/config/config/ eliminada con éxito."
        return f"Error al eliminar carpeta anidada: {res.stderr}"

    def delete_old_archives(self) -> str:
        """Borra los archivos .zip y .tar.gz de más de 30 días."""
        status = self.get_status()
        if not status.old_archives:
            return "No se encontraron archivos antiguos."
        
        count = 0
        for path in status.old_archives:
            res = self.ssh.run(f"rm -f {shlex.quote(path)}", use_sudo=True)
            if res.ok:
                count += 1
        
        return f"Se eliminaron {count} archivos antiguos."

class BackupManager:
    """Lista, crea y elimina backups; informa espacio libre y prune Docker."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh
        self._config_path: str = ""
        self._store_path: str = ""

    def _detect_config_path(self) -> str:
        if self._config_path:
            return self._config_path
        check = self.ssh.run(f"test -d {shlex.quote(REMOTE_CONFIG_DIR)} && echo OK")
        if check.stdout.strip() == "OK":
            self._config_path = REMOTE_CONFIG_DIR
            return self._config_path
        return REMOTE_CONFIG_DIR

    def _detect_store_path(self) -> str:
        if self._store_path:
            return self._store_path
        for candidate in (REMOTE_ZWAVE_STORE, "/opt/zwave-js-ui-store"):
            check = self.ssh.run(f"test -d {shlex.quote(candidate)} && echo OK")
            if check.stdout.strip() == "OK":
                self._store_path = candidate
                return candidate
        return REMOTE_ZWAVE_STORE

    def _parent_dirs(self) -> list[str]:
        paths = {REMOTE_BASE_PATH}
        for p in (self._detect_config_path(), self._detect_store_path()):
            if "/" in p:
                paths.add(p.rsplit("/", 1)[0])
        return sorted(paths)

    def _is_critical_space(self) -> tuple[bool, str]:
        """Determina si el disco está demasiado justo para backup de config."""
        status = self.get_status()
        free_human = status.root_free or f"{status.root_avail_bytes} B"
        critical = status.root_avail_bytes > 0 and status.root_avail_bytes < 500 * 1024 * 1024
        return critical, free_human

    def get_status(self) -> BackupManagerStatus:
        status = BackupManagerStatus(
            ha_config_path=self._detect_config_path(),
            zwave_store_path=self._detect_store_path(),
        )
        status.backups = self.list_backups()

        df = self.ssh.run(
            "df -B1 --output=avail,pcent,target / 2>/dev/null | tail -1"
        )
        parts = df.stdout.split()
        if len(parts) >= 2:
            try:
                status.root_avail_bytes = int(parts[0])
            except ValueError:
                status.root_avail_bytes = 0
            status.root_used_pct = parts[1].strip()
            # human readable free
            human = self.ssh.run("df -h --output=avail / 2>/dev/null | tail -1")
            status.root_free = human.stdout.strip() or parts[0]

        status.low_space = (
            status.root_avail_bytes > 0
            and status.root_avail_bytes < _MIN_FREE_BYTES_HA
        )

        docker = self.ssh.run(
            "docker system df 2>/dev/null | head -8 || echo '(docker no disponible)'"
        )
        status.docker_summary = docker.stdout.strip()
        return status

    def list_backups(self) -> list[BackupEntry]:
        parents = " ".join(shlex.quote(p) for p in self._parent_dirs())
        script = f"""
import os, glob
parents = {self._parent_dirs()!r}
entries = []
for parent in parents:
    for pattern in ("config_backup_*", "zwave-js-ui-store_backup_*"):
        for path in glob.glob(os.path.join(parent, pattern)):
            if not os.path.isdir(path):
                continue
            name = os.path.basename(path)
            kind = "ha" if name.startswith("config_backup_") else "zwave"
            size_bytes = 0
            for root, dirs, files in os.walk(path):
                for f in files:
                    try:
                        size_bytes += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            # human
            n = float(size_bytes)
            unit = "B"
            for u in ("K", "M", "G", "T"):
                if n < 1024:
                    break
                n /= 1024.0
                unit = u
            size = f"{{n:.1f}}{{unit}}" if unit != "B" else f"{{int(n)}}B"
            date_label = name.rsplit("_", 1)[-1] if "_" in name else ""
            if len(date_label) == 8 and date_label.isdigit():
                date_label = f"{{date_label[0:4]}}-{{date_label[4:6]}}-{{date_label[6:8]}}"
            entries.append((path, kind, size, size_bytes, date_label))
entries.sort(key=lambda e: e[0])
for path, kind, size, size_bytes, date_label in entries:
    print("\\t".join([path, kind, size, str(size_bytes), date_label]))
"""
        # parents unused in remote but kept for clarity; script embeds list
        _ = parents
        result = self.ssh.run(f"python3 -c {shlex.quote(script)}", timeout=120)
        backups: list[BackupEntry] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            try:
                size_bytes = int(parts[3])
            except ValueError:
                size_bytes = 0
            backups.append(
                BackupEntry(
                    path=parts[0],
                    kind=parts[1],
                    size=parts[2],
                    size_bytes=size_bytes,
                    date_label=parts[4],
                )
            )
        return backups

    def _assert_backup_path(self, path: str) -> str:
        value = path.strip().rstrip("/")
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValidationError("Ruta de backup inválida.")
        base = value.rsplit("/", 1)[-1]
        if not (
            base.startswith("config_backup_")
            or base.startswith("zwave-js-ui-store_backup_")
        ):
            raise ValidationError(
                "Solo se pueden eliminar carpetas config_backup_* o zwave-js-ui-store_backup_*."
            )
        if not _BACKUP_NAME_RE.match(value):
            raise ValidationError(f"Nombre de backup no reconocido: {base}")
        return value

    def backup_ha_config(self) -> str:
        critical, free_human = self._is_critical_space()
        if critical:
            raise SSHCommandError(
                f"Backup HA bloqueado por espacio crítico ({free_human} libres). "
                "En Nexxo 800 no se recomienda cp -a de config con < 500 MB libres.",
                exit_code=1,
                stderr="",
            )
        config = self._detect_config_path()
        check = self.ssh.run(f"test -d {shlex.quote(config)} && echo OK")
        if check.stdout.strip() != "OK":
            raise SSHCommandError(
                f"No existe la carpeta de configuración HA: {config}",
                exit_code=1,
                stderr="",
            )
        dest = f"{config}_backup_$(date +%Y%m%d)"
        result = self.ssh.run(
            f"cp -a {shlex.quote(config)} {dest} && echo DONE:{dest}",
            use_sudo=True,
            timeout=300,
        )
        if not result.ok or "DONE:" not in result.stdout:
            msg = result.stderr or result.stdout or "Fallo al respaldar config HA."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        path = result.stdout.strip().split("DONE:", 1)[-1].strip()
        return f"Backup HA creado: {path}"

    def backup_zwave_store(self) -> str:
        store = self._detect_store_path()
        check = self.ssh.run(f"test -d {shlex.quote(store)} && echo OK")
        if check.stdout.strip() != "OK":
            raise SSHCommandError(
                f"No existe el store Z-Wave: {store}",
                exit_code=1,
                stderr="",
            )
        dest = f"{store}_backup_$(date +%Y%m%d)"
        result = self.ssh.run(
            f"cp -a {shlex.quote(store)} {dest} && echo DONE:{dest}",
            use_sudo=True,
            timeout=300,
        )
        if not result.ok or "DONE:" not in result.stdout:
            msg = result.stderr or result.stdout or "Fallo al respaldar store Z-Wave."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        path = result.stdout.strip().split("DONE:", 1)[-1].strip()
        return f"Backup Z-Wave creado: {path}"

    def backup_all(self) -> str:
        critical, free_human = self._is_critical_space()
        msgs = []
        if critical:
            msgs.append(
                f"Backup HA omitido por espacio crítico ({free_human} libres, umbral 500 MB)."
            )
        else:
            msgs.append(self.backup_ha_config())
        msgs.append(self.backup_zwave_store())
        return " | ".join(msgs)

    def delete_backup(self, path: str) -> str:
        target = self._assert_backup_path(path)
        check = self.ssh.run(f"test -d {shlex.quote(target)} && echo OK")
        if check.stdout.strip() != "OK":
            raise SSHCommandError(
                f"No existe el backup: {target}",
                exit_code=1,
                stderr="",
            )
        result = self.ssh.run(
            f"rm -rf {shlex.quote(target)} && echo OK",
            use_sudo=True,
            timeout=180,
        )
        if not result.ok or result.stdout.strip() != "OK":
            msg = result.stderr or result.stdout or "No se pudo eliminar el backup."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        return f"Backup eliminado: {target}"

    def cleanup_keep_recent(self, keep: int = 2, kind: str = "all") -> str:
        """Elimina backups antiguos; conserva los `keep` más recientes por tipo."""
        if keep < 1:
            raise ValidationError("Debe conservar al menos 1 backup reciente.")
        backups = self.list_backups()
        deleted: list[str] = []
        for filter_kind in ("ha", "zwave"):
            if kind not in ("all", filter_kind):
                continue
            group = [b for b in backups if b.kind == filter_kind]
            # Orden: fecha en nombre (más reciente primero), luego path
            group.sort(key=lambda b: (b.date_label, b.path), reverse=True)
            for entry in group[keep:]:
                deleted.append(self.delete_backup(entry.path))
        if not deleted:
            return "No había backups antiguos que eliminar."
        return f"Eliminados {len(deleted)} backup(s). " + " | ".join(deleted)

    def docker_prune(self) -> str:
        """Libera imágenes/contenedores no usados (docker system prune -a -f)."""
        result = self.ssh.run(
            "docker system prune -a -f 2>&1",
            use_sudo=True,
            timeout=300,
        )
        if not result.ok:
            msg = result.stderr or result.stdout or "docker system prune falló."
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        return result.stdout.strip() or "Docker prune completado."

    def ensure_space_for_ha_update(self) -> None:
        """Lanza error si no hay espacio suficiente para pull de HA."""
        status = self.get_status()
        if status.root_avail_bytes <= 0:
            return  # no se pudo medir; no bloquear
        if status.root_avail_bytes < _MIN_FREE_BYTES_HA:
            free = status.root_free or f"{status.root_avail_bytes} B"
            raise SSHCommandError(
                f"Espacio insuficiente en disco (libre: {free}, usado: {status.root_used_pct}). "
                f"Se recomiendan al menos ~2.5 GB libres antes de actualizar HA. "
                "Use Gestión de backups para eliminar backups viejos o liberar Docker.",
                exit_code=1,
                stderr="",
            )
