<#
.SYNOPSIS
    Verifica e instala los requisitos previos del Gestor Nexxo 800.

.DESCRIPTION
    1. Conexión a Internet (solo si hay que descargar algo).
    2. Python 3.10+            (necesario para ejecutar desde el código fuente).
    3. Entorno virtual .venv + paquetes de requirements.txt (paramiko, rich).
    4. cloudflared             (necesario para la conexión remota por Cloudflare).

.PARAMETER SoloVerificar
    Muestra el estado sin instalar nada.

.PARAMETER SinPython
    Omite Python y paquetes (PC que solo usa el .exe portable).
#>
param(
    [switch]$SoloVerificar,
    [switch]$SinPython
)

$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Here = $PSScriptRoot
$Root = Split-Path -Parent $Here
$Requirements = Join-Path $Here 'requirements.txt'
$CheckScript = Join-Path $Here 'check_paquetes.py'
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

$MinPython = [version]'3.10'
$PythonWingetId = 'Python.Python.3.12'
$PythonInstallerUrl = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$CloudflaredWingetId = 'Cloudflare.cloudflared'
$CloudflaredUrl = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
$CloudflaredUserDir = Join-Path $env:LOCALAPPDATA 'Programs\cloudflared'

$Resumen = [ordered]@{}

function Write-Ok([string]$msg)    { Write-Host "  [OK]    $msg" -ForegroundColor Green }
function Write-Falta([string]$msg) { Write-Host "  [FALTA] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)   { Write-Host "  [ERROR] $msg" -ForegroundColor Red }
function Write-Info([string]$msg)  { Write-Host "  [INFO]  $msg" -ForegroundColor Cyan }
function Write-Paso([string]$msg)  { Write-Host ""; Write-Host " $msg" -ForegroundColor White }

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-Internet {
    try {
        Invoke-WebRequest -Uri 'https://pypi.org' -Method Head -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-Winget {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Install-WithWinget([string]$id, [string[]]$extra = @()) {
    if (-not (Test-Winget)) { return $false }
    Write-Info "Instalando $id con winget..."
    $wingetArgs = @('install', '--id', $id, '-e', '--silent',
                    '--accept-package-agreements', '--accept-source-agreements') + $extra
    & winget @wingetArgs | Out-Host
    Update-SessionPath
    return ($LASTEXITCODE -eq 0)
}

function Save-Download([string]$url, [string]$dest) {
    Write-Info "Descargando $url"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
}

# ----------------------------------------------------------------- Python

function Get-PythonVersion([string]$exe, [string[]]$prefix = @()) {
    try {
        $out = & $exe @prefix -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [version]($out | Select-Object -First 1).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $v = Get-PythonVersion 'py' @('-3')
        if ($v -and $v -ge $MinPython) { return @{ Exe = 'py'; Prefix = @('-3'); Version = $v } }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') {
        $v = Get-PythonVersion $cmd.Source
        if ($v -and $v -ge $MinPython) { return @{ Exe = $cmd.Source; Prefix = @(); Version = $v } }
    }
    $local = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending
    foreach ($p in $local) {
        $v = Get-PythonVersion $p.FullName
        if ($v -and $v -ge $MinPython) { return @{ Exe = $p.FullName; Prefix = @(); Version = $v } }
    }
    return $null
}

function Install-Python {
    if (Install-WithWinget $PythonWingetId @('--scope', 'user')) { return }
    $installer = Join-Path $env:TEMP 'python-installer.exe'
    Save-Download $PythonInstallerUrl $installer
    Write-Info 'Ejecutando instalador de Python (modo silencioso, solo para este usuario)...'
    $p = Start-Process -FilePath $installer -Wait -PassThru -ErrorAction Stop -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0')
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    Update-SessionPath
    if ($p.ExitCode -ne 0) { throw "El instalador de Python terminó con código $($p.ExitCode)." }
}

function Step-Python {
    Write-Paso '1. Python 3.10 o superior'
    $py = Find-Python
    if ($py) {
        Write-Ok "Python $($py.Version) encontrado ($($py.Exe))"
        $Resumen['Python'] = 'OK'
        return $py
    }
    Write-Falta 'Python 3.10+ no está instalado.'
    if ($SoloVerificar) { $Resumen['Python'] = 'FALTA'; return $null }
    Install-Python
    $py = Find-Python
    if (-not $py) {
        Write-Err 'Python se instaló pero no se detecta. Cierre esta ventana y vuelva a ejecutar.'
        $Resumen['Python'] = 'ERROR'
        return $null
    }
    Write-Ok "Python $($py.Version) instalado"
    $Resumen['Python'] = 'INSTALADO'
    return $py
}

# ---------------------------------------------------- Entorno virtual + pip

function Step-Paquetes($py) {
    Write-Paso '2. Entorno virtual (.venv) y paquetes'
    if (-not $py) {
        Write-Falta 'Se omite: primero hace falta Python.'
        $Resumen['Paquetes'] = 'PENDIENTE'
        return
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Falta 'No existe el entorno virtual .venv'
        if ($SoloVerificar) { $Resumen['Paquetes'] = 'FALTA'; return }
        Write-Info 'Creando .venv...'
        $prefix = $py.Prefix
        & $py.Exe @prefix -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { Write-Err 'No se pudo crear .venv'; $Resumen['Paquetes'] = 'ERROR'; return }
        Write-Ok '.venv creado'
    } else {
        Write-Ok '.venv existe'
    }

    $report = & $VenvPython $CheckScript $Requirements
    $ok = ($LASTEXITCODE -eq 0)
    foreach ($line in $report) {
        if ($line -like 'OK *') { Write-Ok $line.Substring(3) } else { Write-Falta $line.Substring(6) }
    }
    if ($ok) { $Resumen['Paquetes'] = 'OK'; return }
    if ($SoloVerificar) { $Resumen['Paquetes'] = 'FALTA'; return }

    Write-Info 'Instalando paquetes de requirements.txt...'
    & $VenvPython -m pip install --upgrade pip -q --disable-pip-version-check
    & $VenvPython -m pip install -r $Requirements -q --disable-pip-version-check
    & $VenvPython $CheckScript $Requirements | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'Paquetes instalados'
        $Resumen['Paquetes'] = 'INSTALADO'
    } else {
        Write-Err 'Falló la instalación de paquetes (revise el mensaje de pip).'
        $Resumen['Paquetes'] = 'ERROR'
    }
}

# ------------------------------------------------------------- cloudflared

function Find-Cloudflared {
    foreach ($p in @((Join-Path $Root 'cloudflared.exe'),
                     (Join-Path $Root 'dist\cloudflared.exe'),
                     (Join-Path $CloudflaredUserDir 'cloudflared.exe'))) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-Cloudflared {
    if ((Install-WithWinget $CloudflaredWingetId) -and (Find-Cloudflared)) { return }
    New-Item -ItemType Directory -Path $CloudflaredUserDir -Force | Out-Null
    Save-Download $CloudflaredUrl (Join-Path $CloudflaredUserDir 'cloudflared.exe')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $CloudflaredUserDir) {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$CloudflaredUserDir", 'User')
    }
    Update-SessionPath
}

function Step-Cloudflared {
    Write-Paso '3. cloudflared (conexión remota)'
    $exe = Find-Cloudflared
    if ($exe) {
        $ver = (& $exe --version 2>&1 | Select-Object -First 1)
        Write-Ok "$ver ($exe)"
        $Resumen['cloudflared'] = 'OK'
        return
    }
    Write-Falta 'cloudflared no está instalado (sin él no funciona la conexión remota).'
    if ($SoloVerificar) { $Resumen['cloudflared'] = 'FALTA'; return }
    try {
        Install-Cloudflared
        $exe = Find-Cloudflared
        if (-not $exe) { throw 'No se detecta tras la instalación.' }
        Write-Ok "cloudflared instalado ($exe)"
        $Resumen['cloudflared'] = 'INSTALADO'
    } catch {
        Write-Err "No se pudo instalar cloudflared: $($_.Exception.Message)"
        $Resumen['cloudflared'] = 'ERROR'
    }
}

# ------------------------------------------------------------------ Main

Write-Host ''
Write-Host '  Gestor Nexxo 800 - Requisitos previos' -ForegroundColor Cyan
Write-Host '  =====================================' -ForegroundColor Cyan
if ($SoloVerificar) { Write-Info 'Modo solo verificación: no se instalará nada.' }

if (-not $SoloVerificar) {
    Write-Paso '0. Conexión a Internet'
    if (Test-Internet) { Write-Ok 'Hay conexión' } else { Write-Falta 'Sin Internet: solo se podrá verificar.'; $SoloVerificar = $true }
}

try {
    if ($SinPython) {
        Write-Info 'Se omiten Python y paquetes (-SinPython).'
    } else {
        $py = Step-Python
        Step-Paquetes $py
    }
    Step-Cloudflared
} catch {
    Write-Err $_.Exception.Message
}

Write-Paso 'Resumen'
foreach ($k in $Resumen.Keys) {
    $estado = $Resumen[$k]
    $color = switch ($estado) { 'OK' { 'Green' } 'INSTALADO' { 'Green' } 'ERROR' { 'Red' } default { 'Yellow' } }
    Write-Host ("  {0,-12} {1}" -f $k, $estado) -ForegroundColor $color
}
Write-Host ''

$fallos = @($Resumen.Values | Where-Object { $_ -in 'FALTA', 'ERROR', 'PENDIENTE' })
if ($fallos.Count -gt 0) { exit 1 }
exit 0
