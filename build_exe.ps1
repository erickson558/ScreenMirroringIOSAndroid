param(
    [string]$Python = "python",
    [string]$EntryPoint = "app.py",
    [string]$AppName = "LonelyScreenIOS",
    [string]$Icon = "lonelyscreenIOS.ico",
    [string]$UxPlayRoot = "tools/uxplay",
    [string]$UxPlayBinaryRelative = "bin/uxplay.exe",
    [string]$VersionFile = "version.json",
    [string]$WorkPath = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EntryPoint)) {
    throw "Entry point '$EntryPoint' was not found."
}

if (-not (Test-Path $Icon)) {
    throw "Icon '$Icon' was not found in project root."
}

if (-not (Test-Path $UxPlayRoot)) {
    throw "UxPlay folder '$UxPlayRoot' was not found."
}

$uxplayBinaryPath = Join-Path $UxPlayRoot $UxPlayBinaryRelative
if (-not (Test-Path $uxplayBinaryPath)) {
    throw "UxPlay binary was not found at '$uxplayBinaryPath'."
}

$uxplayRootResolved = (Resolve-Path $UxPlayRoot).Path
$uxplayFileCount = (Get-ChildItem $uxplayRootResolved -Recurse -File | Measure-Object).Count

if ([string]::IsNullOrWhiteSpace($WorkPath)) {
    $WorkPath = Join-Path $env:TEMP "$AppName-pyinstaller-build"
}

if (Test-Path $WorkPath) {
    Remove-Item $WorkPath -Recurse -Force -ErrorAction SilentlyContinue
}

& $Python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found. Installing..."
    & $Python -m pip install pyinstaller
}

Write-Host "Embedding '$UxPlayRoot' recursively ($uxplayFileCount files) into onefile executable..."
Write-Host "Building $AppName.exe in current folder..."

$versionFileAbsolute = Join-Path (Get-Location) $VersionFile
$env:LONELYSCREENIOS_VERSION_FILE = $versionFileAbsolute
$versionOutput = & $Python -c "import os; from pathlib import Path; from backend.versioning import bump_patch_version; info = bump_patch_version(Path(os.environ['LONELYSCREENIOS_VERSION_FILE'])); print(info.version)"
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo incrementar la versión del proyecto."
}
$currentVersion = ($versionOutput | Select-Object -Last 1).Trim()
Write-Host "Version actual: $currentVersion"
Remove-Item Env:LONELYSCREENIOS_VERSION_FILE -ErrorAction SilentlyContinue

$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $AppName,
    "--icon", $Icon,
    "--distpath", ".",
    "--workpath", $WorkPath,
    "--specpath", ".",
    "--add-data", "$uxplayRootResolved;$UxPlayRoot",
    $EntryPoint
)

& $Python @pyArgs

if ($LASTEXITCODE -ne 0) {
    throw "Build failed."
}

Write-Host "Build completed: .\$AppName.exe"
