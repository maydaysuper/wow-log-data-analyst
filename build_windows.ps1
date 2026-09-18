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
        throw "$Step 失败，退出码：$LASTEXITCODE"
    }
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonLauncher = "python"
    $PythonLauncherArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonLauncher = "py"
    $PythonLauncherArgs = @("-3")
} else {
    throw "未检测到 Python 3。请安装 Python 3.11/3.12 x64，并勾选 Add Python to PATH。"
}

if (-not (Test-Path $Venv)) {
    & $PythonLauncher @PythonLauncherArgs -m venv $Venv
}
$Python = Join-Path $Venv "Scripts\python.exe"
$PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"

Write-Host "[1/6] 更新构建工具..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check --upgrade pip wheel setuptools } "更新构建工具"

Write-Host "[2/6] 安装依赖..." -ForegroundColor Cyan
Invoke-Checked { & $Python -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.10,<7" pytest } "安装依赖"

Write-Host "[3/6] 运行编译检查和自动测试..." -ForegroundColor Cyan
$env:PYTHONPATH = "."
Invoke-Checked { & $Python -m compileall -q desktop_app.py core tests } "Python 编译检查"
Invoke-Checked { & $Python -m pytest -q } "自动测试"

Write-Host "[4/6] 构建 Windows 便携版（优先启动速度）..." -ForegroundColor Cyan
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
  desktop_app.py } "PyInstaller 构建"

$DistDir = Join-Path "dist" $AppName
$Exe = Join-Path $DistDir "$AppName.exe"
if (-not (Test-Path $Exe)) { throw "构建失败：没有找到 $Exe" }

Write-Host "[5/6] 运行冻结 EXE 自检..." -ForegroundColor Cyan
Invoke-Checked { & $Exe --self-test } "冻结 EXE 自检"

Write-Host "[6/6] 生成 Windows 测试压缩包..." -ForegroundColor Cyan
$Zip = "WoW-Log-Data-Analyst-v$Version-Windows-Portable.zip"
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$DistDir\*" -DestinationPath $Zip -CompressionLevel Optimal

Write-Host ""
Write-Host "构建完成。" -ForegroundColor Green
Write-Host "EXE: $Exe" -ForegroundColor Green
Write-Host "便携包: $Zip" -ForegroundColor Green
Write-Host ""
Write-Host "测试时请保持 EXE 与 _internal 目录在一起，不要只复制单个 EXE。" -ForegroundColor Yellow
