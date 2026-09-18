from __future__ import annotations

import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QInputDialog,
    QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget, QHeaderView, QProgressBar,
    QAbstractItemView,
)

from core.ai_client import (
    render_analysis_markdown, run_coach_pipeline, summarize_personal_model_with_deepseek,
    learn_lesson_with_deepseek, DEFAULT_MODEL,
)
from core.analyzer import (
    compact_comparison_for_ai, infer_player_specs, infer_run_metadata, list_players,
    split_mplus_runs, team_overview,
)
from core.desktop_settings import app_data_dir, get_secret, load_settings, save_settings, set_secret
from core.knowledge import init_knowledge_db, knowledge_context_for_analysis, knowledge_context_for_signatures, save_knowledge_sample
from core.learning import (
    init_db, memory_context_for_ai, retrieve_lessons, retrieve_similar_cases, save_case, update_feedback, add_lesson,
)
from core.online_learning import init_online_learning_db, online_context_for_analysis, learn_from_wcl_rankings
from core.personal_baseline import (
    best_personal_profile_for_signature, build_personal_profile, character_identity,
    compare_to_personal_profile, init_personal_baseline_db, list_personal_samples,
    personal_context_for_ai, save_personal_sample, sync_recent_wcl_personal_baseline,
    sync_timed_wcl_personal_baseline, latest_personal_model_summary, save_personal_model_summary,
)
from core.parser import parse_text
from core.wcl_client import WCLClient
from core.wcl_direct_analysis import (
    build_targeted_wcl_payload, fights_for_source, resolve_report_actor,
    spec_and_item_level_for_fight,
)
from core.wcl_identity import parse_character_locator
from core.wcl_recent_runs import timed_runs_from_report
from core.batch_analysis import build_multi_fight_payload
from core.target_focus import compare_signature_target_focus
from core.season_targets import dominant_class_spec, learn_current_season_targets


APP_TITLE = "WoW Log Data Analyst"


def _resource_path(name: str) -> Path:
    """Resolve bundled resources in source and PyInstaller builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def _read_app_version() -> str:
    try:
        value = _resource_path("VERSION").read_text(encoding="utf-8").strip()
        return value or "0.18.0"
    except Exception:
        return "0.18.0"


APP_VERSION = _read_app_version()
PAGE_MY_WCL = 0
PAGE_PERSONAL = 1
PAGE_LOCAL = 2
PAGE_REPORT = 3
PAGE_SETTINGS = 4


def _fmt_ms(ms: Any) -> str:
    try:
        s = float(ms or 0) / 1000.0
    except Exception:
        return "-"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}:{sec:02d}"


def _fmt_ts(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.result.emit(self.fn(*self.args, **self.kwargs))
        except Exception as e:
            try:
                log_path = app_data_dir() / "diagnostic.log"
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n--- background task error ---\n{traceback.format_exc()}\n")
            except Exception:
                pass
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()


class TargetComparisonChart(QWidget):
    """Lightweight paired-bar chart without an extra QtCharts dependency."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self.setMinimumHeight(120)

    def set_rows(self, rows: list[dict[str, Any]] | None) -> None:
        self._rows = list(rows or [])[:8]
        self.setMinimumHeight(max(120, 42 + len(self._rows) * 46))
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#11151c"))
        if not self._rows:
            painter.setPen(QColor("#7f8998"))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无足够的同条件重要目标参考样本")
            return

        left = 250
        right = 92
        top = 34
        usable = max(120, self.width() - left - right)
        max_value = max(1.0, max(float(r.get("current_damage_share_pct") or 0) for r in self._rows),
                        max(float(r.get("reference_damage_share_pct") or 0) for r in self._rows),
                        max(float(r.get("reference_high_damage_share_pct") or 0) for r in self._rows))
        # A little headroom keeps labels from sitting on the edge.
        max_value *= 1.12
        painter.setPen(QColor("#aeb7c5"))
        painter.drawText(left, 18, "当前场次")
        painter.setPen(QColor("#62d394"))
        painter.drawText(left + 72, 18, "同专精参考")
        for i, row in enumerate(self._rows):
            y = top + i * 46
            name = str(row.get("npc_name") or row.get("npc_id") or "目标")
            role = str(row.get("role_cn") or "重要目标")
            label = f"{role} · {name}"
            if len(label) > 24:
                label = label[:23] + "…"
            painter.setPen(QColor("#e8eaed"))
            painter.drawText(10, y + 18, label)

            cur = float(row.get("current_damage_share_pct") or 0)
            ref = float(row.get("reference_damage_share_pct") or 0)
            hi = float(row.get("reference_high_damage_share_pct") or 0)
            cur_w = int(usable * min(max(cur / max_value, 0.0), 1.0))
            ref_w = int(usable * min(max(ref / max_value, 0.0), 1.0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#4d8dff"))
            painter.drawRoundedRect(left, y + 4, max(2, cur_w), 9, 4, 4)
            painter.setBrush(QColor("#62d394"))
            painter.drawRoundedRect(left, y + 18, max(2, ref_w), 9, 4, 4)
            if hi > ref:
                hx = left + int(usable * min(max(hi / max_value, 0.0), 1.0))
                painter.setPen(QPen(QColor("#8f99a8"), 1))
                painter.drawLine(hx, y + 16, hx, y + 29)
            painter.setPen(QColor("#c7cfdb"))
            painter.drawText(left + usable + 8, y + 12, f"{cur:.1f}%")
            painter.setPen(QColor("#86d9aa"))
            painter.drawText(left + usable + 8, y + 27, f"{ref:.1f}%")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        init_db(); init_knowledge_db(); init_online_learning_db(); init_personal_baseline_db()
        self.settings = load_settings()
        self.thread_pool = QThreadPool.globalInstance()
        # Network/AI work stays off the GUI thread, but unbounded workers make Windows
        # laptops and WCL rate limits worse. Six is enough because inner WCL event fetches
        # already parallelize their own independent streams.
        self.thread_pool.setMaxThreadCount(6)
        self._wcl_client_obj: WCLClient | None = None
        self._wcl_client_key: tuple[str, str] | None = None
        self.character_profile: dict[str, Any] = {}
        self.reports: list[dict[str, Any]] = []
        self.timed_runs: list[dict[str, Any]] = []
        self.current_report: dict[str, Any] = {}
        self.current_source_id: int = 0
        self.current_player_name = ""
        self.current_class_name = ""
        self.local_runs: dict[str, Any] = {}
        self.last_ai_context: dict[str, Any] = {}
        self.last_ai_payload: dict[str, Any] = {}
        self.last_ai_json: dict[str, Any] = {}
        self.last_case_id: int | None = None
        self.current_personal_profile: dict[str, Any] = {}
        self.last_report_markdown = ""
        self.last_online_refresh: dict[str, Any] = {}

        self.setWindowTitle(f"{APP_TITLE} · v{APP_VERSION}")
        self.resize(1320, 860)
        self.setMinimumSize(1080, 720)
        self._build_ui()
        self._load_settings_into_ui()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("TopBar")
        top = QHBoxLayout(topbar)
        top.setContentsMargins(22, 12, 22, 12)
        brand = QLabel(f"{APP_TITLE}  ·  v{APP_VERSION}")
        brand.setObjectName("Brand")
        top.addWidget(brand)
        top.addStretch()
        self.wcl_chip = QLabel("WCL  未配置")
        self.wcl_chip.setObjectName("StatusChip")
        self.ai_chip = QLabel("AI  未配置")
        self.ai_chip.setObjectName("StatusChip")
        top.addWidget(self.wcl_chip)
        top.addWidget(self.ai_chip)
        root_layout.addWidget(topbar)

        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Navigation")
        self.nav.setFixedWidth(205)
        self.nav.addItems(["我的战斗", "个人模型", "本地 Log", "AI 报告", "设置"])
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._change_page)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_my_wcl_page())
        self.pages.addWidget(self._build_personal_page())
        self.pages.addWidget(self._build_local_page())
        self.pages.addWidget(self._build_report_page())
        self.pages.addWidget(self._build_settings_page())

        layout.addWidget(self.nav)
        layout.addWidget(self.pages, 1)
        root_layout.addWidget(body, 1)

        self.statusBar().setObjectName("StatusBar")
        self.statusBar().showMessage("就绪")
        self.setStyleSheet(self._app_stylesheet())

    def _app_stylesheet(self) -> str:
        return r"""
        QMainWindow, QWidget { background:#0f1115; color:#e8eaed; font-size:14px; }
        QFrame#TopBar { background:#11141a; border-bottom:1px solid #242833; }
        QLabel#Brand { font-size:15px; font-weight:700; color:#f3f5f7; }
        QLabel#StatusChip { background:#191d25; border:1px solid #2b313d; border-radius:12px; padding:5px 10px; color:#aab2bf; }
        QListWidget#Navigation { background:#11141a; border:0; padding:14px 10px; outline:0; }
        QListWidget#Navigation::item { padding:13px 14px; margin:3px 0; border-radius:8px; color:#aeb6c2; }
        QListWidget#Navigation::item:selected { background:#20293a; color:#ffffff; font-weight:700; }
        QListWidget#Navigation::item:hover { background:#181d26; color:#ffffff; }
        QFrame#Card { background:#151922; border:1px solid #242a35; border-radius:12px; }
        QLabel#PageTitle { font-size:25px; font-weight:800; color:#ffffff; }
        QLabel#Subtitle { color:#8f99a8; font-size:13px; }
        QLabel#SectionTitle { font-size:16px; font-weight:700; color:#f2f4f7; }
        QLabel#Positive { color:#62d394; font-weight:700; }
        QLabel#Muted { color:#7f8998; }
        QLineEdit, QComboBox { background:#10141b; border:1px solid #303746; border-radius:8px; padding:8px 10px; min-height:22px; selection-background-color:#3b82f6; }
        QLineEdit:focus, QComboBox:focus { border:1px solid #4d8dff; }
        QPushButton { background:#222936; border:1px solid #333b49; border-radius:8px; padding:8px 14px; font-weight:650; }
        QPushButton:hover { background:#2a3342; border-color:#465268; }
        QPushButton:pressed { background:#1a202a; }
        QPushButton:disabled { color:#5d6572; background:#171b22; border-color:#222733; }
        QPushButton#PrimaryButton { background:#2f6fed; border-color:#3d7df2; color:white; padding:9px 16px; }
        QPushButton#PrimaryButton:hover { background:#3d7df2; }
        QTableWidget { background:#11151c; alternate-background-color:#141922; border:1px solid #252b36; border-radius:9px; gridline-color:#202632; selection-background-color:#243a61; selection-color:white; }
        QHeaderView::section { background:#191e27; color:#aeb7c5; border:0; border-bottom:1px solid #2a313e; padding:8px; font-weight:700; }
        QTextBrowser { background:#11151c; border:1px solid #252b36; border-radius:9px; padding:10px; }
        QSplitter::handle { background:#1b2029; height:5px; }
        QProgressBar { background:#161b23; border:1px solid #2a313e; border-radius:4px; height:6px; text-align:center; }
        QProgressBar::chunk { background:#4d8dff; border-radius:3px; }
        QStatusBar { background:#11141a; color:#8b94a2; border-top:1px solid #242833; }
        QMessageBox { background:#151922; }
        """

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        return frame, layout

    def _page_shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(14)
        h = QLabel(title); h.setObjectName("PageTitle")
        sub = QLabel(subtitle); sub.setWordWrap(True); sub.setObjectName("Subtitle")
        outer.addWidget(h); outer.addWidget(sub)
        return page, outer

    def _build_my_wcl_page(self) -> QWidget:
        page, outer = self._page_shell(
            "我的战斗",
            "输入一次角色信息后，软件会自动扫描最近公开记录，只列出这个角色近期“限时成功”的大秘境。直接多选场次分析，不再反复进入 Report 再选 Fight。",
        )

        search_card, search_layout = self._card()
        title = QLabel("① 找到你的角色")
        title.setObjectName("SectionTitle")
        search_layout.addWidget(title)
        search_row = QHBoxLayout()
        self.identity_mode = QComboBox(); self.identity_mode.addItems(["角色 / 角色页面", "WCL 用户 ID"]); self.identity_mode.setFixedWidth(160)
        self.identity_input = QLineEdit(); self.identity_input.setPlaceholderText("粘贴角色页面，或输入 cn:白银之手:不是酒鬼丶")
        self.search_btn = QPushButton("读取最近限时大秘境"); self.search_btn.setObjectName("PrimaryButton"); self.search_btn.clicked.connect(self._search_wcl_logs)
        self.identity_input.returnPressed.connect(self._search_wcl_logs)
        self.identity_input.textChanged.connect(self._update_action_states)
        search_row.addWidget(self.identity_mode); search_row.addWidget(self.identity_input, 1); search_row.addWidget(self.search_btn)
        search_layout.addLayout(search_row)
        self.wcl_status = QLabel("先在“设置”中配置 WCL。查询后会自动整理最近限时成功的大秘境。")
        self.wcl_status.setObjectName("Muted"); self.wcl_status.setWordWrap(True)
        search_layout.addWidget(self.wcl_status)
        outer.addWidget(search_card)

        runs_card, runs_layout = self._card()
        head = QHBoxLayout()
        rt = QLabel("② 最近限时成功大秘境"); rt.setObjectName("SectionTitle"); head.addWidget(rt)
        hint = QLabel("⌘ / Shift 多选 · 建议选择同副本、相近层数一起分析，横向对比会更准确"); hint.setObjectName("Muted"); head.addWidget(hint)
        head.addStretch()
        self.timed_analyze_btn = QPushButton("分析选中场次"); self.timed_analyze_btn.setObjectName("PrimaryButton"); self.timed_analyze_btn.setEnabled(False); self.timed_analyze_btn.clicked.connect(self._analyze_selected_timed_runs)
        head.addWidget(self.timed_analyze_btn)
        runs_layout.addLayout(head)

        self.timed_table = QTableWidget(0, 8)
        self.timed_table.setHorizontalHeaderLabels(["日期", "副本", "层数", "限时结果", "用时", "专精", "装等", "Report / Fight"])
        self.timed_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.timed_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.timed_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.timed_table.setAlternatingRowColors(True); self.timed_table.verticalHeader().setVisible(False)
        self.timed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.timed_table.itemSelectionChanged.connect(self._timed_selection_changed)
        self.timed_table.doubleClicked.connect(lambda _idx: self._analyze_selected_timed_runs())
        runs_layout.addWidget(self.timed_table)

        info = QLabel("只显示限时完成的大秘境。失败、超时、团本/木桩等不会混入个人基线和默认横向比较。")
        info.setObjectName("Muted"); info.setWordWrap(True); runs_layout.addWidget(info)
        outer.addWidget(runs_card, 1)

        # Keep the older Report/Fight widgets as hidden compatibility objects. The main
        # user flow no longer requires them, but legacy helper methods/tests still refer
        # to these attributes.
        self.batch_analyze_btn = QPushButton(); self.load_report_btn = QPushButton()
        self.report_table = QTableWidget(0, 5)
        self.report_table.setHorizontalHeaderLabels(["日期", "标题", "副本/区域", "可见性", "Report Code"])
        self.report_table.setSelectionBehavior(QTableWidget.SelectRows); self.report_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.report_table.itemSelectionChanged.connect(self._reports_selection_changed)
        self.player_combo = QComboBox(); self.player_combo.setVisible(False)
        self.analyze_fight_btn = QPushButton(); self.analyze_fight_btn.setEnabled(False)
        self.fight_table = QTableWidget(0, 7)
        self.fight_table.setHorizontalHeaderLabels(["Fight", "副本/战斗", "层数", "结果", "用时", "装等", "专精"])
        return page


    def _build_personal_page(self) -> QWidget:
        page, outer = self._page_shell(
            "个人模型",
            "后台学习你最近限时成功的大秘境，只用于判断“这一把是否偏离你自己平时”。这里不是评分页，真正的改进建议统一放进 AI 战斗报告。",
        )
        controls = QHBoxLayout()
        self.personal_sync_count = QComboBox(); self.personal_sync_count.addItems(["5", "10", "20", "40"]); self.personal_sync_count.setCurrentText("10")
        self.personal_sync_btn = QPushButton("同步最近限时大秘境"); self.personal_sync_btn.clicked.connect(self._sync_personal_baseline)
        self.personal_refresh_btn = QPushButton("刷新基线"); self.personal_refresh_btn.clicked.connect(self._refresh_personal_page)
        self.season_target_btn = QPushButton("学习本赛季 BOSS / 重点怪"); self.season_target_btn.clicked.connect(self._learn_season_targets)
        self.season_target_btn.setEnabled(False)
        controls.addWidget(QLabel("本次最多新增")); controls.addWidget(self.personal_sync_count); controls.addWidget(QLabel("场")); controls.addWidget(self.personal_sync_btn); controls.addWidget(self.season_target_btn); controls.addWidget(self.personal_refresh_btn); controls.addStretch()
        outer.addLayout(controls)
        self.season_target_status = QLabel("目标知识库：尚未学习。本功能会从 WCL 限时样本学习每个副本的 BOSS、常见优先集火目标和重要大怪。")
        self.season_target_status.setObjectName("Muted"); self.season_target_status.setWordWrap(True)
        outer.addWidget(self.season_target_status)
        self.personal_status = QLabel("先到“我的 WCL”查询你的角色，然后这里可以同步和查看个人长期基线。")
        self.personal_status.setWordWrap(True); self.personal_status.setStyleSheet("color:#777")
        outer.addWidget(self.personal_status)

        split = QSplitter(Qt.Vertical)
        self.personal_summary = QTextBrowser(); self.personal_summary.setOpenExternalLinks(False)
        split.addWidget(self.personal_summary)
        self.personal_table = QTableWidget(0, 9)
        self.personal_table.setHorizontalHeaderLabels(["日期","副本","层数","专精","版本","装等","DPS","响应分","Report / Fight"])
        self.personal_table.setEditTriggers(QTableWidget.NoEditTriggers); self.personal_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.personal_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        split.addWidget(self.personal_table); split.setSizes([300, 360])
        outer.addWidget(split, 1)
        return page

    def _build_local_page(self) -> QWidget:
        page, outer = self._page_shell("本地 Log", "保留本地 WoWCombatLog.txt 分析。可以导入整份日志，自动拆分 M+ Run。")
        bar = QHBoxLayout(); self.local_path = QLabel("尚未选择文件"); self.local_path.setStyleSheet("color:#777")
        btn = QPushButton("选择 Combat Log"); btn.clicked.connect(self._choose_local_log)
        bar.addWidget(self.local_path,1); bar.addWidget(btn); outer.addLayout(bar)
        self.local_run_combo = QComboBox(); self.local_run_combo.currentIndexChanged.connect(self._refresh_local_run)
        self.local_player_combo = QComboBox()
        row = QHBoxLayout(); row.addWidget(QLabel("Run")); row.addWidget(self.local_run_combo,2); row.addWidget(QLabel("目标玩家")); row.addWidget(self.local_player_combo,1)
        outer.addLayout(row)
        self.local_table = QTableWidget(0, 8)
        self.local_table.setHorizontalHeaderLabels(["玩家","DPS","伤害","HPS","承伤","Cast","打断","死亡"])
        self.local_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.local_table.setEditTriggers(QTableWidget.NoEditTriggers)
        outer.addWidget(self.local_table,1)
        self.local_ai_btn = QPushButton("AI 分析当前 Run / 玩家"); self.local_ai_btn.clicked.connect(self._analyze_local); self.local_ai_btn.setEnabled(False)
        outer.addWidget(self.local_ai_btn)
        return page

    def _build_report_page(self) -> QWidget:
        page, outer = self._page_shell("AI 战斗报告", "数据层先计算，DeepSeek 再做分析与中文报告编辑。默认只展示玩家能理解的结论，不直接堆统计术语。")
        meta_card, meta_layout = self._card()
        self.report_meta = QLabel("还没有生成报告。")
        self.report_meta.setWordWrap(True); self.report_meta.setObjectName("Muted")
        meta_layout.addWidget(self.report_meta)
        outer.addWidget(meta_card)

        self.target_chart_card, target_chart_layout = self._card()
        target_title = QLabel("BOSS / 优先目标伤害对比"); target_title.setObjectName("SectionTitle")
        target_hint = QLabel("柱状图比较当前场次与同副本、相近层数同专精参考玩家在重要目标上的伤害占比。重要大怪来自多场 WCL 行为学习，不把经验模式冒充官方机制。")
        target_hint.setObjectName("Muted"); target_hint.setWordWrap(True)
        target_chart_layout.addWidget(target_title); target_chart_layout.addWidget(target_hint)
        self.target_chart = TargetComparisonChart()
        target_chart_layout.addWidget(self.target_chart)
        self.target_chart_card.setVisible(False)
        outer.addWidget(self.target_chart_card)

        self.report_browser = QTextBrowser(); self.report_browser.setOpenExternalLinks(True)
        self.report_browser.setStyleSheet("QTextBrowser{font-size:16px; padding:30px 34px;}")
        self.report_browser.document().setDefaultStyleSheet("""
            h1 { font-size: 27px; margin: 4px 0 22px 0; color: #ffffff; }
            h2 { font-size: 20px; margin: 28px 0 14px 0; color: #ffffff; background-color: #192231; padding: 9px 12px; }
            h3 { font-size: 17px; margin: 20px 0 9px 0; color: #dce6f7; }
            p  { line-height: 175%; margin: 10px 0 13px 0; }
            li { line-height: 170%; margin: 7px 0; }
            blockquote { color: #b7c3d6; background-color:#171d27; margin: 14px 6px; padding: 10px 14px; }
            strong { color: #ffffff; }
        """)
        outer.addWidget(self.report_browser,1)
        row = QHBoxLayout(); row.addWidget(QLabel("这份分析是否符合实际："))
        for text, rating in (("准确",2),("部分准确",1),("不准确",-1)):
            b=QPushButton(text); b.clicked.connect(lambda _=False, r=rating: self._quick_feedback(r)); row.addWidget(b)
        row.addStretch(); outer.addLayout(row)
        return page

    def _build_settings_page(self) -> QWidget:
        page, outer = self._page_shell("设置", "API 凭据集中放这里。密钥优先保存在 macOS Keychain / Windows Credential Manager，不写进项目和 Log。")
        form = QFormLayout()
        self.ds_key = QLineEdit(); self.ds_key.setEchoMode(QLineEdit.Password)
        self.ds_model = QComboBox(); self.ds_model.addItems(["deepseek-flash", "deepseek-v4-pro"]); self.ds_model.setEditable(True)
        self.ai_speed = QComboBox(); self.ai_speed.addItems(["快速（推荐）", "平衡", "深度"]); self.ai_speed.setCurrentIndex(0)
        # Hidden compatibility object for older settings/tests. The new pipeline maps speed mode to reasoning internally.
        self.reasoning = QComboBox(); self.reasoning.addItems(["low","high","max","none"]); self.reasoning.setVisible(False)
        self.wcl_client_id = QLineEdit()
        self.wcl_secret = QLineEdit(); self.wcl_secret.setEchoMode(QLineEdit.Password)
        self.remember_secrets = QCheckBox("使用系统钥匙串保存 API 密钥"); self.remember_secrets.setChecked(True)
        self.auto_online_compare = QCheckBox("分析时自动联网补充同职业同专精公开参考样本（推荐）"); self.auto_online_compare.setChecked(True)
        form.addRow("DeepSeek API Key", self.ds_key); form.addRow("DeepSeek 模型", self.ds_model); form.addRow("AI 速度", self.ai_speed)
        form.addRow("WCL Client ID", self.wcl_client_id); form.addRow("WCL Client Secret", self.wcl_secret); form.addRow("", self.remember_secrets); form.addRow("", self.auto_online_compare)
        outer.addLayout(form)
        row = QHBoxLayout(); test = QPushButton("测试 WCL 连接"); test.clicked.connect(self._test_wcl); save = QPushButton("保存并立即生效"); save.setObjectName("PrimaryButton"); save.clicked.connect(self._save_settings)
        row.addWidget(test); row.addWidget(save); row.addStretch(); outer.addLayout(row); outer.addStretch()
        return page

    def _ai_speed_mode(self) -> str:
        text = self.ai_speed.currentText() if hasattr(self, "ai_speed") else "快速（推荐）"
        if "深度" in text:
            return "deep"
        if "平衡" in text:
            return "balanced"
        return "fast"

    def _change_page(self, row: int):
        self.pages.setCurrentIndex(max(0,row))

    def _update_action_states(self):
        if hasattr(self, "search_btn"):
            self.search_btn.setEnabled(bool(self.identity_input.text().strip()))
        self._update_connection_status()

    def _update_connection_status(self, wcl_connected: bool | None = None):
        cid = self.wcl_client_id.text().strip() if hasattr(self, "wcl_client_id") else str(self.settings.get("wcl_client_id") or "")
        secret = self.wcl_secret.text().strip() if hasattr(self, "wcl_secret") else get_secret("wcl_client_secret")
        ai = self.ds_key.text().strip() if hasattr(self, "ds_key") else get_secret("deepseek_api_key")
        if hasattr(self, "wcl_chip"):
            configured = bool(cid and secret)
            good = configured if wcl_connected is None else bool(wcl_connected)
            self.wcl_chip.setText("WCL  ● 已连接" if good else ("WCL  ◐ 已配置" if configured else "WCL  ○ 未配置"))
            self.wcl_chip.setStyleSheet("color:#62d394;" if good else "")
        if hasattr(self, "ai_chip"):
            self.ai_chip.setText("AI  ● 已配置" if ai else "AI  ○ 未配置")
            self.ai_chip.setStyleSheet("color:#62d394;" if ai else "")

    def _set_busy(self, busy: bool, message: str = ""):
        if hasattr(self, "search_btn"):
            self.search_btn.setEnabled((not busy) and bool(self.identity_input.text().strip()))
        if hasattr(self, "personal_sync_btn"):
            self.personal_sync_btn.setEnabled(not busy)
        if hasattr(self, "season_target_btn"):
            self.season_target_btn.setEnabled((not busy) and bool(self.timed_runs))
        if message: self.statusBar().showMessage(message)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def _run_async(self, fn: Callable, on_result: Callable[[Any],None], busy_message: str):
        self._set_busy(True,busy_message)
        w=Worker(fn)
        w.signals.result.connect(on_result)
        w.signals.error.connect(self._show_error)
        w.signals.finished.connect(lambda: self._set_busy(False,"就绪"))
        self.thread_pool.start(w)

    def _show_error(self, text: str):
        raw = str(text or "未知错误")
        msg = raw.split("\n", 1)[0]
        if "Variable" in raw and "never used" in raw:
            msg = "WCL 查询格式错误。请更新到最新版本。"
        elif "ReportMap" in raw and "Cannot query field" in raw:
            msg = "WCL 接口字段已变化，当前查询不兼容。请更新到最新版本。"
        elif "401" in raw or "Unauthorized" in raw:
            msg = "WCL 认证失败，请检查 Client ID / Client Secret。"
        elif "character" in raw.lower() and "null" in raw.lower():
            msg = "没有找到这个角色，请确认服务器、地区和角色名。"
        QMessageBox.critical(self,"操作失败",msg + "\n\n详细诊断已写入应用数据目录的 diagnostic.log。")

    def _wcl(self) -> WCLClient:
        cid = self.wcl_client_id.text().strip(); secret = self.wcl_secret.text().strip()
        if not cid or not secret:
            raise ValueError("请先到 设置 填入 WCL Client ID / Client Secret。")
        key = (cid, secret)
        if self._wcl_client_obj is None or self._wcl_client_key != key:
            if self._wcl_client_obj is not None:
                self._wcl_client_obj.close()
            self._wcl_client_obj = WCLClient(cid, secret)
            self._wcl_client_key = key
        return self._wcl_client_obj

    def closeEvent(self, event):
        try:
            if self._wcl_client_obj is not None:
                self._wcl_client_obj.close()
        finally:
            super().closeEvent(event)

    def _search_wcl_logs(self):
        mode=self.identity_mode.currentIndex(); raw=self.identity_input.text().strip()
        if not raw: self._show_error("请输入角色 ID / 角色链接，或 WCL 用户 ID。"); return
        def job():
            cli=self._wcl()
            if mode==0:
                loc=parse_character_locator(raw)
                data=cli.character_recent_reports(character_id=loc.character_id,name=loc.name,server_slug=loc.server_slug,server_region=loc.server_region,limit=30)
                char=((data.get("characterData") or {}).get("character") or {})
                if not char: raise ValueError("没有找到这个 WCL 角色。")
                return {"mode":"character","character":char,"reports":((char.get("recentReports") or {}).get("data") or []),"rate":data.get("rateLimitData") or {}}
            if not raw.isdigit(): raise ValueError("WCL 用户 ID 必须是数字。")
            data=cli.user_reports(int(raw),limit=30)
            return {"mode":"user","character":{},"reports":((((data.get("reportData") or {}).get("reports") or {}).get("data")) or []),"rate":data.get("rateLimitData") or {}}
        self._run_async(job,self._apply_reports,"正在读取最近 WCL Log...")

    def _apply_reports(self, result: dict[str,Any]):
        self.character_profile=result.get("character") or {}; self.reports=result.get("reports") or []
        self.report_table.setRowCount(len(self.reports))
        for r,rep in enumerate(self.reports):
            vals=[_fmt_ts(rep.get("startTime")),str(rep.get("title") or ""),str((rep.get("zone") or {}).get("name") or ""),str(rep.get("visibility") or ""),str(rep.get("code") or "")]
            for c,v in enumerate(vals): self.report_table.setItem(r,c,QTableWidgetItem(v))
        if self.character_profile:
            srv=self.character_profile.get("server") or {}
            self.wcl_status.setText(f"✓ 已找到：{self.character_profile.get('name')} · {srv.get('name','')} · 最近 {len(self.reports)} 份公开 Log")
            self.wcl_status.setStyleSheet("color:#62d394;font-weight:650;")
        else:
            self.wcl_status.setText(f"✓ 已读取 {len(self.reports)} 份该 WCL 用户的公开个人 Log。")
            self.wcl_status.setStyleSheet("color:#62d394;font-weight:650;")
        self._update_connection_status(True)
        if self.character_profile:
            self._refresh_personal_page()
            self._scan_recent_timed_runs()

    def _scan_recent_timed_runs(self):
        if not self.character_profile or not self.reports:
            self.timed_runs = []
            self.timed_table.setRowCount(0)
            return
        char = dict(self.character_profile); reports = list(self.reports[:30])
        def job():
            base_cli = self._wcl()
            srv = char.get("server") or {}
            cname = str(char.get("name") or "")
            sslug = str(srv.get("slug") or srv.get("name") or "")
            runs = []; failures = []
            def fetch_one(stub):
                code = str(stub.get("code") or "")
                if not code:
                    return [], ""
                try:
                    cli = base_cli.fork()
                    raw = cli.report_with_talents(code)
                    report = (((raw.get("reportData") or {}).get("report") or {}))
                    sid, player_name, class_name = resolve_report_actor(report, character_name=cname, server_slug=sslug)
                    if not sid:
                        return [], ""
                    rows = []
                    for row in timed_runs_from_report(report, sid, report_code=code):
                        row["player_name"] = player_name
                        row["class_name"] = class_name
                        rows.append(row)
                    return rows, ""
                except Exception as exc:
                    return [], f"{code}: {str(exc)[:120]}"
            # Report metadata calls are independent. A small pool cuts scan latency while
            # respecting WCL rate limits and keeping the GUI responsive.
            with ThreadPoolExecutor(max_workers=min(5, max(1, len(reports)))) as ex:
                futs = [ex.submit(fetch_one, stub) for stub in reports]
                for fut in as_completed(futs):
                    rows, err = fut.result()
                    runs.extend(rows)
                    if err:
                        failures.append(err)
            runs.sort(key=lambda r: float(r.get("absolute_start_time") or 0), reverse=True)
            return {"runs": runs[:80], "failures": failures}
        self._run_async(job, self._apply_timed_runs, "正在并行扫描最近公开记录，只整理限时成功的大秘境...")

    def _apply_timed_runs(self, result: dict[str, Any]):
        self.timed_runs = list(result.get("runs") or [])
        self.timed_table.setRowCount(len(self.timed_runs))
        for r, run in enumerate(self.timed_runs):
            bonus = int(run.get("keystone_bonus") or 0)
            vals = [
                _fmt_ts(run.get("absolute_start_time")),
                str(run.get("dungeon") or ""),
                f"+{int(run.get('key_level') or 0)}",
                f"限时 +{bonus}" if bonus > 0 else "限时",
                _fmt_ms(run.get("duration_ms")),
                str(run.get("spec_name") or ""),
                str(run.get("item_level") or ""),
                f"{run.get('report_code','')} / {run.get('fight_id','')}",
            ]
            for c, v in enumerate(vals):
                self.timed_table.setItem(r, c, QTableWidgetItem(v))
        self.timed_table.resizeColumnsToContents(); self.timed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        if hasattr(self, "season_target_btn"):
            self.season_target_btn.setEnabled(bool(self.timed_runs))
        failures = result.get("failures") or []
        if self.timed_runs:
            self.wcl_status.setText(f"✓ 已整理 {len(self.timed_runs)} 场最近限时成功大秘境。直接多选后分析即可。")
            self.wcl_status.setStyleSheet("color:#62d394;font-weight:650;")
        else:
            self.wcl_status.setText("已找到角色，但最近公开记录里没有识别到限时成功的大秘境。")
        if failures:
            self.statusBar().showMessage(f"已完成扫描；{len(failures)} 份 Report 读取失败，详情见 diagnostic.log")

    def _selected_timed_run_records(self) -> list[dict[str, Any]]:
        if not self.timed_table.selectionModel():
            return []
        rows = sorted({idx.row() for idx in self.timed_table.selectionModel().selectedRows()})
        return [self.timed_runs[r] for r in rows if 0 <= r < len(self.timed_runs)]

    def _timed_selection_changed(self):
        n = len(self._selected_timed_run_records())
        self.timed_analyze_btn.setEnabled(n > 0)
        self.timed_analyze_btn.setText(f"综合分析选中 {n} 场" if n > 1 else "分析选中场次")

    def _analyze_selected_timed_runs(self):
        records = self._selected_timed_run_records()
        if not records:
            self._show_error("请先选择至少一场限时大秘境。")
            return
        if len(records) > 10:
            self._show_error("一次最多综合分析 10 场。建议优先选择同副本、相近层数。")
            return
        if not self.character_profile:
            self._show_error("请先查询你的 WCL 角色。")
            return
        api_key = self.ds_key.text().strip(); model = self.ds_model.currentText().strip() or DEFAULT_MODEL
        reasoning = self.reasoning.currentText(); auto_online = self.auto_online_compare.isChecked(); char = dict(self.character_profile)

        def job():
            cli = self._wcl(); identity = character_identity(char)
            signatures=[]; sample_records=[]; failures=[]; online_refreshes=[]; refreshed_keys=set()
            # Multiple selected fights are independent.  Load them concurrently with small
            # per-thread WCL clients; each fight also parallelizes its own event streams.
            def load_one(rec):
                local_cli = cli.fork()
                result = build_targeted_wcl_payload(
                    local_cli, str(rec.get("report_code") or ""), int(rec.get("fight_id") or 0),
                    character_name=str(rec.get("player_name") or char.get("name") or ""),
                    source_id=int(rec.get("source_id") or 0),
                )
                return rec, result
            loaded=[]
            with ThreadPoolExecutor(max_workers=min(3, len(records))) as ex:
                futs={ex.submit(load_one, rec): rec for rec in records}
                for fut in as_completed(futs):
                    rec=futs[fut]
                    try:
                        loaded.append(fut.result())
                    except Exception as exc:
                        failures.append(f"{rec.get('report_code')} / {rec.get('fight_id')}: {str(exc)[:180]}")
            # Keep the user's selection order for multi-fight summaries.
            order={(str(r.get("report_code") or ""), int(r.get("fight_id") or 0)): i for i,r in enumerate(records)}
            loaded.sort(key=lambda pair: order.get((str(pair[0].get("report_code") or ""), int(pair[0].get("fight_id") or 0)), 999))
            for rec, result in loaded:
                sig = result["signature"]; signatures.append(sig)
                sample_records.append({"signature":sig,"report_code":rec.get("report_code"),"fight_id":rec.get("fight_id"),"source_id":rec.get("source_id")})
                ctx=sig.get("context") or {}; fight=result.get("fight") or {}
                key=(int(fight.get("encounterID") or 0),str(ctx.get("class_name") or ""),str(ctx.get("spec_name") or ""),int(ctx.get("key_level") or 0))
                if auto_online and key not in refreshed_keys and len(refreshed_keys) < 3:
                    refreshed_keys.add(key); online_refreshes.append(self._try_online_cohort_refresh(cli,result,sig,True))
            if not signatures:
                raise RuntimeError("选中的场次都没有成功读取。")

            # Compare against history first; only then teach the current batch back.
            profiles=[best_personal_profile_for_signature(identity["character_key"],sig) for sig in signatures]
            personal=[compare_to_personal_profile(sig,pf) for sig,pf in zip(signatures,profiles)]
            for rec in sample_records:
                save_personal_sample(identity,rec["signature"],metadata={"source":"timed_mplus_batch","report_code":rec["report_code"],"fight_id":rec["fight_id"]})

            batch=build_multi_fight_payload(signatures,personal) if len(signatures)>1 else signatures[0]
            first_ctx=signatures[0].get("context") or {}
            classes={str((x.get("context") or {}).get("class_name") or "") for x in signatures if (x.get("context") or {}).get("class_name")}
            specs={str((x.get("context") or {}).get("spec_name") or "") for x in signatures if (x.get("context") or {}).get("spec_name")}
            patches={str((x.get("context") or {}).get("patch_scope") or "") for x in signatures if (x.get("context") or {}).get("patch_scope")}
            common_class=next(iter(classes)) if len(classes)==1 else ""
            common_spec=next(iter(specs)) if len(specs)==1 else ""
            dungeons={str((x.get("context") or {}).get("dungeon") or "") for x in signatures}; dungeon=next(iter(dungeons)) if len(dungeons)==1 else ""
            keys=[int((x.get("context") or {}).get("key_level") or 0) for x in signatures if int((x.get("context") or {}).get("key_level") or 0)]
            key_level=round(sum(keys)/len(keys)) if keys and dungeon else 0
            patch_scope=next(iter(patches)) if len(patches)==1 else ""
            context={"analysis_mode":"timed_mplus_selection","player":str(char.get("name") or ""),"class_name":common_class,"spec_name":common_spec,"dungeon":dungeon,"key_level":key_level,"patch_scope":patch_scope,"character_key":identity["character_key"],"fight_count":len(signatures),"selection_policy":"仅限时成功大秘境"}
            if not api_key:
                for rec in sample_records:
                    save_knowledge_sample(rec["signature"],source="wcl_direct",sample_role="auto",metadata={"report_code":rec["report_code"],"fight_id":rec["fight_id"],"source_id":rec["source_id"],"timed_success":True})
                return {"kind":"batch_local" if len(signatures)>1 else "local","signature":batch,"context":context,"fight_count":len(signatures),"failures":failures,"online_refreshes":online_refreshes}
            memory=memory_context_for_ai(retrieve_similar_cases(context,batch,limit=6,require_feedback=True),retrieve_lessons(context,limit=10))
            grouped_knowledge=knowledge_context_for_signatures(signatures) if common_class and common_spec else {"cohort_groups":[]}
            if len(grouped_knowledge.get("cohort_groups") or []) == 1:
                knowledge=dict((grouped_knowledge.get("cohort_groups") or [])[0].get("cohort") or {})
                knowledge["cohort_groups"]=grouped_knowledge.get("cohort_groups") or []
                knowledge["group_policy"]=grouped_knowledge.get("policy") or ""
            else:
                knowledge={"policy":grouped_knowledge.get("policy") or "","cohort_groups":grouped_knowledge.get("cohort_groups") or []}
            knowledge["online_reference_evidence"]=online_context_for_analysis(common_class,common_spec,limit=10) if common_class and common_spec else {}
            knowledge["online_refresh_this_analysis"]=online_refreshes
            target_rows=[]; target_all=[]; target_ref_count=0
            cohort_groups = grouped_knowledge.get("cohort_groups") or []
            for g in cohort_groups:
                cmp = ((g.get("cohort") or {}).get("target_focus_comparison") or {})
                target_ref_count += int(cmp.get("reference_sample_count") or 0)
                dungeon_name = str(g.get("dungeon") or "")
                for row in (cmp.get("rows") or []):
                    rr=dict(row)
                    if len(cohort_groups) > 1 and dungeon_name:
                        rr["npc_name"] = f"{dungeon_name} · {rr.get('npc_name','')}"
                    target_all.append(rr)
                for row in (cmp.get("chart_rows") or []):
                    rr=dict(row)
                    if len(cohort_groups) > 1 and dungeon_name:
                        rr["npc_name"] = f"{dungeon_name} · {rr.get('npc_name','')}"
                    target_rows.append(rr)
            target_focus_comparison={"reference_sample_count":target_ref_count,"rows":target_all[:18],"chart_rows":target_rows[:8]}
            knowledge["target_focus_comparison_summary"] = target_focus_comparison
            cohort_count=sum(int((((g.get("cohort") or {}).get("empirical_profile") or {}).get("sample_count")) or 0) for g in (grouped_knowledge.get("cohort_groups") or []))
            group_matches=[((g.get("cohort") or {}).get("cohort_match_details") or {}) for g in (grouped_knowledge.get("cohort_groups") or [])]
            source_summary={"mode":"timed_mplus_selection","fight_count":len(signatures),"context":context,"cohort_sample_count":cohort_count,"cohort_group_matches":group_matches,"online_refresh":online_refreshes,"failed_runs":len(failures),"selection_policy":"最近公开、限时成功的大秘境","personal_model_scopes":[p.get("scope","") for p in profiles],"personal_model_sample_counts":[int(p.get("sample_count") or 0) for p in profiles]}
            # Freeze the cohort first, then teach the selected fights back. This prevents
            # the current run from improving its own benchmark.
            for rec in sample_records:
                save_knowledge_sample(rec["signature"],source="wcl_direct",sample_role="auto",metadata={"report_code":rec["report_code"],"fight_id":rec["fight_id"],"source_id":rec["source_id"],"timed_success":True})
            ai,md,ai_meta=run_coach_pipeline(api_key,batch,model=model,source_summary=source_summary,memory_context=memory,knowledge_context=knowledge,speed_mode=self._ai_speed_mode())
            case=save_case(context=context,payload=batch,report_markdown=md,report_json=ai,model=model,metadata={"source":"timed_mplus_selection","selected_runs":[f"{r.get('report_code')}/{r.get('fight_id')}" for r in records],"failures":failures,"online_refresh":online_refreshes})
            return {"kind":"ai","signature":batch,"context":context,"ai":ai,"markdown":md,"case_id":case,"fight_count":len(signatures),"failures":failures,"online_refreshes":online_refreshes,"cohort_sample_count":cohort_count,"cohort_group_matches":group_matches,"target_focus_comparison":target_focus_comparison,"ai_meta":ai_meta}

        self._run_async(job,self._apply_analysis,f"正在读取 {len(records)} 场限时大秘境 → 横向同专精样本 → AI 教练分析 → 中文报告...")

    def _personal_profile_markdown(self, profile: dict[str, Any], identity: dict[str, Any]) -> str:
        n = int(profile.get("sample_count") or 0)
        lines = [f"# {identity.get('display') or '个人模型'}", ""]
        if not n:
            lines += ["还没有可用的个人历史。同步后，个人模型只在后台帮助 AI 判断“这一把是不是偏离你自己平时”，不会拿个人习惯当成正确打法。"]
            return "\n".join(lines)
        ctx = profile.get("context") or {}; resp = profile.get("responsiveness") or {}
        lines += [
            f"已学习 **{n} 场限时大秘境**。个人模型主要在后台工作，页面只保留真正有用的状态。",
            "",
            "## 这个模型现在能做什么",
            "- 判断某一场是否明显偏离你自己平时的技能节奏。",
            "- 把“你一直这样打”与“这一把突然异常”分开。",
            "- 配合同专精横向样本，识别长期习惯是否和优秀参考玩家存在稳定差距。",
        ]
        dungeons = ctx.get("dungeons") or []
        if dungeons:
            top = "、".join(str(x[0]) for x in dungeons[:4] if x and x[0])
            if top:
                lines += ["", "## 当前历史覆盖", f"主要包含：{top}。分析同副本时会优先使用更接近的历史，不会把所有副本硬混在一起。"]
        if resp:
            maxgap = resp.get("max_gap_s") or {}
            if maxgap:
                typical = float(maxgap.get("p50") or 0)
                rare = float(maxgap.get("p90") or 0)
                lines += ["", "## 响应性基线", f"你在正常战斗中较长的停手通常约 **{typical:.1f} 秒**；明显少见的长停手大约从 **{rare:.1f} 秒**开始。死亡期间的空档不会计入异常停手。"]
        lines += ["", "> 这里不是评分页。真正的“哪里该改”会放到 AI 战斗报告里，并同时和同职业同专精玩家做横向比较。"]
        return "\n".join(lines)


    def _refresh_personal_page(self):
        if not self.character_profile:
            self.personal_status.setText("先到“我的 WCL”查询你的角色，然后这里可以同步和查看个人长期模型。")
            self.personal_summary.setMarkdown("# 个人模型\n\n尚未选择 WCL 角色。")
            self.personal_table.setRowCount(0)
            return
        identity = character_identity(self.character_profile)
        rows = list_personal_samples(identity["character_key"], limit=300)
        profile = build_personal_profile(rows)
        self.current_personal_profile = profile
        self.personal_status.setText(f"当前角色：{identity.get('display','')} · 已学习 {len(rows)} 场限时大秘境。同步只下载新场次。")
        cached = latest_personal_model_summary(identity["character_key"])
        if cached and int(cached.get("sample_count") or 0) == int(profile.get("sample_count") or len(rows)):
            self.personal_summary.setMarkdown(str(cached.get("summary_markdown") or self._personal_profile_markdown(profile, identity)))
        else:
            self.personal_summary.setMarkdown(self._personal_profile_markdown(profile, identity))
        self.personal_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            try: sig=json.loads(row.get("signature_json") or "{}")
            except Exception: sig={}
            resp=(sig.get("responsiveness") or {})
            resp_score=resp.get("score",0)
            vals=[str(row.get("created_at") or "")[:16].replace("T"," "),str(row.get("dungeon") or ""),str(row.get("key_level") or ""),str(row.get("spec_name") or ""),str(row.get("patch_scope") or ""),f"{float(row.get('player_item_level') or 0):.1f}" if row.get("player_item_level") else "",f"{float(row.get('performance_value') or 0):,.0f}",f"{float(resp_score or 0):.1f}",f"{row.get('report_code','')} / {row.get('fight_id','')}"]
            for c,v in enumerate(vals): self.personal_table.setItem(r,c,QTableWidgetItem(v))
        self.personal_table.resizeColumnsToContents(); self.personal_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)

    def _refresh_personal_ai_summary_async(self, force: bool = False):
        if not self.character_profile:
            return
        api_key = self.ds_key.text().strip()
        if not api_key:
            return
        identity = character_identity(self.character_profile)
        rows = list_personal_samples(identity["character_key"], limit=300)
        profile = build_personal_profile(rows)
        sample_count = int(profile.get("sample_count") or len(rows))
        if sample_count < 3:
            return
        cached = latest_personal_model_summary(identity["character_key"])
        if not force and cached and int(cached.get("sample_count") or 0) == sample_count:
            return
        model = self.ds_model.currentText().strip() or DEFAULT_MODEL
        def job():
            ctx = profile.get("context") or {}
            specs = ctx.get("specs") or []
            spec = str(specs[0][0]) if specs and specs[0] else ""
            # Class is not aggregated in older personal profiles; infer it from stored samples when possible.
            cls = ""
            for row in rows:
                try:
                    rsig = json.loads(row.get("signature_json") or "{}")
                except Exception:
                    rsig = {}
                cls = str((rsig.get("context") or {}).get("class_name") or "")
                if cls:
                    break
            # Prefer the most represented dungeon and typical key so the summary can compare
            # this player's habits to a same-type cohort instead of summarizing in isolation.
            dungeons = ctx.get("dungeons") or []
            dungeon = str(dungeons[0][0]) if dungeons and dungeons[0] else ""
            keys = ctx.get("key_level") or {}
            key_level = int(float(keys.get("p50") or 0)) if isinstance(keys, dict) else 0
            patches = ctx.get("patches") or []
            patch = str(patches[0][0]) if patches and patches[0] else ""
            cohort = knowledge_context_for_analysis(cls, spec, dungeon, key_level, patch_scope=patch) if cls and spec else {}
            md = summarize_personal_model_with_deepseek(api_key, profile, cohort, model=model)
            save_personal_model_summary(identity["character_key"], sample_count, md, model=model, scope=str((cohort.get("cohort_match_details") or {}).get("scope") or ""))
            return {"markdown": md, "sample_count": sample_count}
        def done(result):
            # Only apply if the user has not switched characters while AI was working.
            if self.character_profile and character_identity(self.character_profile)["character_key"] == identity["character_key"]:
                self.personal_summary.setMarkdown(str(result.get("markdown") or ""))
                self.personal_status.setText(self.personal_status.text() + " · AI 已总结长期模式")
        self._run_async(job, done, "正在后台总结个人模型，不影响其他操作...")

    def _learn_season_targets(self):
        if not self.character_profile or not self.timed_runs:
            self._show_error("请先在‘我的战斗’查询角色，等最近限时大秘境列表加载完成后再学习本赛季目标。")
            return
        class_name, spec_name = dominant_class_spec(self.timed_runs)
        if not class_name or not spec_name:
            self._show_error("最近限时场次里没有识别到职业/专精，暂时无法建立同专精赛季目标库。")
            return
        answer = QMessageBox.question(
            self,
            "学习本赛季重要目标",
            f"将使用 WCL 当前赛季目录，为 {class_name} / {spec_name} 下载各副本少量限时样本，学习 BOSS、常见优先集火目标和重要大怪。\n\n"
            "第一次运行可能消耗较多 WCL API points；已经学过的 schema-v5 样本会直接复用。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        runs = list(self.timed_runs)
        self.season_target_btn.setEnabled(False)

        def job():
            cli = self._wcl()
            return learn_current_season_targets(
                cli, class_name=class_name, spec_name=spec_name, fallback_runs=runs,
                samples_per_dungeon=5, max_dungeons=12,
            )

        def done(result: dict[str, Any]):
            self.season_target_btn.setEnabled(bool(self.timed_runs))
            total = int(result.get("dungeon_count") or 0)
            imported = int(result.get("imported") or 0)
            skipped = int(result.get("skipped") or 0)
            failed = int(result.get("failed") or 0)
            complete = bool(result.get("catalog_complete"))
            source_text = "WCL 当前赛季目录" if complete else "最近限时场次回退目录（未确认完整赛季）"
            learned_bosses = sum(int(r.get("boss_count") or 0) for r in (result.get("results") or []))
            learned_priority = sum(int(r.get("priority_count") or 0) for r in (result.get("results") or []))
            learned_important = sum(int(r.get("important_count") or 0) for r in (result.get("results") or []))
            self.season_target_status.setText(
                f"目标知识库：{source_text} · {class_name}/{spec_name} · 扫描 {total} 个副本 · 新增 {imported} 场参考 · "
                f"已识别 BOSS {learned_bosses} 个 / 常见优先集火 {learned_priority} 个 / 重要大怪 {learned_important} 个 · 失败 {failed}。"
                "报告中的‘优先目标’是多场WCL经验规律，不冒充官方固定击杀顺序。"
            )
            rate = result.get("rate_limit") or {}
            rate_text = ""
            if rate:
                rate_text = f"\nWCL API：{rate.get('pointsSpentThisHour', 0)} / {rate.get('limitPerHour', 0)} points used this hour"
            rows = result.get("results") or []
            bad = [r for r in rows if r.get("status") == "failed"]
            extra = ""
            if bad:
                extra = "\n\n部分失败：\n" + "\n".join(f"{r.get('dungeon')}: {(r.get('errors') or ['未知错误'])[0]}" for r in bad[:5])
            QMessageBox.information(
                self, "赛季目标学习完成",
                f"已处理 {total} 个副本。\n新增参考样本：{imported}\n复用/跳过：{skipped}\n失败：{failed}{rate_text}{extra}"
            )
            self._refresh_personal_page()

        self._run_async(job, done, "正在从 WCL 学习本赛季各副本 BOSS / 优先目标；首次运行可能需要几分钟...")

    def _sync_personal_baseline(self):
        if not self.character_profile:
            self._show_error("请先在“我的 WCL”查询你的角色。")
            return
        if not self.reports and not self.timed_runs:
            self._show_error("当前没有可同步的最近 WCL Log。")
            return
        try: count=int(self.personal_sync_count.currentText())
        except Exception: count=10
        profile=dict(self.character_profile); reports=list(self.reports); runs=list(self.timed_runs)
        self.personal_sync_btn.setEnabled(False)
        def job():
            if runs:
                return sync_timed_wcl_personal_baseline(self._wcl(), profile, runs, max_new_fights=count).as_dict()
            return sync_recent_wcl_personal_baseline(self._wcl(), profile, reports, max_new_fights=count).as_dict()
        def done(result: dict[str,Any]):
            self.personal_sync_btn.setEnabled(True)
            self._refresh_personal_page()
            self._refresh_personal_ai_summary_async(force=bool(result.get("imported")))
            rate=result.get("rate_limit") or {}
            msg=(f"个人模型同步完成。\n新增：{result.get('imported',0)} 场\n已存在直接跳过：{result.get('skipped_existing',0)} 场\n实际读取 Report：{result.get('reports_scanned',0)}\n失败：{result.get('failed',0)}")
            if rate: msg += f"\nWCL API：{rate.get('pointsSpentThisHour','?')} / {rate.get('limitPerHour','?')} points used this hour"
            if result.get("errors"): msg += "\n\n部分错误：\n" + "\n".join(result.get("errors")[:5])
            msg += "\n\n个人模型的 AI 长期总结会在后台自动更新，不需要等待弹窗。"
            QMessageBox.information(self,"个人模型",msg)
        def failed(msg):
            self.personal_sync_btn.setEnabled(True)
            self._show_error(msg)
        worker=Worker(job); worker.signals.result.connect(done); worker.signals.error.connect(failed); self.thread_pool.start(worker)
        self.statusBar().showMessage(f"正在增量同步最多 {count} 场新个人样本；已学过的场次不会重复下载...")

    def _selected_report_codes(self) -> list[str]:
        rows = sorted({idx.row() for idx in self.report_table.selectionModel().selectedRows()}) if self.report_table.selectionModel() else []
        out = []
        for row in rows:
            item = self.report_table.item(row, 4)
            if item and item.text().strip():
                out.append(item.text().strip())
        return out

    def _selected_report_code(self) -> str:
        codes = self._selected_report_codes()
        if codes:
            return codes[0]
        row=self.report_table.currentRow(); return self.report_table.item(row,4).text() if row>=0 and self.report_table.item(row,4) else ""

    def _reports_selection_changed(self):
        count = len(self._selected_report_codes())
        self.load_report_btn.setEnabled(count == 1)
        self.batch_analyze_btn.setEnabled(count >= 1)
        self.batch_analyze_btn.setText(f"综合分析选中 Log（{count}）" if count > 1 else "分析选中 Log")

    @staticmethod
    def _primary_fight_for_source(report: dict[str, Any], source_id: int) -> dict[str, Any]:
        fights = fights_for_source(report, source_id)
        if not fights:
            return {}
        meaningful = [f for f in fights if float(f.get("endTime") or 0) - float(f.get("startTime") or 0) >= 30000]
        if meaningful:
            fights = meaningful
        mplus = [f for f in fights if int(f.get("keystoneLevel") or 0) > 0]
        pool = mplus or fights
        return max(pool, key=lambda f: (int(f.get("keystoneLevel") or 0), float(f.get("endTime") or 0) - float(f.get("startTime") or 0)))

    def _load_selected_report(self):
        code=self._selected_report_code()
        if not code: return
        self._run_async(lambda:self._wcl().report_with_talents(code),self._apply_report,"正在加载这份 Log 的战斗列表...")

    def _apply_report(self, raw: dict[str,Any]):
        report=(((raw.get("reportData") or {}).get("report") or {})); self.current_report=report
        actors=[a for a in ((report.get("masterData") or {}).get("actors") or []) if str(a.get("type") or "").lower()=="player"]
        self.player_combo.blockSignals(True); self.player_combo.clear()
        for a in actors: self.player_combo.addItem(f"{a.get('name')} · {a.get('subType','')}",int(a.get("id") or 0))
        self.player_combo.blockSignals(False)
        if self.character_profile:
            srv=self.character_profile.get("server") or {}
            sid,name,cls=resolve_report_actor(report,character_name=str(self.character_profile.get("name") or ""),server_slug=str(srv.get("slug") or srv.get("name") or ""))
            self.current_source_id=sid; self.current_player_name=name; self.current_class_name=cls; self.player_combo.setVisible(False)
            fights=fights_for_source(report,sid)
        else:
            self.player_combo.setVisible(True); self._player_changed(); fights=fights_for_source(report,self.current_source_id) if self.current_source_id else (report.get("fights") or [])
        self._fill_fights(fights)

    def _player_changed(self):
        self.current_source_id=int(self.player_combo.currentData() or 0)
        txt=self.player_combo.currentText(); self.current_player_name=txt.split(" · ",1)[0] if txt else ""
        if self.current_report and self.current_source_id: self._fill_fights(fights_for_source(self.current_report,self.current_source_id))

    def _fill_fights(self, fights: list[dict[str,Any]]):
        self.fight_table.setRowCount(len(fights))
        for r,f in enumerate(fights):
            spec, ilvl=spec_and_item_level_for_fight(f,self.current_source_id) if self.current_source_id else ("",0)
            vals=[str(f.get("id") or ""),str(f.get("name") or ""),str(f.get("keystoneLevel") or ""),"完成" if f.get("kill") else "未完成",_fmt_ms((f.get("endTime") or 0)-(f.get("startTime") or 0)),str(ilvl or f.get("averageItemLevel") or ""),spec]
            for c,v in enumerate(vals): self.fight_table.setItem(r,c,QTableWidgetItem(v))
        self.fight_table.resizeColumnsToContents(); self.fight_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)

    def _selected_fight_id(self) -> int:
        row=self.fight_table.currentRow();
        if row<0 or not self.fight_table.item(row,0): return 0
        try:return int(self.fight_table.item(row,0).text())
        except Exception:return 0

    def _try_online_cohort_refresh(self, cli: WCLClient, result: dict[str, Any], sig: dict[str, Any], enabled: bool) -> dict[str, Any]:
        if not enabled:
            return {"status": "disabled"}
        fight = result.get("fight") or {}
        ctx = sig.get("context") or {}
        encounter_id = int(fight.get("encounterID") or 0)
        class_name = str(ctx.get("class_name") or result.get("class_name") or "")
        spec_name = str(ctx.get("spec_name") or result.get("spec_name") or "")
        key_level = int(ctx.get("key_level") or fight.get("keystoneLevel") or 0)
        if not encounter_id or not class_name or not spec_name:
            return {"status": "skipped", "reason": "当前战斗缺少可用于 WCL 横向排名采样的 Encounter / 职业 / 专精信息。"}
        target_context = {**ctx, "duration_s": float((sig.get("overview") or {}).get("duration_s") or 0)}
        # Reuse a sufficiently large, fresh local cohort. Blocking on WCL discovery every time
        # made reports much slower while adding little information on repeat analyses.
        existing = knowledge_context_for_analysis(
            class_name, spec_name, str(ctx.get("dungeon") or ""), key_level,
            patch_scope=str(ctx.get("patch_scope") or ""), target_context=target_context,
        )
        details = existing.get("cohort_match_details") or {}
        count = int(details.get("sample_count") or ((existing.get("empirical_profile") or {}).get("sample_count") or 0))
        latest = str(details.get("latest_sample_at") or "")
        fresh = False
        if latest:
            try:
                ts = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
                fresh = (now - ts).total_seconds() < 24 * 3600
            except Exception:
                pass
        if count >= 6 and fresh:
            return {"status": "cached_enough", "sample_count": count, "scope": details.get("scope") or existing.get("cohort_match_scope") or ""}
        want = max(3, min(6, 6 - count)) if count < 6 else 3
        try:
            learned = learn_from_wcl_rankings(
                cli,
                encounter_id=encounter_id,
                class_name=class_name,
                spec_name=spec_name,
                bracket=key_level or None,
                pages=3,
                sample_limit=want,
                evidence_level="standard",
                target_context=target_context,
            )
            return {"status": "ok", "existing_before": count, **learned}
        except Exception as exc:
            return {"status": "failed", "existing_before": count, "reason": str(exc)[:500]}

    def _analyze_selected_wcl_fight(self):
        code=self.current_report.get("code") or self._selected_report_code(); fid=self._selected_fight_id()
        if not code or not fid or not self.current_source_id: self._show_error("请先选择 Log、目标角色和 Fight。"); return
        api_key = self.ds_key.text().strip()
        model = self.ds_model.currentText().strip() or DEFAULT_MODEL
        reasoning = self.reasoning.currentText()
        auto_online = self.auto_online_compare.isChecked()
        player_name = self.current_player_name
        source_id = self.current_source_id
        def job():
            cli=self._wcl(); result=build_targeted_wcl_payload(cli,code,fid,character_name=player_name,source_id=source_id)
            sig=result["signature"]
            context=sig.get("context") or {}; context={**context,"analysis_mode":"selected_wcl_fight"}
            personal_profile={}; personal_comparison={}; personal_ai={}
            if self.character_profile:
                identity=character_identity(self.character_profile)
                personal_profile=best_personal_profile_for_signature(identity["character_key"],sig)
                personal_comparison=compare_to_personal_profile(sig,personal_profile)
                personal_ai=personal_context_for_ai(personal_profile,personal_comparison)
                context={**context,"character_key":identity["character_key"],"personal_baseline_scope":personal_profile.get("scope","")}
                save_personal_sample(identity,sig,metadata={"source":"selected_wcl_fight","report_code":code,"fight_id":fid})
            online_refresh = self._try_online_cohort_refresh(cli, result, sig, auto_online)
            if not api_key:
                save_knowledge_sample(sig,source="wcl_direct",sample_role="auto",metadata={"report_code":code,"fight_id":fid,"source_id":source_id})
                return {"kind":"local","signature":sig,"context":context,"personal_profile":personal_profile,"personal_comparison":personal_comparison,"online_refresh":online_refresh}
            memory=memory_context_for_ai(retrieve_similar_cases(context,sig,limit=5,require_feedback=True),retrieve_lessons(context,limit=8))
            target_context={**context,"duration_s":float((sig.get("overview") or {}).get("duration_s") or 0),"pull_count":int(((sig.get("context") or {}).get("pull_count")) or 0)}
            knowledge=knowledge_context_for_analysis(str(context.get("class_name") or ""),str(context.get("spec_name") or ""),str(context.get("dungeon") or ""),int(context.get("key_level") or 0),patch_scope=str(context.get("patch_scope") or ""),target_context=target_context)
            target_focus_comparison = compare_signature_target_focus(
                sig,
                ((knowledge.get("empirical_profile") or {}).get("target_focus") or {}),
                knowledge.get("dungeon_target_knowledge") or {},
            )
            knowledge["target_focus_comparison"] = target_focus_comparison
            knowledge["online_reference_evidence"]=online_context_for_analysis(str(context.get("class_name") or ""),str(context.get("spec_name") or ""),limit=8)
            knowledge["online_refresh_this_analysis"] = online_refresh
            if personal_ai: knowledge["personal_behavior_model"]=personal_ai
            cohort_count = int((((knowledge.get("empirical_profile") or {}).get("sample_count")) or 0))
            source_summary={"mode":"single","fight_count":1,"context":context,"cohort_sample_count":cohort_count,"cohort_match_details":knowledge.get("cohort_match_details") or {},"online_refresh":online_refresh,"personal_sample_count":int(personal_profile.get("sample_count") or 0),"personal_match_scope":personal_profile.get("scope","")}
            save_knowledge_sample(sig,source="wcl_direct",sample_role="auto",metadata={"report_code":code,"fight_id":fid,"source_id":source_id})
            ai,md,ai_meta=run_coach_pipeline(api_key,sig,model=model,source_summary=source_summary,memory_context=memory,knowledge_context=knowledge,speed_mode=self._ai_speed_mode())
            case=save_case(context=context,payload=sig,report_markdown=md,report_json=ai,model=model,metadata={"source":"wcl_direct","personal_anomaly":personal_comparison,"online_refresh":online_refresh})
            return {"kind":"ai","signature":sig,"context":context,"ai":ai,"markdown":md,"case_id":case,"personal_profile":personal_profile,"personal_comparison":personal_comparison,"online_refresh":online_refresh,"cohort_sample_count":cohort_count,"cohort_match_details":knowledge.get("cohort_match_details") or {},"target_focus_comparison":target_focus_comparison,"fight_count":1,"ai_meta":ai_meta}
        self._run_async(job,self._apply_analysis,"正在读取战斗数据 → 横向样本 → AI 分析 → 中文报告整理...")

    def _analyze_selected_reports(self):
        codes = self._selected_report_codes()
        if not codes:
            self._show_error("请先选择至少一份 Log。")
            return
        if not self.character_profile:
            self._show_error("多场综合分析需要先通过角色页面/角色 ID 找到你的角色。")
            return
        if len(codes) > 10:
            self._show_error("一次最多综合分析 10 份 Log。请减少选择数量，避免 WCL API 请求过多。")
            return
        api_key = self.ds_key.text().strip()
        model = self.ds_model.currentText().strip() or DEFAULT_MODEL
        reasoning = self.reasoning.currentText()
        auto_online = self.auto_online_compare.isChecked()
        char = dict(self.character_profile)
        def job():
            cli = self._wcl()
            srv = char.get("server") or {}
            cname = str(char.get("name") or "")
            sslug = str(srv.get("slug") or srv.get("name") or "")
            identity = character_identity(char)
            signatures: list[dict[str, Any]] = []
            personal_comparisons: list[dict[str, Any]] = []
            sample_records: list[dict[str, Any]] = []
            imported_contexts: list[dict[str, Any]] = []
            online_refreshes: list[dict[str, Any]] = []
            failures: list[str] = []
            refreshed_keys = set()
            for code in codes:
                try:
                    raw = cli.report_with_talents(code)
                    report = (((raw.get("reportData") or {}).get("report") or {}))
                    sid, player_name, _cls = resolve_report_actor(report,character_name=cname,server_slug=sslug)
                    if not sid:
                        failures.append(f"{code}: 找不到目标角色")
                        continue
                    fight = self._primary_fight_for_source(report, sid)
                    if not fight:
                        failures.append(f"{code}: 没有找到可分析的战斗")
                        continue
                    fid = int(fight.get("id") or 0)
                    result = build_targeted_wcl_payload(cli,code,fid,character_name=player_name,source_id=sid)
                    sig = result["signature"]
                    signatures.append(sig)
                    ctx = sig.get("context") or {}
                    imported_contexts.append(ctx)
                    sample_records.append({"signature": sig, "report_code": code, "fight_id": fid, "source_id": sid})
                    key=(int((result.get("fight") or {}).get("encounterID") or 0),str(ctx.get("class_name") or ""),str(ctx.get("spec_name") or ""),int(ctx.get("key_level") or 0))
                    if auto_online and key not in refreshed_keys and len(refreshed_keys) < 2:
                        refreshed_keys.add(key)
                        online_refreshes.append(self._try_online_cohort_refresh(cli,result,sig,True))
                except Exception as exc:
                    failures.append(f"{code}: {str(exc)[:180]}")
            if not signatures:
                raise RuntimeError("选中的 Log 都没有成功读取。" + ("\n" + "\n".join(failures[:3]) if failures else ""))
            # Compare every selected fight against the same pre-batch personal history.
            # Only after all comparisons are finished do we teach the current batch back to the databases.
            profiles = [best_personal_profile_for_signature(identity["character_key"], sig) for sig in signatures]
            personal_comparisons = [compare_to_personal_profile(sig, profile) for sig, profile in zip(signatures, profiles)]
            for rec in sample_records:
                save_personal_sample(identity, rec["signature"], metadata={"source":"multi_wcl_fights","report_code":rec["report_code"],"fight_id":rec["fight_id"]})
            batch = build_multi_fight_payload(signatures, personal_comparisons)
            first_ctx = signatures[0].get("context") or {}
            common_spec = str(first_ctx.get("spec_name") or "")
            common_class = str(first_ctx.get("class_name") or "")
            dungeons = {str((x.get("context") or {}).get("dungeon") or "") for x in signatures}
            dungeon = next(iter(dungeons)) if len(dungeons) == 1 else ""
            key_levels = [int((x.get("context") or {}).get("key_level") or 0) for x in signatures if int((x.get("context") or {}).get("key_level") or 0)]
            key_level = round(sum(key_levels)/len(key_levels)) if key_levels and dungeon else 0
            context={"analysis_mode":"multi_wcl_fights","player":cname,"class_name":common_class,"spec_name":common_spec,"dungeon":dungeon,"key_level":key_level,"character_key":identity["character_key"],"fight_count":len(signatures)}
            if not api_key:
                for rec in sample_records:
                    save_knowledge_sample(rec["signature"],source="wcl_direct",sample_role="auto",metadata={"report_code":rec["report_code"],"fight_id":rec["fight_id"],"source_id":rec["source_id"]})
                return {"kind":"batch_local","signature":batch,"context":context,"fight_count":len(signatures),"failures":failures,"online_refreshes":online_refreshes}
            memory=memory_context_for_ai(retrieve_similar_cases(context,batch,limit=6,require_feedback=True),retrieve_lessons(context,limit=10))
            knowledge=knowledge_context_for_signatures(signatures) if common_class and common_spec else {}
            knowledge["online_reference_evidence"]=online_context_for_analysis(common_class,common_spec,limit=10) if common_class and common_spec else {}
            knowledge["online_refresh_this_analysis"] = online_refreshes
            legacy_target_rows=[]; legacy_target_all=[]; legacy_ref_count=0
            legacy_groups=knowledge.get("cohort_groups") or []
            for g in legacy_groups:
                cmp=((g.get("cohort") or {}).get("target_focus_comparison") or {})
                legacy_ref_count += int(cmp.get("reference_sample_count") or 0)
                dname=str(g.get("dungeon") or "")
                for row in (cmp.get("rows") or []):
                    rr=dict(row)
                    if len(legacy_groups)>1 and dname: rr["npc_name"]=f"{dname} · {rr.get('npc_name','')}"
                    legacy_target_all.append(rr)
                for row in (cmp.get("chart_rows") or []):
                    rr=dict(row)
                    if len(legacy_groups)>1 and dname: rr["npc_name"]=f"{dname} · {rr.get('npc_name','')}"
                    legacy_target_rows.append(rr)
            target_focus_comparison={"reference_sample_count":legacy_ref_count,"rows":legacy_target_all[:18],"chart_rows":legacy_target_rows[:8]}
            knowledge["target_focus_comparison_summary"]=target_focus_comparison
            cohort_count=sum(int((((g.get("cohort") or {}).get("empirical_profile") or {}).get("sample_count")) or 0) for g in legacy_groups)
            source_summary={"mode":"multi","fight_count":len(signatures),"context":context,"cohort_sample_count":cohort_count,"cohort_group_matches":[((g.get("cohort") or {}).get("cohort_match_details") or {}) for g in (knowledge.get("cohort_groups") or [])],"online_refresh":online_refreshes,"failed_reports":len(failures),"personal_model_scopes":[p.get("scope","") for p in profiles],"personal_model_sample_counts":[int(p.get("sample_count") or 0) for p in profiles]}
            for rec in sample_records:
                save_knowledge_sample(rec["signature"],source="wcl_direct",sample_role="auto",metadata={"report_code":rec["report_code"],"fight_id":rec["fight_id"],"source_id":rec["source_id"]})
            ai,md,ai_meta=run_coach_pipeline(api_key,batch,model=model,source_summary=source_summary,memory_context=memory,knowledge_context=knowledge,speed_mode=self._ai_speed_mode())
            case=save_case(context=context,payload=batch,report_markdown=md,report_json=ai,model=model,metadata={"source":"multi_wcl_fights","selected_reports":codes,"failures":failures,"online_refresh":online_refreshes})
            return {"kind":"ai","signature":batch,"context":context,"ai":ai,"markdown":md,"case_id":case,"fight_count":len(signatures),"failures":failures,"online_refreshes":online_refreshes,"cohort_sample_count":cohort_count,"cohort_group_matches":source_summary.get("cohort_group_matches") or [],"target_focus_comparison":target_focus_comparison,"ai_meta":ai_meta}
        self._run_async(job,self._apply_analysis,f"正在综合读取 {len(codes)} 份 Log → 学习重复模式 → 横向对比 → 生成中文报告...")

    def _apply_analysis(self,result:dict[str,Any]):
        self.last_ai_context=result.get("context") or {}; self.last_ai_payload=result.get("signature") or {}; self.last_case_id=result.get("case_id")
        fight_count = int(result.get("fight_count") or 1)
        cohort_count = int(result.get("cohort_sample_count") or 0)
        online = result.get("online_refresh") or result.get("online_refreshes") or {}
        if result.get("kind")=="ai":
            self.last_ai_json=result.get("ai") or {}; text=result.get("markdown") or render_analysis_markdown(self.last_ai_json)
        elif result.get("kind") == "batch_local":
            batch=result.get("signature") or {}
            fights=batch.get("fights") or []
            lines=["# 多场综合分析（本地统计）","",f"本次成功读取 **{len(fights)} 场**。未配置 DeepSeek API Key，因此暂时只展示程序汇总。", "", "## 重复出现的主要技能行为"]
            for row in (batch.get("repeated_skill_behavior") or [])[:12]:
                lines.append(f"- **{row.get('spell_name')}**：出现在 {row.get('present_in_fights',0)}/{len(fights)} 场；每分钟使用次数大致 {row.get('min_casts_per_min',0):.1f}–{row.get('max_casts_per_min',0):.1f}")
            lines += ["", "> 配置 DeepSeek 后会加入：多场重复问题识别、和本人历史比较、同职业同专精横向样本、中文自然语言报告。"]
            text="\n".join(lines)
        else:
            sig=result.get("signature") or {}; ov=sig.get("overview") or {}; ctx=sig.get("context") or {}; skills=sig.get("skills") or []
            lines=["# 针对性 Log 分析（本地统计）","",f"- 玩家：{ctx.get('player','')}",f"- {ctx.get('class_name','')} / {ctx.get('spec_name','')}",f"- 副本：{ctx.get('dungeon','')} +{ctx.get('key_level',0)}",f"- DPS：{ov.get('dps',0):,.0f}",f"- 总动作次数：{ov.get('casts',0)}","","## 主要技能"]
            for sk in skills[:12]: lines.append(f"- {sk.get('spell_name')}: 伤害占比 {sk.get('damage_pct',0):.1f}% · 每分钟约 {sk.get('casts_per_min',0):.1f} 次")
            personal=result.get("personal_comparison") or {}
            if personal:
                lines += ["", "## 和你自己平时相比", f"- 本场个人异常程度：{personal.get('label','')} · 历史样本 {personal.get('sample_count',0)} 场"]
                for f in (personal.get("findings") or [])[:8]: lines.append(f"- {f.get('evidence','')}")
            lines += ["","> 未配置 DeepSeek API Key。设置后会由 AI 把统计结果整理成完整中文报告，并加入职业横向比较。"]
            text="\n".join(lines)
        self.last_report_markdown = text
        self.report_browser.setMarkdown(text)
        source = "多场综合" if fight_count > 1 else "单场针对性"
        online_text = "已实时联网补充参考样本" if online else "使用本地已学习参考样本"
        match = result.get("cohort_match_details") or {}
        group_matches = result.get("cohort_group_matches") or []
        if match:
            quality_text = str(match.get("scope") or "")
        elif group_matches:
            scopes = [str(x.get("scope") or "") for x in group_matches if x.get("scope")]
            quality_text = "；".join(dict.fromkeys(scopes[:3]))
        else:
            quality_text = "暂无足够横向样本"
        ai_meta = result.get("ai_meta") or {}
        ai_seconds = ai_meta.get("total_seconds")
        if ai_meta.get("cache_hit"):
            speed_note = "AI 缓存命中 · 即时"
        else:
            speed_note = f"AI {ai_seconds:.1f}s" if isinstance(ai_seconds, (int, float)) else f"AI {ai_meta.get('speed_mode', self._ai_speed_mode())}"
        target_cmp = result.get("target_focus_comparison") or {}
        chart_rows = list(target_cmp.get("chart_rows") or [])
        if chart_rows:
            self.target_chart.set_rows(chart_rows)
            self.target_chart_card.setVisible(True)
        else:
            self.target_chart.set_rows([])
            self.target_chart_card.setVisible(False)
        target_note = f" | 重要目标参考 {int(target_cmp.get('reference_sample_count') or 0)} 场" if target_cmp else ""
        self.report_meta.setText(f"{source} · {fight_count} 场 | 横向参考 {cohort_count} 场{target_note} | {online_text} | {speed_note}\n参考条件：{quality_text}")
        self.nav.setCurrentRow(PAGE_REPORT)
        if self.character_profile: self._refresh_personal_page()

    def _choose_local_log(self):
        path,_=QFileDialog.getOpenFileName(self,"选择 WoWCombatLog.txt",str(Path.home()),"Combat Log (*.txt *.log);;All files (*)")
        if not path:return
        self.local_path.setText(path)
        def job():
            txt=Path(path).read_text(encoding="utf-8",errors="replace"); df=parse_text(txt); return split_mplus_runs(df,Path(path).name)
        self._run_async(job,self._apply_local_runs,"正在解析本地 Combat Log...")

    def _apply_local_runs(self,runs:dict):
        self.local_runs=runs; self.local_run_combo.clear(); self.local_run_combo.addItems(list(runs)); self.local_ai_btn.setEnabled(bool(runs)); self._refresh_local_run()

    def _refresh_local_run(self):
        name=self.local_run_combo.currentText(); df=self.local_runs.get(name)
        if df is None:return
        players=list_players(df); self.local_player_combo.clear(); self.local_player_combo.addItems(players)
        t=team_overview(df); self.local_table.setRowCount(len(t))
        for r,row in t.reset_index(drop=True).iterrows():
            vals=[row.get("player",""),f"{row.get('dps',0):,.0f}",f"{row.get('damage',0):,.0f}",f"{row.get('hps',0):,.0f}",f"{row.get('damage_taken',0):,.0f}",str(int(row.get("casts",0))),str(int(row.get("interrupts",0))),str(int(row.get("deaths",0)))]
            for c,v in enumerate(vals): self.local_table.setItem(r,c,QTableWidgetItem(str(v)))

    def _analyze_local(self):
        name=self.local_run_combo.currentText(); player=self.local_player_combo.currentText(); df=self.local_runs.get(name)
        if df is None or not player:return
        meta=infer_run_metadata(df); specs=infer_player_specs(df); info=specs.get(player,{})
        payload=compact_comparison_for_ai({name:df},{name:player},[name],"All",run_meta={name:meta})
        context={"analysis_mode":"local_single_run","run":name,"player":player,"dungeon":meta.get("dungeon",""),"key_level":meta.get("key_level",0),"class_name":info.get("class_name",""),"spec_name":info.get("spec_name","")}
        api_key=self.ds_key.text().strip(); model=self.ds_model.currentText().strip() or DEFAULT_MODEL; reasoning=self.reasoning.currentText()
        if not api_key:
            self._apply_analysis({"kind":"local","signature":payload,"context":context,"fight_count":1})
            return
        def job():
            memory=memory_context_for_ai(retrieve_similar_cases(context,payload,limit=5,require_feedback=True),retrieve_lessons(context,limit=8))
            knowledge=knowledge_context_for_analysis(context["class_name"],context["spec_name"],context["dungeon"],int(context["key_level"] or 0),patch_scope=str(context.get("patch_scope") or "")) if context["class_name"] and context["spec_name"] else {}
            knowledge["online_reference_evidence"]=online_context_for_analysis(context["class_name"],context["spec_name"],limit=8) if context["class_name"] and context["spec_name"] else {}
            cohort_count=int((((knowledge.get("empirical_profile") or {}).get("sample_count")) or 0))
            source_summary={"mode":"single_local","fight_count":1,"context":context,"cohort_sample_count":cohort_count,"cohort_match_details":knowledge.get("cohort_match_details") or {}}
            ai,md,ai_meta=run_coach_pipeline(api_key,payload,model=model,source_summary=source_summary,memory_context=memory,knowledge_context=knowledge,speed_mode=self._ai_speed_mode())
            case=save_case(context=context,payload=payload,report_markdown=md,report_json=ai,model=model,metadata={"source":"local_desktop"}); return {"kind":"ai","signature":payload,"context":context,"ai":ai,"markdown":md,"case_id":case,"fight_count":1,"cohort_sample_count":cohort_count,"cohort_match_details":knowledge.get("cohort_match_details") or {},"ai_meta":ai_meta}
        self._run_async(job,self._apply_analysis,"正在分析本地 Log → 生成中文玩家报告...")

    def _quick_feedback(self,rating:int):
        if not self.last_case_id:
            QMessageBox.information(self,"反馈","当前报告没有可学习的 AI 案例。")
            return
        correction = ""
        cause = ""
        if rating < 2:
            correction, ok = QInputDialog.getMultiLineText(
                self, "补充真实情况", "告诉 AI 哪里判断错了、真实原因是什么。\n例如：这里不是网络卡，是我在躲机制。"
            )
            if not ok:
                correction = ""
        update_feedback(int(self.last_case_id), int(rating), cause, correction)
        api_key=self.ds_key.text().strip(); model=self.ds_model.currentText().strip() or DEFAULT_MODEL
        if not api_key:
            QMessageBox.information(self,"已学习","反馈已经保存。以后分析相似 Log 时会作为历史纠错证据。")
            return
        case_id=int(self.last_case_id); context=dict(self.last_ai_context); report=str(self.last_report_markdown or "")
        def job():
            lesson=learn_lesson_with_deepseek(api_key,context,report,int(rating),cause,correction,model=model)
            add_lesson(case_id,context,str(lesson.get("pattern") or ""),str(lesson.get("when_to_apply") or ""),str(lesson.get("when_not_to_apply") or ""),str(lesson.get("confidence") or "low"))
            return lesson
        def done(_lesson):
            QMessageBox.information(self,"AI 已学习","你的反馈已经保存，并由 AI 提炼成可复用经验。以后遇到相似 Log 会优先参考这次纠正。")
        self._run_async(job,done,"正在把你的反馈提炼成可复用经验...")

    def _load_settings_into_ui(self):
        self.wcl_client_id.setText(str(self.settings.get("wcl_client_id") or ""))
        saved_model = str(self.settings.get("deepseek_model") or DEFAULT_MODEL)
        if saved_model in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
            saved_model = DEFAULT_MODEL
        self.ds_model.setCurrentText(saved_model)
        speed = str(self.settings.get("ai_speed_mode") or "fast")
        self.ai_speed.setCurrentText("深度" if speed == "deep" else "平衡" if speed == "balanced" else "快速（推荐）")
        self.reasoning.setCurrentText(str(self.settings.get("reasoning_effort") or "low"))
        self.auto_online_compare.setChecked(bool(self.settings.get("auto_online_compare", True)))
        self.identity_input.setText(str(self.settings.get("last_identity") or ""))
        self.ds_key.setText(get_secret("deepseek_api_key"))
        self.wcl_secret.setText(get_secret("wcl_client_secret"))
        self._update_action_states()

    def _save_settings(self):
        data={"wcl_client_id":self.wcl_client_id.text().strip(),"deepseek_model":self.ds_model.currentText().strip(),"reasoning_effort":self.reasoning.currentText(),"ai_speed_mode":self._ai_speed_mode(),"last_identity":self.identity_input.text().strip(),"auto_online_compare":self.auto_online_compare.isChecked()}
        save_settings(data)
        ok=True
        if self.remember_secrets.isChecked():
            ok=set_secret("deepseek_api_key",self.ds_key.text().strip()) and set_secret("wcl_client_secret",self.wcl_secret.text().strip())
        self.settings = load_settings()
        if self._wcl_client_obj is not None:
            self._wcl_client_obj.close()
            self._wcl_client_obj = None
            self._wcl_client_key = None
        self._update_action_states()
        QMessageBox.information(self,"设置", "已保存并立即生效。" + ("密钥已写入系统钥匙串。" if ok and self.remember_secrets.isChecked() else "密钥仅在本次运行中使用。"))

    def _test_wcl(self):
        def done(x):
            self._update_connection_status(True)
            used=x.get("pointsSpentThisHour","?")
            limit=x.get("limitPerHour","?")
            QMessageBox.information(self,"WCL 连接成功",f"连接正常。\n本小时 API 点数：{used} / {limit}")
        self._run_async(lambda:self._wcl().rate_limit(),done,"正在测试 WCL...")

    def closeEvent(self,event):
        try:
            save_settings({"wcl_client_id":self.wcl_client_id.text().strip(),"deepseek_model":self.ds_model.currentText().strip(),"reasoning_effort":self.reasoning.currentText(),"ai_speed_mode":self._ai_speed_mode(),"last_identity":self.identity_input.text().strip(),"auto_online_compare":self.auto_online_compare.isChecked()})
        finally:
            super().closeEvent(event)


def self_test() -> int:
    """Frozen-build smoke test used by GitHub Actions and local packaging scripts."""
    if not APP_VERSION:
        return 2
    # Exercise a few pure helpers that are easy for PyInstaller hidden-import mistakes
    # to break while keeping the smoke test offline and credential-free.
    parsed = parse_character_locator("cn:白银之手:不是酒鬼丶")
    if parsed.server_region != "cn" or parsed.server_slug != "白银之手":
        return 3
    if not callable(run_coach_pipeline) or not callable(build_targeted_wcl_payload):
        return 4
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    app=QApplication(sys.argv)
    app.setApplicationName(APP_TITLE); app.setOrganizationName("WoW Log Data Analyst")
    win=MainWindow(); win.show(); return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
