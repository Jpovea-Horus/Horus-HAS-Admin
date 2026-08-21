"""Cliente SSH para controladores HAS."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import paramiko

from exceptions import NotConnectedError, SSHCommandError, SSHConnectionError
from models import CommandResult, SessionInfo, SystemHealthStatus

from paths import EXE_DIR, get_cloudflared_exe

KNOWN_HOSTS_PATH = os.path.join(EXE_DIR, "ssh_known_hosts")

DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 15
CLOUDFLARE_TIMEOUT = 45
CLOUDFLARE_DOMAIN = "rhorus.com"


class TofuHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Trust on first use: guarda la clave y rechaza cambios posteriores."""

    def __init__(self, path: str):
        self.path = path

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key) -> None:
        client.get_host_keys().add(hostname, key.get_name(), key)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(self.path)


def resolve_cloudflare_hostname(host_or_id: str, domain: str = CLOUDFLARE_DOMAIN) -> str:
    """Convierte ID corto (ssh-xx00) o hostname completo a FQDN Cloudflare."""
    value = host_or_id.strip().lower()
    if not value:
        raise SSHConnectionError("Hostname Cloudflare vacío.")
    if "." in value:
        return value
    return f"{value}.{domain}"


def cloudflared_path() -> Optional[str]:
    exe = get_cloudflared_exe()
    if exe != "cloudflared" and os.path.isfile(exe):
        return exe
    return shutil.which("cloudflared")


def cloudflared_available() -> bool:
    return cloudflared_path() is not None


class CloudflareAccessSock:
    """
    Socket-like para `cloudflared access ssh` (equivalente a ProxyCommand).

    Paramiko.ProxyCommand usa select() sobre pipes y falla en Windows
    (WinError 10038). Este wrapper lee stdout en un hilo y expone send/recv.
    """

    def __init__(self, hostname: str, timeout: float = CLOUDFLARE_TIMEOUT):
        exe = cloudflared_path()
        if not exe:
            raise SSHConnectionError(
                "cloudflared no está en el PATH. Instálelo y vuelva a intentar."
            )
        self.cmd = [exe, "access", "ssh", "--hostname", hostname]
        self.timeout: Optional[float] = float(timeout)
        kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self.process = subprocess.Popen(self.cmd, **kwargs)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._stdout_loop, daemon=True)
        self._err_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self._reader.start()
        self._err_reader.start()

    def _stdout_loop(self) -> None:
        assert self.process.stdout is not None
        while not self._closed:
            try:
                data = self.process.stdout.read(4096)
            except Exception:
                break
            if not data:
                break
            with self._lock:
                self._buf.extend(data)

    def _stderr_loop(self) -> None:
        assert self.process.stderr is not None
        try:
            for line in iter(self.process.stderr.readline, b""):
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_lines.append(text)
        except Exception:
            return

    def settimeout(self, timeout: Optional[float]) -> None:
        self.timeout = None if timeout is None else float(timeout)

    def setblocking(self, flag: int) -> None:
        if not flag:
            self.timeout = 0.0
        elif self.timeout == 0.0:
            self.timeout = float(CLOUDFLARE_TIMEOUT)

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 0)

    def send(self, data: bytes) -> int:
        if self._closed or self.process.poll() is not None:
            raise OSError(self._exit_message())
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()
        return len(data)

    def recv(self, size: int) -> bytes:
        if size <= 0:
            return b""
        deadline = (
            None if self.timeout is None else time.monotonic() + float(self.timeout)
        )
        while True:
            with self._lock:
                if self._buf:
                    out = bytes(self._buf[:size])
                    del self._buf[:size]
                    return out
            if self.process.poll() is not None:
                with self._lock:
                    if self._buf:
                        out = bytes(self._buf[:size])
                        del self._buf[:size]
                        return out
                raise OSError(self._exit_message())
            if deadline is not None and time.monotonic() >= deadline:
                raise socket.timeout("Timeout leyendo del túnel Cloudflare")
            time.sleep(0.01)

    def _exit_message(self) -> str:
        tail = " | ".join(self._stderr_lines[-6:]) if self._stderr_lines else ""
        code = self.process.poll()
        base = f"cloudflared access ssh terminó (código {code})"
        return f"{base}: {tail}" if tail else base

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


class SSHClient:
    """Conexión SSH reutilizable al controlador."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self._timeout = timeout
        self._client: Optional[paramiko.SSHClient] = None
        self._cf_sock: Optional[CloudflareAccessSock] = None
        self._user: str = ""
        self._host: str = ""
        self._is_root: bool = False
        self._use_cloudflare: bool = False

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    @property
    def user(self) -> str:
        return self._user

    @property
    def host(self) -> str:
        return self._host

    @property
    def use_cloudflare(self) -> bool:
        return self._use_cloudflare

    def connect(
        self,
        user: str,
        host: str,
        password: str,
        port: int = DEFAULT_PORT,
        use_cloudflare: bool = False,
    ) -> SessionInfo:
        """Conecta por SSH local (puerto 22) o remoto vía Cloudflare Access."""
        self.disconnect()
        self._user = user.strip()
        self._use_cloudflare = use_cloudflare
        self._host = (
            resolve_cloudflare_hostname(host) if use_cloudflare else host.strip()
        )

        client = paramiko.SSHClient()
        if os.path.isfile(KNOWN_HOSTS_PATH):
            client.load_host_keys(KNOWN_HOSTS_PATH)
        client.set_missing_host_key_policy(TofuHostKeyPolicy(KNOWN_HOSTS_PATH))

        sock = None
        connect_timeout = self._timeout
        if use_cloudflare:
            connect_timeout = max(self._timeout, CLOUDFLARE_TIMEOUT)
            try:
                # Equivalente a ProxyCommand, compatible con Windows.
                sock = CloudflareAccessSock(self._host, timeout=connect_timeout)
                self._cf_sock = sock
            except SSHConnectionError:
                raise
            except Exception as exc:
                raise SSHConnectionError(
                    f"No se pudo iniciar cloudflared access ssh: {exc}"
                ) from exc

        try:
            client.connect(
                hostname=self._host,
                port=port,
                username=self._user,
                password=password,
                timeout=connect_timeout,
                banner_timeout=connect_timeout,
                auth_timeout=connect_timeout,
                allow_agent=False,
                look_for_keys=False,
                sock=sock,
            )
        except (socket.timeout, TimeoutError) as exc:
            self._cleanup_cf_sock()
            raise SSHConnectionError(f"Timeout al conectar a {self._user}@{self._host}") from exc
        except paramiko.BadHostKeyException as exc:
            self._cleanup_cf_sock()
            raise SSHConnectionError(
                f"La clave SSH de {self._host} cambió (posible MITM). "
                f"Si el equipo se reinstalo, borre la entrada en {KNOWN_HOSTS_PATH}."
            ) from exc
        except paramiko.AuthenticationException as exc:
            self._cleanup_cf_sock()
            raise SSHConnectionError("Autenticación fallida: usuario o contraseña incorrectos.") from exc
        except paramiko.SSHException as exc:
            self._cleanup_cf_sock()
            raise SSHConnectionError(f"Error SSH: {exc}") from exc
        except OSError as exc:
            self._cleanup_cf_sock()
            raise SSHConnectionError(f"No se pudo alcanzar {self._host}: {exc}") from exc

        self._client = client
        whoami = self.run("whoami").stdout.strip()
        self._is_root = whoami == "root"
        has_sudo = self._is_root or self._check_sudo(whoami)

        return SessionInfo(
            user=self._user,
            host=self._host,
            remote_user=whoami,
            has_sudo=has_sudo,
        )

    def _cleanup_cf_sock(self) -> None:
        if self._cf_sock is not None:
            try:
                self._cf_sock.close()
            except Exception:
                pass
            self._cf_sock = None

    def _check_sudo(self, remote_user: str) -> bool:
        if remote_user == "root":
            return True
        result = self.run("sudo -n true 2>/dev/null; echo $?")
        return result.stdout.strip().endswith("0")

    def run(
        self, command: str, use_sudo: bool = False, timeout: Optional[int] = None
    ) -> CommandResult:
        """Ejecuta un comando remoto."""
        if not self.is_connected:
            raise NotConnectedError("No hay sesión SSH. Llame a connect() primero.")

        if use_sudo and self._needs_sudo(command) and not self._is_root:
            cmd = f"sudo {command}"
        else:
            cmd = command
        assert self._client is not None
        cmd_timeout = self._timeout if timeout is None else timeout
        _, stdout, stderr = self._client.exec_command(cmd, timeout=cmd_timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()

        return CommandResult(stdout=out, stderr=err, exit_code=exit_code)

    def run_or_raise(
        self, command: str, use_sudo: bool = False, timeout: Optional[int] = None
    ) -> str:
        result = self.run(command, use_sudo=use_sudo, timeout=timeout)
        if not result.ok:
            msg = result.stderr or result.stdout or "Comando falló"
            raise SSHCommandError(msg, exit_code=result.exit_code, stderr=result.stderr)
        return result.stdout

    def get_motd(self) -> str:
        """Obtiene el banner/MOTD del sistema."""
        # Intentamos leer el MOTD dinámico primero, luego el estático
        cmd = "cat /run/motd.dynamic 2>/dev/null || cat /etc/motd 2>/dev/null || echo 'No hay información del sistema disponible.'"
        return self.run(cmd).stdout.strip()

    def refresh_motd(self) -> str:
        """Regenera el MOTD dinámico."""
        self.run("run-parts /etc/update-motd.d/", use_sudo=True)
        return self.get_motd()

    def get_system_health(self) -> SystemHealthStatus:
        disk = self.run("df -h / /home 2>/dev/null || df -h /").stdout
        memory = self.run("free -h 2>/dev/null").stdout
        uptime = self.run("uptime").stdout
        failed = self.run(
            "systemctl --failed --no-legend --plain --no-pager 2>/dev/null"
        )
        failed_txt = failed.stdout.strip()
        has_failed = bool(failed_txt) and "0 loaded units listed" not in failed_txt.lower()
        if failed_txt.lower().startswith("0 loaded"):
            has_failed = False
            failed_txt = ""
        return SystemHealthStatus(
            disk=disk,
            memory=memory,
            uptime=uptime,
            failed_units=failed_txt,
            has_failed=has_failed,
        )

    def get_top_snapshot(self, lines: int = 25) -> str:
        """Snapshot de procesos (fallback si no hay TTY para htop)."""
        cmd = (
            f"(command -v htop >/dev/null && htop -v 2>/dev/null | head -1; echo '---'; "
            f"top -b -n1 -o %CPU 2>/dev/null | head -n {lines + 8}) || "
            f"ps aux --sort=-%cpu | head -n {lines}"
        )
        return self.run(cmd).stdout

    def run_interactive_tty(self, command: str) -> None:
        """Ejecuta un programa TUI (htop/top) con pseudo-TTY."""
        if not self.is_connected:
            raise NotConnectedError("No hay sesión SSH. Llame a connect() primero.")
        assert self._client is not None
        transport = self._client.get_transport()
        if transport is None:
            raise SSHConnectionError("Transporte SSH no disponible.")

        channel = transport.open_session()
        channel.get_pty(term="vt100", width=120, height=36)
        channel.exec_command(command)
        stop = threading.Event()

        def _forward_stdin() -> None:
            if sys.platform == "win32":
                import msvcrt

                while not stop.is_set() and not channel.closed:
                    if msvcrt.kbhit():
                        data = msvcrt.getch()
                        if data in (b"\x03",):
                            channel.close()
                            break
                        channel.send(data)
                    time.sleep(0.03)
            else:
                import select
                import tty
                from termios import tcgetattr, tcsetattr, TCSADRAIN

                fd = sys.stdin.fileno()
                old = tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    while not stop.is_set() and not channel.closed:
                        readable, _, _ = select.select([sys.stdin], [], [], 0.1)
                        if readable:
                            ch = sys.stdin.read(1)
                            if ch == "\x03":
                                channel.close()
                                break
                            channel.send(ch.encode("utf-8", errors="replace"))
                finally:
                    tcsetattr(fd, TCSADRAIN, old)

        reader = threading.Thread(target=_forward_stdin, daemon=True)
        reader.start()
        try:
            while not channel.closed:
                if channel.recv_ready():
                    data = channel.recv(4096)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                if channel.recv_stderr_ready():
                    err = channel.recv_stderr(4096)
                    if err:
                        sys.stdout.buffer.write(err)
                        sys.stdout.buffer.flush()
                if channel.exit_status_ready():
                    break
                time.sleep(0.02)
        except KeyboardInterrupt:
            channel.close()
        finally:
            stop.set()
            reader.join(timeout=1)

    @staticmethod
    def _needs_sudo(command: str) -> bool:
        stripped = command.strip()
        return not stripped.startswith("sudo ")

    def open_sftp(self) -> paramiko.SFTPClient:
        """Abre canal SFTP sobre la sesión SSH actual (LAN o Cloudflare)."""
        if not self.is_connected or self._client is None:
            raise NotConnectedError("No hay sesión SSH. Llame a connect() primero.")
        try:
            return self._client.open_sftp()
        except Exception as exc:
            raise SSHConnectionError(f"No se pudo abrir SFTP: {exc}") from exc

    def upload_directory(
        self,
        local_dir: str,
        remote_dir: str,
        skip_dirs: Optional[set[str]] = None,
    ) -> int:
        """
        Sube un árbol de directorios por SFTP.
        Destino remoto se crea si no existe. Devuelve cantidad de archivos subidos.
        """
        import os

        local = Path(local_dir)
        if not local.is_dir():
            raise SSHCommandError(f"Carpeta local no encontrada: {local_dir}")

        ignore = skip_dirs or {"__pycache__", ".git", ".idea", ".vscode", "__MACOSX"}
        uploaded = 0
        sftp = self.open_sftp()
        try:
            self._sftp_makedirs(sftp, remote_dir)
            for root, dirs, files in os.walk(local):
                dirs[:] = [d for d in dirs if d not in ignore]
                rel = os.path.relpath(root, local)
                if rel == ".":
                    remote_root = remote_dir.rstrip("/")
                else:
                    remote_root = f"{remote_dir.rstrip('/')}/{rel.replace(os.sep, '/')}"
                self._sftp_makedirs(sftp, remote_root)
                for name in files:
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    local_path = os.path.join(root, name)
                    remote_path = f"{remote_root}/{name}"
                    sftp.put(local_path, remote_path)
                    uploaded += 1
        finally:
            sftp.close()
        return uploaded

    @staticmethod
    def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_path: str) -> None:
        """Crea directorios remotos recursivamente (como mkdir -p)."""
        parts = [p for p in remote_path.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                sftp.stat(current)
            except OSError:
                sftp.mkdir(current)

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
        self._client = None
        self._cleanup_cf_sock()
        self._user = ""
        self._host = ""
        self._is_root = False
        self._use_cloudflare = False
