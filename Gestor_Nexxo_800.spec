# -*- mode: python ; coding: utf-8 -*-
# PyInstaller — Gestor Nexxo 800 (portable Windows)

import os
import re

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
APP_DIR = os.path.join(SPEC_DIR, "app")
ICON_PATH = os.path.join(SPEC_DIR, "assets", "icon.ico")

# Se lee con regex (no import) para no ejecutar paths.py, que crea carpetas en LOCALAPPDATA.
with open(os.path.join(APP_DIR, "paths.py"), encoding="utf-8") as fh:
    _paths_src = fh.read()
APP_NAME = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', _paths_src, re.M).group(1)
APP_VERSION = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', _paths_src, re.M).group(1)
_ver = tuple((int(p) for p in (APP_VERSION.split(".") + ["0"] * 4)[:4]))

VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_ver, prodvers=_ver, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([
            StringTable("0C0A04B0", [
                StringStruct("CompanyName", "Nexxo"),
                StringStruct("FileDescription", f"{APP_NAME} - Administración de controladores HAS"),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("InternalName", "Gestor_Nexxo_800"),
                StringStruct("LegalCopyright", "© Nexxo — Investigación y desarrollo"),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", APP_VERSION),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0C0A, 1200])]),
    ],
)
INTEGRATIONS_SRC = r"C:\DataJpovea\Documentos\Home Assistant\HAS - App\integrations"

block_cipher = None

_paramiko_datas, _paramiko_binaries, _paramiko_hidden = collect_all("paramiko")
_rich_datas, _rich_binaries, _rich_hidden = collect_all("rich")

extra_datas = [(ICON_PATH, "assets")]
cloudflared_src = os.path.join(SPEC_DIR, "cloudflared.exe")
plugin_v2_src = os.path.join(SPEC_DIR, "plugin_serviceV2")
if os.path.isfile(cloudflared_src):
    extra_datas.append((cloudflared_src, "."))
if os.path.isdir(plugin_v2_src):
    extra_datas.append((plugin_v2_src, "plugin_serviceV2"))
admin_network_src = os.path.join(INTEGRATIONS_SRC, "admin_network")
helper_cc = os.path.join(INTEGRATIONS_SRC, "helper_manager", "custom_components")
zwave_panel_src = os.path.join(INTEGRATIONS_SRC, "panel_zwave_js_ui")
if os.path.isdir(os.path.join(admin_network_src, "custom_components")):
    extra_datas.append((os.path.join(admin_network_src, "custom_components"), "integrations/admin_network/custom_components"))
if os.path.isdir(os.path.join(admin_network_src, "host")):
    extra_datas.append((os.path.join(admin_network_src, "host"), "integrations/admin_network/host"))
if os.path.isdir(helper_cc):
    extra_datas.append((helper_cc, "integrations/helper_manager/custom_components"))
blueprint_src = os.path.join(INTEGRATIONS_SRC, "blueprint_manager")
if os.path.isfile(os.path.join(blueprint_src, "manifest.json")):
    extra_datas.append((blueprint_src, "integrations/blueprint_manager"))
if os.path.isdir(zwave_panel_src):
    extra_datas.append((zwave_panel_src, "integrations/panel_zwave_js_ui"))
hacs_src = os.path.join(INTEGRATIONS_SRC, "hacs")
if os.path.isdir(hacs_src):
    extra_datas.append((hacs_src, "integrations/hacs"))
aws_cred_src = os.path.join(
    INTEGRATIONS_SRC,
    "plugin_service_energy",
    "plugin_service_aws_credentials - folder",
    "plugin_service_aws_credentials",
)
if os.path.isfile(aws_cred_src):
    extra_datas.append((aws_cred_src, "integrations/plugin_service_aws_credentials"))

a = Analysis(
    [os.path.join(APP_DIR, "main.py")],
    pathex=[APP_DIR],
    binaries=_paramiko_binaries + _rich_binaries,
    datas=_paramiko_datas + _rich_datas + extra_datas,
    hiddenimports=_paramiko_hidden + _rich_hidden + [
        "controller",
        "ssh_client",
        "network_manager",
        "hostname_manager",
        "zerotier_check",
        "exceptions",
        "models",
        "ui",
                "paths",
                "session_store",
                "cloudflare_manager",
                "cellular_manager",
                "mqtt_manager",
                "ha_users_manager",
                "ha_config_manager",
                "ha_theme_manager",
                "plugin_service_manager",
                "ha_integration_manager",
                "hacs_manager",
                "admin_network_host_manager",
                "zwave_panel_manager",
                "backup_manager",
                "remote_backup_manager",
                "ha_compat",
                "ha_profiles",
                "ha_update_manager",
                "support_manager",
                "clock_manager",
                "debian_repair_manager",
                "self_heal_manager",
                "local_config",
                "updater",
                "menus",
                "menus.connect",
                "menus.network",
                "menus.ha",
                "menus.backup",
                "menus.horus_backup",
                "menus.ha_update",
                "menus.remote",
                "menus.diagnostics",
            ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Gestor Nexxo 800",
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
    version=VERSION_INFO,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX dispara falsos positivos de antivirus y rompe la firma digital.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
