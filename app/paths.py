import os
import sys

APP_NAME = "Gestor Nexxo 800"
APP_VERSION = "1.2.0"

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
