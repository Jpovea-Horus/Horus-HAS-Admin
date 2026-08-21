# Gestor Nexxo 800

Herramienta portable para **Windows** que administra controladores HAS por SSH: red (Ethernet/Wi-Fi), hostname, Home Assistant, ZeroTier, MQTT y mantenimiento.

**Versión:** 1.2.0

## Estructura del proyecto

```
api_conet_HASv1/
├── app/
│   ├── main.py             # Arranque y menú principal
│   ├── menus/              # Submenús (red, HA, remoto, diagnóstico)
│   ├── controller.py       # API unificada
│   ├── ui.py               # Interfaz Rich
│   ├── ssh_client.py
│   ├── session_store.py    # Hosts recientes (sin contraseñas)
│   └── … managers
├── assets/icon.ico
├── scripts/
│   ├── run.bat
│   └── build_exe.bat
├── Horus_HAS_Admin.spec
├── requirements.txt
└── dist/                   # (tras compilar) Gestor Nexxo 800.exe
```

## Uso rápido

### Desarrollo (requiere Python 3.10+)

Doble clic en **`scripts/run.bat`** o:

```powershell
cd api_conet_HASv1
py app\main.py
```

### Portable (cualquier PC Windows)

1. Doble clic en **`scripts/build_exe.bat`**
2. Copiar **`dist/Gestor Nexxo 800.exe`** a USB o escritorio
3. Doble clic en el `.exe` (no requiere Python)

Junto al `.exe` se crean (si aplica):

- `session_hosts.json` — últimos hosts (usuario + LAN/CF, **sin contraseña**)
- `ssh_known_hosts` — huellas SSH (TOFU: primera conexión se guarda; si cambia, se bloquea)

## Conexión SSH

| Tipo | Entrada | Equivalente |
|------|---------|-------------|
| **1 · Local** | IP (ej. `10.0.5.111`) | `ssh root@10.0.5.111` |
| **2 · Remota** | ID o hostname Cloudflare | `ssh -o "ProxyCommand=cloudflared access ssh --hostname %h" root@…` |
| **H** | Host reciente | Reutiliza IP/túnel y usuario |
| **C** | Reconectar | Misma sesión en memoria (incluye reconexión si se corta SSH) |

En el menú principal, **C** cambia de controlador o reconecta (útil tras IP estática).

**Requisito remoto:** [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) en el PATH, o `cloudflared.exe` al lado del ejecutable.

## Menú principal

1. Estado de red  
2. Ethernet (DHCP / estática)  
3. Wi-Fi  
4. Administrativa → hostname + Home Assistant (usuarios, backups, mantenimiento, `plugin_service`, `configuration.yaml`)  
5. Conexión remota (ZeroTier, Cloudflare en el controlador)  
6. Diagnóstico (htop + disco/memoria/`systemctl --failed`)  
7. Corrección (cellular up/down, MQTT Z-Wave JS UI)

`plugin_service`: verificar / eliminar / instalar local o GitHub / **reiniciar HA**.

## Instalación de dependencias

```powershell
pip install -r requirements.txt
```

*Nexxo — Investigación y desarrollo*
