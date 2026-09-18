from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from typing import Any

from openai import OpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"  # Preferred current id; routes to DeepSeek V4.1 Flash

SYSTEM_PROMPT = """你是一名专门分析《魔兽世界》正式服 Mythic+ / 团本战斗日志的高级数据分析师。
你的工作不是复述表格，而是像数据分析师一样解释“差异从哪里来、证据是什么、下一步怎么验证”。

硬性规则：
1. 优先相信程序提供的结构化统计，不自行编造技能、职业机制、天赋、装备或数值。
2. 数值事实、可能原因、改进建议必须分开写；不能把推断说成日志已经证明的事实。
3. 横向比较时优先做归一化：战斗时长、技能伤害占比、施放次数/分钟、每次施放伤害、队伍伤害占比、死亡和打断。
4. 同职业/同专精比较重点：技能伤害构成、施放频率、命中/暴击、平均命中、每次施放伤害、每波怪表现和爆发窗口。
5. 队伍比较重点：总时长、队伍DPS、人员伤害贡献、每波怪耗时、死亡、打断、治疗、承伤和节奏差异。
6. 深度效率数据存在时，必须检查：观察到的 Buff uptime、技能施法 cadence、资源高位/低位驻留、ENERGIZE overcap、目标切换、Pull 间空档、死亡后回到有效动作的时间、Buff 与峰值伤害窗口的重合。
7. “施法间隔”只是日志观察到的 cadence，除非程序提供职业技能数据库/理论冷却，不得把某个间隔直接称为“CD延误”。资源快照和 overcap 也只能解释日志能观察到的那部分资源机制。
8. Pull 间空档可能是跑图、RP、电梯、等怪、路线或队伍决策；没有更多证据时不能直接判定为失误。
9. 如果没有天赋、装备、路线、目标数量、爆发窗口、Buff uptime、饰品/药水等证据，明确写“当前数据不能证明”。
10. 响应性/卡顿诊断必须区分证据强度：Combat Log 只能证明“出现了异常停手/施法节奏”，不能单独证明网络问题。只有 latency/FPS 遥测与异常窗口时间重合时，才能把网络延迟或客户端卡顿作为有直接支持的原因。
11. 分析响应性时重点看：战斗活跃期间长施法空档、空档时队友是否持续施法、玩家是否继续承伤/平砍、读条时长离群、失败施法、平砍节奏异常。移动、机制、转火、失去目标、资源等待都可能造成类似现象。
12. 如果 telemetry 显示 World latency 异常而 FPS 正常，并与停手窗口重合，可写“网络问题得到遥测支持”；若 FPS 明显下降而 World latency 正常，可写“本地性能/掉帧得到遥测支持”；两者都没有则不要归因。
13. 历史案例/经验是辅助材料。用户明确纠正过的历史案例优先于旧AI报告，但历史经验永远不能覆盖当前日志的直接证据。
14. 如果提供 learned_class_knowledge / 职业知识上下文，它来自真实日志样本的经验归纳，只能称为“经验规律/参考组差异/样本基准”；除非有独立职业机制证据，不能把它写成理论最优循环或因果关系。
15. 样本学习中优先关注跨多个样本稳定出现的行为：核心技能 casts/min、damage share、hits/cast、Buff覆盖、资源高位/溢出、爆发窗口、目标切换和响应性。样本少于4时必须显著降低职业知识结论置信度。
16. 不要建议为了提高总伤害而故意忽略关键机制、中断或生存责任。
17. 如果证据互相冲突，明确写冲突点并降低置信度。
18. 如果 analysis_request.mode 是 selected_wcl_fight，说明用户主动从自己的 WCL 最近 Log 中选择了这一场。优先分析该玩家这一场的技能构成、casts/min、Buff、资源、承伤、死亡、打断和响应性；不要把同一 Report 里没有选择的其他 Fight 混进结论。
19. direct_fight_evidence 是 WCL 底层事件直接汇总，属于当前战斗的强证据；职业知识/历史案例只用于解释和对照。
20. 如果 learned_class_knowledge.personal_behavior_model 存在，它表示“这个角色自己的历史行为分布”。个人基线优先用于判断“本场是否异常偏离本人平时”，职业群体基准用于判断“本人长期模式与同职业样本有何不同”；两者不能混为一谈。
21. 个人基线必须受样本量和条件范围约束。同版本/同专精/同副本/相近层数的样本最可靠；若回退到跨副本或跨版本历史，必须降低置信度。
22. current_vs_personal 的异常只能证明本场相对本人历史异常。即使响应异常分、P95施法间隔或最大空档超过个人 P90，也不能单独证明网络或掉帧；必须结合 latency/FPS 遥测或其他直接证据。
23. 所有给玩家看的解释必须使用简体中文。程序内部可以使用 percentile、casts_per_min 等字段，但用户可见结论不得直接堆 P10/P25/P50/P75/P90、casts/min、hits/cast 等统计术语；必须翻译成“低于本人常态 / 接近同类玩家典型水平 / 高于多数参考玩家 / 优秀参考范围”等自然语言。
24. 单场异常与长期习惯必须分开：只在一场出现的问题称为“本场异常”，多场重复出现且有足够样本时才能称为“重复性问题/长期习惯”。
25. 多场分析时先找重复出现的问题，再找单次异常；不得把某一场的偶发事件概括为长期结论。
26. 横向职业比较必须注明样本数量、匹配条件和可能的装备/副本/层数/路线混杂，但面向玩家时用自然语言表达，不要求玩家理解统计学术语。
27. “异常停手”只有在玩家整个窗口内存活、且该窗口位于明确战斗/Pull 内时才成立。任何跨越死亡事件的施法空档必须排除，不得归类为卡顿、输入中断或响应异常。
28. 如果 direct_fight_evidence.team_activity_stalls 提供 player_alive_for_entire_gap / inside_combat_pull，优先使用这些经过过滤的窗口；原始最大施法间隔、死亡后的复活等待、波次间跑图都不能单独作为卡顿证据。
29. 如果选择的多场来自不同副本，横向同职业比较必须按副本、版本、相近层数分组。跨副本只能讨论重复出现的个人习惯，不能直接把DPS或技能占比平均后下结论。
30. learned_class_knowledge.cohort_match_scope 表示本次横向样本的匹配质量。样本不足或已放宽到跨副本/跨版本时，必须明确降低结论强度。
31. 面向玩家的技能名称尽量使用简体中文官方/常用译名。若结构化数据只有英文技能名，可在不改变技能身份的前提下翻译；无法确认时写“中文说明（英文原名）”，不要只输出英文技能名。
32. 如果 learned_class_knowledge.cohort_groups 存在，必须按组分别使用横向参考，不得把不同副本的参考样本合并成一个“同条件样本”。
33. cohort_match_details.tier 为 strong/strong_context 时可使用“同条件参考”措辞；standard/relaxed_key 只能写“相近条件参考”；broad_patch/cross_patch 必须明确写“参考条件已放宽”，并降低结论强度。
34. 横向样本数量少于4时，不得用“稳定差距/长期落后”等强措辞；只能写“目前样本不足，出现了某种倾向”。
35. 教练建议必须优先来自“重复出现且横向样本也支持”的问题；只在个人基线异常但职业横向不支持时，应该称为“本场偏离本人习惯”，不要自动升级为职业打法问题。
36. 如果 efficiency.cast_cadence 存在，优先使用 total_casts、同一 Pull 内的施放间隔和 casts_by_pull 判断技能节奏；不要用跨 Pull 的长间隔误判“技能没按”。
37. 如果 efficiency.cast_duration 存在，start_to_cast_* 只代表日志观察到的 begincast→cast 时长；可以用于比较同一玩家/同技能的异常读条，但不能冒充技能数据库中的理论施法时间。
38. Buff 分析优先使用 combat_uptime_pct，而不是 whole_run_uptime_pct。整场流程覆盖会被跑图、RP、Pull 间隔稀释；只有讨论整个副本流程时才使用整场口径。
39. 如果 efficiency.buff_burst_overlap 存在，优先回答“关键 Buff 是否覆盖主要伤害窗口”；如果只有 Buff 总覆盖率，不要臆测爆发同步问题。
40. 如果 timeline.pull_breakdown 存在，尽量指出问题发生在哪几波，并区分“总次数不足”与“次数够但分配到错误波次”。
41. skills.total_casts 是本场实际观察到的成功施放次数；面向玩家可以直接说“本场用了 X 次”，比只报每分钟频率更易理解。
42. 如果 target_focus / learned_class_knowledge.target_focus_comparison / dungeon_target_knowledge 存在，必须单独检查 BOSS、常见优先集火目标、重要大怪的伤害投入。优先比较目标伤害占比、目标DPS、前8秒直接目标技能投入和是否在当前场次出现。
43. “优先集火目标”是从多份限时 WCL 中学习到的经验行为标签，不是官方机制定义。只有样本重复、置信度足够时才可以写“参考玩家通常优先处理”；低样本或低置信度只能写“目前样本显示可能更常被优先处理”。
44. 重要目标对比必须优先回答：玩家是否把伤害打在真正高价值目标上，而不是只看总DPS。总DPS高但BOSS/优先目标伤害明显偏低时，要明确指出“总伤害并没有转化为关键目标伤害”。
45. Boss战中的小怪/召唤物不能因为出现在Boss Pull就自动当成Boss；以程序提供的 learned_role / is_boss 为准。

普通文本输出时固定结构：
- 执行摘要：3-6条最重要差异
- 队伍层面差异
- 同职业/技能层面差异
- 每波怪 / 死亡 / 爆发时间轴（如有）
- 深度战斗效率：Buff / 资源 / cadence / 转火 / Pull空档 / 死亡恢复（如有）
- 响应性 / 卡顿 / 网络诊断（如有）
- 能被日志直接证明的事实
- 仍需验证的可能原因
- 下一把最值得验证的3-5件事
"""

JSON_SYSTEM_PROMPT = SYSTEM_PROMPT + """

你必须输出一个合法 json object，且只输出 JSON。格式：
{
  "executive_summary": ["..."],
  "team_differences": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "skill_differences": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "target_focus_findings": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "timeline_findings": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "efficiency_findings": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "personal_baseline_findings": [{"finding":"...","evidence":"...","confidence":"high|medium|low"}],
  "responsiveness": {
    "diagnosis":"...",
    "network_supported": false,
    "fps_supported": false,
    "evidence":["..."],
    "alternative_explanations":["..."]
  },
  "proven_facts": ["..."],
  "hypotheses": [{"hypothesis":"...","why":"...","how_to_verify":"...","confidence":"high|medium|low"}],
  "next_actions": ["..."],
  "overall_confidence": "high|medium|low"
}
"""


_CLIENT_CACHE: dict[tuple[str, str], OpenAI] = {}
_CLIENT_CACHE_LOCK = threading.RLock()

# Re-opening the same fight/report should not pay two DeepSeek round-trips again.
# Cache is process-local, bounded, and keyed by a one-way hash of both evidence and API key.
_COACH_CACHE: dict[str, tuple[float, dict[str, Any], str, dict[str, Any]]] = {}
_COACH_CACHE_LOCK = threading.RLock()
_COACH_CACHE_TTL_S = 15 * 60
_COACH_CACHE_MAX = 12
_COACH_INFLIGHT_LOCK = threading.RLock()
_COACH_INFLIGHT: dict[str, threading.Event] = {}


def _coach_cache_key(
    api_key: str, model: str, mode: str, payload: Any, source_summary: Any,
    memory_context: Any, knowledge_context: Any,
) -> str:
    packed = _body({
        "api_key_hash": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
        "model": model or DEFAULT_MODEL, "mode": mode,
        "payload": payload, "source_summary": source_summary,
        "memory_context": memory_context, "knowledge_context": knowledge_context,
    })
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _coach_cache_get(key: str) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    now = time.time()
    with _COACH_CACHE_LOCK:
        row = _COACH_CACHE.get(key)
        if not row:
            return None
        expires, analysis, report, meta = row
        if now >= expires:
            _COACH_CACHE.pop(key, None)
            return None
        out_meta = dict(meta)
        out_meta["cache_hit"] = True
        out_meta["total_seconds"] = 0.0
        return copy.deepcopy(analysis), report, out_meta


def _coach_cache_put(key: str, analysis: dict[str, Any], report: str, meta: dict[str, Any]) -> None:
    with _COACH_CACHE_LOCK:
        if len(_COACH_CACHE) >= _COACH_CACHE_MAX:
            oldest = min(_COACH_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _COACH_CACHE.pop(oldest, None)
        _COACH_CACHE[key] = (time.time() + _COACH_CACHE_TTL_S, copy.deepcopy(analysis), report, dict(meta))


def _client(api_key: str, base_url: str) -> OpenAI:
    """Reuse the OpenAI-compatible HTTP client so consecutive DeepSeek passes share connections."""
    key = api_key.strip()
    if not key:
        raise ValueError("DeepSeek API Key 为空")
    cache_key = (key, base_url)
    with _CLIENT_CACHE_LOCK:
        cli = _CLIENT_CACHE.get(cache_key)
        if cli is None:
            cli = OpenAI(api_key=key, base_url=base_url, timeout=60.0, max_retries=2)
            _CLIENT_CACHE[cache_key] = cli
        return cli


def _body(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=lambda o: o.item() if hasattr(o, "item") else str(o),
    )


def _thinking_kwargs(effort: str) -> dict[str, Any]:
    effort = effort if effort in {"none", "low", "high", "max"} else "high"
    return {"reasoning_effort": effort, "extra_body": {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}}


def analyze_with_deepseek(
    api_key: str,
    payload: dict[str, Any],
    model: str = DEFAULT_MODEL,
    extra_context: str = "",
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "high",
    memory_context: dict[str, Any] | None = None,
    knowledge_context: dict[str, Any] | None = None,
) -> str:
    client = _client(api_key, base_url)
    enriched = dict(payload)
    if memory_context:
        enriched["historical_learning_context"] = memory_context
    if knowledge_context:
        enriched["learned_class_knowledge"] = knowledge_context
    user = (
        "下面是程序从战斗日志计算出的结构化对比数据。请把它当作事实数据源，"
        "不要重新猜测基础统计。重点解释横向差异和下一步验证动作。\n\n" + _body(enriched)
    )
    if extra_context.strip():
        user += f"\n\n用户补充信息：{extra_context.strip()}"

    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "max_tokens": 8000,
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def analyze_with_deepseek_json(
    api_key: str,
    payload: dict[str, Any],
    model: str = DEFAULT_MODEL,
    extra_context: str = "",
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "high",
    memory_context: dict[str, Any] | None = None,
    knowledge_context: dict[str, Any] | None = None,
    max_tokens: int = 5200,
) -> dict[str, Any]:
    client = _client(api_key, base_url)
    enriched = dict(payload)
    if memory_context:
        enriched["historical_learning_context"] = memory_context
    if knowledge_context:
        enriched["learned_class_knowledge"] = knowledge_context
    user = (
        "请根据下面的结构化战斗数据生成 json 分析。只使用数据中存在的证据。"
        "历史案例只是辅助，不得覆盖当前证据。\n\n" + _body(enriched)
    )
    if extra_context.strip():
        user += f"\n\n用户补充信息：{extra_context.strip()}"
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": JSON_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max(1800, min(9000, int(max_tokens))),
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {
            "executive_summary": ["模型没有返回可解析的 JSON，已保留原始输出。"],
            "raw_output": content,
            "overall_confidence": "low",
        }



PLAYER_REPORT_SYSTEM_PROMPT = """你是《魔兽世界》正式服大秘境的资深中文教练兼战斗日志主编。你收到的是已经由统计程序和分析模型整理过的证据，不是原始日志。你的工作是把证据变成玩家下一把能执行的指导，而不是把数字换一种说法重新念一遍。

硬性要求：
1. 全文使用简体中文。不要输出 JSON、代码块或英文分析术语。
2. 技能、职业、专精、副本优先使用简体中文官方/常用名称。只有无法确认中文名称时，才写“中文说明（英文原名）”。禁止整段英文技能列表。
3. 不要直接向玩家展示 P10/P25/P50/P75/P90、percentile、casts/min、hits/cast、z-score、cadence 等后台统计术语。必须翻译成“低于本人常态”“接近同条件玩家常见水平”“高于多数优秀参考玩家”等人话。
4. 第一屏只给最重要的 2-4 个结论。不要为了显得全面而堆十几条指标。
5. 每个问题必须回答三件事：**哪里有问题、为什么重要、下一把具体怎么改**。禁止“优化循环”“注意细节”“多练习”这类空话。
6. 必须严格区分：本场偶发异常、本人长期习惯、与同职业同专精参考玩家的稳定差距。不要把一次偶发事件说成长期开法。
7. 横向比较必须优先使用 cohort_match_details / cohort_match_scope。只有“同版本、同副本、相近层数、限时成功”的强匹配样本才可以用肯定语气；如果样本已跨副本、跨版本或层数范围明显放宽，必须明确说“参考条件不完全一致”，并降低结论强度。
8. 多场分析时，先按副本/层数分组，再判断跨副本重复习惯。不同副本的 DPS、技能占比不能直接平均后拿来下结论。
9. 网络/卡顿判断只能引用“玩家存活、处于明确战斗窗口、队友仍持续战斗”的单人停手证据。死亡、等待复活、跑图、转阶段、全队停手全部不能写成卡顿。
10. 没有 latency/FPS 遥测时，不得写“确定网络卡”；只能说“存在单人动作中断证据，原因仍需结合网络/掉帧/机制进一步判断”。
11. 不要把相关性包装成职业理论。如果只是 WCL 样本显示优秀参考玩家更常做某件事，要写成“参考玩家更常出现这种行为”，不能写“正确循环必须这样”。
12. 如果用户自己的长期习惯本身就落后同专精参考玩家，要明确区分：“这把没有偏离你平时，但你平时的习惯本身与同条件参考玩家有差距”。
13. 用“教练语言”解释影响：例如“少按了核心技能，会让资源转化和爆发窗口利用同时变差”，但只有结构化证据支持时才能这样写；证据不足就说不足。
14. 如果分析里有技能总施放次数、战斗内 Buff 覆盖、Buff 与爆发窗口重合、同一 Pull 内施放间隔或每波技能分配，优先用这些信息解释“少按 / 晚按 / 错波次 / Buff没覆盖爆发”的区别，不要退化成只念伤害占比。
15. 如果存在 target_focus_findings 或目标对比证据，必须增加“BOSS / 优先目标伤害”小节。重点讲：BOSS伤害、常见优先集火大怪、重要目标的伤害投入是否接近同专精参考；不能只讨论总伤害。
16. 对WCL学习出的优先目标，必须使用“参考样本通常更早/更集中处理”这类经验措辞；不要写成官方机制规则。
17. Buff 覆盖默认说“战斗中覆盖”，不要拿包含跑图和波次间隔的整场覆盖误导玩家。
18. 文章必须短段落、短项目。每段最多 3 句，每个小节最多 4 个项目。
19. 报告结构固定为：
   # 战斗教练报告
   ## 先看结论
   ## 最值得先改的问题
   ## 和你自己平时相比
   ## 和同职业同专精玩家相比
   ## BOSS / 优先目标伤害
   ## 异常事件与具体位置
   ## 下一把只做这 3 件事
   ## 证据可靠性
20. “证据可靠性”只需要告诉玩家：参考了多少场、条件匹配是否严格、有没有放宽，不要写统计学术语。
21. 控制篇幅：单场约 700-1300 中文字，多场约 900-1700 中文字。重点清楚优先于面面俱到。
"""


def write_player_report_with_deepseek(
    api_key: str,
    analysis_json: dict[str, Any],
    source_summary: dict[str, Any] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "low",
    max_tokens: int = 2800,
) -> str:
    """Second-pass editor: turn evidence-bound JSON into player-facing Chinese Markdown."""
    client = _client(api_key, base_url)
    payload = {
        "analysis": analysis_json,
        "source_summary": source_summary or {},
        "instruction": "只做中文表达与优先级整理，不新增分析事实。",
    }
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": PLAYER_REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": _body(payload)},
        ],
        "max_tokens": max(1200, min(5000, int(max_tokens))),
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        try:
            resp = client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return render_analysis_markdown(analysis_json)
        return text
    except Exception:
        # The analytical JSON already exists; a report-editor outage must not destroy the report.
        return render_analysis_markdown(analysis_json)


FACT_CHECK_SYSTEM_PROMPT = """你是《魔兽世界》战斗日志报告的事实审校员。输入包含：结构化分析结论、已经写好的中文教练报告、样本来源摘要。你必须输出最终中文 Markdown 报告。

要求：
1. 不新增任何分析事实、技能机制、数值或结论。
2. 删除或降级所有无法从 analysis / source_summary 支持的断言。
3. 检查异常停手：只有玩家存活、战斗中、队友仍活跃的窗口才能保留；死亡/复活/跑图相关必须删除或注明已排除。
4. 检查横向对比：如果 cohort_match_details.relaxed=true 或 tier 属于 broad_patch/cross_patch，必须把强结论改成弱参考；不得写“同条件玩家”这种误导性表达。
5. 检查重要目标结论：只有 target_focus_comparison / dungeon_target_knowledge 支持时才能保留“BOSS/优先目标”判断；learned_role=priority_focus 仍然只是WCL经验规律，不能改写成官方必须击杀顺序。
6. 全文简体中文。把遗留的 percentile、P50/P90、casts/min、hits/cast、cadence 等术语改成玩家语言。
7. 英文技能名尽量翻成简体中文常用名；无法确认时保留“中文说明（英文原名）”，不要臆造官方译名。
8. 保持教练式结构和可执行建议；不要把报告改回数据流水账。
9. 只输出修订后的 Markdown，不要解释审校过程。"""


def verify_player_report_with_deepseek(
    api_key: str,
    analysis_json: dict[str, Any],
    draft_markdown: str,
    source_summary: dict[str, Any] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "low",
) -> str:
    """Third pass: evidence-check the coach report and remove unsupported language."""
    if not draft_markdown.strip():
        return draft_markdown
    client = _client(api_key, base_url)
    payload = {
        "analysis": analysis_json,
        "source_summary": source_summary or {},
        "draft_report": draft_markdown,
    }
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": FACT_CHECK_SYSTEM_PROMPT},
            {"role": "user", "content": _body(payload)},
        ],
        "max_tokens": 5200,
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        try:
            resp = client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("reasoning_effort", None)
            resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return text or draft_markdown
    except Exception:
        return draft_markdown


def synthesize_class_playbook_with_deepseek(
    api_key: str,
    class_name: str,
    spec_name: str,
    empirical_profile: dict[str, Any],
    source_evidence: dict[str, Any] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    """Synthesize an evidence-bound empirical playbook from learned log samples.

    This is not model fine-tuning. DeepSeek summarizes cohort evidence into a versioned
    local playbook that can be revised as more samples arrive.
    """
    client = _client(api_key, base_url)
    system = """你是 WoW 战斗日志职业知识研究员。你会收到程序从多份真实 Log 聚合出的经验统计。
只输出合法 JSON object。绝对不能把相关性伪装成因果，不能凭记忆补职业技能机制，也不能把高DPS样本直接称为理论最优。
你的任务是总结：哪些行为在参考样本中稳定不同、哪些只是弱相关、还缺什么证据。
如果同时提供联网资料：只能把带来源/版本信息的资料用于解释或提出验证方向；真实Log样本与资料观点必须分开陈述；社区建议不能升级成游戏机制事实；版本不明确的资料降低置信度。
格式：
{
  "summary":["..."],
  "observed_core_patterns":[{"pattern":"...","evidence":"...","confidence":"high|medium|low","scope":"..."}],
  "rotation_hypotheses":[{"hypothesis":"...","evidence":"...","how_to_validate":"...","confidence":"high|medium|low"}],
  "burst_hypotheses":[...],
  "resource_hypotheses":[...],
  "responsiveness_norms":[...],
  "anti_patterns":[{"pattern":"...","evidence":"...","confidence":"high|medium|low"}],
  "unknowns":["..."],
  "sample_quality":"high|medium|low"
}
样本少于4时 sample_quality 必须为 low；样本少于8时不能轻易给 high confidence。"""
    payload = {
        "class_name": class_name,
        "spec_name": spec_name,
        "empirical_profile": empirical_profile,
        "source_evidence": source_evidence or {},
    }
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": _body(payload)}],
        "response_format": {"type": "json_object"},
        "max_tokens": 7000,
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"summary": ["模型未返回可解析 JSON"], "raw_output": content, "sample_quality": "low"}



def extract_reference_evidence_with_deepseek(
    api_key: str,
    document: dict[str, Any],
    class_name: str = "",
    spec_name: str = "",
    patch_scope: str = "",
    evidence_kind: str = "reference",
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    """Turn one fetched public reference into compact, provenance-bound evidence.

    The caller stores the source URL/hash separately. This function never converts a
    community recommendation into a hard game-mechanics fact without labeling it.
    """
    client = _client(api_key, base_url)
    system = """你是 WoW 战斗分析知识抽取器。用户会给你一份已经抓取的公开网页正文和来源信息。
只输出合法 JSON object。你的任务是提炼少量可用于日志分析的结构化事实/建议，不能复制长段原文，不能凭你的记忆补内容。
必须区分：official_mechanic（官方明确机制）、documented_mechanic（资料明确描述的机制）、community_recommendation（社区打法建议）、uncertain（无法确认）。
如果资料没有明确版本/补丁信息，必须把 version_confidence 设为 low；旧资料不能直接作为当前版本规则。
格式：
{
  "summary":["..."],
  "claims":[{
    "claim":"...",
    "claim_type":"official_mechanic|documented_mechanic|community_recommendation|uncertain",
    "evidence_excerpt":"不超过40个中文字的证据摘要，不要长引用",
    "analysis_use":"这个信息可以怎样帮助解释Combat Log",
    "confidence":"high|medium|low"
  }],
  "rotation_or_priority_hints":[{"hint":"...","confidence":"high|medium|low"}],
  "version_scope":"...",
  "version_confidence":"high|medium|low",
  "do_not_generalize":["..."]
}
没有证据就留空，不要猜。"""
    payload = {
        "source": {
            "title": document.get("title"),
            "url": document.get("url"),
            "fetched_at": document.get("fetched_at"),
            "trust_class": document.get("trust_class"),
            "evidence_kind": evidence_kind,
        },
        "requested_context": {"class_name": class_name, "spec_name": spec_name, "patch_scope": patch_scope},
        "document_text": str(document.get("text") or "")[:35000],
    }
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": _body(payload)}],
        "response_format": {"type": "json_object"},
        "max_tokens": 5000,
    }
    kwargs.update(_thinking_kwargs(reasoning_effort))
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {
            "summary": ["模型没有返回可解析 JSON。"],
            "claims": [],
            "rotation_or_priority_hints": [],
            "version_scope": patch_scope,
            "version_confidence": "low",
            "raw_output": content,
        }

def _friendly_confidence(value: str) -> str:
    return {"high": "证据较强", "medium": "有一定证据", "low": "证据有限"}.get(str(value or "").lower(), "证据程度未知")


def _humanize_stats_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "P10": "本人/样本较低范围", "P25": "较低参考范围", "P50": "典型水平",
        "P75": "较好参考范围", "P90": "优秀参考范围", "p10": "本人/样本较低范围",
        "p25": "较低参考范围", "p50": "典型水平", "p75": "较好参考范围", "p90": "优秀参考范围",
        "casts/min": "每分钟使用次数", "casts_per_min": "每分钟使用次数",
        "hits/cast": "每次使用命中数", "hits_per_cast": "每次使用命中数",
        "percentile": "参考位置",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\bmedian\b", "典型水平", text, flags=re.I)
    return text


def render_analysis_markdown(data: dict[str, Any]) -> str:
    """Readable Chinese fallback when the second-pass report editor is unavailable."""
    if data.get("raw_output"):
        return "# 战斗分析结论\n\nAI 返回内容无法结构化解析。建议重新分析一次。\n"
    lines = ["# 战斗分析结论", "", "## 最重要的结论"]
    for item in (data.get("executive_summary") or [])[:4]:
        lines.append(f"- {_humanize_stats_text(item)}")
    sections = [
        ("和队伍整体相比", "team_differences"),
        ("和同职业同专精玩家相比", "skill_differences"),
        ("具体问题发生在哪里", "timeline_findings"),
        ("资源、爆发与战斗效率", "efficiency_findings"),
        ("和你自己平时相比", "personal_baseline_findings"),
    ]
    for title, key in sections:
        items = data.get(key, []) or []
        if not items:
            continue
        lines += ["", f"## {title}"]
        for item in items[:8]:
            if isinstance(item, dict):
                finding = _humanize_stats_text(item.get("finding") or "").strip()
                ev = _humanize_stats_text(item.get("evidence") or "").strip()
                conf = _friendly_confidence(str(item.get("confidence") or ""))
                if finding:
                    lines.append(f"- **{finding}**（{conf}）")
                if ev:
                    lines.append(f"  - 依据：{ev}")
            elif item:
                lines.append(f"- {_humanize_stats_text(item)}")
    resp = data.get("responsiveness") or {}
    if resp:
        lines += ["", "## 卡顿 / 响应异常"]
        diagnosis = _humanize_stats_text(resp.get("diagnosis") or "").strip()
        if diagnosis:
            lines.append(diagnosis)
        for ev in (resp.get("evidence") or [])[:8]:
            lines.append(f"- {_humanize_stats_text(ev)}")
        if not resp.get("network_supported") and not resp.get("fps_supported"):
            lines.append("- 当前没有足够的网络延迟或帧率证据，不能把停手直接归因于网络或电脑卡顿。")
    actions = data.get("next_actions", []) or []
    if actions:
        lines += ["", "## 下一把只做这 3 件事"] + [f"{i+1}. {_humanize_stats_text(x)}" for i, x in enumerate(actions[:3])]
    lines += ["", "## 证据与限制", f"- 整体判断：{_friendly_confidence(str(data.get('overall_confidence') or ''))}。", "- 结论以当前日志和已学习样本为依据；路线、装备、怪量和队伍配置可能影响横向比较。"]
    return "\n".join(lines).strip() + "\n"




def _compact_for_ai(value: Any, *, depth: int = 0, key: str = "") -> Any:
    """Bound large evidence payloads without removing the metrics used by the coach.

    DeepSeek latency scales with prompt size. The raw learning stores keep full structured
    evidence; the API receives a compact view with the most useful rows first.
    """
    if depth > 7:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in {"raw", "raw_output", "signature_json", "metadata_json"}:
                continue
            out[str(k)] = _compact_for_ai(v, depth=depth + 1, key=str(k))
        return out
    if isinstance(value, list):
        limits = {
            "skills": 24, "buff_uptime": 18, "cast_cadence": 20,
            "cast_duration": 10, "buff_burst_overlap": 14, "burst_windows": 8,
            "pull_breakdown": 24, "windows": 8, "dungeon_pulls": 20, "pulls": 24,
            "player_pulls": 24, "fights": 8, "cohort_groups": 6,
            "recent_playbooks": 2, "samples": 12, "errors": 6,
            "targets": 24, "all_targets": 24, "priority_targets": 16, "important_targets": 16,
            "bosses": 12, "target_focus_findings": 12, "chart_rows": 10,
        }
        limit = limits.get(key, 30)
        return [_compact_for_ai(x, depth=depth + 1, key=key) for x in value[:limit]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…"
    return value


def sanitize_player_markdown(markdown: str) -> str:
    """Fast local finalizer used instead of a third model call in normal mode."""
    text = str(markdown or "").strip()
    if not text:
        return text
    replacements = {
        "casts/min": "每分钟使用次数", "casts_per_min": "每分钟使用次数",
        "hits/cast": "每次使用命中数", "hits_per_cast": "每次使用命中数",
        "cadence": "技能节奏", "percentile": "参考位置",
        "P50": "典型水平", "P90": "优秀参考范围", "P75": "较好参考范围",
        "P25": "较低参考范围", "P10": "较低范围",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Guard against the model surrounding the report with markdown fences.
    text = re.sub(r"^```(?:markdown|md)?\\s*", "", text, flags=re.I)
    text = re.sub(r"\\s*```$", "", text)
    return text.strip()


def run_coach_pipeline(
    api_key: str,
    payload: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    source_summary: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    knowledge_context: dict[str, Any] | None = None,
    speed_mode: str = "fast",
    base_url: str = DEEPSEEK_BASE_URL,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Optimized analysis -> coach pipeline.

    fast/balanced use two network calls. deep keeps the optional third model fact-check.
    The source evidence stays deterministic; only wording/priority is delegated to AI.
    """
    mode = (speed_mode or "fast").strip().lower()
    if mode not in {"fast", "balanced", "deep"}:
        mode = "fast"
    compact_payload = _compact_for_ai(payload)
    compact_memory = _compact_for_ai(memory_context or {}) if memory_context else None
    compact_knowledge = _compact_for_ai(knowledge_context or {}) if knowledge_context else None
    compact_source = _compact_for_ai(source_summary or {})
    cache_key = _coach_cache_key(
        api_key, model or DEFAULT_MODEL, mode, compact_payload, compact_source,
        compact_memory, compact_knowledge,
    )
    cached = _coach_cache_get(cache_key)
    if cached is not None:
        return cached

    # Deduplicate simultaneous clicks / parallel callers for the same evidence.
    # Only the owner performs DeepSeek network calls; waiters reuse the finished cache.
    owner = False
    with _COACH_INFLIGHT_LOCK:
        event = _COACH_INFLIGHT.get(cache_key)
        if event is None:
            event = threading.Event()
            _COACH_INFLIGHT[cache_key] = event
            owner = True
    if not owner:
        event.wait(timeout=180.0)
        cached = _coach_cache_get(cache_key)
        if cached is not None:
            return cached
        # Owner may have failed. Re-enter once the gate is released so a caller can retry.
        return run_coach_pipeline(
            api_key, payload, model=model, source_summary=source_summary,
            memory_context=memory_context, knowledge_context=knowledge_context,
            speed_mode=mode, base_url=base_url,
        )

    try:
        if mode == "deep":
            analysis_effort, analysis_tokens = "high", 6200
            editor_effort, editor_tokens = "low", 3600
        elif mode == "balanced":
            analysis_effort, analysis_tokens = "low", 4800
            editor_effort, editor_tokens = "none", 2800
        else:
            analysis_effort, analysis_tokens = "low", 3600
            editor_effort, editor_tokens = "none", 2200
    
        started = time.perf_counter()
        t0 = time.perf_counter()
        analysis = analyze_with_deepseek_json(
            api_key, compact_payload, model=model, base_url=base_url,
            reasoning_effort=analysis_effort, memory_context=compact_memory,
            knowledge_context=compact_knowledge, max_tokens=analysis_tokens,
        )
        analysis_elapsed = time.perf_counter() - t0
        t1 = time.perf_counter()
        draft = write_player_report_with_deepseek(
            api_key, analysis, compact_source, model=model,
            base_url=base_url, reasoning_effort=editor_effort, max_tokens=editor_tokens,
        )
        editor_elapsed = time.perf_counter() - t1
        if mode == "deep":
            final = verify_player_report_with_deepseek(
                api_key, analysis, draft, compact_source, model=model,
                base_url=base_url, reasoning_effort="low",
            )
            passes = 3
        else:
            final = sanitize_player_markdown(draft)
            passes = 2
        meta = {
            "speed_mode": mode, "api_passes": passes, "model": model,
            "analysis_seconds": round(analysis_elapsed, 2),
            "editor_seconds": round(editor_elapsed, 2),
            "total_seconds": round(time.perf_counter() - started, 2),
            "cache_hit": False,
        }
        _coach_cache_put(cache_key, analysis, final, meta)
        return analysis, final, meta
    finally:
        with _COACH_INFLIGHT_LOCK:
            done = _COACH_INFLIGHT.pop(cache_key, None)
            if done is not None:
                done.set()


PERSONAL_MODEL_COACH_PROMPT = """你是一名《魔兽世界》正式服大秘境中文教练。输入是一个玩家已经同步完成的个人历史模型，以及同职业同专精参考样本。请只总结长期重复模式，不点评某一场。
要求：
1. 全文简体中文，技能尽量使用中文；不要出现 P10/P50/P90、casts/min、percentile 等术语。
2. 先说明当前个人模型覆盖了多少场、主要是什么副本/层数，样本少时明确降低语气。
3. 只给 2-4 条真正有用的长期结论：哪些习惯稳定、哪些长期与同专精参考玩家存在差距、哪些只是数据不足。
4. 每条需要包含“现象 → 为什么值得关注 → 下一把怎么做”。
5. 个人历史不等于正确打法；只有同专精横向样本也支持时，才把它称为长期改进方向。
6. 响应/停手只总结玩家存活且处于战斗中的有效异常；死亡、复活、跑图不算。
7. 如果个人模型包含 buffs / cadence，优先总结重复出现的“战斗内 Buff 覆盖”“技能总次数/同一波内节奏”“爆发与 Buff 同步”模式，不要只总结 DPS 或每分钟频率。
8. 输出短 Markdown，控制在 500-900 中文字。"""


def summarize_personal_model_with_deepseek(
    api_key: str,
    profile: dict[str, Any],
    cohort_context: dict[str, Any] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
) -> str:
    client = _client(api_key, base_url)
    payload = {
        "personal_model": _compact_for_ai(profile),
        "same_spec_reference": _compact_for_ai(cohort_context or {}),
    }
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": PERSONAL_MODEL_COACH_PROMPT},
            {"role": "user", "content": _body(payload)},
        ],
        "max_tokens": 1500,
    }
    kwargs.update(_thinking_kwargs("none"))
    try:
        resp = client.chat.completions.create(**kwargs)
    except TypeError:
        kwargs.pop("reasoning_effort", None)
        resp = client.chat.completions.create(**kwargs)
    return sanitize_player_markdown(resp.choices[0].message.content or "")


def learn_lesson_with_deepseek(
    api_key: str,
    context: dict[str, Any],
    report: str,
    rating: int,
    confirmed_cause: str,
    correction: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEEPSEEK_BASE_URL,
) -> dict[str, Any]:
    """Convert user feedback into a reusable, cautious lesson.

    This is memory/RAG, not model weight training. The generated lesson is stored locally
    and can be deleted by the user.
    """
    client = _client(api_key, base_url)
    prompt = {
        "context": context,
        "previous_report_excerpt": report[:5000],
        "user_rating": rating,
        "confirmed_cause": confirmed_cause,
        "user_correction": correction,
    }
    system = """你负责把用户对 WoW Log AI 报告的反馈转成可复用经验。输出合法 json object，只能总结用户明确确认/纠正的内容，不能创造新事实。格式：{"pattern":"一句经验","when_to_apply":"何时适用","when_not_to_apply":"何时不应套用","confidence":"high|medium|low"}。如果反馈不足，confidence=low。"""
    kwargs = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": _body(prompt)}],
        "response_format": {"type": "json_object"},
        "max_tokens": 1200,
    }
    resp = client.chat.completions.create(**kwargs)
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return {"pattern": correction or confirmed_cause or "反馈不足", "when_to_apply": "", "when_not_to_apply": "", "confidence": "low"}
