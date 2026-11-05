# Firma digital Authenticode (SHA-256 + sello de tiempo) para Gestor Nexxo 800.
#
#   sign_exe.ps1 -Deps   Firma los .pyd/.dll sin firma de .venv (cryptography, bcrypt, nacl, cffi).
#                        Va ANTES de PyInstaller: el .exe los extrae y carga, y Smart App Control
#                        bloquea las DLL sin firma aunque el .exe este firmado.
#   sign_exe.ps1         Firma dist\Gestor Nexxo 800.exe (despues de PyInstaller).
#
# Configure UNA de estas opciones con variables de entorno:
#
#   A) Certificado OV/EV instalado en Windows (token USB o almacen "Personal"):
#        NEXXO_SIGN_THUMBPRINT = huella SHA-1 del certificado
#
#   B) Azure Trusted Signing:
#        NEXXO_SIGN_DLIB     = ruta a Azure.CodeSigning.Dlib.dll
#        NEXXO_SIGN_METADATA = ruta al metadata.json (Endpoint, CodeSigningAccountName, CertificateProfileName)
#
# Opcional: NEXXO_SIGN_TIMESTAMP = URL del servidor de sello de tiempo.

param(
    [switch]$Deps,
    [string]$Exe = (Join-Path $PSScriptRoot "..\dist\Gestor Nexxo 800.exe")
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$thumb = $env:NEXXO_SIGN_THUMBPRINT
$dlib = $env:NEXXO_SIGN_DLIB
$metadata = $env:NEXXO_SIGN_METADATA

if (-not $thumb -and -not ($dlib -and $metadata)) {
    if (-not $Deps) {
        Write-Host "  [INFO] Sin certificado configurado: el .exe queda SIN FIRMA." -ForegroundColor Yellow
        Write-Host "         Smart App Control / SmartScreen lo bloquearan en otros PCs." -ForegroundColor Yellow
        Write-Host "         Ver scripts\sign_exe.ps1 para configurar la firma." -ForegroundColor Yellow
    }
    exit 0
}

$signtool = Find-SignTool
if (-not $signtool) {
    Write-Host "  [ERROR] signtool.exe no encontrado. Instale el Windows SDK (componente 'Signing Tools')." -ForegroundColor Red
    exit 1
}

if ($dlib -and $metadata) {
    $ts = if ($env:NEXXO_SIGN_TIMESTAMP) { $env:NEXXO_SIGN_TIMESTAMP } else { "http://timestamp.acs.microsoft.com" }
    $baseArgs = @("sign", "/fd", "SHA256", "/tr", $ts, "/td", "SHA256", "/dlib", $dlib, "/dmdf", $metadata)
} else {
    $ts = if ($env:NEXXO_SIGN_TIMESTAMP) { $env:NEXXO_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
    $baseArgs = @("sign", "/fd", "SHA256", "/tr", $ts, "/td", "SHA256", "/sha1", $thumb)
}

function Invoke-Sign([string]$File) {
    & $signtool @baseArgs $File | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Fallo la firma de $File" -ForegroundColor Red
        exit 1
    }
    & $signtool verify /pa $File | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] La firma de $File no verifica." -ForegroundColor Red
        exit 1
    }
}

if ($Deps) {
    $site = Join-Path $PSScriptRoot "..\.venv\Lib\site-packages"
    if (-not (Test-Path $site)) { exit 0 }
    $unsigned = Get-ChildItem $site -Recurse -Include *.pyd, *.dll |
        Where-Object { $_.FullName -notmatch "\\(PyInstaller|pip|setuptools|PIL)\\" } |
        Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -eq "NotSigned" }
    foreach ($f in $unsigned) {
        Write-Host "  [INFO] Firmando dependencia $($f.Name)"
        Invoke-Sign $f.FullName
    }
    Write-Host "  [OK] Dependencias firmadas ($(@($unsigned).Count))." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $Exe)) {
    Write-Host "  [ERROR] No existe $Exe" -ForegroundColor Red
    exit 1
}
Write-Host "  [INFO] Firmando $Exe"
Invoke-Sign $Exe
Write-Host "  [OK] Ejecutable firmado y verificado." -ForegroundColor Green
