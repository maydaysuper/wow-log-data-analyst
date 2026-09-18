from __future__ import annotations

import faulthandler
import os
import re
import sys
import traceback
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLockFile, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import desktop_app as ui
import core.knowledge as knowledge_module
import core.target_focus as target_focus_module
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
    """Persist Python exceptions and fatal faults for windowed builds."""
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


def _metric_value(value: Any, key: str = "p50") -> float:
    if isinstance(value, dict):
        value = value.get(key)
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _npc_name_key(value: Any) -> str:
    """Conservative same-dungeon name key used only when WCL gameID does not match."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _best_reference_alias(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}

    def score(row: dict[str, Any]) -> tuple[int, float]:
        dist = row.get("damage_share_pct") or {}
        count = int(dist.get("sample_count") or row.get("samples_seen") or 0) if isinstance(dist, dict) else int(row.get("samples_seen") or 0)
        p50 = _metric_value(dist)
        return count, p50

    return max(rows, key=score)


def _combine_current_aliases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine same-name current-run aliases without turning missing data into zero."""
    if not rows:
        return {}
    if len(rows) == 1:
        return dict(rows[0])
    first = dict(rows[0])
    first["damage_share_pct"] = min(100.0, sum(_metric_value(r.get("damage_share_pct")) for r in rows))
    first["target_dps_over_run"] = sum(_metric_value(r.get("target_dps_over_run")) for r in rows)
    first["early_targeted_cast_share_pct"] = min(
        100.0, sum(_metric_value(r.get("early_targeted_cast_share_pct")) for r in rows)
    )
    first["matched_alias_count"] = len(rows)
    return first


def _fixed_compare_target_profiles(
    current_profile: dict[str, Any],
    reference_profile: dict[str, Any],
    role_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare target focus without silently interpreting an ID miss as 0% damage.

    WCL report actor IDs are mapped to creature gameIDs, but seasonal/legacy dungeon
    variants can still expose a different creature gameID for the same named target.
    Within a comparison that is already scoped to one dungeon/patch, exact gameID is
    preferred; a normalized target-name match is a conservative fallback. If neither
    matches, current metrics stay unavailable (None) instead of becoming a fake zero.
    """
    current_rows = list(current_profile.get("targets") or current_profile.get("all_targets") or [])
    reference_rows = list(reference_profile.get("all_targets") or [])
    role_profile = role_profile or {}
    role_rows = list(role_profile.get("all_targets") or [])

    current_map = {
        int(r.get("npc_id") or 0): r for r in current_rows if int(r.get("npc_id") or 0) > 0
    }
    ref_map = {
        int(r.get("npc_id") or 0): r for r in reference_rows if int(r.get("npc_id") or 0) > 0
    }
    role_map = {
        int(r.get("npc_id") or 0): r for r in role_rows if int(r.get("npc_id") or 0) > 0
    }

    current_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    ref_by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current_rows:
        key = _npc_name_key(row.get("npc_name"))
        if key:
            current_by_name[key].append(row)
    for row in reference_rows:
        key = _npc_name_key(row.get("npc_name"))
        if key:
            ref_by_name[key].append(row)

    candidate_ids: list[int] = []
    for source in (role_map, ref_map):
        for npc_id, row in source.items():
            role = str(row.get("learned_role") or "normal")
            if role in {"boss", "priority_focus", "important_large"} and npc_id not in candidate_ids:
                candidate_ids.append(npc_id)

    rows: list[dict[str, Any]] = []
    for npc_id in candidate_ids:
        ref = ref_map.get(npc_id) or {}
        role_ref = role_map.get(npc_id) or ref
        role = str(role_ref.get("learned_role") or ref.get("learned_role") or "normal")
        if role not in {"boss", "priority_focus", "important_large"}:
            continue

        canonical_name = str(role_ref.get("npc_name") or ref.get("npc_name") or npc_id)
        name_key = _npc_name_key(canonical_name)

        if not ref and name_key:
            ref = _best_reference_alias(ref_by_name.get(name_key, []))

        cur = current_map.get(npc_id) or {}
        match_method = "npc_id" if cur else ""
        if not cur and name_key:
            aliases = current_by_name.get(name_key, [])
            if aliases:
                cur = _combine_current_aliases(aliases)
                match_method = "npc_name_fallback"

        current_available = bool(cur)
        cur_share = _metric_value(cur.get("damage_share_pct")) if current_available else None
        cur_dps = _metric_value(cur.get("target_dps_over_run")) if current_available else None
        cur_early = _metric_value(cur.get("early_targeted_cast_share_pct")) if current_available else None

        share_dist = ref.get("damage_share_pct") or {}
        dps_dist = ref.get("target_dps_over_run") or {}
        early_dist = ref.get("early_targeted_cast_share_pct") or {}
        ref_share = _metric_value(share_dist)
        ref_share_hi = _metric_value(share_dist, "p75")
        ref_dps = _metric_value(dps_dist)
        ref_early = _metric_value(early_dist)
        metric_samples = (
            int(share_dist.get("sample_count") or ref.get("samples_seen") or 0)
            if isinstance(share_dist, dict)
            else int(ref.get("samples_seen") or 0)
        )
        reference_available = bool(ref_share > 0 and metric_samples > 0)

        if not current_available:
            delta_share = None
            label = "当前场次未匹配到该目标，不能按 0% 解读"
        elif not reference_available:
            delta_share = None
            label = "已识别重要目标，暂缺同专精数值参考"
        else:
            delta_share = float(cur_share or 0) - ref_share
            ratio = float(cur_share or 0) / ref_share if ref_share > 1e-9 else 1.0
            if ratio < 0.78:
                label = "低于同专精参考玩家常见投入"
            elif ratio > 1.22:
                label = "高于同专精参考玩家常见投入"
            else:
                label = "接近同专精参考玩家常见投入"

        rows.append({
            "npc_id": npc_id,
            "matched_current_npc_id": int(cur.get("npc_id") or 0) if current_available else 0,
            "npc_name": canonical_name,
            "role": role,
            "role_cn": str(role_ref.get("learned_role_cn") or ref.get("learned_role_cn") or role),
            "confidence": str(role_ref.get("confidence") or ref.get("confidence") or "low"),
            "role_samples_seen": int(role_ref.get("samples_seen") or 0),
            "same_spec_samples_seen": metric_samples,
            "current_damage_share_pct": round(float(cur_share), 3) if cur_share is not None else None,
            "reference_damage_share_pct": round(ref_share, 3),
            "reference_high_damage_share_pct": round(ref_share_hi, 3),
            "damage_share_delta_points": round(float(delta_share), 3) if delta_share is not None else None,
            "current_target_dps": round(float(cur_dps), 2) if cur_dps is not None else None,
            "reference_target_dps": round(ref_dps, 2),
            "current_early_focus_pct": round(float(cur_early), 3) if cur_early is not None else None,
            "reference_early_focus_pct": round(ref_early, 3),
            "comparison_label": label,
            "present_in_current_run": current_available,
            "current_data_available": current_available,
            "current_match_method": match_method or "missing",
            "same_spec_reference_available": reference_available,
        })

    # A seasonal/legacy variant may produce several gameIDs with the same visible NPC
    # name. Do not show duplicate bars; keep the row with the strongest usable evidence.
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    role_order = {"boss": 0, "priority_focus": 1, "important_large": 2}
    for row in rows:
        key = (str(row.get("role") or ""), _npc_name_key(row.get("npc_name")))
        previous = dedup.get(key)
        score = (
            1 if row.get("current_data_available") else 0,
            1 if row.get("same_spec_reference_available") else 0,
            int(row.get("same_spec_samples_seen") or 0),
            int(row.get("role_samples_seen") or 0),
        )
        if previous is None:
            dedup[key] = row
        else:
            previous_score = (
                1 if previous.get("current_data_available") else 0,
                1 if previous.get("same_spec_reference_available") else 0,
                int(previous.get("same_spec_samples_seen") or 0),
                int(previous.get("role_samples_seen") or 0),
            )
            if score > previous_score:
                dedup[key] = row

    rows = list(dedup.values())
    rows.sort(key=lambda r: (
        role_order.get(str(r.get("role")), 9),
        0 if r.get("current_data_available") else 1,
        0 if r.get("same_spec_reference_available") else 1,
        -int(r.get("role_samples_seen") or 0),
        -float(r.get("reference_damage_share_pct") or 0),
    ))
    chart_rows = [
        r for r in rows
        if r.get("current_data_available") and r.get("same_spec_reference_available")
    ][:8]
    return {
        "reference_sample_count": int(reference_profile.get("sample_count") or 0),
        "dungeon_role_sample_count": int(role_profile.get("sample_count") or 0),
        "rows": rows[:16],
        "chart_rows": chart_rows,
        "unmatched_current_target_count": sum(1 for r in rows if not r.get("current_data_available")),
        "guardrail": (
            "重要目标角色可由副本级多职业WCL行为库识别，但数值横向比较仍只使用同专精参考样本。"
            "优先按稳定NPC gameID匹配；同副本/版本内gameID不一致时才按规范化NPC名称回退。"
            "无法匹配的当前目标保持无数据，不得当作0%伤害。"
        ),
    }


def _fixed_compare_signature_target_focus(
    signature: dict[str, Any],
    reference_profile: dict[str, Any],
    role_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _fixed_compare_target_profiles(
        signature.get("target_focus") or {}, reference_profile or {}, role_profile or {}
    )


# Patch the functions imported by the desktop orchestration layer. This keeps existing
# library/storage compatibility while correcting comparisons before they reach DeepSeek.
target_focus_module.compare_target_profiles = _fixed_compare_target_profiles
target_focus_module.compare_signature_target_focus = _fixed_compare_signature_target_focus
knowledge_module.compare_target_profiles = _fixed_compare_target_profiles
ui.compare_signature_target_focus = _fixed_compare_signature_target_focus


class ResponsiveTargetComparisonChart(QWidget):
    """Paired bars that can shrink when the user gives more space to the report."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self.setMinimumHeight(92)

    def set_rows(self, rows: list[dict[str, Any]] | None) -> None:
        self._rows = list(rows or [])[:8]
        # Never make row count dictate the whole report page height.
        self.setMinimumHeight(92)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#11151c"))
        if not self._rows:
            painter.setPen(QColor("#7f8998"))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无可可靠对齐的当前场次 / 同专精目标数据")
            return

        width = max(360, self.width())
        left = min(330, max(205, int(width * 0.34)))
        right = 92
        top = 30
        n = max(1, len(self._rows))
        available_h = max(60, self.height() - top - 6)
        row_h = max(24, min(44, available_h // n))
        usable = max(100, self.width() - left - right)

        values: list[float] = []
        for row in self._rows:
            if row.get("current_damage_share_pct") is not None:
                values.append(float(row.get("current_damage_share_pct") or 0))
            values.append(float(row.get("reference_damage_share_pct") or 0))
            values.append(float(row.get("reference_high_damage_share_pct") or 0))
        max_value = max(1.0, max(values or [1.0])) * 1.12

        painter.setPen(QColor("#4d8dff"))
        painter.drawText(left, 18, "当前")
        painter.setPen(QColor("#62d394"))
        painter.drawText(left + 48, 18, "同专精参考")

        for i, row in enumerate(self._rows):
            y = top + i * row_h
            raw_name = str(row.get("npc_name") or row.get("npc_id") or "目标")
            dungeon = str(row.get("dungeon_name") or "")
            name = raw_name
            if " · " in raw_name and not dungeon:
                dungeon, name = raw_name.rsplit(" · ", 1)
            role = str(row.get("role_cn") or "重要目标")
            label = f"{role} · {name}"
            label = painter.fontMetrics().elidedText(label, Qt.ElideRight, max(100, left - 22))
            painter.setPen(QColor("#e8eaed"))
            painter.drawText(10, y + min(16, row_h - 6), label)
            if dungeon and row_h >= 36:
                painter.setPen(QColor("#7f8998"))
                dungeon_text = painter.fontMetrics().elidedText(dungeon, Qt.ElideRight, max(100, left - 22))
                painter.drawText(10, y + min(31, row_h - 2), dungeon_text)

            cur_raw = row.get("current_damage_share_pct")
            cur = float(cur_raw) if cur_raw is not None else None
            ref = float(row.get("reference_damage_share_pct") or 0)
            hi = float(row.get("reference_high_damage_share_pct") or 0)
            ref_w = int(usable * min(max(ref / max_value, 0.0), 1.0))
            cur_w = int(usable * min(max((cur or 0) / max_value, 0.0), 1.0)) if cur is not None else 0

            bar_y = y + max(2, (row_h - 18) // 2)
            painter.setPen(Qt.NoPen)
            if cur is not None:
                painter.setBrush(QColor("#4d8dff"))
                painter.drawRoundedRect(left, bar_y, max(2, cur_w), 7, 3, 3)
            painter.setBrush(QColor("#62d394"))
            painter.drawRoundedRect(left, bar_y + 10, max(2, ref_w), 7, 3, 3)
            if hi > ref:
                hx = left + int(usable * min(max(hi / max_value, 0.0), 1.0))
                painter.setPen(QPen(QColor("#8f99a8"), 1))
                painter.drawLine(hx, bar_y + 8, hx, bar_y + 19)

            painter.setPen(QColor("#c7cfdb"))
            painter.drawText(left + usable + 8, bar_y + 7, "—" if cur is None else f"{cur:.1f}%")
            painter.setPen(QColor("#86d9aa"))
            painter.drawText(left + usable + 8, bar_y + 18, f"{ref:.1f}%")


class MainWindow(AppMainWindow):
    """Windows-packaged window focused on fast, recoverable startup."""

    def __init__(self):
        self._startup_ready = False
        self._startup_started = False
        self._startup_error = ""
        self._startup_worker = None
        self._busy_job_count = 0
        self._busy_message = ""
        self._db_initializers = (
            ui.init_db,
            ui.init_knowledge_db,
            ui.init_online_learning_db,
            ui.init_personal_baseline_db,
        )

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

    def _build_report_page(self) -> QWidget:
        """Build a report page where the chart/report split is user-adjustable."""
        page, outer = self._page_shell(
            "AI 战斗报告",
            "数据层先计算，DeepSeek 再做分析与中文报告编辑。图表与正文之间的分隔线可以直接拖动。",
        )
        meta_card, meta_layout = self._card()
        meta_row = QHBoxLayout()
        self.report_meta = QLabel("还没有生成报告。")
        self.report_meta.setWordWrap(True)
        self.report_meta.setObjectName("Muted")
        self.target_chart_toggle = QPushButton("收起目标图")
        self.target_chart_toggle.setVisible(False)
        self.target_chart_toggle.clicked.connect(self._toggle_target_chart)
        meta_row.addWidget(self.report_meta, 1)
        meta_row.addWidget(self.target_chart_toggle)
        meta_layout.addLayout(meta_row)
        outer.addWidget(meta_card)

        self.target_chart_card, target_chart_layout = self._card()
        target_title = QLabel("BOSS / 优先目标伤害对比")
        target_title.setObjectName("SectionTitle")
        target_hint = QLabel(
            "蓝色=当前场次，绿色=同副本/相近层数同专精参考。无法可靠匹配当前目标时不再显示伪造的 0%。"
        )
        target_hint.setObjectName("Muted")
        target_hint.setWordWrap(True)
        target_chart_layout.addWidget(target_title)
        target_chart_layout.addWidget(target_hint)
        self.target_chart = ResponsiveTargetComparisonChart()
        target_chart_layout.addWidget(self.target_chart, 1)
        self.target_chart_card.setVisible(False)

        self.report_browser = QTextBrowser()
        self.report_browser.setOpenExternalLinks(True)
        self.report_browser.setMinimumHeight(180)
        self.report_browser.setStyleSheet("QTextBrowser{font-size:16px; padding:22px 28px;}")
        self.report_browser.document().setDefaultStyleSheet("""
            h1 { font-size: 27px; margin: 4px 0 22px 0; color: #ffffff; }
            h2 { font-size: 20px; margin: 28px 0 14px 0; color: #ffffff; background-color: #192231; padding: 9px 12px; }
            h3 { font-size: 17px; margin: 20px 0 9px 0; color: #dce6f7; }
            p  { line-height: 175%; margin: 10px 0 13px 0; }
            li { line-height: 170%; margin: 7px 0; }
            blockquote { color: #b7c3d6; background-color:#171d27; margin: 14px 6px; padding: 10px 14px; }
            strong { color: #ffffff; }
        """)

        self.report_splitter = QSplitter(Qt.Vertical)
        self.report_splitter.setChildrenCollapsible(True)
        self.report_splitter.setHandleWidth(9)
        self.report_splitter.addWidget(self.target_chart_card)
        self.report_splitter.addWidget(self.report_browser)
        self.report_splitter.setStretchFactor(0, 0)
        self.report_splitter.setStretchFactor(1, 1)
        self.report_splitter.setSizes([220, 620])
        outer.addWidget(self.report_splitter, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("这份分析是否符合实际："))
        for text, rating in (("准确", 2), ("部分准确", 1), ("不准确", -1)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, r=rating: self._quick_feedback(r))
            row.addWidget(button)
        row.addStretch()
        outer.addLayout(row)
        return page

    def _toggle_target_chart(self) -> None:
        if not hasattr(self, "target_chart_card"):
            return
        visible = self.target_chart_card.isVisible()
        self.target_chart_card.setVisible(not visible)
        self.target_chart_toggle.setText("显示目标图" if visible else "收起目标图")
        if not visible:
            QTimer.singleShot(0, self._set_default_report_split)

    def _set_default_report_split(self) -> None:
        if not hasattr(self, "report_splitter") or not self.target_chart_card.isVisible():
            return
        total = max(480, self.report_splitter.height())
        chart = min(255, max(150, int(total * 0.30)))
        report = max(300, total - chart)
        self.report_splitter.setSizes([chart, report])

    def _apply_analysis(self, result: dict[str, Any]):
        # The comparison has already been corrected before DeepSeek sees it. The base
        # renderer can keep handling markdown/meta; then restore a report-first layout.
        super()._apply_analysis(result)
        target_cmp = result.get("target_focus_comparison") or {}
        chart_rows = list(target_cmp.get("chart_rows") or [])
        self.target_chart_toggle.setVisible(bool(chart_rows))
        self.target_chart_toggle.setText("收起目标图")
        if chart_rows:
            QTimer.singleShot(0, self._set_default_report_split)

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

    def _refresh_busy_controls(self) -> None:
        active = int(getattr(self, "_busy_job_count", 0)) > 0
        ready = bool(getattr(self, "_startup_ready", False))
        if hasattr(self, "search_btn"):
            self.search_btn.setEnabled(ready and (not active) and bool(self.identity_input.text().strip()))
        if hasattr(self, "personal_sync_btn"):
            self.personal_sync_btn.setEnabled(ready and not active)
        if hasattr(self, "personal_refresh_btn"):
            self.personal_refresh_btn.setEnabled(ready and not active)
        if hasattr(self, "season_target_btn"):
            self.season_target_btn.setEnabled(ready and (not active) and bool(getattr(self, "timed_runs", [])))
        if hasattr(self, "timed_analyze_btn"):
            try:
                selected = bool(self._selected_timed_run_records())
            except Exception:
                selected = False
            self.timed_analyze_btn.setEnabled(ready and (not active) and selected)
        if hasattr(self, "local_ai_btn"):
            self.local_ai_btn.setEnabled(ready and (not active) and bool(getattr(self, "local_runs", {})))

    def _set_busy(self, busy: bool, message: str = ""):
        if busy:
            self._busy_job_count = int(getattr(self, "_busy_job_count", 0)) + 1
            if message:
                self._busy_message = message
                self.statusBar().showMessage(message)
        else:
            self._busy_job_count = max(0, int(getattr(self, "_busy_job_count", 0)) - 1)
            if self._busy_job_count == 0:
                self._busy_message = ""
                self.statusBar().showMessage("就绪")
            elif message and message != "就绪":
                self._busy_message = message
                self.statusBar().showMessage(message)
        try:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
        except Exception:
            pass
        self._refresh_busy_controls()

    def _update_action_states(self):
        super()._update_action_states()
        self._refresh_busy_controls()

    def _timed_selection_changed(self):
        super()._timed_selection_changed()
        self._refresh_busy_controls()

    def _set_startup_controls_enabled(self, ready: bool) -> None:
        self._startup_ready = bool(ready)
        if ready:
            super()._update_action_states()
            if hasattr(self, "_timed_selection_changed"):
                super()._timed_selection_changed()
        self._refresh_busy_controls()

    def _apply_windows_layout_hardening(self) -> None:
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
    """Exercise real DB init, window construction, target matching, layout and shutdown."""
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
        # Regression: same named target with a different gameID must not become fake 0%.
        current = {"targets": [{
            "npc_id": 999, "npc_name": "测试首领", "damage_share_pct": 12.0,
            "target_dps_over_run": 12000, "early_targeted_cast_share_pct": 18.0,
        }]}
        reference_row = {
            "npc_id": 100, "npc_name": "测试首领", "learned_role": "boss", "learned_role_cn": "BOSS",
            "samples_seen": 6, "damage_share_pct": {"p50": 20.0, "p75": 24.0, "sample_count": 6},
            "target_dps_over_run": {"p50": 19000, "sample_count": 6},
            "early_targeted_cast_share_pct": {"p50": 20.0, "sample_count": 6},
        }
        cmp = _fixed_compare_target_profiles(
            current,
            {"sample_count": 6, "all_targets": [reference_row]},
            {"sample_count": 6, "all_targets": [reference_row]},
        )
        if not cmp.get("chart_rows"):
            return 21
        row = cmp["chart_rows"][0]
        if abs(float(row.get("current_damage_share_pct") or 0) - 12.0) > 0.01:
            return 22
        if row.get("current_match_method") != "npc_name_fallback":
            return 23

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
        if getattr(win, "report_splitter", None) is None:
            return 24
        if win.target_chart.minimumHeight() > 120:
            return 25

        win._set_startup_controls_enabled(True)
        win._set_busy(True, "self-test busy")
        if QApplication.overrideCursor() is not None:
            return 16
        if int(getattr(win, "_busy_job_count", -1)) != 1:
            return 17
        win._set_busy(False, "就绪")
        if int(getattr(win, "_busy_job_count", -1)) != 0:
            return 18

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
