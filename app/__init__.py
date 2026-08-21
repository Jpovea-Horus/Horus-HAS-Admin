"""
Horus HAS Admin — administración de controladores HAS vía SSH.

Módulos: red (Ethernet/Wi-Fi), hostname, usuarios HA, ZeroTier.
"""

from controller import HasControllerAPI
from exceptions import (
    HasApiError,
    NotConnectedError,
    SSHCommandError,
    SSHConnectionError,
    ValidationError,
)

from paths import APP_NAME, APP_VERSION

__version__ = APP_VERSION
__app_name__ = APP_NAME
__all__ = [
    "HasControllerAPI",
    "HasApiError",
    "NotConnectedError",
    "SSHCommandError",
    "SSHConnectionError",
    "ValidationError",
    "__version__",
    "__app_name__",
]
