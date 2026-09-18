#!/bin/bash
set -u
cd "$(dirname "$0")"

show_dialog() {
  /usr/bin/osascript -e "display dialog \"$1\" buttons {\"OK\"} default button \"OK\" with title \"WoW Log Data Analyst\"" >/dev/null 2>&1 || true
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  show_dialog "这个构建器只能在 macOS 上运行。"
  exit 1
fi

VERSION="$(tr -d "[:space:]" < VERSION)"
LOG="$HOME/Desktop/WoW-Log-Data-Analyst-build.log"
echo "WoW Log Data Analyst v${VERSION} build" > "$LOG"

echo "正在构建 Mac 测试版，请稍候……"
if /bin/bash ./build_macos_app.sh >> "$LOG" 2>&1; then
  show_dialog "v${VERSION} 构建完成。将自动打开 DMG。"
  open "WoW-Log-Data-Analyst-macOS.dmg"
else
  show_dialog "构建失败。详细日志已经放到桌面：WoW-Log-Data-Analyst-build.log"
  open -R "$LOG"
  exit 1
fi
