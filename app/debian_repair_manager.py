"""Reparación APT + python3-venv en BND Debian 11 (bullseye)."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from exceptions import SSHCommandError
from models import DebianRepairStatus

if TYPE_CHECKING:
    from ssh_client import SSHClient

SOURCES_LIST = "/etc/apt/sources.list"
SOURCES_BACKUP = "/etc/apt/sources.list.bak-horus"
APT_RELAX = (
    "-o Acquire::Check-Valid-Until=false -o Acquire::Check-Date=false"
)
ARCHIVE_SOURCES = (
    "deb http://archive.debian.org/debian bullseye main contrib non-free\n"
    "deb http://archive.debian.org/debian bullseye-updates main contrib non-free\n"
)
SNAPSHOT_URL = (
    "https://snapshot.debian.org/archive/debian-security/"
    "20260831T000000Z/pool/updates/main/p/python3.9"
)
SNAPSHOT_FALLBACK = (
    "https://snapshot.debian.org/file/60fa79334920faac51a1c2caa27deaf5a50e06a5"
)

STEP_LABELS = {
    0: "Comprobar requisitos (venv + OS)",
    1: "Backup de sources.list",
    2: "Configurar archive.debian.org (sin security)",
    3: "apt-get update (ignorar fechas)",
    4: "Instalar pip stack desde MAIN",
    5: "Instalar python3.9-venv desde snapshot",
    6: "Verificar venv final",
}


class DebianRepairManager:
    """Diagnóstico y pasos de reparación APT/venv para bullseye EOL."""

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def get_status(self) -> DebianRepairStatus:
        status = DebianRepairStatus()
        try:
            os_res = self.ssh.run(
                ". /etc/os-release; "
                'echo "ID=$ID"; echo "VERSION_ID=$VERSION_ID"; '
                'echo "VERSION_CODENAME=$VERSION_CODENAME"'
            )
            for line in os_res.stdout.splitlines():
                if line.startswith("ID="):
                    status.os_id = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    status.version_id = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_CODENAME="):
                    status.version_codename = line.split("=", 1)[1].strip().strip('"')

            status.is_debian11 = (
                status.os_id == "debian"
                and (
                    status.version_id == "11"
                    or status.version_codename == "bullseye"
                )
            )

            arch = self.ssh.run("dpkg --print-architecture")
            status.arch = arch.stdout.strip()

            py = self.ssh.run("python3 -V 2>&1")
            status.python_version = py.stdout.strip() or py.stderr.strip()

            py39 = self.ssh.run(
                "dpkg -s python3.9 2>/dev/null | awk -F': ' '/^Version:/{print $2; exit}'"
            )
            status.python39_version = py39.stdout.strip()

            venv = self.ssh.run(
                "rm -rf /tmp/horus-venv-check; "
                "python3 -m venv /tmp/horus-venv-check "
                "&& echo VENV_OK || echo VENV_FAIL; "
                "rm -rf /tmp/horus-venv-check"
            )
            status.venv_works = "VENV_OK" in venv.stdout

            src = self.ssh.run(f"cat {shlex.quote(SOURCES_LIST)} 2>/dev/null || true")
            sources = src.stdout or ""
            status.sources_preview = "\n".join(sources.splitlines()[:12])
            status.has_archive_mirror = "archive.debian.org" in sources
            status.has_security_mirror = (
                "debian-security" in sources or "bullseye-security" in sources
            )

            if not status.is_debian11:
                status.detail = (
                    "Este host no es Debian 11 / bullseye; "
                    "la guía de reparación no aplica tal cual."
                )
                status.needs_repair = False
            elif status.venv_works:
                status.detail = (
                    "python3 -m venv funciona. "
                    "Si Instalar Admin Network falla, el problema es otro."
                )
                status.needs_repair = False
            else:
                status.needs_repair = True
                status.detail = (
                    "venv FALLA: ejecute los pasos 1→6 en orden "
                    "(o use Ejecutar reparación completa)."
                )
        except Exception as exc:
            status.error = str(exc)
        return status

    def run_step(self, step: int) -> str:
        if step not in STEP_LABELS:
            raise SSHCommandError(f"Paso no válido: {step}")
        runners = {
            0: self._step_0,
            1: self._step_1,
            2: self._step_2,
            3: self._step_3,
            4: self._step_4,
            5: self._step_5,
            6: self._step_6,
        }
        return runners[step]()

    def run_full_repair(self) -> str:
        """Ejecuta pasos 1→6; aborta si el paso 0 indica VENV_OK o no es bullseye."""
        status = self.get_status()
        logs: list[str] = [f"=== Diagnóstico ===\n{self._format_status_brief(status)}"]

        if not status.is_debian11:
            raise SSHCommandError(
                "Reparación abortada: no es Debian 11 / bullseye.\n" + logs[0]
            )
        if status.venv_works:
            return (
                "Reparación no necesaria: VENV_OK.\n" + logs[0]
            )

        for step in range(1, 7):
            label = STEP_LABELS[step]
            logs.append(f"\n=== Paso {step}: {label} ===")
            try:
                out = self.run_step(step)
                logs.append(out or "(sin salida)")
            except SSHCommandError as exc:
                logs.append(str(exc))
                raise SSHCommandError(
                    f"Falló el paso {step} ({label}).\n" + "\n".join(logs)
                ) from exc

        verify = self.get_status()
        logs.append(f"\n=== Resultado final ===\n{self._format_status_brief(verify)}")
        if not verify.venv_works:
            raise SSHCommandError(
                "Reparación terminó pero venv sigue fallando.\n" + "\n".join(logs)
            )
        return "\n".join(logs)

    def _format_status_brief(self, status: DebianRepairStatus) -> str:
        return (
            f"OS={status.os_id} {status.version_id} ({status.version_codename}) "
            f"arch={status.arch}\n"
            f"python={status.python_version} python3.9={status.python39_version or '-'}\n"
            f"venv={'OK' if status.venv_works else 'FAIL'} "
            f"archive={status.has_archive_mirror} security={status.has_security_mirror}\n"
            f"{status.detail}"
        )

    def _combine(self, result) -> str:
        parts = []
        if result.stdout:
            parts.append(result.stdout.strip())
        if result.stderr:
            parts.append(result.stderr.strip())
        return "\n".join(parts) if parts else "(sin salida)"

    def _step_0(self) -> str:
        status = self.get_status()
        body = self._format_status_brief(status)
        if status.sources_preview:
            body += f"\n\n--- sources.list ---\n{status.sources_preview}"
        if status.error:
            raise SSHCommandError(f"{body}\nError: {status.error}")
        return body

    def _step_1(self) -> str:
        cmd = (
            f"cp -n {shlex.quote(SOURCES_LIST)} {shlex.quote(SOURCES_BACKUP)} && "
            f"echo BACKUP_OK && cat {shlex.quote(SOURCES_LIST)}"
        )
        result = self.ssh.run(cmd, use_sudo=True)
        if not result.ok:
            raise SSHCommandError(
                self._combine(result) or "No se pudo hacer backup de sources.list.",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return self._combine(result)

    def _step_2(self) -> str:
        payload = (
            f"printf %s {shlex.quote(ARCHIVE_SOURCES)} > {shlex.quote(SOURCES_LIST)}"
        )
        result = self.ssh.run(f"bash -c {shlex.quote(payload)}", use_sudo=True)
        if not result.ok:
            raise SSHCommandError(
                self._combine(result) or "No se pudo escribir sources.list.",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        check = self.ssh.run(f"cat {shlex.quote(SOURCES_LIST)}", use_sudo=True)
        return (
            "sources.list → solo archive.debian.org (sin security).\n"
            + (check.stdout or "")
        )

    def _step_3(self) -> str:
        cmd = f"apt-get {APT_RELAX} update"
        result = self.ssh.run(cmd, use_sudo=True, timeout=300)
        out = self._combine(result)
        if not result.ok:
            raise SSHCommandError(
                f"apt-get update falló. Revise red/DNS; no instale a ciegas.\n{out}",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return out

    def _step_4(self) -> str:
        cmd = (
            f"DEBIAN_FRONTEND=noninteractive apt-get {APT_RELAX} install -y "
            "python3-pip python3-setuptools python3-wheel python-pip-whl"
        )
        result = self.ssh.run(cmd, use_sudo=True, timeout=600)
        out = self._combine(result)
        if not result.ok:
            raise SSHCommandError(
                f"Fallo al instalar pip stack.\n{out}",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        ver_pip = self.ssh.run(
            "dpkg -s python3-pip 2>/dev/null | grep -i ^Version || true"
        )
        ver_py = self.ssh.run(
            "dpkg -s python3.9 2>/dev/null | grep -i ^Version || true"
        )
        return (
            f"{out}\n\n{ver_pip.stdout.strip()}\n{ver_py.stdout.strip()}\n"
            "Nota: no use apt para python3-venv / python3.9-venv en este BND."
        )

    def _step_5(self) -> str:
        script = f"""
set -e
ARCH=$(dpkg --print-architecture)
VER=$(dpkg -s python3.9 | awk -F': ' '/^Version:/{{print $2; exit}}')
echo "ARCH=$ARCH VER=$VER"
if [ -z "$VER" ]; then
  echo "No se pudo leer versión de python3.9" >&2
  exit 1
fi
DEB="python3.9-venv_${{VER}}_${{ARCH}}.deb"
URL="{SNAPSHOT_URL}/${{DEB}}"
cd /tmp
rm -f "/tmp/$DEB"
if ! curl -fL -o "$DEB" "$URL"; then
  echo "URL principal falló; intentando fallback..."
  curl -fL -o "$DEB" "{SNAPSHOT_FALLBACK}/${{DEB}}"
fi
dpkg -i "/tmp/$DEB"
echo DEB_INSTALL_OK
"""
        result = self.ssh.run(
            f"bash -c {shlex.quote(script)}",
            use_sudo=True,
            timeout=300,
        )
        out = self._combine(result)
        if not result.ok:
            raise SSHCommandError(
                "No se pudo instalar python3.9-venv desde snapshot. "
                "Busque el .deb exacto en https://snapshot.debian.org/package/python3.9/\n"
                f"{out}",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return out

    def _step_6(self) -> str:
        result = self.ssh.run(
            "rm -rf /tmp/horus-venv-check; "
            "python3 -m venv /tmp/horus-venv-check "
            "&& echo VENV_OK || echo VENV_FAIL; "
            "rm -rf /tmp/horus-venv-check"
        )
        out = self._combine(result)
        if "VENV_OK" not in out:
            raise SSHCommandError(
                f"Verificación falló: venv sigue sin funcionar.\n{out}",
                exit_code=result.exit_code,
                stderr=result.stderr,
            )
        return (
            f"{out}\n\nCon VENV_OK, vuelva a Admin Network → "
            "Instalar solo servicio host (o Instalar TODO)."
        )
