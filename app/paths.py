import os
import sys

APP_NAME = "Gestor Nexxo 800"
APP_VERSION = "1.3.0"

def get_base_path():
    """Retorna la ruta base de la aplicación (desempaquetada si es frozen)."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_executable_dir():
    """Retorna el directorio donde reside el archivo .exe o el script principal."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Rutas Locales
BASE_PATH = get_base_path()
EXE_DIR = get_executable_dir()

# Carpeta de plugins (buscada primero en EXE_DIR para portabilidad externa, luego en BASE_PATH)
def get_local_plugin_source():
    # Opción 1: Carpeta al lado del .exe (Portabilidad manual)
    external_path = os.path.join(EXE_DIR, "plugin_serviceV2")
    if os.path.isdir(external_path):
        return external_path
    
    # Opción 2: Carpeta dentro del .exe (Bundled)
    bundled_path = os.path.join(BASE_PATH, "plugin_serviceV2")
    if os.path.isdir(bundled_path):
        return bundled_path
    
    # Fallback al directorio actual
    return os.path.join(os.getcwd(), "plugin_serviceV2")

DEFAULT_LOCAL_SOURCE = get_local_plugin_source()

INTEGRATION_ADMIN_NETWORK = "admin_network"
INTEGRATION_HELPER = "helper_manager"

_DEV_INTEGRATIONS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(EXE_DIR))),
    "Home Assistant",
    "HAS - App",
    "integrations",
)
_KNOWN_INTEGRATIONS_ROOT = r"C:\DataJpovea\Documentos\Home Assistant\HAS - App\integrations"


def _integration_looks_valid(path: str, domain: str) -> bool:
    """True if path is the integration root or the custom component folder."""
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "manifest.json")):
        return True
    if os.path.isfile(os.path.join(path, "custom_components", domain, "manifest.json")):
        return True
    if domain == INTEGRATION_ADMIN_NETWORK and os.path.isfile(os.path.join(path, "host", "install.sh")):
        return True
    return False


def _integration_candidate_paths(domain: str) -> list[str]:
    env_root = (os.environ.get("HAS_INTEGRATIONS_DIR") or "").strip()
    paths: list[str] = []
    if env_root:
        paths.append(os.path.join(env_root, domain))
    paths.extend(
        [
            os.path.join(EXE_DIR, "integrations", domain),
            os.path.join(BASE_PATH, "integrations", domain),
            os.path.join(os.getcwd(), "integrations", domain),
            os.path.join(_KNOWN_INTEGRATIONS_ROOT, domain),
            os.path.join(_DEV_INTEGRATIONS_ROOT, domain),
        ]
    )
    # Unique while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        key = os.path.normcase(os.path.normpath(p))
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def find_integration_dir(domain: str) -> str:
    """Ruta local por defecto de una integración (admin_network / helper_manager)."""
    candidates = _integration_candidate_paths(domain)
    for path in candidates:
        if _integration_looks_valid(path, domain):
            return path
    return candidates[0] if candidates else os.path.join(os.getcwd(), "integrations", domain)


def get_local_admin_network_source() -> str:
    return find_integration_dir(INTEGRATION_ADMIN_NETWORK)


def get_local_helper_manager_source() -> str:
    return find_integration_dir(INTEGRATION_HELPER)

def get_cloudflared_exe():
    # Opción 1: Al lado del .exe
    external_exe = os.path.join(EXE_DIR, "cloudflared.exe")
    if os.path.isfile(external_exe):
        return external_exe
    
    # Opción 2: Dentro del .exe
    bundled_exe = os.path.join(BASE_PATH, "cloudflared.exe")
    if os.path.isfile(bundled_exe):
        return bundled_exe
    
    return "cloudflared" # Rely on PATH

# Rutas Remotas (Base configurable)
REMOTE_BASE_PATH = "/home/cat"

def get_remote_path(subpath):
    # Asegurar que subpath no empiece con / si REMOTE_BASE_PATH ya lo tiene
    clean_subpath = subpath.lstrip("/")
    return f"{REMOTE_BASE_PATH}/{clean_subpath}"

REMOTE_CONFIG_DIR = get_remote_path("config")
REMOTE_CUSTOM_COMPONENTS = f"{REMOTE_CONFIG_DIR}/custom_components"
REMOTE_CONFIGURATION_YAML = f"{REMOTE_CONFIG_DIR}/configuration.yaml"
REMOTE_ZWAVE_STORE = get_remote_path("zwave-js-ui-store")
REMOTE_COMPOSE_DIR = REMOTE_BASE_PATH
REMOTE_COMPOSE_FILE = f"{REMOTE_COMPOSE_DIR}/docker-compose.yml"
