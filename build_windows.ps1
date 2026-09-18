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

Write-Host "[1/7] Updating build tools..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check --upgrade pip wheel setuptools } "Updating build tools"

Write-Host "[2/7] Installing dependencies..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.10,<7" pytest } "Installing dependencies"

Write-Host "[3/7] Running compile checks and tests..." -ForegroundColor Cyan
$env:PYTHONPATH = "."
Invoke-Checked { & $Python -m compileall -q desktop_entry.py desktop_app.py core tests } "Python compile check"
Invoke-Checked { & $Python -m pytest -q } "Automated tests"

Write-Host "[4/7] Running source GUI startup smoke test..." -ForegroundColor Cyan
$OldQtPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    Invoke-Checked { & $Python desktop_entry.py --self-test } "Source GUI startup smoke test"
} finally {
    $env:QT_QPA_PLATFORM = $OldQtPlatform
}

Write-Host "[5/7] Building Windows portable app (onedir)..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build,dist -ErrorAction SilentlyContinue

# onedir intentionally beats onefile for this Qt/Pandas application on Windows:
# onefile extracts a large runtime on every launch, while onedir starts directly.
# desktop_entry.py provides deferred startup initialization, crash logging, single-instance
# protection, high-DPI layout hardening, deterministic WCL cleanup, and a real GUI smoke test.
Invoke-Checked { & $PyInstaller `
  --noconfirm --clean --windowed --onedir `
  --optimize 1 `
  --name $AppName `
  --collect-all keyring `
  --hidden-import keyring.backends.Windows `
  --add-data "addons;addons" `
  --add-data "VERSION;." `
  --exclude-module tkinter `
  desktop_entry.py } "PyInstaller build"

$DistDir = Join-Path "dist" $AppName
$Exe = Join-Path $DistDir "$AppName.exe"
if (-not (Test-Path $Exe)) { throw "Build failed: executable not found at $Exe" }

Write-Host "[6/7] Running frozen EXE GUI self-test and waiting for clean exit..." -ForegroundColor Cyan
# A Windows-subsystem GUI executable can return control to PowerShell before the process
# has fully exited when invoked with '&'. Start-Process -Wait is required; otherwise the
# following Compress-Archive can race the still-running EXE and hit locked _internal files.
$OldQtPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    $Smoke = Start-Process -FilePath $Exe -ArgumentList "--self-test" -PassThru -Wait
    if ($Smoke.ExitCode -ne 0) {
        throw "Frozen EXE GUI self-test failed with exit code $($Smoke.ExitCode)"
    }
} finally {
    $env:QT_QPA_PLATFORM = $OldQtPlatform
}

Write-Host "[7/7] Creating Windows portable ZIP..." -ForegroundColor Cyan
$Zip = "WoW-Log-Data-Analyst-v$Version-Windows-Portable.zip"
Remove-Item $Zip -Force -ErrorAction SilentlyContinue

# Give antivirus/indexing a very short chance to release newly-created files. The app
# process itself has already exited above, so repeated locking here is a real packaging error.
$BaseLibrary = Join-Path $DistDir "_internal\base_library.zip"
for ($i = 0; $i -lt 10; $i++) {
    try {
        $Stream = [System.IO.File]::Open($BaseLibrary, 'Open', 'Read', 'Read')
        $Stream.Close()
        break
    } catch {
        if ($i -eq 9) { throw }
        Start-Sleep -Milliseconds 250
    }
}

Compress-Archive -Path "$DistDir\*" -DestinationPath $Zip -CompressionLevel Optimal
if (-not (Test-Path $Zip)) { throw "Build failed: portable ZIP was not created." }

Write-Host ""
Write-Host "Build completed." -ForegroundColor Green
Write-Host "EXE: $Exe" -ForegroundColor Green
Write-Host "Portable ZIP: $Zip" -ForegroundColor Green
Write-Host ""
Write-Host "Keep the EXE and _internal directory together when testing." -ForegroundColor Yellow
