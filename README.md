# Gestor Nexxo 800

Herramienta portable para **Windows** que administra controladores HAS por SSH: red (Ethernet/Wi-Fi), hostname, Home Assistant, ZeroTier, MQTT y mantenimiento.

**Versión:** 2.0.0 (api_connect_HAS_V2)

## Estructura del proyecto

```
api_connect_HAS_V2/
├── app/
│   ├── main.py             # Arranque y menú principal
│   ├── menus/              # Submenús (red, HA, remoto, diagnóstico)
│   ├── controller.py       # API unificada
│   ├── ui.py               # Interfaz Rich
│   ├── ssh_client.py
│   ├── session_store.py    # Hosts recientes (sin contraseñas)
│   └── … managers
├── assets/icon.ico
├── requisitos_previos/
│   ├── instalar_requisitos.bat   # Doble clic: verifica e instala lo que falte
│   ├── verificar_requisitos.ps1
│   ├── check_paquetes.py
│   └── requirements.txt
├── scripts/
│   ├── run.bat
│   └── build_exe.bat
├── Gestor_Nexxo_800.bat    # Lanzador (doble clic)
├── Gestor_Nexxo_800.spec
└── dist/                   # (tras compilar) Gestor Nexxo 800.exe
```

## Uso rápido

### Desarrollo (requiere Python 3.10+)

Doble clic en **`scripts/run.bat`** o:

```powershell
cd api_connect_HAS_V2
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
4. Administrativa → hostname + Home Assistant (usuarios, backups, mantenimiento, `plugin_service`, `configuration.yaml`, Admin Network, Helper Manager)  
5. Conexión remota (ZeroTier, Cloudflare en el controlador)  
6. Diagnóstico (htop + disco/memoria/`systemctl --failed`)  
7. Corrección (cellular up/down, MQTT Z-Wave JS UI)

`plugin_service`: verificar / eliminar / instalar local o GitHub / **reiniciar HA**.

## Actualizar Home Assistant + Z-Wave JS UI (v2)

Menú **5 → 4**. Instala un par de versiones compatible en lugar de `:stable` / `@latest`:

| Perfil | HA | Z-Wave JS UI | Estado |
|--------|----|--------------|--------|
| Recomendado | `2026.9.3` | `11.24.1` | Validado (schema 50 ↔ 49–50, Node ≥ 24.20) |
| Última | la publicada | la publicada | **No validado**, solo equipos de prueba |

- **Verificación previa**: tag existente, imagen para la arquitectura del equipo, espacio libre + imagen vieja ≥ 2 GB, Node que pide `engines.node`, ExecStart de `zwave-ui.service` global y compatibilidad de schema HA ↔ Z-Wave JS Server (GitHub + npm).
- **Ejecución**: limpieza (APT, NPM, journal, `/config/config`, `*.zip`), borrado de la HA vieja, fijado del tag en el compose (`.bak.horus.update`), `pull` + `up -d` y `npm install -g zwave-js-ui@<versión>`. Corre como trabajo remoto: sigue aunque se corte el SSH. Sin `cp -a` de config.
- **Cierre**: HA en `:8123`, `zwave-ui.service` activo, disco < 85 %, DB < 500 MB y errores `zwave_js` en el log de HA.
- **Perfiles propios**: opción **G** registra las versiones del controlador conectado como perfil validado en `%LOCALAPPDATA%\Gestor Nexxo 800\ha_profiles.json`.

## Requisitos previos

Doble clic en **`requisitos_previos/instalar_requisitos.bat`**. Revisa y, si falta, instala:

| Requisito | Para qué | Cómo se instala |
|-----------|----------|-----------------|
| Python 3.10+ | Ejecutar desde el código | winget (`Python.Python.3.12`) o instalador de python.org |
| `.venv` + paramiko, rich | Dependencias de la app | `requisitos_previos/requirements.txt` |
| cloudflared | Conexión remota (Cloudflare) | winget (`Cloudflare.cloudflared`) o descarga de GitHub |

Opciones: **2** solo verifica; **3** es para PCs que solo usan el `.exe` (instala únicamente cloudflared).
`scripts/run.bat` lo ejecuta solo la primera vez si no existe `.venv`.

*Nexxo — Investigación y desarrollo*
