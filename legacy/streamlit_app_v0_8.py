from __future__ import annotations

import json
import os
from io import BytesIO

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from core.parser import parse_text
from core.analyzer import (
    events_df,
    split_mplus_runs,
    list_players,
    infer_run_metadata,
    infer_player_specs,
    team_overview,
    team_summary,
    compare_teams,
    team_player_matrix,
    compare_selected_players,
    compare_selected_skills,
    skill_share_matrix,
    skill_benchmark,
    selected_player_vs_cohort_median,
    run_quality,
    cohort_runs,
    compact_comparison_for_ai,
)
from core.ai_client import (
    analyze_with_deepseek,
    analyze_with_deepseek_json,
    render_analysis_markdown,
    learn_lesson_with_deepseek,
    synthesize_class_playbook_with_deepseek,
    extract_reference_evidence_with_deepseek,
    DEFAULT_MODEL,
)
from core.wcl_client import WCLClient
from core.responsiveness import responsiveness_summary, responsiveness_details
from core.telemetry import parse_savedvariables, telemetry_summary, correlate_gaps_with_telemetry
from core.platform_support import candidate_wow_addon_dirs, install_telemetry_addon
from core.advanced_timeline import (
    segment_pulls,
    player_pull_breakdown,
    death_contexts,
    burst_windows,
    advanced_timeline_payload,
)
from core.combat_efficiency import (
    buff_uptime,
    resource_efficiency,
    cooldown_cadence,
    target_switch_analysis,
    pull_downtime,
    death_recovery,
    buff_burst_overlap,
    deep_efficiency_payload,
    compare_efficiency_runs,
    buff_uptime_matrix,
)
from core.knowledge import (
    init_knowledge_db,
    behavior_signature,
    save_knowledge_sample,
    update_sample_role,
    delete_knowledge_sample,
    list_knowledge_samples,
    select_knowledge_samples,
    empirical_cohort_profile,
    save_playbook,
    list_playbooks,
    delete_playbook,
    knowledge_context_for_analysis,
)

from core.online_learning import (
    init_online_learning_db,
    learn_from_wcl_rankings,
    ingest_public_reference,
    save_external_evidence,
    list_online_sources,
    list_external_evidence,
    online_context_for_analysis,
    list_online_learning_runs,
)
from core.learning import (
    init_db,
    save_case,
    update_feedback,
    add_lesson,
    recent_cases,
    recent_lessons,
    delete_case,
    clear_memory,
    retrieve_similar_cases,
    retrieve_lessons,
    memory_context_for_ai,
    default_db_path,
)

load_dotenv()

st.set_page_config(page_title="WoW Log Data Analyst", page_icon="📊", layout="wide")
st.title("📊 WoW 正式服战斗日志 · AI 数据分析师")
st.caption("上传自己的 Combat Log → 队伍横向对比 → 同层数/同职业技能占比 → 可选 DeepSeek V4.1 AI 诊断")

# ---- session state ----
if "runs" not in st.session_state:
    st.session_state.runs = {}
if "run_meta" not in st.session_state:
    st.session_state.run_meta = {}
if "wcl_data" not in st.session_state:
    st.session_state.wcl_data = {}
if "ai_report" not in st.session_state:
    st.session_state.ai_report = ""
if "ai_json" not in st.session_state:
    st.session_state.ai_json = {}
if "ai_case_id" not in st.session_state:
    st.session_state.ai_case_id = None
if "ai_context" not in st.session_state:
    st.session_state.ai_context = {}
if "ai_payload" not in st.session_state:
    st.session_state.ai_payload = {}
init_db()
init_knowledge_db()
init_online_learning_db()

# ---- sidebar: secrets are session-only widgets ----
with st.sidebar:
    st.header("AI / API")
    st.caption("Key 只用于当前会话请求，不写入 Git、Log 或导出文件。")
    ds_key = st.text_input("DeepSeek API Key（可选）", value="", type="password", placeholder="sk-...")
    model_label = st.selectbox(
        "模型",
        ["DeepSeek V4.1 Flash（推荐）", "DeepSeek V4 Pro", "自定义 DeepSeek 模型 ID"],
        index=0,
    )
    custom_model = ""
    if model_label.startswith("自定义"):
        custom_model = st.text_input("模型 ID", value=DEFAULT_MODEL)
    if model_label.startswith("DeepSeek V4.1"):
        ds_model = DEFAULT_MODEL
    elif model_label == "DeepSeek V4 Pro":
        ds_model = "deepseek-v4-pro"
    else:
        ds_model = custom_model.strip() or DEFAULT_MODEL
    reasoning_label = st.selectbox("AI 分析强度", ["深入（High）", "快速（Low）", "极深（Max）", "关闭思考"], index=0)
    reasoning_effort = {"深入（High）": "high", "快速（Low）": "low", "极深（Max）": "max", "关闭思考": "none"}[reasoning_label]
    use_learning = st.toggle("使用本地学习记忆", value=True, help="检索你过去确认/纠正过的相似案例，作为 DeepSeek 的辅助上下文。")
    use_class_knowledge = st.toggle("使用职业样本知识", value=True, help="把本机从多份 Log 和 WCL 学到的同职业/专精经验基准提供给 DeepSeek；它是经验规律，不是写死攻略。")
    use_online_knowledge = st.toggle("使用联网资料知识", value=True, help="把已经抓取并带来源/版本标记的公开资料作为辅助证据提供给 DeepSeek。真实 Log 证据优先。")
    auto_learn_logs = st.toggle("AI分析时自动沉淀Log样本", value=True, help="每次正式AI分析前，把参与分析的玩家行为指纹加入本机职业样本库；相同样本自动去重，不会额外调用AI。")

    st.divider()
    st.subheader("Warcraft Logs（可选）")
    wcl_id = st.text_input("WCL Client ID", value=os.getenv("WCL_CLIENT_ID", ""), type="password")
    wcl_secret = st.text_input("WCL Client Secret", value=os.getenv("WCL_CLIENT_SECRET", ""), type="password")


def _safe_key(name: str) -> str:
    return str(abs(hash(name)))


def _all_segments(runs: dict[str, pd.DataFrame]) -> list[str]:
    segs = set()
    run_segs = set()
    for df in runs.values():
        if df.empty:
            continue
        if "segment" in df:
            segs.update(str(x) for x in df["segment"].dropna().unique() if x != "Full Log")
        if "run_segment" in df:
            run_segs.update(str(x) for x in df["run_segment"].dropna().unique() if x != "Full Log")
    return ["All"] + [f"RUN::{x}" for x in sorted(run_segs)] + sorted(segs)


def _segment_label(v: str) -> str:
    return v[5:] if v.startswith("RUN::") else ("全部" if v == "All" else v)


def _download_df(label: str, df: pd.DataFrame, filename: str):
    if df is None or df.empty:
        return
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"), file_name=filename, mime="text/csv")


def _memory_for(context: dict, payload: dict) -> dict:
    if not use_learning:
        return {}
    cases = retrieve_similar_cases(context, payload, limit=5, require_feedback=True)
    lessons = retrieve_lessons(context, limit=8)
    return memory_context_for_ai(cases, lessons)


def _class_knowledge_for(context: dict) -> dict:
    if not use_class_knowledge:
        return {}
    cls = str(context.get("class_name") or "").strip()
    spec = str(context.get("spec_name") or "").strip()
    if not cls or not spec:
        return {}
    out = knowledge_context_for_analysis(
        cls, spec, str(context.get("dungeon") or ""), int(context.get("key_level") or 0)
    )
    if use_online_knowledge:
        out["online_reference_evidence"] = online_context_for_analysis(cls, spec, limit=8)
    return out


def _store_ai_result(context: dict, payload: dict, report_json: dict, report_markdown: str) -> int:
    case_id = save_case(
        context=context,
        payload=payload,
        report_markdown=report_markdown,
        report_json=report_json,
        model=ds_model,
        metadata={"reasoning_effort": reasoning_effort},
    )
    st.session_state.ai_json = report_json
    st.session_state.ai_report = report_markdown
    st.session_state.ai_case_id = case_id
    st.session_state.ai_context = context
    st.session_state.ai_payload = payload
    return case_id


def _feedback_panel(prefix: str):
    case_id = st.session_state.get("ai_case_id")
    if not case_id or not st.session_state.get("ai_report"):
        return
    st.divider()
    st.markdown("#### 让 AI 从这次结果里学习")
    st.caption("这不是修改 DeepSeek 模型权重，而是把你确认/纠正过的案例保存到本机案例库。以后分析相似日志时会检索这些经验。")
    rating_label = st.radio(
        "这份分析怎么样？", ["准确", "部分准确", "不准确"], horizontal=True, key=f"{prefix}_rating"
    )
    cause = st.selectbox(
        "如果你已经知道真实原因，可确认原因（可选）",
        ["", "网络延迟", "客户端掉帧/性能", "操作停顿", "移动/机制", "转火/目标问题", "资源/循环", "队伍路线/拉怪差异", "其他"],
        key=f"{prefix}_cause",
    )
    correction = st.text_area(
        "纠正 AI / 补充真实情况（可选）",
        placeholder="例如：这里不是网络卡，是我在躲地板；或者录像确认 World 延迟突然到 400ms。",
        key=f"{prefix}_correction",
    )
    learn_with_ai = st.checkbox("用 DeepSeek 把我的反馈整理成可复用经验", value=True, key=f"{prefix}_learn")
    if st.button("保存反馈并用于以后分析", key=f"{prefix}_save_feedback"):
        rating = {"准确": 2, "部分准确": 1, "不准确": -1}[rating_label]
        update_feedback(int(case_id), rating, cause, correction)
        if learn_with_ai and ds_key:
            try:
                lesson = learn_lesson_with_deepseek(
                    ds_key,
                    st.session_state.get("ai_context") or {},
                    st.session_state.ai_report,
                    rating,
                    cause,
                    correction,
                    ds_model,
                )
                add_lesson(
                    int(case_id), st.session_state.get("ai_context") or {},
                    str(lesson.get("pattern") or ""),
                    str(lesson.get("when_to_apply") or ""),
                    str(lesson.get("when_not_to_apply") or ""),
                    str(lesson.get("confidence") or "medium"),
                )
            except Exception as e:
                st.warning(f"反馈已保存，但经验整理失败：{e}")
        st.success("已保存。后续相似日志会优先参考你确认/纠正过的经验。")


library_tab, team_tab, class_tab, wcl_tab, response_tab, timeline_tab, efficiency_tab, ai_tab, memory_tab, knowledge_tab, online_tab = st.tabs([
    "① 日志库", "② 队伍 vs 队伍", "③ 同层数职业/技能", "④ WCL",
    "⑤ 响应/卡顿诊断", "⑥ 每波怪/时间轴", "⑦ 深度战斗效率", "⑧ AI 数据分析师", "⑨ AI 学习库", "⑩ 职业知识学习",
    "⑪ 联网学习中心"
])

# ---- 1. library ----
with library_tab:
    st.subheader("上传 Combat Log")
    files = st.file_uploader(
        "可一次上传多份 WoWCombatLog.txt / .log；每份文件视为一个可比较样本。",
        type=["txt", "log"],
        accept_multiple_files=True,
    )
    if files:
        for f in files:
            if f.name in st.session_state.runs:
                continue
            text = f.getvalue().decode("utf-8", errors="replace")
            df = events_df(parse_text(text))
            samples = split_mplus_runs(df, f.name)
            for sample_name, sample_df in samples.items():
                st.session_state.runs[sample_name] = sample_df
                inferred = infer_run_metadata(sample_df)
                st.session_state.run_meta[sample_name] = {
                    **inferred,
                    "source_file": f.name,
                    "label": sample_name,
                    "focus_player": "",
                    "class_name": "",
                    "spec_name": "",
                    "notes": "",
                }

    runs = st.session_state.runs
    if not runs:
        st.info("先上传至少一份日志。上传两份或更多后，就可以做横向对比。")
    else:
        st.success(f"已载入 {len(runs)} 份日志。")
        st.markdown("#### 给每份样本补充比较标签")
        st.caption("副本和层数会尽量从 CHALLENGE_MODE_START 自动读取；职业/专精目前由你给比较对象打标签，避免程序误判。")

        for run, df in runs.items():
            meta = st.session_state.run_meta.setdefault(run, {**infer_run_metadata(df)})
            players = list_players(df)
            with st.expander(run, expanded=len(runs) <= 3):
                a, b, c, d = st.columns([2, 1, 1.2, 1.2])
                meta["dungeon"] = a.text_input("副本", value=str(meta.get("dungeon") or ""), key=f"dun_{_safe_key(run)}")
                meta["key_level"] = int(b.number_input("层数", min_value=0, step=1, value=int(meta.get("key_level") or 0), key=f"key_{_safe_key(run)}"))
                default_focus = meta.get("focus_player") if meta.get("focus_player") in players else (players[0] if players else "")
                meta["focus_player"] = c.selectbox("职业对比对象", [""] + players, index=([""] + players).index(default_focus) if default_focus in players else 0, key=f"focus_{_safe_key(run)}")
                auto_specs = infer_player_specs(df)
                if meta["focus_player"] in auto_specs:
                    auto = auto_specs[meta["focus_player"]]
                    if not meta.get("class_name"):
                        meta["class_name"] = auto.get("class_name", "")
                    if not meta.get("spec_name"):
                        meta["spec_name"] = auto.get("spec_name", "")
                meta["class_name"] = d.text_input("职业", value=str(meta.get("class_name") or ""), placeholder="Warrior", key=f"class_{_safe_key(run)}")
                meta["spec_name"] = st.text_input("专精", value=str(meta.get("spec_name") or ""), placeholder="Protection / Fury / Arms", key=f"spec_{_safe_key(run)}")
                meta["notes"] = st.text_input("备注", value=str(meta.get("notes") or ""), placeholder="例如：固定队A、路线1、装等…", key=f"note_{_safe_key(run)}")
                st.session_state.run_meta[run] = meta

                inferred = infer_run_metadata(df)
                st.caption(f"自动识别：副本={inferred.get('dungeon') or '—'} · 层数={inferred.get('key_level') or '—'} · Map={inferred.get('map_id') or '—'} · Affix={inferred.get('affixes') or '—'}")
                quality = run_quality(df)
                if quality["warnings"]:
                    st.warning("数据质量提示：" + "；".join(quality["warnings"]))
                else:
                    st.caption(f"数据质量：M+ 起止完整 · 玩家 {quality['player_count']} · 事件 {quality['events']:,}")
                ov = team_overview(df)
                if not ov.empty:
                    st.dataframe(ov, use_container_width=True, hide_index=True)

        st.divider()
        if st.button("清空所有已上传日志"):
            st.session_state.runs = {}
            st.session_state.run_meta = {}
            st.rerun()

# ---- 2. team compare ----
with team_tab:
    runs = st.session_state.runs
    if len(runs) < 2:
        st.info("至少上传两份日志后才能做队伍横向对比。")
    else:
        run_names = list(runs)
        selected_runs = st.multiselect("选择要横向比较的队伍/日志", run_names, default=run_names[: min(4, len(run_names))])
        segments = _all_segments({k: runs[k] for k in selected_runs}) if selected_runs else ["All"]
        segment = st.selectbox("比较区段", segments, format_func=_segment_label, key="team_segment")

        if selected_runs:
            meta_rows = []
            for r in selected_runs:
                m = st.session_state.run_meta.get(r, {})
                meta_rows.append({"run": r, "副本": m.get("dungeon", ""), "层数": m.get("key_level", 0), "备注": m.get("notes", "")})
            st.dataframe(pd.DataFrame(meta_rows), use_container_width=True, hide_index=True)

            st.markdown("#### 队伍总览")
            team_cmp = compare_teams(runs, selected_runs, segment)
            st.dataframe(team_cmp, use_container_width=True, hide_index=True)
            _download_df("导出队伍对比 CSV", team_cmp, "team_comparison.csv")

            if not team_cmp.empty:
                chart = team_cmp.set_index("run")[["team_dps", "deaths", "interrupts"]]
                st.bar_chart(chart)

            st.markdown("#### 每个队员在队伍中的贡献")
            member_cmp = team_player_matrix(runs, selected_runs, segment)
            if not member_cmp.empty:
                st.dataframe(member_cmp, use_container_width=True, hide_index=True)
                _download_df("导出队员贡献 CSV", member_cmp, "team_player_contribution.csv")

# ---- 3. class/skill cohort ----
with class_tab:
    runs = st.session_state.runs
    meta = st.session_state.run_meta
    if len(runs) < 2:
        st.info("至少两份日志后才能建立同层数职业横向样本。")
    else:
        dungeons = sorted({m.get("dungeon", "") for m in meta.values() if m.get("dungeon")})
        levels = sorted({int(m.get("key_level") or 0) for m in meta.values() if int(m.get("key_level") or 0) > 0})
        classes = sorted({m.get("class_name", "") for m in meta.values() if m.get("class_name")})
        specs = sorted({m.get("spec_name", "") for m in meta.values() if m.get("spec_name")})

        c1, c2, c3, c4 = st.columns(4)
        dungeon = c1.selectbox("史诗地下城", [""] + dungeons)
        level = c2.selectbox("同层数", [0] + levels, format_func=lambda x: "全部" if x == 0 else f"+{x}")
        class_name = c3.selectbox("职业", [""] + classes)
        spec_name = c4.selectbox("专精", [""] + specs)

        matched = cohort_runs(meta, dungeon, int(level or 0), class_name, spec_name)
        if len(matched) < 2:
            st.warning("当前筛选条件下不足 2 个样本。请在“日志库”给至少两份日志设置相同副本/层数/职业/专精，并指定职业对比对象。")
        else:
            st.caption(f"匹配样本：{len(matched)} 个 · " + "、".join(matched))
            segment = st.selectbox("比较区段", _all_segments({k: runs[k] for k in matched}), format_func=_segment_label, key="cohort_segment")
            selections = {r: meta[r].get("focus_player", "") for r in matched}

            st.markdown("#### 同职业玩家总览")
            player_cmp = compare_selected_players(runs, selections, segment)
            st.dataframe(player_cmp, use_container_width=True, hide_index=True)
            _download_df("导出职业对比 CSV", player_cmp, "class_player_comparison.csv")

            st.markdown("#### 技能伤害占比横向矩阵")
            top_n = st.slider("显示前 N 个技能", 5, 40, 20)
            share = skill_share_matrix(runs, selections, segment, top_n=top_n)
            if not share.empty:
                st.dataframe(share, use_container_width=True, hide_index=True)
                chart_cols = [c for c in share.columns if c not in {"spell_name", "样本均值", "最大差值"}]
                if chart_cols:
                    st.bar_chart(share.set_index("spell_name")[chart_cols])
                _download_df("导出技能占比矩阵 CSV", share, "skill_share_matrix.csv")

            st.markdown("#### 样本基准：技能 P25 / P50(中位数) / P75 / P90 / 均值")
            bench = skill_benchmark(runs, selections, segment)
            if not bench.empty:
                st.dataframe(bench, use_container_width=True, hide_index=True)
                _download_df("导出技能样本基准 CSV", bench, "skill_benchmark.csv")

            st.markdown("#### 深度效率横向对比")
            eff_cmp = compare_efficiency_runs(runs, selections, segment)
            if not eff_cmp.empty:
                st.dataframe(eff_cmp, use_container_width=True, hide_index=True)
                _download_df("导出深度效率横向对比 CSV", eff_cmp, "deep_efficiency_comparison.csv")

            st.markdown("#### Buff 覆盖横向矩阵")
            buff_cmp = buff_uptime_matrix(runs, selections, segment, top_n=20)
            if buff_cmp.empty:
                st.caption("这些样本没有足够的 Buff aura 记录，或未开启能提供这些事件的日志。")
            else:
                st.dataframe(buff_cmp, use_container_width=True, hide_index=True)
                _download_df("导出 Buff 覆盖矩阵 CSV", buff_cmp, "buff_uptime_matrix.csv")

            st.markdown("#### 每个样本相对中位数差异")
            vs_med = selected_player_vs_cohort_median(runs, selections, segment)
            if not vs_med.empty:
                show_cols = [c for c in [
                    "run", "player", "spell_name", "damage_pct", "damage_pct_vs_median_pct",
                    "casts", "casts_per_min", "casts_per_min_vs_median_pct", "hits_per_cast",
                    "hits_per_cast_vs_median_pct", "crit_pct", "avg_hit", "damage_per_cast"
                ] if c in vs_med.columns]
                st.dataframe(vs_med[show_cols], use_container_width=True, hide_index=True)
                _download_df("导出相对中位数差异 CSV", vs_med, "skill_vs_cohort_median.csv")

            st.markdown("#### 技能明细：伤害 / 占比 / 命中 / 暴击 / 平均伤害 / 施放次数 / 每分钟施放 / 每次施放命中 / 每次施放伤害")
            skill_long = compare_selected_skills(runs, selections, segment)
            if not skill_long.empty:
                st.dataframe(skill_long, use_container_width=True, hide_index=True)
                _download_df("导出技能明细 CSV", skill_long, "skill_detail_comparison.csv")

# ---- 4. WCL ----
with wcl_tab:
    st.markdown("### Warcraft Logs 数据源（可选）")
    st.caption("本地上传日志是主数据源；WCL 用于读取公开 Report 和后续建立外部基准样本。")
    report_value = st.text_input("WCL report 链接或 report code")
    if st.button("读取 WCL Report"):
        if not (wcl_id and wcl_secret and report_value):
            st.error("需要 WCL Client ID、Client Secret 和 report。")
        else:
            try:
                cli = WCLClient(wcl_id, wcl_secret)
                st.session_state.wcl_data = cli.report_summary(report_value)
                st.success("WCL Report 读取成功")
            except Exception as e:
                st.exception(e)

    data = st.session_state.wcl_data
    report = (((data or {}).get("reportData") or {}).get("report") or {})
    if report:
        st.write({k: report.get(k) for k in ["code", "title", "visibility"]})
        fights = pd.DataFrame(report.get("fights") or [])
        if not fights.empty:
            st.dataframe(fights, use_container_width=True, hide_index=True)
            fight_id = st.selectbox("Fight ID", fights["id"].tolist())
            dtype = st.selectbox("WCL 表类型", ["DamageDone", "DamageTaken", "Healing", "Casts", "Deaths", "Interrupts", "Buffs", "Debuffs", "Resources"])
            if st.button("读取 Fight 表"):
                try:
                    cli = WCLClient(wcl_id, wcl_secret)
                    st.json(cli.report_table(report["code"], int(fight_id), dtype))
                except Exception as e:
                    st.exception(e)

# ---- 5. responsiveness / lag diagnostics ----
with response_tab:
    runs = st.session_state.runs
    if not runs:
        st.info("先上传日志。这个页面会分析施法节奏、战斗中停手、平砍节奏、失败施法，以及可选的网络/FPS遥测。")
    else:
        st.markdown("### 响应性 / 卡顿 / 网络异常诊断")
        st.caption("Combat Log 可以发现异常时间模式，但看不到你按键的时刻，因此单靠 Log 不能证明网络问题。可选遥测可以提高置信度。")

        rname = st.selectbox("选择一把日志", list(runs), key="resp_run")
        rdf = runs[rname]
        players = list_players(rdf)
        if not players:
            st.warning("这份日志没有识别到玩家。")
        else:
            default_player = st.session_state.run_meta.get(rname, {}).get("focus_player", "")
            if default_player not in players:
                default_player = players[0]
            player = st.selectbox("诊断玩家", players, index=players.index(default_player), key="resp_player")
            seg = st.selectbox("诊断区段", _all_segments({rname: rdf}), format_func=_segment_label, key="resp_segment")
            seg_value = None if seg == "All" else seg

            summary = responsiveness_summary(rdf, player, seg_value)
            details = responsiveness_details(rdf, player, seg_value)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("响应异常分", f"{summary.get('score', 0)}/100")
            m2.metric("最长活跃期停手", f"{summary.get('longest_gap_s', 0):.2f}s")
            m3.metric("异常停手窗口", summary.get("active_long_gap_count", 0))
            m4.metric("失败施法", summary.get("cast_failures", 0))

            if summary.get("score", 0) >= 60:
                st.error(summary.get("label"))
            elif summary.get("score", 0) >= 40:
                st.warning(summary.get("label"))
            else:
                st.success(summary.get("label"))

            st.caption(
                f"证据置信度：{summary.get('confidence')} · 动作间隔中位数 {summary.get('median_action_interval_s', 0):.2f}s · "
                f"P95 {summary.get('p95_action_interval_s', 0):.2f}s · 估计异常停手 {summary.get('estimated_stall_seconds', 0):.1f}s"
            )
            for item in summary.get("evidence", []):
                st.write("• " + item)
            st.info(summary.get("network_conclusion", ""))

            gaps = details.get("action_gaps")
            if gaps is not None and not gaps.empty:
                st.markdown("#### 战斗活跃期间的异常停手窗口")
                show = gaps.copy()
                base_ts = float(rdf["ts"].min()) if not rdf.empty else 0
                show.insert(0, "from_run_start_s", (show["start_ts"] - base_ts).round(2))
                st.dataframe(show, use_container_width=True, hide_index=True)
                _download_df("导出异常停手窗口 CSV", show, "responsiveness_action_gaps.csv")

            cast_out = details.get("cast_timing_outliers")
            if cast_out is not None and not cast_out.empty:
                st.markdown("#### 读条时长离群点")
                st.caption("这是弱证据：急速变化、机制和技能本身都可能改变读条时间。")
                st.dataframe(cast_out, use_container_width=True, hide_index=True)

            swing_out = details.get("swing_stalls")
            if swing_out is not None and not swing_out.empty:
                st.markdown("#### 近战平砍节奏异常")
                st.caption("平砍中断也可能来自移动、转火、离开近战范围或目标死亡，因此只作为辅助证据。")
                st.dataframe(swing_out, use_container_width=True, hide_index=True)

            st.divider()
            st.markdown("#### 高级：用 WoW 遥测区分网络延迟 vs 本地掉帧")
            st.caption("可选组件只记录 FPS、Home/World latency 和带宽，不读取游戏内存，也不自动操作游戏。注意 WoW 的 Home/World latency 本身约每 30 秒更新一次，所以极短网络尖峰可能无法被直接捕获。正式桌面版会把组件安装入口内置在程序里。")
            detected_addons = candidate_wow_addon_dirs()
            if detected_addons:
                st.caption(f"检测到 WoW AddOns：{detected_addons[0]}")
                if st.button("一键安装 / 更新网络与FPS监测组件", key="install_telemetry_addon"):
                    try:
                        dst = install_telemetry_addon(detected_addons[0])
                        st.success(f"已安装：{dst}。下次启动/重载 WoW 后生效。")
                    except Exception as e:
                        st.error(str(e))
            else:
                st.caption("没有自动检测到 WoW AddOns 目录；不安装监测组件也可以使用纯 Log 响应性分析。")

            telemetry_file = st.file_uploader("上传 WowLogTelemetry.lua（可选）", type=["lua", "txt"], key="telemetry_upload")
            telemetry_corr = None
            if telemetry_file is not None:
                tdf = parse_savedvariables(telemetry_file.getvalue().decode("utf-8", errors="replace"))
                if tdf.empty:
                    st.warning("没有在文件中识别到遥测样本。")
                else:
                    x = rdf
                    if seg_value:
                        if seg_value.startswith("RUN::") and "run_segment" in x:
                            x = x[x["run_segment"] == seg_value[5:]]
                        elif "segment" in x:
                            x = x[x["segment"] == seg_value]
                    if not x.empty:
                        tdf_run = tdf[(tdf["ts"] >= float(x["ts"].min()) - 10) & (tdf["ts"] <= float(x["ts"].max()) + 10)]
                    else:
                        tdf_run = tdf
                    if tdf_run.empty:
                        st.warning("遥测时间与当前日志区段没有重合。")
                    else:
                        tsum = telemetry_summary(tdf_run)
                        a, b, c, d = st.columns(4)
                        a.metric("World延迟中位数", f"{tsum.get('world_ms_median', 0):.0f} ms")
                        b.metric("World延迟P95", f"{tsum.get('world_ms_p95', 0):.0f} ms")
                        c.metric("FPS中位数", f"{tsum.get('fps_median', 0):.0f}")
                        d.metric("FPS P05", f"{tsum.get('fps_p05', 0):.0f}")
                        telemetry_corr = correlate_gaps_with_telemetry(gaps, tdf_run)
                        assessment = telemetry_corr.get("assessment")
                        if assessment == "network_supported":
                            st.error("异常停手窗口与 World latency 峰值明显重合：网络延迟得到遥测支持。")
                        elif assessment == "fps_stutter_supported":
                            st.error("异常停手窗口与 FPS 明显下降重合：本地性能/掉帧得到遥测支持。")
                        elif assessment == "mixed_performance_issue":
                            st.warning("网络延迟与 FPS 下降都在异常窗口出现，可能是混合问题。")
                        elif assessment == "telemetry_does_not_support_network_or_fps_issue":
                            st.success("当前遥测没有支持‘网络延迟或掉帧导致这些停手’的证据，更应检查操作、移动、转火、资源或机制。")
                        if telemetry_corr.get("details"):
                            st.dataframe(pd.DataFrame(telemetry_corr["details"]), use_container_width=True, hide_index=True)

            st.markdown("#### 让 AI 解释这一把的响应异常")
            if st.button("AI 分析卡顿 / 响应问题", key="resp_ai", type="primary"):
                if not ds_key:
                    st.error("请先在左侧输入 DeepSeek API Key。")
                else:
                    payload = {
                        "run": rname,
                        "player": player,
                        "responsiveness": summary,
                        "action_gaps": gaps.head(20).round(3).to_dict("records") if gaps is not None and not gaps.empty else [],
                        "cast_timing_outliers": cast_out.head(20).round(3).to_dict("records") if cast_out is not None and not cast_out.empty else [],
                        "swing_stalls": swing_out.head(20).round(3).to_dict("records") if swing_out is not None and not swing_out.empty else [],
                        "telemetry_correlation": telemetry_corr or {},
                        "timeline": advanced_timeline_payload(rdf, player, seg_value),
                        "rule": "Combat Log alone cannot prove network latency; only attribute network/FPS when telemetry supports it.",
                    }
                    m = st.session_state.run_meta.get(rname, {})
                    context = {
                        "run_label": rname, "player": player,
                        "dungeon": m.get("dungeon", ""), "key_level": m.get("key_level", 0),
                        "class_name": m.get("class_name", ""), "spec_name": m.get("spec_name", ""),
                        "analysis_mode": "responsiveness",
                    }
                    try:
                        if auto_learn_logs and m.get("class_name") and m.get("spec_name"):
                            save_knowledge_sample(behavior_signature(rdf, player, {**m, "label": rname}, "All"), source="ai_analysis", sample_role="auto", metadata={"analysis_mode": "responsiveness"})
                        memory = _memory_for(context, payload)
                        class_knowledge = _class_knowledge_for(context)
                        with st.spinner("AI 正在分析时间轴异常..."):
                            result = analyze_with_deepseek_json(
                                ds_key, payload, ds_model,
                                "重点判断：操作停顿、移动/转火、客户端掉帧、网络延迟分别有哪些证据。",
                                reasoning_effort=reasoning_effort,
                                memory_context=memory,
                                knowledge_context=class_knowledge,
                            )
                        report = render_analysis_markdown(result)
                        _store_ai_result(context, payload, result, report)
                    except Exception as e:
                        st.exception(e)
            if st.session_state.get("ai_report") and st.session_state.get("ai_context", {}).get("analysis_mode") == "responsiveness":
                st.markdown(st.session_state.ai_report)
                _feedback_panel("resp")

# ---- 6. advanced timeline ----
with timeline_tab:
    runs = st.session_state.runs
    if not runs:
        st.info("先上传日志。这里会自动近似拆分每波怪，并分析每波DPS、死亡前10秒和伤害爆发窗口。")
    else:
        st.markdown("### 每波怪 / 死亡 / 爆发时间轴")
        st.caption("Pull 边界按战斗活跃间隔近似推断；链式拉怪可能被合并，所以界面会明确标记为近似结果。")
        t_run = st.selectbox("选择一把日志", list(runs), key="timeline_run")
        t_df = runs[t_run]
        t_players = list_players(t_df)
        if not t_players:
            st.warning("没有识别到玩家。")
        else:
            default_p = st.session_state.run_meta.get(t_run, {}).get("focus_player", "")
            if default_p not in t_players:
                default_p = t_players[0]
            t_player = st.selectbox("分析玩家", t_players, index=t_players.index(default_p), key="timeline_player")
            t_seg = st.selectbox("区段", _all_segments({t_run: t_df}), format_func=_segment_label, key="timeline_segment")
            t_seg_value = None if t_seg == "All" else t_seg
            idle_gap = st.slider("判定新一波怪的脱战间隔（秒）", min_value=4.0, max_value=15.0, value=8.0, step=0.5)

            pulls = segment_pulls(t_df, t_seg_value, idle_gap_s=idle_gap)
            if pulls.empty:
                st.info("这个区段没有形成可识别的 Pull。")
            else:
                base = float(t_df["ts"].min()) if not t_df.empty else 0.0
                show_pulls = pulls.copy()
                show_pulls.insert(1, "from_run_start_s", (show_pulls["start_ts"] - base).round(1))
                a, b, c, d = st.columns(4)
                a.metric("近似 Pull 数", len(pulls))
                b.metric("Boss Pull", int((pulls["kind"] == "Boss").sum()))
                c.metric("最长一波", f"{pulls['duration_s'].max():.1f}s")
                d.metric("队伍死亡", int(pulls["player_deaths"].sum()))
                st.dataframe(show_pulls, use_container_width=True, hide_index=True)
                _download_df("导出 Pull 时间轴 CSV", show_pulls, "pull_timeline.csv")

                st.markdown("#### 该玩家每波表现")
                pp = player_pull_breakdown(t_df if t_seg_value is None else (
                    t_df[t_df["run_segment"] == t_seg_value[5:]] if t_seg_value.startswith("RUN::") and "run_segment" in t_df else t_df[t_df["segment"] == t_seg_value]
                ), t_player, pulls)
                if not pp.empty:
                    st.dataframe(pp, use_container_width=True, hide_index=True)
                    _download_df("导出玩家每波表现 CSV", pp, "player_pull_breakdown.csv")

            deaths = death_contexts(t_df, t_player, t_seg_value)
            st.markdown("#### 死亡前后时间轴")
            if deaths.empty:
                st.caption("该区段没有记录到这个玩家死亡。")
            else:
                st.dataframe(deaths, use_container_width=True, hide_index=True)
                _download_df("导出死亡上下文 CSV", deaths, "death_contexts.csv")

            bursts = burst_windows(t_df, t_player, t_seg_value)
            st.markdown("#### 最高伤害的 10 秒窗口")
            st.caption("这是从实际伤害峰值识别的窗口，不等同于职业官方定义的爆发阶段。")
            if not bursts.empty:
                st.dataframe(bursts, use_container_width=True, hide_index=True)
                _download_df("导出爆发窗口 CSV", bursts, "burst_windows.csv")

            if st.button("用 DeepSeek 分析这把的每波怪 / 死亡 / 爆发", key="timeline_ai", type="primary"):
                if not ds_key:
                    st.error("请先在左侧输入 DeepSeek API Key。")
                else:
                    payload = {
                        "run": t_run,
                        "player": t_player,
                        "timeline": advanced_timeline_payload(t_df, t_player, t_seg_value),
                        "deep_efficiency": deep_efficiency_payload(t_df, t_player, t_seg_value),
                    }
                    m = st.session_state.run_meta.get(t_run, {})
                    context = {
                        "run_label": t_run, "player": t_player,
                        "dungeon": m.get("dungeon", ""), "key_level": m.get("key_level", 0),
                        "class_name": m.get("class_name", ""), "spec_name": m.get("spec_name", ""),
                        "analysis_mode": "timeline",
                    }
                    try:
                        if auto_learn_logs and m.get("class_name") and m.get("spec_name"):
                            save_knowledge_sample(behavior_signature(t_df, t_player, {**m, "label": t_run}, "All"), source="ai_analysis", sample_role="auto", metadata={"analysis_mode": "timeline"})
                        memory = _memory_for(context, payload)
                        class_knowledge = _class_knowledge_for(context)
                        with st.spinner("DeepSeek 正在分析每波怪和关键时间轴..."):
                            result = analyze_with_deepseek_json(
                                ds_key, payload, ds_model,
                                "重点找：哪几波效率最低、死亡前发生了什么、爆发窗口是否稳定，以及下一把最值得验证的点。",
                                reasoning_effort=reasoning_effort,
                                memory_context=memory,
                                knowledge_context=class_knowledge,
                            )
                        report = render_analysis_markdown(result)
                        _store_ai_result(context, payload, result, report)
                    except Exception as e:
                        st.exception(e)
            if st.session_state.get("ai_report") and st.session_state.get("ai_context", {}).get("analysis_mode") == "timeline":
                st.markdown(st.session_state.ai_report)
                _feedback_panel("timeline")

# ---- 7. deep combat efficiency ----
with efficiency_tab:
    runs = st.session_state.runs
    if not runs:
        st.info("先上传日志。这里会分析 Buff、资源、技能施法节奏、转火、Pull 间空档和死亡恢复。")
    else:
        st.markdown("### 深度战斗效率")
        st.caption("这一页只展示 Combat Log 能观察到的证据。施法间隔不等于已知技能CD；Pull空档也可能是跑图、RP、电梯或路线决策。")
        e_run = st.selectbox("选择一把日志", list(runs), key="eff_run")
        e_df = runs[e_run]
        e_players = list_players(e_df)
        if not e_players:
            st.warning("没有识别到玩家。")
        else:
            default_p = st.session_state.run_meta.get(e_run, {}).get("focus_player", "")
            if default_p not in e_players:
                default_p = e_players[0]
            e_player = st.selectbox("分析玩家", e_players, index=e_players.index(default_p), key="eff_player")
            e_seg = st.selectbox("区段", _all_segments({e_run: e_df}), format_func=_segment_label, key="eff_segment")
            e_seg_value = None if e_seg == "All" else e_seg

            res = resource_efficiency(e_df, e_player, e_seg_value)
            sw = target_switch_analysis(e_df, e_player, e_seg_value)
            gaps, gap_sum = pull_downtime(e_df, e_seg_value)
            rec = death_recovery(e_df, e_player, e_seg_value)

            a, b, c, d = st.columns(4)
            a.metric("资源快照", res.get("advanced_snapshots", 0))
            avg_r = res.get("avg_resource_pct")
            b.metric("平均资源", "—" if avg_r is None else f"{avg_r:.1f}%")
            c.metric("资源溢出", f"{res.get('overcap_pct_of_generated', 0):.1f}%")
            d.metric("Pull间空档", f"{gap_sum.get('inter_pull_downtime_s', 0):.1f}s")

            st.markdown("#### Buff 覆盖")
            buffs = buff_uptime(e_df, e_player, e_seg_value)
            if buffs.empty:
                st.caption("没有可用的 Buff aura apply/remove 记录。")
            else:
                st.dataframe(buffs, use_container_width=True, hide_index=True)
                _download_df("导出 Buff 覆盖 CSV", buffs, "buff_uptime.csv")

            st.markdown("#### 技能施法 cadence")
            cadence = cooldown_cadence(e_df, e_player, e_seg_value)
            st.caption("这里是观察到的施法节奏，不会把间隔直接当成技能理论冷却。")
            if not cadence.empty:
                st.dataframe(cadence, use_container_width=True, hide_index=True)
                _download_df("导出施法节奏 CSV", cadence, "cast_cadence.csv")

            st.markdown("#### 资源证据")
            if not res.get("resource_snapshot_available"):
                st.info("这份日志没有足够的 Advanced Combat Logging power 快照；仍可使用其它分析。")
            st.json(res)

            st.markdown("#### 目标切换 / 转火")
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("有明确目标的施法", sw.get("targeted_casts", 0))
            s2.metric("观察到目标数", sw.get("unique_targets", 0))
            s3.metric("目标切换", sw.get("target_switches", 0))
            s4.metric("切换/分钟", f"{sw.get('switches_per_min', 0):.2f}")
            st.caption(sw.get("note", ""))

            st.markdown("#### Pull 间空档")
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Pull数", gap_sum.get("pulls", 0))
            p2.metric("空档总计", f"{gap_sum.get('inter_pull_downtime_s', 0):.1f}s")
            p3.metric("中位空档", f"{gap_sum.get('median_gap_s', 0):.1f}s")
            p4.metric("最大空档", f"{gap_sum.get('max_gap_s', 0):.1f}s")
            st.caption(gap_sum.get("note", ""))
            if not gaps.empty:
                st.dataframe(gaps.sort_values("gap_s", ascending=False), use_container_width=True, hide_index=True)

            st.markdown("#### 死亡后回到有效动作")
            if rec.empty:
                st.caption("该区段没有记录到该玩家死亡。")
            else:
                st.dataframe(rec, use_container_width=True, hide_index=True)

            st.markdown("#### Buff 与最高伤害窗口重合")
            overlap = buff_burst_overlap(e_df, e_player, e_seg_value)
            if not overlap.empty:
                st.dataframe(overlap, use_container_width=True, hide_index=True)

            if st.button("用 DeepSeek 深度分析效率问题", key="eff_ai", type="primary"):
                if not ds_key:
                    st.error("请先在左侧输入 DeepSeek API Key。")
                else:
                    payload = {"run": e_run, "player": e_player, "deep_efficiency": deep_efficiency_payload(e_df, e_player, e_seg_value)}
                    m = st.session_state.run_meta.get(e_run, {})
                    context = {
                        "run_label": e_run, "player": e_player,
                        "dungeon": m.get("dungeon", ""), "key_level": m.get("key_level", 0),
                        "class_name": m.get("class_name", ""), "spec_name": m.get("spec_name", ""),
                        "analysis_mode": "deep_efficiency",
                    }
                    try:
                        if auto_learn_logs and m.get("class_name") and m.get("spec_name"):
                            save_knowledge_sample(behavior_signature(e_df, e_player, {**m, "label": e_run}, "All"), source="ai_analysis", sample_role="auto", metadata={"analysis_mode": "deep_efficiency"})
                        memory = _memory_for(context, payload)
                        class_knowledge = _class_knowledge_for(context)
                        with st.spinner("DeepSeek 正在分析 Buff、资源、施法节奏、转火和时间损失..."):
                            result = analyze_with_deepseek_json(
                                ds_key, payload, ds_model,
                                "重点区分日志能直接证明的效率问题与仍需职业技能库/录像验证的假设。不要把观察到的施法间隔直接当作理论CD。",
                                reasoning_effort=reasoning_effort, memory_context=memory, knowledge_context=class_knowledge,
                            )
                        report = render_analysis_markdown(result)
                        _store_ai_result(context, payload, result, report)
                    except Exception as e:
                        st.exception(e)
            if st.session_state.get("ai_report") and st.session_state.get("ai_context", {}).get("analysis_mode") == "deep_efficiency":
                st.markdown(st.session_state.ai_report)
                _feedback_panel("eff")

# ---- 8. AI ----
with ai_tab:
    runs = st.session_state.runs
    meta = st.session_state.run_meta
    if len(runs) < 1:
        st.info("先上传日志。AI 是可选层；没有 API Key 时所有本地统计和横向比较仍可使用。")
    else:
        st.markdown("### DeepSeek · WoW 战斗数据分析师")
        st.caption("AI 不直接计算基础数字，而是读取程序已经算好的结构化统计、响应异常、每波怪时间轴，以及你过去确认过的相似案例。")
        mode = st.radio("分析模式", ["队伍横向分析", "同层数同职业技能分析"], horizontal=True)
        segment = st.selectbox("AI 分析区段", _all_segments(runs), format_func=_segment_label, key="ai_segment")
        extra = st.text_area("补充背景（可选）", placeholder="例如：两队路线相同；这个玩家录像里确认卡了；装等接近；目标是找伤害差距来源……")

        selections: dict[str, str] = {}
        team_runs: list[str] = []
        dun = ""
        lvl = 0
        cls = ""
        spec = ""

        if mode == "队伍横向分析":
            team_runs = st.multiselect("选择参与 AI 对比的队伍", list(runs), default=list(runs)[: min(4, len(runs))], key="ai_team_runs")
            for r in team_runs:
                fp = meta.get(r, {}).get("focus_player", "")
                if fp:
                    selections[r] = fp
        else:
            dungeons = sorted({m.get("dungeon", "") for m in meta.values() if m.get("dungeon")})
            levels = sorted({int(m.get("key_level") or 0) for m in meta.values() if int(m.get("key_level") or 0) > 0})
            classes = sorted({m.get("class_name", "") for m in meta.values() if m.get("class_name")})
            specs = sorted({m.get("spec_name", "") for m in meta.values() if m.get("spec_name")})
            a, b, c, d = st.columns(4)
            dun = a.selectbox("副本", [""] + dungeons, key="ai_dun")
            lvl = b.selectbox("层数", [0] + levels, format_func=lambda x: "全部" if x == 0 else f"+{x}", key="ai_lvl")
            cls = c.selectbox("职业", [""] + classes, key="ai_cls")
            spec = d.selectbox("专精", [""] + specs, key="ai_spec")
            team_runs = cohort_runs(meta, dun, int(lvl or 0), cls, spec)
            selections = {r: meta[r].get("focus_player", "") for r in team_runs if meta[r].get("focus_player")}
            st.caption("AI 样本：" + ("、".join(team_runs) if team_runs else "无匹配样本"))

        payload = compact_comparison_for_ai(runs, selections, team_runs, segment, top_skills=25, run_meta=meta)
        # Add key time-axis evidence for selected focus players; bound it so API requests stay manageable.
        timeline_by_run = {}
        resp_by_run = {}
        efficiency_by_run = {}
        for r, player_name in list(selections.items())[:4]:
            if not player_name or r not in runs:
                continue
            seg_value = None if segment == "All" else segment
            timeline_by_run[r] = advanced_timeline_payload(runs[r], player_name, seg_value)
            resp_by_run[r] = responsiveness_summary(runs[r], player_name, seg_value)
            efficiency_by_run[r] = deep_efficiency_payload(runs[r], player_name, seg_value)
        if timeline_by_run:
            payload["timeline_by_run"] = timeline_by_run
        if resp_by_run:
            payload["responsiveness_by_run"] = resp_by_run
        if efficiency_by_run:
            payload["deep_efficiency_by_run"] = efficiency_by_run

        if mode == "同层数同职业技能分析":
            context = {
                "run_label": ", ".join(team_runs[:4]),
                "player": ", ".join(sorted(set(selections.values()))),
                "dungeon": dun, "key_level": int(lvl or 0), "class_name": cls, "spec_name": spec,
                "analysis_mode": "class_cohort",
            }
        else:
            first_run = team_runs[0] if team_runs else ""
            first_meta = meta.get(first_run, {}) if first_run else {}
            context = {
                "run_label": ", ".join(team_runs[:4]),
                "player": ", ".join(sorted(set(selections.values()))),
                "dungeon": first_meta.get("dungeon", ""), "key_level": first_meta.get("key_level", 0),
                "class_name": first_meta.get("class_name", "") if len(set(m.get("class_name", "") for m in [meta.get(r,{}) for r in team_runs])) <= 1 else "",
                "spec_name": first_meta.get("spec_name", "") if len(set(m.get("spec_name", "") for m in [meta.get(r,{}) for r in team_runs])) <= 1 else "",
                "analysis_mode": "team_compare",
            }

        memory = _memory_for(context, payload) if team_runs else {}
        class_knowledge = _class_knowledge_for(context) if team_runs else {}
        if use_learning:
            similar_n = len(memory.get("similar_cases", []))
            lesson_n = len(memory.get("learned_lessons", []))
            st.caption(f"本地学习记忆：找到 {similar_n} 个已反馈相似案例、{lesson_n} 条可复用经验。")
        if use_class_knowledge and class_knowledge:
            prof = class_knowledge.get("empirical_profile") or {}
            st.caption(f"职业样本知识：{prof.get('sample_count',0)} 个同职业/专精样本 · {len(class_knowledge.get('recent_playbooks', []))} 个历史知识版本。")

        with st.expander("查看将发送给 AI 的结构化摘要（不会发送完整原始 Log）"):
            st.json(payload)
        if use_learning and memory:
            with st.expander("查看本次会提供给 AI 的历史学习上下文"):
                st.json(memory)
        if use_class_knowledge and class_knowledge:
            with st.expander("查看本次会提供给 AI 的职业样本知识"):
                st.json(class_knowledge)

        if st.button("使用 DeepSeek V4.1 生成深度分析报告", type="primary"):
            if not ds_key:
                st.error("请在左侧输入 DeepSeek API Key。")
            elif not team_runs:
                st.error("没有选中可分析样本。")
            else:
                try:
                    if auto_learn_logs:
                        for r, player_name in selections.items():
                            if r not in runs or not player_name:
                                continue
                            rm = meta.get(r, {})
                            if not rm.get("class_name") or not rm.get("spec_name"):
                                continue
                            sig = behavior_signature(runs[r], player_name, {**rm, "label": r}, "All")
                            save_knowledge_sample(sig, source="ai_analysis", sample_role="auto", metadata={"analysis_mode": context.get("analysis_mode")})
                        # Regenerate knowledge context so the current sample can immediately participate in the cohort profile.
                        class_knowledge = _class_knowledge_for(context)
                    with st.spinner("DeepSeek 正在分析结构化日志、时间轴与历史经验..."):
                        result = analyze_with_deepseek_json(
                            ds_key, payload, ds_model, extra,
                            reasoning_effort=reasoning_effort,
                            memory_context=memory,
                            knowledge_context=class_knowledge,
                        )
                    report = render_analysis_markdown(result)
                    _store_ai_result(context, payload, result, report)
                except Exception as e:
                    st.exception(e)

        if st.session_state.get("ai_report") and st.session_state.get("ai_context", {}).get("analysis_mode") in {"team_compare", "class_cohort"}:
            st.markdown(st.session_state.ai_report)
            st.download_button(
                "下载 AI 分析报告",
                st.session_state.ai_report.encode("utf-8"),
                file_name="wow_log_ai_analysis.md",
                mime="text/markdown",
            )
            _feedback_panel("main_ai")

# ---- 9. learning memory ----
with memory_tab:
    st.markdown("### AI 学习库")
    st.caption("这里保存的是本机案例记忆，不是 DeepSeek 官方模型训练。API Key 从不写入这个数据库。你可以随时查看、删除或清空。")
    st.code(str(default_db_path()), language=None)

    cases = recent_cases(100)
    lessons = recent_lessons(100)
    a, b, c = st.columns(3)
    a.metric("历史 AI 案例", len(cases))
    b.metric("已给反馈", sum(1 for x in cases if int(x.get("rating") or 0) != 0))
    c.metric("提炼经验", len(lessons))

    if cases:
        view = pd.DataFrame([{
            "id": x.get("id"), "时间": x.get("created_at"), "模式": x.get("analysis_mode"),
            "副本": x.get("dungeon"), "层数": x.get("key_level"), "职业": x.get("class_name"),
            "专精": x.get("spec_name"), "玩家": x.get("player"), "反馈": x.get("rating"),
            "确认原因": x.get("confirmed_cause"), "纠正": x.get("correction"), "模型": x.get("model"),
        } for x in cases])
        st.dataframe(view, use_container_width=True, hide_index=True)
        st.download_button(
            "导出学习库 JSON",
            json.dumps({"cases": cases, "lessons": lessons}, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="wow_log_ai_learning_memory.json",
            mime="application/json",
        )
        delete_id = st.number_input("删除单个案例 ID", min_value=0, step=1, value=0)
        if st.button("删除这个案例") and int(delete_id) > 0:
            delete_case(int(delete_id))
            st.success("已删除。")
            st.rerun()
    else:
        st.info("还没有 AI 历史案例。生成一份报告并保存反馈后，这里会开始积累。")

    if lessons:
        st.markdown("#### 已提炼的经验")
        st.dataframe(pd.DataFrame(lessons)[["id", "class_name", "spec_name", "dungeon", "analysis_mode", "pattern", "when_to_apply", "when_not_to_apply", "confidence"]], use_container_width=True, hide_index=True)

    st.divider()
    st.warning("清空后无法恢复。")
    confirm_clear = st.checkbox("我确认要清空本机 AI 学习库")
    if st.button("清空学习库", disabled=not confirm_clear):
        clear_memory()
        st.session_state.ai_case_id = None
        st.success("学习库已清空。")
        st.rerun()

# ---- 10. class knowledge learning ----
with knowledge_tab:
    st.markdown("### 职业知识学习 · 从真实 Log 归纳打法规律")
    st.caption("这里不是手工写死攻略，也不是修改 DeepSeek 官方模型权重。程序把真实 Log 转成行为指纹，积累同职业/专精样本，统计稳定规律，再让 DeepSeek 把这些证据总结成可版本化的职业知识。")
    st.warning("高 DPS ≠ 一定正确打法。路线、怪量、装备、队友和层数都会混杂结果；因此这里的结论默认叫‘经验规律’，不是理论定律。")

    runs = st.session_state.runs
    meta = st.session_state.run_meta
    if runs:
        st.markdown("#### A. 把当前上传的 Log 加入职业样本库")
        learnable = [r for r in runs if meta.get(r, {}).get("focus_player") and meta.get(r, {}).get("class_name") and meta.get(r, {}).get("spec_name")]
        chosen = st.multiselect("选择要学习的样本", learnable, default=learnable[: min(10, len(learnable))], key="knowledge_ingest_runs")
        role_label = st.selectbox(
            "这些样本是什么性质？",
            ["自动学习样本", "我确认是优质参考样本", "普通/对照样本"],
            help="优质参考样本会优先作为参考组；如果不标记，样本>=4时程序才会临时用DPS前25%做探索组，但不会把它当作理论最优。",
        )
        role = {"自动学习样本": "auto", "我确认是优质参考样本": "reference", "普通/对照样本": "normal"}[role_label]
        if st.button("加入职业学习库", type="primary", key="knowledge_ingest"):
            if not chosen:
                st.error("请先选择样本。")
            else:
                saved = 0
                for r in chosen:
                    m = meta.get(r, {})
                    player = m.get("focus_player", "")
                    if not player:
                        continue
                    sig = behavior_signature(runs[r], player, {**m, "label": r}, "All")
                    save_knowledge_sample(sig, source="local_log", sample_role=role, metadata={"notes": m.get("notes", "")})
                    saved += 1
                st.success(f"已加入/更新 {saved} 个行为样本。相同样本会自动去重。")
                st.rerun()
    else:
        st.info("先在日志库上传 Log，给比较对象标记职业/专精后，就可以把样本加入这里。")

    stored = list_knowledge_samples(1000)
    st.markdown("#### B. 本机职业样本库")
    if stored:
        classes = sorted({str(x.get("class_name") or "") for x in stored if x.get("class_name")})
        specs = sorted({str(x.get("spec_name") or "") for x in stored if x.get("spec_name")})
        a, b = st.columns(2)
        k_cls = a.selectbox("职业知识过滤", [""] + classes, key="knowledge_cls")
        available_specs = sorted({str(x.get("spec_name") or "") for x in stored if (not k_cls or str(x.get("class_name") or "") == k_cls) and x.get("spec_name")})
        k_spec = b.selectbox("专精知识过滤", [""] + available_specs, key="knowledge_spec")
        filtered = [x for x in stored if (not k_cls or x.get("class_name") == k_cls) and (not k_spec or x.get("spec_name") == k_spec)]
        sample_view = pd.DataFrame([{
            "id": x.get("id"), "run": x.get("run_label"), "player": x.get("player"), "副本": x.get("dungeon"),
            "层数": x.get("key_level"), "职业": x.get("class_name"), "专精": x.get("spec_name"),
            "角色": x.get("sample_role"), "DPS(仅作样本特征)": round(float(x.get("performance_value") or 0), 1), "来源": x.get("source"),
        } for x in filtered[:500]])
        st.dataframe(sample_view, use_container_width=True, hide_index=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("当前过滤样本", len(filtered))
        c2.metric("优质参考", sum(1 for x in filtered if x.get("sample_role") == "reference"))
        c3.metric("覆盖副本/层数", len({(x.get("dungeon"), x.get("key_level")) for x in filtered}))

        edit_id = st.number_input("修改样本 ID", min_value=0, step=1, value=0, key="knowledge_edit_id")
        edit_role = st.selectbox("改为", ["auto", "reference", "normal", "exclude"], key="knowledge_edit_role")
        ec1, ec2 = st.columns(2)
        if ec1.button("更新样本角色", disabled=int(edit_id) <= 0):
            update_sample_role(int(edit_id), edit_role)
            st.success("已更新。")
            st.rerun()
        if ec2.button("删除这个职业样本", disabled=int(edit_id) <= 0):
            delete_knowledge_sample(int(edit_id))
            st.success("已删除。")
            st.rerun()

        if k_cls and k_spec:
            st.markdown("#### C. 从样本自动归纳经验基准")
            cohort = select_knowledge_samples(k_cls, k_spec, "", 0, 0, 300)
            profile = empirical_cohort_profile(cohort)
            st.caption(f"样本数 {profile.get('sample_count',0)} · 参考样本 {profile.get('reference_sample_count',0)} · 上下文 {profile.get('context_count',0)}")
            if profile.get("skills"):
                st.markdown("**观察到的技能行为分位数**")
                st.dataframe(pd.DataFrame(profile["skills"]), use_container_width=True, hide_index=True)
            if profile.get("empirical_patterns"):
                st.markdown("**参考组与其他样本的稳定差异候选**")
                st.dataframe(pd.DataFrame(profile["empirical_patterns"]), use_container_width=True, hide_index=True)
            with st.expander("查看完整经验 Profile"):
                st.json(profile)

            st.markdown("#### D. 让 DeepSeek 总结并迭代职业知识")
            st.caption("DeepSeek 只读取上面的聚合证据，不读取整份原始 Log。每次生成都会保存成一个新版本；样本增加后可重新生成，不覆盖旧版本。")
            if st.button("用 DeepSeek 生成/迭代职业知识", key="synthesize_playbook", type="primary"):
                if not ds_key:
                    st.error("请先在左侧输入 DeepSeek API Key。")
                elif int(profile.get("sample_count") or 0) < 2:
                    st.error("至少需要 2 个样本才能开始归纳；建议 8 个以上再形成较稳定的经验知识。")
                else:
                    try:
                        with st.spinner("DeepSeek 正在从真实 Log 样本中归纳职业行为规律..."):
                            source_evidence = online_context_for_analysis(k_cls, k_spec, limit=10) if use_online_knowledge else {}
                            playbook = synthesize_class_playbook_with_deepseek(
                                ds_key, k_cls, k_spec, profile, source_evidence, ds_model, reasoning_effort=reasoning_effort
                            )
                        pb_id = save_playbook(k_cls, k_spec, profile, playbook, ds_model, notes="Generated from local learned log cohort")
                        st.success(f"职业知识版本 #{pb_id} 已保存。后续 AI 分析会自动检索使用。")
                        st.json(playbook)
                    except Exception as e:
                        st.exception(e)

            books = list_playbooks(k_cls, k_spec, 20)
            if books:
                st.markdown("#### E. 历史职业知识版本")
                book_view = pd.DataFrame([{
                    "id": x.get("id"), "时间": x.get("created_at"), "样本数": x.get("sample_count"),
                    "模型": x.get("model"), "版本范围": x.get("patch_scope"), "副本范围": x.get("dungeon_scope"), "层数范围": x.get("key_scope"),
                } for x in books])
                st.dataframe(book_view, use_container_width=True, hide_index=True)
                latest = books[0]
                try:
                    latest_json = json.loads(latest.get("playbook_json") or "{}")
                except Exception:
                    latest_json = {}
                with st.expander("查看最新职业知识"):
                    st.json(latest_json)
                del_pb = st.number_input("删除职业知识版本 ID", min_value=0, step=1, value=0, key="delete_playbook_id")
                if st.button("删除这个知识版本", disabled=int(del_pb) <= 0):
                    delete_playbook(int(del_pb))
                    st.success("已删除。")
                    st.rerun()
    else:
        st.info("职业样本库还是空的。先从上面的已上传 Log 加入样本。")


# ---- 11. online learning center ----
with online_tab:
    st.markdown("### 联网学习中心 · WCL 实战样本 + 可追溯公开资料")
    st.caption(
        "联网学习分两条证据链：WCL API 用来学习真实玩家行为；公开网页只作为机制/攻略辅助资料。"
        "所有来源都会保存 URL/Report/Fight/Revision/抓取时间，DeepSeek 只负责归纳，不把网页观点伪装成 Log 事实。"
    )
    st.info("WCL 排名只负责发现候选样本。真正进入职业知识库前，程序会重新读取对应 Report/Fight 和玩家 Cast/Damage/Buff 事件。")

    st.markdown("#### A. 从 Warcraft Logs 自动发现并学习同职业样本")
    st.caption("可以直接从 WCL 目录选择版本/副本/Encounter，不需要自己查数字 ID。目录数据会缓存到当前软件会话。")

    cat1, cat2 = st.columns([1, 3])
    if cat1.button("加载 WCL 版本目录", key="load_wcl_expansions"):
        if not (wcl_id and wcl_secret):
            st.error("请先在左侧输入 WCL Client ID / Client Secret。")
        else:
            try:
                with st.spinner("正在读取 WCL WorldData 目录..."):
                    cli = WCLClient(wcl_id, wcl_secret)
                    raw_exp = cli.world_expansions()
                    st.session_state["wcl_world_expansions"] = (((raw_exp.get("worldData") or {}).get("expansions")) or [])
                st.success("WCL 版本目录已加载。")
            except Exception as e:
                st.exception(e)

    expansions = st.session_state.get("wcl_world_expansions") or []
    selected_catalog_encounter = 0
    if expansions:
        exp_options = {f"{x.get('name')} · ID {x.get('id')}": int(x.get('id') or 0) for x in expansions if x.get('id')}
        exp_labels = list(exp_options)
        # WCL normally returns expansions in chronological order; default to the latest entry.
        exp_label = cat2.selectbox("WCL 版本", exp_labels, index=max(0, len(exp_labels)-1), key="wcl_catalog_expansion") if exp_labels else None
        selected_expansion_id = exp_options.get(exp_label, 0) if exp_label else 0
        if selected_expansion_id:
            zone_cache_key = f"wcl_world_zones_{selected_expansion_id}"
            if st.button("加载该版本副本 / Encounter", key="load_wcl_zones"):
                try:
                    with st.spinner("正在读取该版本的副本与 Encounter..."):
                        cli = WCLClient(wcl_id, wcl_secret)
                        raw_zones = cli.world_zones(selected_expansion_id)
                        st.session_state[zone_cache_key] = (((raw_zones.get("worldData") or {}).get("zones")) or [])
                    st.success("副本目录已加载。")
                except Exception as e:
                    st.exception(e)
            zones = st.session_state.get(zone_cache_key) or []
            encounter_options = {}
            for z in zones:
                zname = str(z.get("name") or f"Zone {z.get('id')}")
                bracket = z.get("brackets") or {}
                bracket_type = str(bracket.get("type") or "")
                for enc in z.get("encounters") or []:
                    if not enc.get("id"):
                        continue
                    extra = f" · {bracket_type}" if bracket_type else ""
                    label = f"{zname} → {enc.get('name')} · Encounter {enc.get('id')}{extra}"
                    encounter_options[label] = int(enc.get("id") or 0)
            if encounter_options:
                enc_label = st.selectbox("从 WCL 目录选择 Encounter", ["手动输入 Encounter ID"] + list(encounter_options), key="wcl_catalog_encounter")
                if enc_label != "手动输入 Encounter ID":
                    selected_catalog_encounter = encounter_options.get(enc_label, 0)

    w1, w2, w3 = st.columns(3)
    online_encounter = w1.number_input("WCL Encounter ID（目录选择后自动覆盖）", min_value=0, step=1, value=0, key="online_encounter")
    online_class = w2.text_input("职业（WCL 英文名）", value="Warrior", key="online_class")
    online_spec = w3.text_input("专精（WCL 英文名）", value="Protection", key="online_spec")
    selected_encounter_id = int(selected_catalog_encounter or online_encounter or 0)
    if selected_catalog_encounter:
        w1.caption(f"当前使用目录 Encounter ID：{selected_catalog_encounter}")

    w4, w5, w6, w7 = st.columns(4)
    online_bracket = w4.number_input("钥匙/Bracket（0=不限）", min_value=0, step=1, value=0, key="online_bracket")
    online_pages = w5.number_input("排名页数", min_value=1, max_value=5, step=1, value=1, key="online_pages")
    online_limit = w6.number_input("本次最多学习样本", min_value=1, max_value=50, step=1, value=8, key="online_limit")
    online_level = w7.selectbox("证据深度", ["standard", "deep", "light"], index=0, help="standard=施法+伤害+Buff；deep 再读取资源事件；light 只读取施法+伤害。")
    online_region = st.text_input("服务器区域（可选，例如 US/EU/KR/TW/CN；留空不筛）", value="", key="online_region")
    online_auto_playbook = st.checkbox(
        "本批 WCL 学习完成后自动让 DeepSeek 生成/迭代职业 Playbook（会产生 API 费用）",
        value=False, key="online_auto_playbook",
        help="默认关闭。只有勾选并且成功导入样本后才会调用你的 DeepSeek API。",
    )

    if st.button("从 WCL 发现并学习", type="primary", key="online_wcl_learn"):
        if not (wcl_id and wcl_secret):
            st.error("请先在左侧输入 WCL Client ID / Client Secret。")
        elif int(selected_encounter_id) <= 0 or not online_class.strip() or not online_spec.strip():
            st.error("需要 Encounter ID、职业和专精。")
        elif online_auto_playbook and not ds_key:
            st.error("你勾选了自动迭代 Playbook，请先输入 DeepSeek API Key；或者取消勾选。")
        else:
            try:
                with st.spinner("正在从 WCL 排名发现候选，并重新读取底层 Report/Fight 事件..."):
                    cli = WCLClient(wcl_id, wcl_secret)
                    result = learn_from_wcl_rankings(
                        cli,
                        encounter_id=int(selected_encounter_id),
                        class_name=online_class.strip(),
                        spec_name=online_spec.strip(),
                        bracket=int(online_bracket) or None,
                        pages=int(online_pages),
                        sample_limit=int(online_limit),
                        server_region=online_region.strip() or None,
                        evidence_level=online_level,
                    )
                if online_auto_playbook and int(result.get("imported") or 0) > 0:
                    imported_versions = [str(x.get("patch_scope") or "") for x in (result.get("samples") or []) if x.get("patch_scope")]
                    target_patch = max(set(imported_versions), key=imported_versions.count) if imported_versions else ""
                    cohort = select_knowledge_samples(
                        online_class.strip(), online_spec.strip(), "", 0, 0, 500,
                        patch_scope=target_patch,
                    )
                    profile = empirical_cohort_profile(cohort)
                    if target_patch:
                        profile["active_patch_scope"] = target_patch
                    if int(profile.get("sample_count") or 0) >= 2:
                        with st.spinner("WCL 样本已入库，DeepSeek 正在生成新一版职业 Playbook..."):
                            source_evidence = online_context_for_analysis(online_class.strip(), online_spec.strip(), limit=15)
                            playbook = synthesize_class_playbook_with_deepseek(
                                ds_key, online_class.strip(), online_spec.strip(), profile, source_evidence,
                                ds_model, reasoning_effort=reasoning_effort,
                            )
                        pb_id = save_playbook(
                            online_class.strip(), online_spec.strip(), profile, playbook, ds_model,
                            notes=f"Auto after WCL online learning; encounter={selected_encounter_id}; patch={target_patch or 'mixed'}; imported={result.get('imported',0)}",
                            patch_scope=target_patch,
                        )
                        result["generated_playbook_id"] = pb_id
                        result["generated_playbook"] = playbook
                    else:
                        result["playbook_note"] = "样本少于 2，已跳过自动 Playbook。"
                st.session_state["online_wcl_result"] = result
                st.success(f"学习完成：候选 {result.get('candidates_seen',0)}，导入 {result.get('imported',0)}，跳过 {result.get('skipped',0)}，失败 {result.get('failed',0)}。")
                if result.get("generated_playbook_id"):
                    st.success(f"DeepSeek 已生成职业知识版本 #{result.get('generated_playbook_id')}。")
            except Exception as e:
                st.exception(e)

    if st.session_state.get("online_wcl_result"):
        rr = st.session_state["online_wcl_result"]
        if rr.get("samples"):
            st.dataframe(pd.DataFrame(rr["samples"]), use_container_width=True, hide_index=True)
        if rr.get("rate_limit"):
            st.caption(f"WCL Rate Limit：{rr.get('rate_limit')}")
        if rr.get("generated_playbook"):
            with st.expander(f"查看本批自动生成的职业 Playbook #{rr.get('generated_playbook_id')}"):
                st.json(rr.get("generated_playbook"))
        if rr.get("playbook_note"):
            st.caption(rr.get("playbook_note"))
        if rr.get("errors"):
            with st.expander("查看跳过/失败原因"):
                for x in rr["errors"]:
                    st.write("-", x)

    st.markdown("#### B. 把公开职业资料变成带来源的辅助知识")
    st.caption("非 WCL 来源默认只抓取你明确提供的公开 URL，不进行无限爬虫。程序限制页面大小并拒绝本机/局域网地址。")
    ref_url = st.text_input("公开资料 URL", value="", placeholder="https://...", key="ref_url")
    r1, r2, r3, r4 = st.columns(4)
    ref_class = r1.text_input("适用职业（可选）", value="", key="ref_class")
    ref_spec = r2.text_input("适用专精（可选）", value="", key="ref_spec")
    ref_patch = r3.text_input("版本/补丁范围（可选）", value="", placeholder="例如 12.0 / S1", key="ref_patch")
    ref_kind = r4.selectbox("资料类型", ["mechanics", "guide", "official_note", "reference"], key="ref_kind")
    rb1, rb2 = st.columns(2)
    if rb1.button("抓取并保存资料", key="fetch_reference"):
        if not ref_url.strip():
            st.error("请输入 URL。")
        else:
            try:
                with st.spinner("正在抓取公开页面并保存来源快照..."):
                    doc = ingest_public_reference(
                        ref_url.strip(), class_name=ref_class.strip(), spec_name=ref_spec.strip(),
                        patch_scope=ref_patch.strip(), evidence_kind=ref_kind,
                    )
                st.session_state["last_reference_doc"] = doc
                st.success(f"已保存来源 #{doc.get('source_id')}：{doc.get('title')}")
                st.caption(f"来源等级：{doc.get('trust_class')} · 正文 {len(doc.get('text') or '')} 字符")
            except Exception as e:
                st.exception(e)

    if rb2.button("抓取 + DeepSeek 提炼为结构化证据", key="extract_reference_ai"):
        if not ds_key:
            st.error("请先输入 DeepSeek API Key。")
        elif not ref_url.strip():
            st.error("请输入 URL。")
        else:
            try:
                with st.spinner("正在抓取资料，并由 DeepSeek 区分机制事实 / 社区建议 / 不确定信息..."):
                    doc = ingest_public_reference(
                        ref_url.strip(), class_name=ref_class.strip(), spec_name=ref_spec.strip(),
                        patch_scope=ref_patch.strip(), evidence_kind=ref_kind,
                    )
                    ev = extract_reference_evidence_with_deepseek(
                        ds_key, doc, ref_class.strip(), ref_spec.strip(), ref_patch.strip(), ref_kind,
                        ds_model, reasoning_effort=reasoning_effort,
                    )
                    evidence_id = save_external_evidence(
                        int(doc["source_id"]), ev,
                        class_name=ref_class.strip(), spec_name=ref_spec.strip(), patch_scope=ref_patch.strip(),
                        evidence_kind=ref_kind, model=ds_model,
                    )
                st.success(f"结构化联网证据 #{evidence_id} 已保存。后续 AI 分析可自动检索使用。")
                st.json(ev)
            except Exception as e:
                st.exception(e)

    st.markdown("#### C. 联网证据库 / 数据血缘")
    source_rows = list_online_sources(200)
    evidence_rows = list_external_evidence("", "", 100)
    learn_runs = list_online_learning_runs(30)
    c1, c2, c3 = st.columns(3)
    c1.metric("已保存联网快照", len(source_rows))
    c2.metric("已提炼外部证据", len(evidence_rows))
    c3.metric("WCL 学习批次", len(learn_runs))
    if source_rows:
        source_view = pd.DataFrame([{
            "id": x.get("id"), "时间": x.get("fetched_at"), "类型": x.get("source_type"),
            "标题": x.get("title"), "标识": x.get("canonical_id"), "revision": x.get("revision"),
            "可信类别": x.get("trust_class"), "URL": x.get("url"),
        } for x in source_rows[:100]])
        st.dataframe(source_view, use_container_width=True, hide_index=True)
    if evidence_rows:
        with st.expander("查看最近的 DeepSeek 结构化联网证据"):
            for item in evidence_rows[:10]:
                st.markdown(f"**#{item.get('id')} · {item.get('title') or item.get('url')}**")
                st.caption(f"{item.get('trust_class')} · {item.get('fetched_at')} · {item.get('url')}")
                st.json(item.get("evidence") or {})
