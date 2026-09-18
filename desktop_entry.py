from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import desktop_app as ui
from core.desktop_settings import app_data_dir, get_secret
from desktop_app import APP_TITLE, MainWindow as AppMainWindow, Worker, self_test as core_self_test


_CRASH_FH = None
_INSTANCE_LOCK: QLockFile | None = None


def _append_diagnostic(title: str, text: str) -> None:
    try:
        path = app_data_dir() / "diagnostic.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {title} ---\n{text.rstrip()}\n")
    except Exception:
        pass


def _install_crash_logging() -> None:
    """Persist Python exceptions and fatal faults for windowed builds.

    PyInstaller's --windowed executable has no console, so an exception during startup
    otherwise looks exactly like a silent flash/crash to the user.
    """
    global _CRASH_FH
    try:
        crash_path = app_data_dir() / "crash.log"
        _CRASH_FH = crash_path.open("a", encoding="utf-8", buffering=1)
        _CRASH_FH.write("\n=== application start ===\n")
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except Exception:
        _CRASH_FH = None

    previous_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _append_diagnostic("unhandled exception", formatted)
        if _CRASH_FH is not None:
            try:
                _CRASH_FH.write(formatted)
                _CRASH_FH.flush()
            except Exception:
                pass
        try:
            previous_hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = hook


class MainWindow(AppMainWindow):
    """Windows-packaged window focused on fast, recoverable startup.

    The legacy window initialized four SQLite schemas before the first frame was shown
    and synchronously queried the OS credential store. A busy/corrupt DB or slow
    Credential Manager therefore made the application appear frozen, and an exception
    looked like a silent crash. The packaged entry defers those operations until after
    the window is visible and keeps the UI usable if initialization fails.
    """

    def __init__(self):
        self._startup_ready = False
        self._startup_started = False
        self._startup_error = ""
        self._startup_worker = None
        self._db_initializers = (
            ui.init_db,
            ui.init_knowledge_db,
            ui.init_online_learning_db,
            ui.init_personal_baseline_db,
        )

        # Base MainWindow performs these synchronously before constructing the UI.
        # Suppress only during super().__init__; all normal methods see the originals.
        originals = (
            ui.init_db,
            ui.init_knowledge_db,
            ui.init_online_learning_db,
            ui.init_personal_baseline_db,
        )
        noop = lambda *args, **kwargs: None
        try:
            ui.init_db = noop
            ui.init_knowledge_db = noop
            ui.init_online_learning_db = noop
            ui.init_personal_baseline_db = noop
            super().__init__()
        finally:
            (
                ui.init_db,
                ui.init_knowledge_db,
                ui.init_online_learning_db,
                ui.init_personal_baseline_db,
            ) = originals

        self._apply_windows_layout_hardening()
        self._set_startup_controls_enabled(False)
        self.statusBar().showMessage("界面已就绪，正在后台初始化本地数据…")

    def _load_settings_into_ui(self):
        """Load non-secret settings immediately; keyring access is deferred."""
        self.wcl_client_id.setText(str(self.settings.get("wcl_client_id") or ""))
        saved_model = str(self.settings.get("deepseek_model") or ui.DEFAULT_MODEL)
        if saved_model in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
            saved_model = ui.DEFAULT_MODEL
        self.ds_model.setCurrentText(saved_model)
        speed = str(self.settings.get("ai_speed_mode") or "fast")
        self.ai_speed.setCurrentText("深度" if speed == "deep" else "平衡" if speed == "balanced" else "快速（推荐）")
        self.reasoning.setCurrentText(str(self.settings.get("reasoning_effort") or "low"))
        self.auto_online_compare.setChecked(bool(self.settings.get("auto_online_compare", True)))
        self.identity_input.setText(str(self.settings.get("last_identity") or ""))
        self.ds_key.clear()
        self.wcl_secret.clear()
        self._update_action_states()

    def _update_action_states(self):
        super()._update_action_states()
        if not getattr(self, "_startup_ready", False):
            if hasattr(self, "search_btn"):
                self.search_btn.setEnabled(False)
            if hasattr(self, "timed_analyze_btn"):
                self.timed_analyze_btn.setEnabled(False)
            if hasattr(self, "local_ai_btn"):
                self.local_ai_btn.setEnabled(False)

    def _set_startup_controls_enabled(self, ready: bool) -> None:
        self._startup_ready = bool(ready)
        if hasattr(self, "personal_sync_btn"):
            self.personal_sync_btn.setEnabled(ready)
        if hasattr(self, "personal_refresh_btn"):
            self.personal_refresh_btn.setEnabled(ready)
        if hasattr(self, "season_target_btn"):
            self.season_target_btn.setEnabled(ready and bool(getattr(self, "timed_runs", [])))
        if hasattr(self, "local_ai_btn"):
            self.local_ai_btn.setEnabled(ready and bool(getattr(self, "local_runs", {})))
        if ready:
            super()._update_action_states()
            if hasattr(self, "_timed_selection_changed"):
                self._timed_selection_changed()

    def _apply_windows_layout_hardening(self) -> None:
        """Keep the UI inside the usable screen at 125%/150% Windows scaling."""
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            min_w = min(860, max(680, area.width() - 80))
            min_h = min(600, max(480, area.height() - 90))
            self.setMinimumSize(min_w, min_h)
            width = min(1320, max(min_w, int(area.width() * 0.94)))
            height = min(860, max(min_h, int(area.height() * 0.90)))
            self.resize(width, height)
            self.move(
                area.x() + max(0, (area.width() - width) // 2),
                area.y() + max(0, (area.height() - height) // 2),
            )
        else:
            self.setMinimumSize(760, 520)
            self.resize(1100, 720)

        if hasattr(self, "nav"):
            self.nav.setFixedWidth(176)
        if hasattr(self, "identity_mode"):
            self.identity_mode.setFixedWidth(145)
        if hasattr(self, "search_btn"):
            self.search_btn.setText("读取最近大秘境")
        if hasattr(self, "personal_sync_btn"):
            self.personal_sync_btn.setText("同步个人样本")
        if hasattr(self, "season_target_btn"):
            self.season_target_btn.setText("学习赛季重点怪")
        if hasattr(self, "personal_refresh_btn"):
            self.personal_refresh_btn.setText("刷新")

        if hasattr(self, "pages"):
            for index in range(self.pages.count()):
                page = self.pages.widget(index)
                layout = page.layout() if page is not None else None
                if layout is not None:
                    layout.setContentsMargins(18, 16, 18, 16)
                    layout.setSpacing(10)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.width()
        if hasattr(self, "nav"):
            self.nav.setFixedWidth(154 if width < 980 else 176)
        # Status chips are useful, but on narrow high-DPI screens they steal the whole
        # top bar and make the title/layout appear broken.
        if hasattr(self, "wcl_chip"):
            self.wcl_chip.setVisible(width >= 850)
        if hasattr(self, "ai_chip"):
            self.ai_chip.setVisible(width >= 850)

    def _run_startup_checks_sync(self, *, load_secrets: bool) -> dict[str, Any]:
        for init_fn in self._db_initializers:
            init_fn()
        result: dict[str, Any] = {"db_ready": True}
        if load_secrets:
            result["deepseek_api_key"] = get_secret("deepseek_api_key")
            result["wcl_client_secret"] = get_secret("wcl_client_secret")
        return result

    def start_deferred_initialization(self) -> None:
        if self._startup_started:
            return
        self._startup_started = True

        def job():
            return self._run_startup_checks_sync(load_secrets=True)

        def done(result: dict[str, Any]):
            if not self.ds_key.text().strip():
                self.ds_key.setText(str(result.get("deepseek_api_key") or ""))
            if not self.wcl_secret.text().strip():
                self.wcl_secret.setText(str(result.get("wcl_client_secret") or ""))
            self._startup_error = ""
            self._set_startup_controls_enabled(True)
            self._update_connection_status()
            self.statusBar().showMessage("就绪")

        def failed(message: str):
            self._startup_error = str(message or "本地数据初始化失败")
            self._set_startup_controls_enabled(False)
            self.statusBar().showMessage("本地数据初始化失败；设置页仍可使用，详情见 diagnostic.log")
            _append_diagnostic("startup initialization failed", self._startup_error)

        worker = Worker(job)
        worker.signals.result.connect(done)
        worker.signals.error.connect(failed)
        self._startup_worker = worker
        self.thread_pool.start(worker)

    def closeEvent(self, event):
        try:
            client = getattr(self, "_wcl_client_obj", None)
            if client is not None:
                client.close()
                self._wcl_client_obj = None
                self._wcl_client_key = None
            try:
                self.thread_pool.clear()
            except Exception:
                pass
        finally:
            super().closeEvent(event)


def packaged_self_test() -> int:
    """Exercise real DB init, window construction, paint/event loop, and shutdown."""
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
        win._run_startup_checks_sync(load_secrets=False)
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
        win.show()
        app.processEvents()
        if not win.isVisible():
            return 15
        QTimer.singleShot(180, app.quit)
        app.exec()
        return 0
    except Exception:
        _append_diagnostic("packaged self-test exception", traceback.format_exc())
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


def _acquire_single_instance_lock() -> bool:
    global _INSTANCE_LOCK
    try:
        lock = QLockFile(str(app_data_dir() / "wow-log-data-analyst.lock"))
        lock.setStaleLockTime(15_000)
        if not lock.tryLock(120):
            return False
        _INSTANCE_LOCK = lock
        return True
    except Exception:
        # Never make lock infrastructure itself a new startup-crash source.
        return True


def main() -> int:
    _install_crash_logging()
    if "--self-test" in sys.argv:
        return packaged_self_test()

    try:
        app = QApplication(sys.argv)
        app.setApplicationName(APP_TITLE)
        app.setOrganizationName("WoW Log Data Analyst")

        if not _acquire_single_instance_lock():
            QMessageBox.information(None, APP_TITLE, "程序已经在运行。请切换到已打开的窗口，不要连续启动多个实例。")
            return 0

        win = MainWindow()
        win.show()
        # Defer disk/keyring work until the first frame has actually had a chance to paint.
        QTimer.singleShot(0, win.start_deferred_initialization)
        return app.exec()
    except Exception:
        details = traceback.format_exc()
        _append_diagnostic("fatal startup exception", details)
        try:
            QMessageBox.critical(
                None,
                "WoW Log Data Analyst 启动失败",
                "程序启动时发生错误，但诊断信息已经保存。\n\n"
                f"日志目录：{app_data_dir()}\n\n"
                "请把 diagnostic.log / crash.log 发给开发者。",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
