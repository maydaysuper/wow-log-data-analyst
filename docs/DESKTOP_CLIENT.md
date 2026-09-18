# Native desktop client

v0.9 switches the release entry point from Streamlit to PySide6.

## Shared core

Windows and macOS use the same Python/core modules. Platform divergence is limited to packaging and secure credential storage.

## Security

- WCL Client Secret and DeepSeek API Key can be stored with `keyring`.
- No API key is written to the source tree, log exports, AI reports or SQLite evidence payloads.
- Non-sensitive preferences are stored in the OS application-data directory.

## Responsiveness

All network and AI calls execute in `QThreadPool` workers. The main Qt event loop remains responsive while WCL events are fetched or DeepSeek is reasoning.

## Packaging

- Windows: PyInstaller one-file GUI executable.
- macOS: PyInstaller `.app`, ad-hoc signed, packed into a `.dmg`.
- Public macOS distribution should later use Developer ID signing and notarization.
