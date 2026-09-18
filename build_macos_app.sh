#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS."
  exit 1
fi

if [ ! -d ".venv-build-macos" ]; then
  python3 -m venv .venv-build-macos
fi
. .venv-build-macos/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pytest
python -m py_compile desktop_app.py core/*.py
PYTHONPATH=. pytest -q
rm -rf build dist dmg-root "WoW-Log-Data-Analyst-macOS.dmg"

pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "WoW Log Data Analyst" \
  --collect-all keyring \
  --add-data "addons:addons" \
  --add-data "VERSION:." \
  desktop_app.py

# Execute the frozen binary before packaging. A bundle that imports successfully in
# source mode can still miss a PyInstaller hidden import/resource.
"dist/WoW Log Data Analyst.app/Contents/MacOS/WoW Log Data Analyst" --self-test

# Ad-hoc sign so the app bundle is internally coherent. Distribution notarization can
# be added later when an Apple Developer ID is available.
codesign --force --deep --sign - "dist/WoW Log Data Analyst.app" || true
mkdir -p dmg-root
cp -R "dist/WoW Log Data Analyst.app" dmg-root/
ln -s /Applications dmg-root/Applications
hdiutil create -volname "WoW Log Data Analyst" -srcfolder dmg-root -ov -format UDZO "WoW-Log-Data-Analyst-macOS.dmg"

echo "Build complete: WoW-Log-Data-Analyst-macOS.dmg"
