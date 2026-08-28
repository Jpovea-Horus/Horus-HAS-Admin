# -*- mode: python ; coding: utf-8 -*-
# PyInstaller — Gestor Nexxo 800 (portable Windows)

import os

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
APP_DIR = os.path.join(SPEC_DIR, "app")
ICON_PATH = os.path.join(SPEC_DIR, "assets", "icon.ico")
INTEGRATIONS_SRC = r"C:\DataJpovea\Documentos\Home Assistant\HAS - App\integrations"

block_cipher = None

_paramiko_datas, _paramiko_binaries, _paramiko_hidden = collect_all("paramiko")
_rich_datas, _rich_binaries, _rich_hidden = collect_all("rich")

extra_datas = [
    (ICON_PATH, "assets"),
    (os.path.join(SPEC_DIR, "cloudflared.exe"), "."),
    (os.path.join(SPEC_DIR, "plugin_serviceV2"), "plugin_serviceV2"),
]
admin_network_src = os.path.join(INTEGRATIONS_SRC, "admin_network")
helper_cc = os.path.join(INTEGRATIONS_SRC, "helper_manager", "custom_components")
zwave_panel_src = os.path.join(INTEGRATIONS_SRC, "panel_zwave_js_ui")
if os.path.isdir(os.path.join(admin_network_src, "custom_components")):
    extra_datas.append((os.path.join(admin_network_src, "custom_components"), "integrations/admin_network/custom_components"))
if os.path.isdir(os.path.join(admin_network_src, "host")):
    extra_datas.append((os.path.join(admin_network_src, "host"), "integrations/admin_network/host"))
if os.path.isdir(helper_cc):
    extra_datas.append((helper_cc, "integrations/helper_manager/custom_components"))
if os.path.isdir(zwave_panel_src):
    extra_datas.append((zwave_panel_src, "integrations/panel_zwave_js_ui"))

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
                "plugin_service_manager",
                "ha_integration_manager",
                "admin_network_host_manager",
                "zwave_panel_manager",
                "backup_manager",
                "menus",
                "menus.connect",
                "menus.network",
                "menus.ha",
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
