# Carpeta de Integraciones (Ayudantes)

Coloque aquí las carpetas de las integraciones que desea administrar. El programa buscará automáticamente en este directorio.

## Estructura requerida:

### 1. Helper Manager (Auxiliares)
`integrations/helper_manager/`
- `manifest.json` (Obligatorio)
- `__init__.py` (Obligatorio)

### 2. Admin Network (Red)
`integrations/admin_network/`
- `manifest.json` (Obligatorio)
- `__init__.py` (Obligatorio)
- `host/install.sh` (Obligatorio para el servicio del sistema)

### 3. Z-Wave JS UI (panel lateral)
`integrations/panel_zwave_js_ui/`
- `zwave-panel.js` (Obligatorio)

---
*Nota: Se ignoran automáticamente carpetas como `__pycache__`, `.git`, `venv` y `node_modules` durante la subida al controlador.*
