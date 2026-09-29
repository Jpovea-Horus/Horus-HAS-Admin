"""Excepciones de la API HAS v1."""


class HasApiError(Exception):
    """Error base de la API."""


class SSHConnectionError(HasApiError):
    """Fallo al conectar por SSH."""


class SSHCommandError(HasApiError):
    """Comando remoto falló."""

    def __init__(self, message: str, exit_code: int = -1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class NotConnectedError(HasApiError):
    """No hay sesión SSH activa."""


class ValidationError(HasApiError):
    """Parámetros inválidos."""
