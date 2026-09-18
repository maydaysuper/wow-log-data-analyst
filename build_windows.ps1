$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$AppName = "WoW Log Data Analyst"
$Version = (Get-Content VERSION -Raw).Trim()
$Venv = ".venv-build-windows"

function Invoke-Checked {
    param(
        [Parameter(Mandatory=$true)][scriptblock]$Command,
        [Parameter(Mandatory=$true)][string]$Step
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonLauncher = "python"
    $PythonLauncherArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonLauncher = "py"
    $PythonLauncherArgs = @("-3")
} else {
    throw "Python 3 was not found. Install Python 3.11/3.12 x64 and add it to PATH."
}

if (-not (Test-Path $Venv)) {
    & $PythonLauncher @PythonLauncherArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the build virtual environment failed." }
}
$Python = Join-Path $Venv "Scripts\python.exe"
$PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"

Write-Host "[1/6] Updating build tools..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check --upgrade pip wheel setuptools } "Updating build tools"

Write-Host "[2/6] Installing dependencies..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.10,<7" pytest } "Installing dependencies"

Write-Host "[3/6] Running compile checks and tests..." -ForegroundColor Cyan
$env:PYTHONPATH = "."
Invoke-Checked { & $Python -m compileall -q desktop_app.py core tests } "Python compile check"
Invoke-Checked { & $Python -m pytest -q } "Automated tests"

Write-Host "[4/6] Building Windows portable app (onedir)..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build,dist -ErrorAction SilentlyContinue

# onedir intentionally beats onefile for this Qt/Pandas application on Windows:
# onefile extracts a large runtime on every launch, while onedir starts directly.
Invoke-Checked { & $PyInstaller `
  --noconfirm --clean --windowed --onedir `
  --optimize 1 `
  --name $AppName `
  --collect-all keyring `
  --hidden-import keyring.backends.Windows `
  --add-data "addons;addons" `
  --add-data "VERSION;." `
  --exclude-module tkinter `
  desktop_app.py } "PyInstaller build"

$DistDir = Join-Path "dist" $AppName
$Exe = Join-Path $DistDir "$AppName.exe"
if (-not (Test-Path $Exe)) { throw "Build failed: executable not found at $Exe" }

Write-Host "[5/6] Running frozen EXE self-test..." -ForegroundColor Cyan
Invoke-Checked { & $Exe --self-test } "Frozen EXE self-test"

Write-Host "[6/6] Creating Windows portable ZIP..." -ForegroundColor Cyan
$Zip = "WoW-Log-Data-Analyst-v$Version-Windows-Portable.zip"
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$DistDir\*" -DestinationPath $Zip -CompressionLevel Optimal
if (-not (Test-Path $Zip)) { throw "Build failed: portable ZIP was not created." }

Write-Host ""
Write-Host "Build completed." -ForegroundColor Green
Write-Host "EXE: $Exe" -ForegroundColor Green
Write-Host "Portable ZIP: $Zip" -ForegroundColor Green
Write-Host ""
Write-Host "Keep the EXE and _internal directory together when testing." -ForegroundColor Yellow
