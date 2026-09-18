from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from desktop_app import APP_TITLE, MainWindow as AppMainWindow, self_test as core_self_test


class MainWindow(AppMainWindow):
    """Packaged desktop window with deterministic resource cleanup.

    desktop_app historically defined closeEvent twice, so the later settings-saving
    implementation shadowed the earlier WCL-session cleanup.  Keep compatibility with
    the existing UI while guaranteeing that the shared HTTP session is closed first.
    """

    def closeEvent(self, event):
        try:
            client = getattr(self, "_wcl_client_obj", None)
            if client is not None:
                client.close()
                self._wcl_client_obj = None
                self._wcl_client_key = None
        finally:
            super().closeEvent(event)


def packaged_self_test() -> int:
    """Exercise the frozen application's real GUI construction offline.

    The previous smoke test only imported helper functions.  That could pass even when
    Qt plugins, database initialization, widgets, or packaged resources were broken.
    This test creates the actual MainWindow using Qt's offscreen platform and verifies
    the main pages/actions exist without touching WCL or DeepSeek.
    """
    base_result = core_self_test()
    if base_result:
        return base_result

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication([APP_TITLE, "--self-test"])
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("WoW Log Data Analyst")

    win = None
    try:
        win = MainWindow()
        if getattr(win, "pages", None) is None or win.pages.count() != 5:
            return 10
        if getattr(win, "timed_table", None) is None:
            return 11
        if getattr(win, "report_browser", None) is None:
            return 12
        if getattr(win, "season_target_btn", None) is None:
            return 13
        if getattr(win, "thread_pool", None) is None:
            return 14
        app.processEvents()
        return 0
    except Exception:
        return 20
    finally:
        if win is not None:
            try:
                win.close()
                app.processEvents()
            except Exception:
                pass
        if owns_app:
            try:
                app.quit()
            except Exception:
                pass


def main() -> int:
    if "--self-test" in sys.argv:
        return packaged_self_test()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("WoW Log Data Analyst")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
