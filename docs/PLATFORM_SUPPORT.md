# Platform support

The project uses **one shared analytics codebase** for Windows and macOS.

## Shared code

- `app.py` — UI and workflows
- `core/parser.py` — Blizzard Combat Log parser
- `core/analyzer.py` — comparison/statistics engine
- `core/ai_client.py` — optional DeepSeek analysis
- `core/wcl_client.py` — optional Warcraft Logs API integration

## macOS

### Double-click version

1. Double-click `install_macos.command` once.
2. Afterwards double-click `start_macos.command`.
3. The application opens in the default browser and listens only on `127.0.0.1`.

### Native `.app`

Run on a Mac:

```bash
./build_macos_app.sh
```

Output: `dist/WoW Log Data Analyst.app`

The application is unsigned by default. Signing/notarization can be added later when an Apple Developer ID is available.

## Windows

1. First run: `install_windows.bat`
2. Afterwards: `start_windows.bat`
3. To build an executable, run `build_windows.ps1` in PowerShell.

## Maintenance rule

Do not fork the analytics logic by operating system. Platform-specific changes belong only in launcher/build scripts unless the OS genuinely requires different behavior.
