param(
    [switch]$InsecureFallback
)

$ErrorActionPreference = "Stop"

function Set-GitGlobalConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Key,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    git config --global $Key $Value | Out-Null
}

function Remove-GitGlobalConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Key
    )
    try {
        git config --global --unset-all $Key | Out-Null
    } catch {
        # Ignore when key does not exist.
    }
}

Write-Host "Aplicando configuracion SSL recomendada para Git en Windows..." -ForegroundColor Cyan

# Prefer Windows certificate store.
Set-GitGlobalConfig "http.sslBackend" "schannel"

# Remove overrides that often break trust chains with VPN/proxy interception.
Remove-GitGlobalConfig "http.sslCAInfo"
Remove-GitGlobalConfig "http.sslCAPath"

if ($InsecureFallback.IsPresent) {
    Write-Warning "Se habilitara fallback inseguro: http.sslVerify=false. Usar solo temporalmente."
    Set-GitGlobalConfig "http.sslVerify" "false"
} else {
    Set-GitGlobalConfig "http.sslVerify" "true"
}

Write-Host "Configuracion aplicada. Validando conectividad HTTPS de Git..." -ForegroundColor Cyan

$testUrl = "https://github.com/erickson558/ScreenMirroringIOSAndroid.git"
git ls-remote $testUrl | Out-Null

Write-Host "OK: Git HTTPS funciona con la configuracion actual." -ForegroundColor Green
Write-Host "Comandos utiles:" -ForegroundColor Yellow
Write-Host "  git config --global --get-all http.sslBackend"
Write-Host "  git config --global --get-all http.sslVerify"
