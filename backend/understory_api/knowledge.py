from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
import threading
from collections import Counter
from difflib import SequenceMatcher
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from json_repair import repair_json

from .agentic import (
    compact_agent_resume_state,
    function_tool,
    normalize_tool_arguments,
    run_tool_agent,
)
from .deepseek import MiniMaxAPIError, MiniMaxClient, MiniMaxOutputTruncated, load_minimax_base_url, load_minimax_key
from .harness import HarnessService
from .knowledge_prompts import VOLUME_POINT_AGENT_SYSTEM, WIKI_ARCHITECT_AGENT_SYSTEM, WIKI_COMPOSER_AGENT_SYSTEM
from .knowledge_prompts import CHAPTER_PREVIEW_SYSTEM
from .library import LibraryRepository
from .models import (
    EvidenceRef,
    HarnessVariant,
    NarrativeTrackRecord,
    VolumeChapterSummary,
    VolumeReportData,
    VolumeReportRecord,
    WikiBlock,
    WikiBlueprintData,
    WikiBlueprintRecord,
    WikiBlueprintSummary,
    WikiCategory,
    WikiCompiledPayload,
    WikiEntryPlan,
    WikiEntryRecord,
    WikiNavigationBox,
    WikiSpoilerPolicy,
    WikiSpecialPlan,
    WikiState,
    AnalysisJobRecord,
    AnalysisStageProgress,
)
from .settings import SettingsRepository
from .embeddings import EmbeddingService, _select_ranked_results



def summarize_analysis_event(
    event_type: str, tool_name: str | None, arguments: dict[str, Any], preview: str,
    step: int = 0, max_steps: int = 0, result_stats: dict[str, Any] | None = None,
) -> str:
    chapter = arguments.get("title") or arguments.get("chapter_id")
    labels = {
        "chapter_report_started": f"开始读取 {chapter or '章节'}",
        "chapter_report_completed": f"完成章条目 {chapter or ''}",
        "chapter_report_cached": f"复用章条目 {chapter or ''}",
        "volume_research_cached": "复用已校验的卷研究计划",
        "volume_components_started": "开始生成卷概览、人物、事件与设定组件",
        "volume_component_overview": "卷概览生成完成",
        "volume_component_overview_cached": "复用已完成的卷概览",
        "volume_component_characters": "人物简传生成完成",
        "volume_component_characters_cached": "复用已完成的人物简传",
        "volume_component_events": "事件链生成完成",
        "volume_component_events_cached": "复用已完成的事件链",
        "volume_component_knowledge": "设定与要点集合生成完成",
        "volume_component_knowledge_cached": "复用已完成的设定与要点集合",
        "volume_report_accepted": "卷报告结构校验通过",
        "wiki_blueprint_raw_output": "Wiki Blueprint 原始输出已收到",
        "wiki_entry_raw_output": "Wiki 词条正文已生成",
        "wiki_entry_completed": f"完成 Wiki 词条：{arguments.get('title') or arguments.get('slug') or ''}",
        "chapter_preview_completed": f"完成章节轻览：{chapter or ''}",
        "agent_fallback": "研究阶段未主动提交，使用卷报告上下文继续写作",
        "agent_research_skipped": "跳过已经耗尽过的重复研究",
        "tool_arguments_parse_error": f"工具参数解析失败：{tool_name or '未知工具'}",
        "tool_name_repaired": f"已在本地修复重复工具名：{tool_name or '未知工具'}",
        "tool_arguments_repaired": f"已在本地修复工具参数：{tool_name or '未知工具'}",
        "tool_arguments_unwrapped": f"已解析工具参数包装：{tool_name or '未知工具'}",
        "final_submission": f"Agent 提交 {tool_name or '最终结果'}",
        "model_completed": "模型完成一轮思考",
        "tool_result": f"工具返回：{tool_name or '未知工具'}",
        "forced_final_round": "模型完成强制提交轮",
        "agent_round_error": "本轮返回错误，将进入唯一一次恢复轮",
        "agent_checkpoint_resumed": "恢复卷内 Agent 断点",
        "agent_checkpoint_compacted": "压缩卷内 Agent 断点",
        "wiki_composer_checkpoint_resumed": "恢复 Wiki Composer 断点",
        "architect_context_ready": "Wiki Architect 上下文已整理",
    }
    label = labels.get(event_type, event_type.replace("_", " "))
    if event_type == "model_completed":
        label = summarize_model_tool_calls(arguments.get("raw_tool_calls") or [])
    if event_type == "tool_result" and preview:
        label = summarize_tool_result(tool_name, preview, result_stats)
    elif event_type == "tool_arguments_parse_error" and preview:
        label = f"{label} · {preview[:160]}"
    if step > 0 and max_steps > 0:
        label = f"{label} · 第 {step}/{max_steps} 轮"
    return label.strip()


def summarize_model_tool_calls(raw_calls: list[Any]) -> str:
    calls = [item for item in raw_calls if isinstance(item, dict) and item.get("name")]
    if not calls:
        return "模型完成本轮，未调用工具"
    counts = {"query_calls": 0, "point_ids": 0, "search_calls": 0, "search_queries": 0,
              "report_calls": 0, "report_items": 0, "wiki_calls": 0, "wiki_items": 0,
              "create_calls": 0, "create_items": 0, "update_calls": 0,
              "update_items": 0, "alias_calls": 0, "alias_items": 0, "submit_calls": 0,
              "architect_entries": 0, "architect_specials": 0, "architect_categories": 0,
              "architect_navigation": 0,
              "architect_framework": 0, "architect_home": 0}
    for call in calls:
        name = str(call.get("name"))
        try:
            parsed = repair_json(str(call.get("arguments") or "{}"), return_objects=True)
            payload, _ = normalize_tool_arguments(parsed if isinstance(parsed, dict) else {})
        except (TypeError, ValueError):
            payload = {}
        if name == "get_knowledge_points":
            counts["query_calls"] += 1
            counts["point_ids"] += len(payload.get("point_ids") or [])
        elif name in {"search_original_text", "search_wiki"}:
            counts["query_calls"] += 1
            counts["search_calls"] += 1
            counts["search_queries"] += len(payload.get("queries") or []) or 1
        elif name in {"get_volume_summaries", "get_chapter_summaries"}:
            counts["query_calls"] += 1
            counts["report_calls"] += 1
            counts["report_items"] += len(payload.get("volume_numbers") or payload.get("chapter_ids") or [])
        elif name == "get_wiki_entries":
            counts["query_calls"] += 1
            counts["wiki_calls"] += 1
            counts["wiki_items"] += len(payload.get("slugs") or [])
        elif name == "create_knowledge_points":
            counts["create_calls"] += 1
            counts["create_items"] += len(payload.get("items") or [])
        elif name == "append_knowledge_points":
            counts["update_calls"] += 1
            counts["update_items"] += len(payload.get("items") or [])
        elif name == "add_point_aliases":
            counts["alias_calls"] += 1
            counts["alias_items"] += len(payload.get("items") or [])
        elif name == "upsert_entry_blueprints":
            counts["architect_entries"] += len(payload.get("items") or [])
        elif name == "upsert_special_blueprints":
            counts["architect_specials"] += len(payload.get("items") or [])
        elif name == "upsert_category_pages":
            counts["architect_categories"] += len(payload.get("items") or [])
        elif name == "upsert_navigation_boxes":
            counts["architect_navigation"] += len(payload.get("items") or [])
        elif name == "submit_work_profile":
            counts["architect_framework"] += 1
        elif name == "upsert_wiki_templates":
            counts["architect_framework"] += len(payload.get("items") or [])
        elif name == "submit_wiki_home":
            counts["architect_home"] += 1
        elif name in {"submit_volume_summary", "submit_wiki_entry", "finalize_wiki_blueprint"}:
            counts["submit_calls"] += 1
    parts = [f"模型完成本轮：共 {len(calls)} 次工具调用"]
    if counts["query_calls"]:
        parts.append(
            f"查询 {counts['query_calls']} 次（{counts['point_ids']} 个要点，"
            f"{counts['report_items']} 份报告，{counts['wiki_items']} 个词条，"
            f"{counts['search_queries']} 条检索）"
        )
    if counts["create_calls"]:
        parts.append(f"新建提交 {counts['create_calls']} 次/{counts['create_items']} 项")
    if counts["update_calls"]:
        parts.append(f"更新提交 {counts['update_calls']} 次/{counts['update_items']} 项")
    if counts["alias_calls"]:
        parts.append(f"别名提交 {counts['alias_calls']} 次/{counts['alias_items']} 项")
    architect_parts = []
    if counts["architect_framework"]:
        architect_parts.append("总体框架")
    if counts["architect_home"]:
        architect_parts.append("首页")
    if counts["architect_entries"]:
        architect_parts.append(f"词条 {counts['architect_entries']} 项")
    if counts["architect_specials"]:
        architect_parts.append(f"专题 {counts['architect_specials']} 项")
    if counts["architect_categories"]:
        architect_parts.append(f"类目 {counts['architect_categories']} 项")
    if counts["architect_navigation"]:
        architect_parts.append(f"作品导航 {counts['architect_navigation']} 项")
    if architect_parts:
        parts.append("Architect 写入：" + "、".join(architect_parts))
    parts.append(f"最终提交 {counts['submit_calls']} 次")
    return "；".join(parts)


def summarize_tool_result(
    tool_name: str | None, preview: str, result_stats: dict[str, Any] | None = None,
) -> str:
    stats = result_stats or {}
    if stats:
        names = [str(name) for name in stats.get("names") or [] if str(name)]
        suffix = f"：{'、'.join(names[:5])}{'等' if len(names) > 5 else ''}" if names else ""
        if tool_name == "get_knowledge_points":
            return f"读取 {int(stats.get('returned', 0))} 个要点{suffix}；缺失 {int(stats.get('missing', 0))} 个"
        if tool_name in {"create_knowledge_points", "append_knowledge_points", "add_point_aliases"}:
            action = {"create_knowledge_points": "新建", "append_knowledge_points": "更新", "add_point_aliases": "补充别名"}[str(tool_name)]
            replaced = int(stats.get("replaced", 0))
            replaced_text = f"；其中覆盖本卷暂存 {replaced} 项" if replaced else ""
            return f"{action}接受 {int(stats.get('accepted', 0))}/{int(stats.get('total', 0))} 项{suffix}；拒绝 {int(stats.get('rejected', 0))} 项{replaced_text}"
        if tool_name in {"search_original_text", "search_wiki"}:
            query_text = f"（{int(stats.get('queries', 0))} 条查询）" if stats.get("queries") else ""
            return f"{tool_name} 返回 {int(stats.get('returned', 0))} 条结果{query_text}"
        if tool_name in {"get_volume_summaries", "get_chapter_summaries", "get_wiki_entries"}:
            label = {"get_volume_summaries": "卷总结", "get_chapter_summaries": "章总结", "get_wiki_entries": "Wiki 词条"}[str(tool_name)]
            if tool_name == "get_wiki_entries":
                return (
                    f"读取 {int(stats.get('returned', 0))} 个已编译 Wiki 词条；"
                    f"{int(stats.get('planned', 0))} 个已有蓝图但尚未编译；"
                    f"缺失 {int(stats.get('missing', 0))} 个"
                )
            return f"读取 {int(stats.get('returned', 0))} 份{label}{suffix}；缺失或未完成 {int(stats.get('missing', 0))} 份"
        if tool_name in {"upsert_entry_blueprints", "upsert_special_blueprints", "upsert_category_pages"}:
            if stats.get("error"):
                return str(stats["error"])
            label = {"upsert_entry_blueprints": "词条蓝图", "upsert_special_blueprints": "专题蓝图", "upsert_category_pages": "类目页"}[str(tool_name)]
            return f"{label}接受 {int(stats.get('accepted', 0))}/{int(stats.get('total', 0))} 项；覆盖 {int(stats.get('replaced', 0))} 项"
    try:
        payload = json.loads(preview)
    except (TypeError, ValueError):
        return f"{tool_name or '工具'} 返回 · {preview[:120]}"
    if tool_name == "get_knowledge_points":
        points = payload.get("points") or []
        names = [str(item.get("canonical_name") or item.get("name")) for item in points if isinstance(item, dict)]
        return f"读取 {len(points)} 个要点" + (f"：{'、'.join(names[:5])}{'等' if len(names) > 5 else ''}" if names else "")
    if tool_name in {"create_knowledge_points", "append_knowledge_points", "add_point_aliases"}:
        results = payload.get("results") or []
        accepted = [item for item in results if isinstance(item, dict) and item.get("accepted")]
        rejected = len(results) - len(accepted)
        names = [str((item.get("point") or {}).get("name") or (item.get("point") or {}).get("canonical_name") or "") for item in accepted]
        action = {"create_knowledge_points": "新建", "append_knowledge_points": "附加", "add_point_aliases": "补充别名"}[str(tool_name)]
        detail = f"{action}了 {len(accepted)} 个要点"
        if names:
            detail += f"：{'、'.join([name for name in names if name][:5])}{'等' if len(names) > 5 else ''}"
        if rejected:
            detail += f"；{rejected} 项未接受"
        return detail
    if tool_name in {"search_original_text", "search_wiki"}:
        if isinstance(payload.get("queries"), list):
            queries = [item for item in payload["queries"] if isinstance(item, dict)]
            returned = sum(len(item.get("matches") or item.get("results") or []) for item in queries)
            return f"{tool_name} 返回 {returned} 条结果（{len(queries)} 条查询）"
        results = payload.get("results") or payload.get("matches") or []
        return f"{tool_name} 返回 {len(results)} 条结果"
    if tool_name in {"get_volume_summaries", "get_chapter_summaries", "get_wiki_entries"}:
        key = {"get_volume_summaries": "summaries", "get_chapter_summaries": "chapters", "get_wiki_entries": "entries"}[str(tool_name)]
        if tool_name == "get_wiki_entries":
            return (
                f"get_wiki_entries 返回 {len(payload.get('entries') or [])} 个已编译词条；"
                f"{len(payload.get('planned_uncompiled') or [])} 个已有蓝图但尚未编译；"
                f"缺失 {len(payload.get('missing') or payload.get('missing_or_uncompiled') or [])} 个"
            )
        missing_key = "missing"
        return f"{tool_name} 返回 {len(payload.get(key) or [])} 项；缺失 {len(payload.get(missing_key) or [])} 项"
    if tool_name in {"upsert_entry_blueprints", "upsert_special_blueprints", "upsert_category_pages", "upsert_wiki_templates"}:
        results = [item for item in payload.get("results") or [] if isinstance(item, dict)]
        accepted = [item for item in results if item.get("accepted")]
        replaced = [item for item in accepted if item.get("replaced")]
        label = {"upsert_entry_blueprints": "词条蓝图", "upsert_special_blueprints": "专题蓝图", "upsert_category_pages": "类目页", "upsert_wiki_templates": "页面模板"}[str(tool_name)]
        total = payload.get("draft_count", payload.get("template_count", 0))
        return f"{label}接受 {len(accepted)}/{len(results)} 项；覆盖 {len(replaced)} 项；草稿共 {total} 项"
    if tool_name == "submit_work_profile":
        return "简短作品画像已保存" if payload.get("accepted") else f"作品画像未接受：{payload.get('error', '未知错误')}"
    if tool_name == "submit_wiki_home":
        return f"Wiki 首页已{'覆盖' if payload.get('replaced') else '保存'}；{payload.get('sections', 0)} 个模块" if payload.get("accepted") else f"首页未接受：{payload.get('error', '未知错误')}"
    return f"{tool_name or '工具'} 已返回结果"

DEFAULT_HARNESS_VARIANT: HarnessVariant = "fast_volume_direct"
READER_CONCURRENCY = 3
WIKI_ENTRY_CONCURRENCY = 3
PREVIEW_CONCURRENCY = 3
API_MAX_ATTEMPTS = 3
CONSECUTIVE_SKIP_PAUSE_THRESHOLD = 3
VOLUME_AGENT_MAX_TOKENS = 64_000
WIKI_ARCHITECT_AGENT_MAX_TOKENS = 96_000
WIKI_COMPOSER_MAX_TOKENS = 80_000


def volume_agent_schedule(volume_number: int | float, analysis_mode: str = "quality") -> dict[str, Any]:
    """Return the bounded per-volume work plan used by the shared-point agent."""
    if analysis_mode == "fast":
        if volume_number == 1:
            return {
                "max_steps": 4,
                "query_steps": 0,
                "round_plan": [
                    "快速建立全卷覆盖清单，并批量初始化核心人物、关系与组织要点",
                    "批量初始化关键事件、地点、物品、技术与世界观要点",
                    "按章节检查遗漏，完成必要的长尾要点与别名维护",
                    "完成最后一批写入并必须提交卷总结",
                    "仅修正提交错误或强制提交卷总结",
                ],
            }
        return {
            "max_steps": 3,
            "query_steps": 1,
            "round_plan": [
                "快速梳理整卷，一次批量读取全部相关既有要点",
                "批量创建、更新要点并补齐别名；覆盖完整即可提交卷总结",
                "完成剩余维护并必须提交卷总结",
                "仅修正提交错误或强制提交卷总结",
            ],
        }
    if volume_number == 1:
        return {
            "max_steps": 8,
            "query_steps": 0,
            "round_plan": [
                "建立全卷要点分类与覆盖清单，并批量初始化核心要点",
                "批量初始化人物与人物关系要点",
                "批量初始化地点、国家与组织要点",
                "批量初始化事件与行动线要点",
                "批量初始化物品、技术、规则与世界观要点",
                "补齐各章遗漏的长尾要点",
                "检查全章覆盖并批量补齐剩余要点",
                "完成最后一批初始化并提交卷总结",
                "仅修正提交错误或强制提交卷总结",
            ],
        }
    return {
        "max_steps": 6,
        "query_steps": 3,
        "round_plan": [
            "阅读整卷并梳理本卷内容；一次性批量读取所有相关既有要点",
            "完成相关要点调查，规划本卷新增与需要更新的要点",
            "最后一轮读取要点；并行批量创建、更新和维护别名",
            "集中完成剩余要点维护；覆盖完整即可同时提交卷总结",
            "依据已有信息规划并提交卷总结，不再研究",
            "完成最后一批维护并必须提交卷总结",
            "仅修正提交错误或强制提交卷总结",
        ],
    }


def volume_stage_request_budget(volume_numbers: list[int | float], analysis_mode: str = "quality") -> int:
    """Return the request-level progress budget for all planned volume reports."""
    return sum(
        int(volume_agent_schedule(volume_number, analysis_mode)["max_steps"])
        for volume_number in volume_numbers
    )


def wiki_composer_schedule(
    plan: WikiEntryPlan, mode: str, analysis_mode: str = "quality",
) -> dict[str, Any]:
    """Bounded Composer schedule selected by page tier and analysis mode."""
    if mode != "agentic" or plan.writer_tier == "local":
        return {
            "research_rounds": 0,
            "round_plan": ["依据固定上下文一次完成并提交完整词条"],
        }
    if analysis_mode == "fast":
        research_rounds = 2 if plan.writer_tier == "core" else 1
    else:
        research_rounds = 3 if plan.writer_tier == "core" else 2
    plans = [
        "阅读固定上下文，整体规划资料缺口；必要时一次批量读取资料，信息足够可直接提交完整词条",
    ]
    if research_rounds >= 2:
        plans.append("结合已有上下文和工具结果完成核查；避免重复读取，信息足够立即提交完整词条")
    if research_rounds >= 3:
        plans.append(
            "收敛跨卷事实、引用与页面结构；只补充仍缺失的资料，完成后主动提交",
        )
    plans.append("强制提交：停止研究，只调用 submit_wiki_entry 提交完整词条")
    return {"research_rounds": research_rounds, "round_plan": plans}


def wiki_architect_schedule(analysis_mode: str = "quality") -> dict[str, Any]:
    return {
        # Hidden safety ceiling. It is deliberately identical across modes and
        # is not exposed to the model as a work quota or countdown.
        "max_steps": 24,
        "visible_steps": 24,
        "final_tool_min_step": 1,
    }


def analysis_reasoning_effort(analysis_mode: str) -> str:
    """Use a low reasoning budget for aggressive analysis and high for quality."""
    return "low" if analysis_mode == "fast" else "high"


def architect_draft_from_blueprint(blueprint: WikiBlueprintData | None) -> dict[str, Any]:
    """Create a mutable, key-addressed Architect draft from the last Blueprint."""
    if blueprint is None:
        return {
            "framework": None, "home": None, "categories": {},
            "entry_plans": {}, "specials": {}, "navigation_boxes": {},
        }
    return {
        "framework": {
            "work_profile": blueprint.work_profile,
            "templates": blueprint.templates,
            "template_metadata": blueprint.template_metadata,
            "infobox_schemas": blueprint.infobox_schemas,
            "architecture_notes": blueprint.architecture_notes,
            "deprecated_entry_slugs": blueprint.deprecated_entry_slugs,
        },
        "home": {
            "home_title": blueprint.home_title,
            "home_intro": blueprint.home_intro,
            "home_body_markdown": blueprint.home_body_markdown,
            "home_sections": blueprint.home_sections,
            "home_action": blueprint.home_action,
        },
        "categories": {item.id: item.model_dump() for item in blueprint.categories},
        "entry_plans": {item.slug: item.model_dump() for item in blueprint.entry_plans},
        "specials": {item.slug: item.model_dump() for item in blueprint.specials},
        "navigation_boxes": {item.id: item.model_dump() for item in blueprint.navigation_boxes},
    }


def architect_entry_count_guidance(total_points: int) -> dict[str, Any]:
    """Scale a curated Wiki to corpus complexity without mapping points 1:1.

    Small works can retain a larger share of their points. Large point tables
    require progressively stronger consolidation because events and local facts
    should primarily enrich broader entity/concept pages.
    """
    count = max(0, int(total_points))
    if count <= 40:
        lower_ratio, upper_ratio = 0.35, 0.65
    elif count <= 150:
        lower_ratio, upper_ratio = 0.25, 0.45
    else:
        lower_ratio, upper_ratio = 0.15, 0.30
    lower = min(count, max(8 if count else 0, (count * int(lower_ratio * 100) + 99) // 100))
    upper = min(count, 160, max(lower, (count * int(upper_ratio * 100) + 99) // 100))
    return {
        "minimum": lower,
        "maximum": upper,
        "featured_ratio": "8%-15%",
        "standard_ratio": "30%-45%",
        "stub_ratio": "45%-60%",
    }


def architect_entry_batch_error(items: list[Any]) -> str | None:
    # Compatibility hook: arbitrary item-count limits made capable models split
    # coherent plans and waste turns. Schema validation and provider output
    # limits remain the actual boundaries.
    return None


def normalize_home_submission(args: dict[str, Any]) -> dict[str, Any]:
    """Normalize the public home-page tool payload into Blueprint field names."""
    return {
        "home_title": str(args.get("home_title") or "").strip(),
        "home_intro": str(args.get("home_intro") or "").strip(),
        # Composer-style models often use body_markdown even when the internal
        # Blueprint field is home_body_markdown. Accept both at the boundary.
        "home_body_markdown": str(
            args.get("home_body_markdown") or args.get("body_markdown") or ""
        ).strip(),
        "home_sections": args.get("home_sections") or [],
        # The home page always exists locally; creating its generated content
        # is an update, unlike creating a new ordinary Wiki entry.
        "home_action": "update" if args.get("home_action") == "create" else args.get("home_action") or "update",
    }


def _decode_json_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        repaired = repair_json(text, return_objects=True)
        return repaired if isinstance(repaired, (dict, list)) else value


def _normalize_model_list(value: Any) -> list[Any]:
    """Accept provider wrappers for a field whose canonical shape is a list."""
    value = _decode_json_container(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("item", "items", "$values"):
            if key in value and len(value) == 1:
                nested = value[key]
                return nested if isinstance(nested, list) else [nested]
        return [value]
    return []


def normalize_wiki_compiled_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize common provider JSON wrappers before Pydantic validation.

    This is a lossless boundary normalization, not a quality repair: it only
    converts JSON strings to objects and singleton/`item` wrappers to arrays
    where the public schema already requires an array.
    """
    normalized = dict(payload)
    # Some OpenAI-compatible providers occasionally serialize two adjacent
    # string fields as lightweight XML delimiters inside the first field.  The
    # content is still complete, so recover the missing sibling before schema
    # validation instead of spending another model call or discarding the page.
    if not str(normalized.get("body_markdown") or "").strip():
        summary = normalized.get("summary")
        if isinstance(summary, str):
            for marker in ("</summary>\n<body_markdown>", "</summary><body_markdown>"):
                if marker not in summary:
                    continue
                clean_summary, body = summary.split(marker, 1)
                normalized["summary"] = clean_summary.strip()
                normalized["body_markdown"] = body.removesuffix("</body_markdown>").strip()
                break
    blocks = []
    for raw in _normalize_model_list(normalized.get("blocks")):
        if not isinstance(raw, dict):
            continue
        block = dict(raw)
        data = _decode_json_container(block.get("data"))
        if not isinstance(data, dict):
            data = {}
        else:
            data = dict(data)
            for key in ("items", "rows", "events", "entry_slugs"):
                if key in data and isinstance(data[key], dict):
                    wrapped = data[key]
                    if len(wrapped) == 1 and any(name in wrapped for name in ("item", "items", "$values")):
                        data[key] = _normalize_model_list(wrapped)
        block["data"] = data
        blocks.append(block)
    normalized["blocks"] = blocks

    tracks = []
    for raw in _normalize_model_list(normalized.get("track_items")):
        if not isinstance(raw, dict):
            continue
        track = dict(raw)
        track["evidence"] = [
            item for item in _normalize_model_list(track.get("evidence"))
            if isinstance(item, dict) and item.get("locator")
        ]
        tracks.append(track)
    normalized["track_items"] = tracks
    return normalized


def format_wiki_compiled_validation_error(error: Exception) -> str:
    errors = getattr(error, "errors", lambda: [])()
    if not errors:
        return f"Wiki 词条结构无效：{str(error)[:600]}"
    details = []
    for item in errors[:6]:
        location = ".".join(str(part) for part in item.get("loc") or [])
        details.append(f"{location}: {item.get('msg', '格式错误')}")
    suffix = f"；另有 {len(errors) - 6} 项" if len(errors) > 6 else ""
    return (
        "Wiki 词条结构无效：" + "；".join(details) + suffix
        + "。blocks[].data 必须是 JSON 对象；track_items[].evidence 必须直接是 JSON 数组，"
          "不要使用 JSON 字符串或 {item: ...} 包装。"
    )


def architect_quality_report(draft: dict[str, Any], total_points: int) -> dict[str, Any]:
    entries = list((draft.get("entry_plans") or {}).values())
    specials = list((draft.get("specials") or {}).values())
    categories = list((draft.get("categories") or {}).values())
    navigation_boxes = list((draft.get("navigation_boxes") or {}).values())
    framework = draft.get("framework")
    home = draft.get("home")
    weighted = len(entries) + len(specials) * 5
    hard_minimum = (max(0, total_points) * 20 + 99) // 100
    guidance = architect_entry_count_guidance(total_points)
    recommended_minimum = guidance["minimum"]
    recommended_maximum = guidance["maximum"]
    issues: list[str] = []
    blockers: list[str] = []

    def block(message: str) -> None:
        blockers.append(message)
        issues.append(message)

    if not isinstance(framework, dict) or not framework.get("work_profile"):
        block("尚未提交总体框架与作品画像")
    if not isinstance(framework, dict) or not (framework.get("templates") or {}):
        block("尚未提交可供 Composer 使用的页面模板")
    if not entries:
        block("尚未规划任何普通词条")
    if not isinstance(home, dict) or not str(home.get("home_title") or "").strip() or not (
        str(home.get("home_intro") or "").strip() or str(home.get("home_body_markdown") or "").strip()
    ):
        block("尚未提交可发布的 Wiki 首页")
    if not categories:
        block("尚未提交词条类目页")
    if not navigation_boxes:
        block("尚未提交作品导航盒")
    elif not any(group.get("entry_slugs") for box in navigation_boxes for group in box.get("groups") or []):
        block("作品导航盒尚未包含任何词条")
    incomplete_categories = [
        str(item.get("label") or item.get("id") or "未命名类目") for item in categories
        if not str(item.get("introduction") or "").strip() and not str(item.get("body_markdown") or "").strip()
    ]
    if incomplete_categories:
        block(f"{len(incomplete_categories)} 个类目页没有导语或正文：{'、'.join(incomplete_categories[:6])}")
    if len(specials) < 3:
        issues.append(f"专题不足：已有 {len(specials)} 个，至少需要 3 个")
    if len(entries) > recommended_maximum:
        issues.append(
            f"普通词条规模超过建议上界：已有 {len(entries)} 个，建议上界 {recommended_maximum}；"
            "应合并材料并移除重复、琐碎或可归入人物/组织/概念页面的计划"
        )
    if weighted < hard_minimum:
        issues.append(
            f"规划覆盖不足：词条 {len(entries)} + 专题 {len(specials)}×5 = {weighted}，"
            f"公共要点 {total_points} 个的 20% 下限为 {hard_minimum}"
        )
    tier_counts = {"core": 0, "supporting": 0, "local": 0}
    bound_point_ids: set[str] = set()
    for item in entries:
        tier = str(item.get("writer_tier") or "supporting")
        if tier in tier_counts:
            tier_counts[tier] += 1
        bound_point_ids.update(str(value) for value in item.get("knowledge_point_ids") or [])
    for item in specials:
        bound_point_ids.update(str(value) for value in item.get("knowledge_point_ids") or [])
    kind_counts = Counter(str(item.get("kind") or "其他") for item in entries)
    if entries and len(kind_counts) < 4:
        issues.append(f"词条类别偏少：目前只有 {len(kind_counts)} 类（{', '.join(kind_counts)}）")
    empty_categories = []
    for category in categories:
        parent = str(category.get("parent_kind") or "").strip().casefold()
        label = str(category.get("label") or "").strip().casefold()
        matched = sum(
            count for kind, count in kind_counts.items()
            if parent == kind.casefold() or kind.casefold() in label or label in kind.casefold()
        )
        if not matched:
            empty_categories.append(str(category.get("label") or category.get("id") or "未命名类目"))
    if empty_categories:
        block(f"空分类或 kind 映射错误：{'、'.join(empty_categories[:8])}")

    planned_slugs = {str(item.get("slug") or "") for item in entries + specials}
    category_slugs = {f"category:{item.get('id')}" for item in categories if item.get("id")}
    valid_link_slugs = planned_slugs | category_slugs
    plans_by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in entries:
        plans_by_kind.setdefault(str(item.get("kind") or "").casefold(), []).append(item)

    def page_block_warnings(owner: str, blocks: list[Any]) -> list[str]:
        warnings: list[str] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                warnings.append(f"{owner} 第 {index + 1} 个模块不是对象")
                continue
            block_type = str(block.get("type") or "")
            data = block.get("data") if isinstance(block.get("data"), dict) else {}
            items = data.get("items") if isinstance(data.get("items"), list) else []
            block_name = str(block.get("title") or block_type or f"模块 {index + 1}")
            if block_type in {"module_buttons", "info_cards"}:
                if not items:
                    warnings.append(f"{owner}「{block_name}」没有可展示项目")
                    continue
                invalid_items = []
                missing_descriptions = []
                missing_links = []
                duplicate_slugs: list[str] = []
                seen_slugs: set[str] = set()
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict):
                        invalid_items.append(str(item_index + 1)); continue
                    slug = str(item.get("slug") or "").strip()
                    title = str(item.get("title") or item.get("label") or item.get("name") or "").strip()
                    description = str(item.get("description") or item.get("summary") or item.get("note") or item.get("reason") or "").strip()
                    if not slug or not title:
                        invalid_items.append(slug or str(item_index + 1))
                    if slug and slug not in valid_link_slugs:
                        missing_links.append(slug)
                    if not description:
                        missing_descriptions.append(title or slug or str(item_index + 1))
                    if slug in seen_slugs:
                        duplicate_slugs.append(slug)
                    seen_slugs.add(slug)
                if invalid_items:
                    warnings.append(f"{owner}「{block_name}」有 {len(invalid_items)} 项缺少 slug 或标题")
                if missing_descriptions:
                    warnings.append(f"{owner}「{block_name}」有 {len(missing_descriptions)} 项缺少推荐理由/简介")
                if missing_links:
                    warnings.append(f"{owner}「{block_name}」包含不存在的链接：{'、'.join(missing_links[:5])}")
                if duplicate_slugs:
                    warnings.append(f"{owner}「{block_name}」包含重复链接：{'、'.join(dict.fromkeys(duplicate_slugs[:5]))}")
            if block_type == "volume_cards":
                invalid_volumes = [
                    str(item.get("title") or item.get("volume_id") or item.get("volume_number") or "未命名卷")
                    for item in items if isinstance(item, dict) and not (
                        item.get("volume_id") or item.get("volume_number") or item.get("number") or item.get("volume")
                    )
                ]
                if invalid_volumes:
                    warnings.append(f"{owner}「{block_name}」卷卡缺少 volume_id/volume_number：{'、'.join(invalid_volumes[:5])}")
        return warnings

    for category in categories:
        category_name = str(category.get("label") or category.get("id") or "未命名类目")
        category_blocks = category.get("blocks") if isinstance(category.get("blocks"), list) else []
        issues.extend(page_block_warnings(f"类目页 {category_name}", category_blocks))
        if len(str(category.get("body_markdown") or "").strip()) < 80:
            issues.append(f"类目页 {category_name} 正文过短；应先用成段文字介绍浏览角度，再穿插精选模块")
        subcategories = [item for item in category.get("subcategories") or [] if isinstance(item, dict)]
        sub_ids = [str(item.get("id") or "").strip() for item in subcategories]
        if len(set(filter(None, sub_ids))) != len(subcategories):
            issues.append(f"类目页 {category_name} 存在空白或重复的子分类 id")
        category_plans = plans_by_kind.get(str(category.get("parent_kind") or "").casefold(), [])
        invalid_subcategory = [
            str(item.get("subcategory") or "") for item in category_plans
            if item.get("subcategory") and str(item.get("subcategory")) not in sub_ids
        ]
        if invalid_subcategory:
            issues.append(f"类目页 {category_name} 有词条使用名称而非子分类 id：{'、'.join(dict.fromkeys(invalid_subcategory[:6]))}")
        if len(subcategories) > 1:
            uncategorized = sum(not str(item.get("subcategory") or "").strip() for item in category_plans)
            if uncategorized:
                issues.append(f"类目页 {category_name} 有 {uncategorized} 个词条未填写子分类 id")
        selected_slugs = {
            str(item.get("slug") or "")
            for block in category_blocks if isinstance(block, dict)
            for item in ((block.get("data") or {}).get("items") or []) if isinstance(item, dict)
        }
        for subcategory in subcategories:
            sub_id = str(subcategory.get("id") or "")
            members = [item for item in category_plans if str(item.get("subcategory") or "") == sub_id]
            selected = [item for item in members if str(item.get("slug") or "") in selected_slugs]
            if not members:
                issues.append(f"类目页 {category_name} 的子分类「{subcategory.get('label') or sub_id}」没有词条")
            elif not selected:
                issues.append(f"类目页 {category_name} 的子分类「{subcategory.get('label') or sub_id}」没有在 Architect 撰写的模块中精选入口")
    total_entries = max(1, len(entries))
    standard_ratio = tier_counts["supporting"] / total_entries
    stub_ratio = tier_counts["local"] / total_entries
    if len(entries) >= 12 and standard_ratio > .55:
        issues.append(f"standard 比例过高：{tier_counts['supporting']}/{len(entries)}；应把长尾对象降为 stub")
    if len(entries) >= 12 and stub_ratio < .25:
        issues.append(f"stub 比例过低：{tier_counts['local']}/{len(entries)}；长篇作品应保留轻量词条层")
    if isinstance(home, dict):
        sections = home.get("home_sections") or []
        invalid_modules = [
            str(section.get("title") or section.get("type") or "未命名模块")
            for section in sections if isinstance(section, dict) and (
                (section.get("type") == "info_cards" and "cards" in section and "items" not in section and "data" not in section)
                or (section.get("type") == "module_buttons" and "buttons" in section and "items" not in section and "data" not in section)
            )
        ]
        if invalid_modules:
            issues.append(f"首页模块未使用标准 data.items 协议：{'、'.join(invalid_modules[:6])}")
        issues.extend(page_block_warnings("首页", sections if isinstance(sections, list) else []))
        quote_sections = [section for section in sections if isinstance(section, dict) and section.get("type") == "quote"]
        if not quote_sections:
            issues.append("首页缺少带 lr:// 证据链接的开场引用模块")
        elif not any(str((section.get("data") or {}).get("locator") or "").startswith("lr://") for section in quote_sections):
            issues.append("首页引用缺少独立、有效的 lr:// locator")
        prose_sections = [section for section in sections if isinstance(section, dict) and section.get("type") in {"prose", "markdown"}]
        prose_chars = sum(len(str((section.get("data") or {}).get("markdown") or (section.get("data") or {}).get("text") or "")) for section in prose_sections)
        module_sections = [section for section in sections if isinstance(section, dict) and section.get("type") in {"info_cards", "module_buttons", "volume_cards"}]
        if len(prose_sections) < 4 or prose_chars < 650:
            issues.append(f"首页正文偏短：目前 {len(prose_sections)} 个正文段、约 {prose_chars} 字；应有至少 4 个正文段并与模块穿插")
        if len(module_sections) < 3:
            issues.append(f"首页导览模块偏少：目前 {len(module_sections)} 个，建议至少 3 个不同角度的模块")
        longest_module_run = run = 0
        for section in sections:
            if isinstance(section, dict) and section.get("type") in {"info_cards", "module_buttons", "volume_cards"}:
                run += 1; longest_module_run = max(longest_module_run, run)
            elif isinstance(section, dict) and section.get("type") in {"prose", "markdown", "quote"}:
                run = 0
        if longest_module_run > 2:
            issues.append("首页有三个以上模块连续堆叠；应插入承接正文形成阅读节奏")
    return {
        # Editorial and scale findings are deliberately advisory. The fixed
        # Architect rounds use them to iterate, never to trap a valid draft.
        "accepted": True,
        "ready": not blockers,
        "blockers": blockers,
        "issues": issues,
        "warnings": issues,
        "counts": {
            "entries": len(entries), "specials": len(specials), "categories": len(categories),
            "navigation_boxes": len(navigation_boxes),
            "weighted_coverage": weighted, "total_points": total_points,
            "hard_minimum": hard_minimum, "recommended_minimum": recommended_minimum,
            "recommended_maximum": recommended_maximum, "bound_points": len(bound_point_ids),
            "featured_ratio": guidance["featured_ratio"],
            "standard_ratio": guidance["standard_ratio"],
            "stub_ratio": guidance["stub_ratio"],
            **tier_counts,
        },
    }


def architect_todo_text(
    draft: dict[str, Any], touched: dict[str, Any], total_points: int,
    step: int = 1, visible_total: int = 0, final_tool_min_step: int = 1,
    provisional_final_step: int | None = None,
) -> str:
    quality = architect_quality_report(draft, total_points)
    counts = quality["counts"]
    framework = draft.get("framework") or {}
    template_ids = sorted((framework.get("templates") or {}).keys())
    blockers = list(quality.get("blockers") or [])
    warnings = [item for item in quality["warnings"] if item not in blockers]
    completed = [
        ("作品画像", bool(framework.get("work_profile"))),
        ("页面模板", bool(template_ids)),
        ("普通词条规划", counts["entries"] > 0),
        ("词条规模处于建议区间", counts["recommended_minimum"] <= counts["entries"] <= counts["recommended_maximum"]),
        ("专题规划", counts["specials"] >= 3),
        ("完整类目页", counts["categories"] > 0),
        ("Wiki 首页", bool((draft.get("home") or {}).get("home_title"))),
        ("作品导航", counts["navigation_boxes"] > 0),
    ]
    done_lines = "\n".join(
        f"  [{'x' if done else ' '}] {label}" for label, done in completed
    )
    foundational_blockers = [item for item in blockers if any(key in item for key in (
        "作品画像", "页面模板", "普通词条",
    ))]
    delivery_blockers = [item for item in blockers if item not in foundational_blockers]
    if foundational_blockers:
        suggested = f"先解决基础阻塞项：{foundational_blockers[0]}。使用最直接的批量写入工具推进。"
    elif counts["entries"] > counts["recommended_maximum"]:
        suggested = (
            f"普通词条已超过建议上界 {counts['recommended_maximum']}；立即停止新增，先把事件、局部角色和重复概念"
            "合并进更稳定的页面，再用 remove_entry_blueprints 批量移除低价值计划。"
        )
    elif delivery_blockers:
        suggested = (
            f"先建立可交付的完整纵向闭环，解决结构阻塞项：{delivery_blockers[0]}。"
            "同一回复可并行补齐互不依赖的首页、类目、专题和导航；暂缓继续扩词，待结构审计后再按真实缺口补充。"
        )
    elif counts["entries"] < counts["recommended_minimum"]:
        suggested = (
            f"发布结构已完整，但词条数仍低于建议区间下界 {counts['recommended_minimum']}；现在根据类目空缺与作品覆盖"
            "检查是否遗漏主要人物、组织、地点、概念和持续线索，合并事件材料后补充真正有独立查询价值的页面。"
        )
    elif warnings:
        suggested = (
            f"处理当前最高优先级软审计：{warnings[0]}。修复同类问题时尽量一次批量完成；"
            "若这批修订足以收敛，请在同一回复的最后调用 finalize_wiki_blueprint。"
        )
    else:
        suggested = "结构与软审计均已收敛；立即完成自我审查并调用 finalize_wiki_blueprint，不要继续扩写。"
    warning_limit = 10
    warning_lines = "\n".join(f"  - {item}" for item in warnings[:warning_limit]) or "  - 无"
    if len(warnings) > warning_limit:
        warning_lines += f"\n  - 另有 {len(warnings) - warning_limit} 项低优先级警告；先完成上列同类问题，再查看下一次 TODO。"
    blocker_lines = "\n".join(f"  - {item}" for item in blockers) or "  - 无；现在可以在自审后提交。"
    return (
        "Architect TODO（这是当前草稿的实时状态，不是固定轮次脚本）：\n"
        "目标：建立规模合理、结构完整、无剧透入口清晰的全作品 Wiki Blueprint，并在没有高价值遗漏时主动提交。\n"
        f"当前草稿：词条 {counts['entries']}（featured {counts['core']} / standard {counts['supporting']} / stub {counts['local']}），"
        f"专题 {counts['specials']}，类目页 {counts['categories']}，作品导航 {counts['navigation_boxes']}，模板 {len(template_ids)}。\n"
        "已完成情况：\n"
        f"{done_lines}\n"
        "提交前必须解决的结构阻塞项：\n"
        f"{blocker_lines}\n"
        f"规模建议：基于 {counts['total_points']} 个公共要点，普通词条通常约 {counts['recommended_minimum']}-{counts['recommended_maximum']} 个；"
        f"featured {counts['featured_ratio']}、standard {counts['standard_ratio']}、stub {counts['stub_ratio']}。这是编辑建议而非数量门槛，"
        "不要为凑数把每个要点拆成页面。\n"
        "待处理的软审计：\n"
        f"{warning_lines}\n"
        f"建议当前动作：{suggested}\n"
        "收敛规则：每次选择能消除最多重要 TODO 的动作；不要重复覆盖已经合格且审计未指出问题的内容。"
        "最新 TODO 会取代你此前自行拟定的后续计划；当建议从扩充转为页面结构、收缩或提交时，不要继续旧的批次清单。"
        "当结构阻塞项为零，并且剩余软警告已经修复或有明确编辑理由可以接受时，立即调用 finalize_wiki_blueprint；"
        "不要等待系统提醒，也不要为了继续行动而制造新词条。"
    )


class AnalysisPaused(RuntimeError):
    pass


class AnalysisStabilityPaused(AnalysisPaused):
    pass


_analysis_schema_lock = threading.RLock()


class KnowledgeService:
    def __init__(
        self,
        library: LibraryRepository,
        harness: HarnessService | None = None,
        provider: Any | None = None,
    ) -> None:
        self.library = library
        self.harness = harness or HarnessService(library)
        settings = SettingsRepository(library.workspace, library.data_dir).load()
        self.provider = provider or (MiniMaxClient(settings.api_key, timeout_seconds=900, base_url=settings.base_url, model=settings.advanced_model, reasoning_split=settings.reasoning_split) if settings.api_key else None)
        self.model_id = settings.advanced_model if provider is None else getattr(self.provider, "model", settings.advanced_model)
        self.fast_model_id = settings.fast_model if provider is None else getattr(self.provider, "fast_model", getattr(self.provider, "model", settings.fast_model))
        self.composer_concurrency = settings.composer_concurrency
        self.preview_concurrency = settings.preview_concurrency
        self.embeddings = EmbeddingService(library)
        with _analysis_schema_lock:
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.library.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS volume_reports (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                    volume_number INTEGER NOT NULL,
                    harness_variant TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    tool_trace_json TEXT NOT NULL DEFAULT '[]',
                    agent_checkpoint_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS volume_reports_lookup_idx
                    ON volume_reports(volume_id, harness_variant, created_at DESC);
                CREATE TABLE IF NOT EXISTS agent_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    tool_name TEXT,
                    status TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    result_preview TEXT NOT NULL DEFAULT '',
                    result_size INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS agent_run_events_run_idx ON agent_run_events(run_id, id);
                CREATE TABLE IF NOT EXISTS wiki_blueprints (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    blueprint_json TEXT NOT NULL,
                    tool_trace_json TEXT NOT NULL DEFAULT '[]',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, version)
                );
                CREATE TABLE IF NOT EXISTS wiki_architect_runs (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    context_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    draft_json TEXT NOT NULL DEFAULT '{}',
                    touched_json TEXT NOT NULL DEFAULT '{}',
                    agent_checkpoint_json TEXT,
                    tool_trace_json TEXT NOT NULL DEFAULT '[]',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    manual_retry_used INTEGER NOT NULL DEFAULT 0,
                    blueprint_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS wiki_architect_runs_series_idx
                    ON wiki_architect_runs(series_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS wiki_entries (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    level TEXT NOT NULL,
                    theme_color TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    infobox_json TEXT NOT NULL DEFAULT '{}',
                    blocks_json TEXT NOT NULL DEFAULT '[]',
                    spoiler_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 1,
                    user_created INTEGER NOT NULL DEFAULT 0,
                    blueprint_id TEXT REFERENCES wiki_blueprints(id),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, slug)
                );
                CREATE TABLE IF NOT EXISTS narrative_tracks (
                    id TEXT PRIMARY KEY,
                    entry_id TEXT NOT NULL REFERENCES wiki_entries(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    note TEXT NOT NULL,
                    start_locator TEXT NOT NULL,
                    end_locator TEXT,
                    importance REAL NOT NULL DEFAULT 0.5,
                    evidence_json TEXT NOT NULL DEFAULT '[]'
                    ,source_type TEXT NOT NULL DEFAULT 'ai'
                );
                CREATE TABLE IF NOT EXISTS wiki_compile_runs (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    blueprint_id TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_output TEXT NOT NULL DEFAULT '',
                    tool_trace_json TEXT NOT NULL DEFAULT '[]',
                    agent_checkpoint_json TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL DEFAULT 'quality',
                    corpus_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    total_steps INTEGER NOT NULL DEFAULT 0,
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                    ,pause_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS analysis_runs_series_idx
                    ON analysis_runs(series_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS analysis_steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    step_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope_id TEXT,
                    label TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    artifact_type TEXT,
                    artifact_id TEXT,
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(run_id,step_key)
                );
                CREATE INDEX IF NOT EXISTS analysis_steps_run_idx
                    ON analysis_steps(run_id,ordinal);
                CREATE TABLE IF NOT EXISTS analysis_run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    step_key TEXT,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chapter_preview_runs (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS knowledge_points (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    canonical_name TEXT NOT NULL,
                    point_type TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    created_volume_number INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, canonical_name)
                );
                CREATE INDEX IF NOT EXISTS knowledge_points_series_idx
                    ON knowledge_points(series_id, point_type, canonical_name);
                CREATE TABLE IF NOT EXISTS knowledge_point_updates (
                    id TEXT PRIMARY KEY,
                    point_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                    volume_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS knowledge_point_updates_point_idx
                    ON knowledge_point_updates(point_id, volume_number, created_at);
                """
            )
            analysis_columns = {row["name"] for row in db.execute("PRAGMA table_info(analysis_runs)")}
            if "mode" not in analysis_columns:
                db.execute("ALTER TABLE analysis_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'quality'")
            if "pause_requested" not in analysis_columns:
                db.execute("ALTER TABLE analysis_runs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0")
            if "vectorize" not in analysis_columns:
                db.execute("ALTER TABLE analysis_runs ADD COLUMN vectorize INTEGER NOT NULL DEFAULT 0")
            volume_report_columns = {row["name"] for row in db.execute("PRAGMA table_info(volume_reports)").fetchall()}
            if "agent_checkpoint_json" not in volume_report_columns:
                db.execute("ALTER TABLE volume_reports ADD COLUMN agent_checkpoint_json TEXT")
            wiki_columns = {row["name"] for row in db.execute("PRAGMA table_info(wiki_entries)")}
            if "infobox_json" not in wiki_columns:
                db.execute("ALTER TABLE wiki_entries ADD COLUMN infobox_json TEXT NOT NULL DEFAULT '{}'")
            if "spoiler_json" not in wiki_columns:
                db.execute("ALTER TABLE wiki_entries ADD COLUMN spoiler_json TEXT NOT NULL DEFAULT '{}'")
            track_columns = {row["name"] for row in db.execute("PRAGMA table_info(narrative_tracks)")}
            if "source_type" not in track_columns:
                db.execute("ALTER TABLE narrative_tracks ADD COLUMN source_type TEXT NOT NULL DEFAULT 'ai'")
            blueprint_columns = {row["name"] for row in db.execute("PRAGMA table_info(wiki_blueprints)")}
            for name, definition in {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0", "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in blueprint_columns:
                    db.execute(f"ALTER TABLE wiki_blueprints ADD COLUMN {name} {definition}")
            compile_run_columns = {row["name"] for row in db.execute("PRAGMA table_info(wiki_compile_runs)")}
            for name, definition in {
                "blueprint_id": "TEXT NOT NULL DEFAULT ''",
                "tool_trace_json": "TEXT NOT NULL DEFAULT '[]'",
                "agent_checkpoint_json": "TEXT",
            }.items():
                if name not in compile_run_columns:
                    db.execute(f"ALTER TABLE wiki_compile_runs ADD COLUMN {name} {definition}")
            # v9: a point has exactly one mutable update slot per volume.  The
            # historical table allowed duplicates; preserve the earliest row
            # once, then enforce the invariant in SQLite as well as in tools.
            db.execute(
                "DELETE FROM knowledge_point_updates WHERE rowid NOT IN ("
                "SELECT MIN(rowid) FROM knowledge_point_updates GROUP BY point_id,volume_id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_point_updates_point_volume_uidx "
                "ON knowledge_point_updates(point_id,volume_id)"
            )
            # Existing blueprints remain usable: enrich their plans from the
            # canonical point names/aliases without forcing an LLM rerun.
            for blueprint_row in db.execute(
                "SELECT id,series_id,blueprint_json FROM wiki_blueprints"
            ).fetchall():
                try:
                    blueprint_payload = json.loads(blueprint_row["blueprint_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                point_rows = db.execute(
                    "SELECT id,canonical_name,aliases_json FROM knowledge_points WHERE series_id=?",
                    (blueprint_row["series_id"],),
                ).fetchall()
                point_names = [{
                    "id": item["id"], "name": item["canonical_name"],
                    "aliases": json.loads(item["aliases_json"] or "[]"),
                } for item in point_rows]
                enriched = bind_blueprint_knowledge_points(blueprint_payload, point_names)
                db.execute(
                    "UPDATE wiki_blueprints SET blueprint_json=? WHERE id=?",
                    (json.dumps(enriched, ensure_ascii=False), blueprint_row["id"]),
                )
            for entry in db.execute("SELECT id,slug,kind FROM wiki_entries").fetchall():
                db.execute("UPDATE wiki_entries SET theme_color=? WHERE id=?", (theme_for_entry(entry["kind"], entry["slug"]), entry["id"]))
            # Discard only obsolete orchestration records. Canonical volume
            # summaries, public points and Wiki artifacts remain reusable.
            db.execute("DROP TABLE IF EXISTS analysis_jobs")
            interrupted = [] if getattr(self.library, '_analysis_recovery_done', False) else db.execute(
                "SELECT id,current_step FROM analysis_runs WHERE status IN ('pending','running')"
            ).fetchall()
            for item in interrupted:
                db.execute(
                    "INSERT INTO analysis_run_logs(run_id,step_key,level,message) VALUES(?,?, 'warning',?)",
                    (item["id"], item["current_step"], "服务中断；下次开始时将从当前步骤继续。"),
                )
            if not getattr(self.library, '_analysis_recovery_done', False):
                db.execute(
                    "UPDATE analysis_runs SET status='failed',error='服务中断，等待继续',updated_at=CURRENT_TIMESTAMP "
                    "WHERE status IN ('pending','running')"
                )
        self.library._analysis_recovery_done = True


    # One persistent manifest, one resumable run, one final acceptance gate.
    def start_analysis(self, series_id: str, mode: str = "quality", vectorize: bool = False) -> AnalysisJobRecord:
        if mode not in {"quality", "fast"}:
            raise ValueError("Unknown analysis mode")
        series = self.library.get_series(series_id)
        if series is None:
            raise LookupError("Series not found")
        if vectorize and not self.embeddings.status(series_id).configured:
            raise ValueError("尚未配置可用的向量化模型")
        signature = self._analysis_corpus_signature(series, mode, vectorize)
        with self.library.connect() as db:
            latest = db.execute(
                "SELECT id,status,mode,vectorize FROM analysis_runs WHERE series_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (series_id,),
            ).fetchone()
            active = latest if latest and latest["status"] != "completed" else None
            if active:
                if active["mode"] != mode:
                    raise ValueError(
                        f"本轮分析已使用{'快速' if active['mode'] == 'fast' else '质量'}模式；"
                        "完成本轮后才能切换模式"
                    )
                if bool(active["vectorize"]) != bool(vectorize):
                    raise ValueError("本轮分析的向量化选项已经固定；完成本轮后才能切换")
                if active["status"] == "running":
                    return self.get_analysis_job(active["id"])
                run_id = active["id"]
                db.execute(
                    "UPDATE analysis_runs SET corpus_signature=?,status='running',pause_requested=0,error=NULL,message='正在恢复分析',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (signature, run_id),
                )
            else:
                # A completed run is an immutable report. Starting again creates
                # a fresh manifest which reuses frozen artifacts and only
                # schedules work made necessary by newly imported volumes.
                run_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO analysis_runs(id,series_id,mode,vectorize,corpus_signature,status,message,pause_requested) VALUES(?,?,?,?,?,'running','正在启动分析',0)",
                    (run_id, series_id, mode, int(vectorize), signature),
                )
        self._ensure_analysis_manifest(run_id, series, mode, vectorize)
        self._log_analysis(run_id, None, "info", f"开始检查全部卷与现有 Wiki 产物（{'快速' if mode == 'fast' else '质量'}模式）。")
        threading.Thread(target=self._run_analysis_manifest, args=(run_id,), daemon=True).start()
        return self.get_analysis_job(run_id)

    def request_analysis_pause(self, run_id: str) -> AnalysisJobRecord:
        with self.library.connect() as db:
            row = db.execute("SELECT status FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise LookupError("Analysis run not found")
            if row["status"] not in {"pending", "running"}:
                return self.get_analysis_job(run_id)
            db.execute(
                "UPDATE analysis_runs SET pause_requested=1,message='已请求暂停，正在等待最近的可恢复保存点',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )
        self._log_analysis(run_id, None, "warning", "收到暂停请求；将在最近的可恢复保存点停止。")
        return self.get_analysis_job(run_id)

    def _raise_if_pause_requested(self, run_id: str) -> None:
        if self._is_pause_requested(run_id):
            raise AnalysisPaused("用户请求暂停")

    def _is_pause_requested(self, run_id: str) -> bool:
        with self.library.connect() as db:
            row = db.execute("SELECT pause_requested FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
        return bool(row and row["pause_requested"])

    def get_analysis_job(self, run_id: str) -> AnalysisJobRecord:
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise LookupError("Analysis run not found")
            counts = db.execute(
                "SELECT COUNT(*) total,SUM(CASE WHEN status IN ('completed','skipped') THEN 1 ELSE 0 END) completed FROM analysis_steps WHERE run_id=?",
                (run_id,),
            ).fetchone()
            current = db.execute(
                "SELECT label FROM analysis_steps WHERE run_id=? AND step_key=?",
                (run_id, row["current_step"]),
            ).fetchone() if row["current_step"] else None
            analyzed_volume_count = int(db.execute(
                "SELECT COUNT(*) FROM analysis_steps WHERE run_id=? AND kind='volume' "
                "AND status IN ('completed','skipped')", (run_id,),
            ).fetchone()[0])
            wiki_entry_count = int(db.execute(
                "SELECT COUNT(*) FROM wiki_entries WHERE series_id=? AND status='published'",
                (row["series_id"],),
            ).fetchone()[0])
        total = int(counts["total"] or 0); completed = int(counts["completed"] or 0)
        stages, current_stage = self._analysis_stage_progress(run_id, row)
        return AnalysisJobRecord(
            id=row["id"], series_id=row["series_id"], corpus_signature=row["corpus_signature"],
            mode=row["mode"],
            vectorize=bool(row["vectorize"]),
            pause_requested=bool(row["pause_requested"]),
            status=row["status"], current_step=current["label"] if current else None,
            total_steps=total, completed_steps=completed,
            progress=(completed / total if total else 0),
            current_stage_index=current_stage.index,
            current_stage_key=current_stage.key,
            current_stage_label=current_stage.label,
            stage_completed_points=current_stage.completed_points,
            stage_total_points=current_stage.total_points,
            stage_progress=current_stage.progress,
            stages=stages,
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"], message=row["message"],
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
            completed_at=row["completed_at"], analyzed_volume_count=analyzed_volume_count,
            wiki_entry_count=wiki_entry_count,
        )

    def _analysis_stage_progress(
        self, run_id: str, run_row: Any,
    ) -> tuple[list[AnalysisStageProgress], AnalysisStageProgress]:
        """Compute four pipeline stages from durable request and step records."""
        mode = str(run_row["mode"] or "quality")
        series = self.library.get_series(run_row["series_id"])
        with self.library.connect() as db:
            steps = [dict(item) for item in db.execute(
                "SELECT step_key,kind,scope_id,status,artifact_id FROM analysis_steps "
                "WHERE run_id=? ORDER BY ordinal", (run_id,),
            ).fetchall()]

            def event_count(event_run_id: str | None) -> int:
                if not event_run_id:
                    return 0
                return int(db.execute(
                    "SELECT COUNT(*) FROM agent_run_events WHERE run_id=? AND event_type='model_completed'",
                    (event_run_id,),
                ).fetchone()[0])

            vector_steps = [item for item in steps if item["kind"] == "vector"]
            embedding_state = self.embeddings.status(str(run_row["series_id"]))
            vector_total = len(vector_steps) if vector_steps else len(series.volumes if series else [])
            vector_done = sum(item["status"] in {"completed", "skipped"} for item in vector_steps)
            if not vector_steps and (embedding_state.complete or not bool(run_row["vectorize"])):
                vector_done = vector_total

            volume_steps = [item for item in steps if item["kind"] == "volume"]
            number_by_id = {volume.id: volume.number for volume in (series.volumes if series else [])}
            volume_total = volume_stage_request_budget(
                [number_by_id.get(step["scope_id"], 2) for step in volume_steps], mode,
            )
            volume_done = 0
            for step in volume_steps:
                budget = int(volume_agent_schedule(number_by_id.get(step["scope_id"], 2), mode)["max_steps"])
                if step["status"] in {"completed", "skipped"}:
                    volume_done += budget
                    continue
                report = db.execute(
                    "SELECT id FROM volume_reports WHERE volume_id=? AND created_at>=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (step["scope_id"], run_row["created_at"]),
                ).fetchone()
                volume_done += min(budget, event_count(report["id"] if report else None))

            architect_step = next((item for item in steps if item["kind"] == "architect"), None)
            architect_total = int(wiki_architect_schedule(mode)["visible_steps"])
            architect_done = (
                architect_total if architect_step and architect_step["status"] in {"completed", "skipped"}
                else min(architect_total, event_count(f"analysis:{run_id}:architect"))
            )

            entry_steps = [item for item in steps if item["kind"] == "entry"]
            blueprint = self.latest_blueprint(run_row["series_id"])
            plans = {
                plan.slug: plan for plan in wiki_compilation_plans(blueprint.blueprint)
            } if blueprint else {}
            compose_total = 0
            compose_done = 0
            for step in entry_steps:
                plan = plans.get(step["scope_id"])
                research_rounds = int(wiki_composer_schedule(
                    plan, plan.compile_mode, mode,
                )["research_rounds"]) if plan else 0
                budget = research_rounds + 1 if research_rounds else 1
                compose_total += budget
                if step["status"] in {"completed", "skipped"}:
                    compose_done += budget
                    continue
                compile_run = db.execute(
                    "SELECT id FROM wiki_compile_runs WHERE series_id=? AND slug=? AND created_at>=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (run_row["series_id"], step["scope_id"], run_row["created_at"]),
                ).fetchone()
                compose_done += min(budget, event_count(compile_run["id"] if compile_run else None))

            preview_steps = [item for item in steps if item["kind"] == "preview"]
            preview_total = len(preview_steps)
            preview_done = sum(item["status"] in {"completed", "skipped"} for item in preview_steps)

        vector_complete = (
            bool(vector_steps) and all(item["status"] in {"completed", "skipped"} for item in vector_steps)
        ) or (not vector_steps and bool(vector_total) and (embedding_state.complete or not bool(run_row["vectorize"])))
        vector_skipped = (
            bool(vector_total) and not bool(run_row["vectorize"])
            and not embedding_state.complete
        )
        volume_complete = bool(volume_steps) and all(
            item["status"] in {"completed", "skipped"} for item in volume_steps
        )
        architect_complete = bool(architect_step) and architect_step["status"] in {"completed", "skipped"}
        compose_complete = bool(entry_steps) and all(
            item["status"] in {"completed", "skipped"} for item in entry_steps
        )
        preview_complete = all(
            item["status"] in {"completed", "skipped"} for item in preview_steps
        ) if preview_steps else bool(compose_complete)
        if run_row["status"] == "completed":
            current_index = 5
        elif not vector_complete:
            current_index = 1
        elif not volume_complete:
            current_index = 2
        elif not architect_complete:
            current_index = 3
        elif not compose_complete:
            current_index = 4
        else:
            current_index = 5

        raw = [
            ("vector", "向量化", vector_done, vector_total, vector_complete, vector_skipped),
            ("volume", "卷总结", volume_done, volume_total, volume_complete, False),
            ("architect", "Wiki Blueprint", architect_done, architect_total, architect_complete, False),
            ("compose", "Wiki Compose", compose_done, compose_total, compose_complete, False),
            ("preview", "章节轻览", preview_done, preview_total, preview_complete, False),
        ]
        stages = []
        for index, (key, label, done, planned, complete, skipped) in enumerate(raw, start=1):
            done = min(int(done), int(planned))
            progress = 1.0 if complete else (done / planned if planned else 0.0)
            status = "skipped" if skipped else "completed" if complete else "running" if index == current_index else "pending"
            stages.append(AnalysisStageProgress(
                key=key, index=index, label=label, completed_points=done,
                total_points=int(planned), progress=progress, status=status,
            ))
        return stages, stages[current_index - 1]

    def latest_analysis_job(self, series_id: str) -> AnalysisJobRecord | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT id FROM analysis_runs WHERE series_id=? ORDER BY created_at DESC LIMIT 1",
                (series_id,),
            ).fetchone()
        return self.get_analysis_job(row["id"]) if row else None

    def analysis_logs(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            if not db.execute("SELECT 1 FROM analysis_runs WHERE id=?", (run_id,)).fetchone():
                raise LookupError("Analysis run not found")
            rows = db.execute(
                "SELECT id,step_key,level,message,input_tokens,output_tokens,created_at "
                "FROM analysis_run_logs WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, max(20, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _point_index(self, series_id: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT id,canonical_name,point_type,aliases_json FROM knowledge_points "
                "WHERE series_id=? ORDER BY point_type,canonical_name", (series_id,),
            ).fetchall()
        return [{"id": row["id"], "name": row["canonical_name"], "type": row["point_type"],
                 "aliases": json.loads(row["aliases_json"] or "[]")} for row in rows]

    def _point_details(self, series_id: str, point_ids: list[str]) -> list[dict[str, Any]]:
        if not point_ids:
            return []
        placeholders = ",".join("?" for _ in point_ids)
        with self.library.connect() as db:
            rows = db.execute(
                f"SELECT * FROM knowledge_points WHERE series_id=? AND id IN ({placeholders})",
                (series_id, *point_ids),
            ).fetchall()
            result = []
            for row in rows:
                updates = db.execute(
                    "SELECT volume_id,volume_number,content,evidence_json,created_at "
                    "FROM knowledge_point_updates WHERE point_id=? ORDER BY volume_number,created_at",
                    (row["id"],),
                ).fetchall()
                result.append({
                    "id": row["id"], "name": row["canonical_name"], "type": row["point_type"],
                    "aliases": json.loads(row["aliases_json"] or "[]"),
                    "updates": [{**dict(item), "evidence": json.loads(item["evidence_json"] or "[]")} for item in updates],
                })
        return result

    def search_knowledge_points(
        self, series_id: str, queries: list[str], limit_per_query: int = 6,
    ) -> dict[str, Any]:
        """Search canonical point names and aliases and return complete histories.

        Each query is evaluated independently so a malformed or unmatched query
        never prevents the remaining batch from returning useful results.
        """
        limit = max(1, min(12, int(limit_per_query)))
        point_index = self._point_index(series_id)
        results: list[dict[str, Any]] = []
        for raw_query in list(queries)[:16]:
            query = str(raw_query or "").strip()
            if not query:
                results.append({"query": query, "points": [], "error": "查询词不能为空"})
                continue
            normalized = query.casefold()
            ranked: list[tuple[float, str]] = []
            for point in point_index:
                names = [str(point.get("name") or ""), *[str(item) for item in point.get("aliases") or []]]
                scores = []
                for name in names:
                    candidate = name.strip().casefold()
                    if not candidate:
                        continue
                    if candidate == normalized:
                        scores.append(1.0)
                    elif normalized in candidate or candidate in normalized:
                        scores.append(0.9)
                    else:
                        scores.append(SequenceMatcher(None, normalized, candidate).ratio())
                score = max(scores, default=0.0)
                if score >= 0.34:
                    ranked.append((score, str(point["id"])))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            point_ids = [point_id for _score, point_id in ranked[:limit]]
            details_by_id = {item["id"]: item for item in self._point_details(series_id, point_ids)}
            results.append({
                "query": query,
                "points": [
                    {**details_by_id[point_id], "match_score": round(score, 3)}
                    for score, point_id in ranked[:limit] if point_id in details_by_id
                ],
            })
        return {"queries": results}

    def _upsert_point_update(
        self, point_id: str, volume_id: str, volume_number: int | float,
        content: Any, evidence: Any = None,
    ) -> bool:
        """Write the single update slot for a point and volume.

        Returns True when a previously submitted update for this volume was
        replaced. Other volumes are never modified.
        """
        normalized_content = normalize_point_update(content)
        if not normalized_content:
            return False
        evidence_json = json.dumps(normalize_evidence(evidence), ensure_ascii=False)
        with self.library.connect() as db:
            existing = db.execute(
                "SELECT id FROM knowledge_point_updates WHERE point_id=? AND volume_id=?",
                (point_id, volume_id),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE knowledge_point_updates SET volume_number=?,content=?,evidence_json=?,"
                    "created_at=CURRENT_TIMESTAMP WHERE id=?",
                    (volume_number, normalized_content, evidence_json, existing["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO knowledge_point_updates(id,point_id,volume_id,volume_number,content,evidence_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, point_id, volume_id, volume_number, normalized_content, evidence_json),
                )
            db.execute(
                "UPDATE knowledge_points SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (point_id,),
            )
        return bool(existing)

    def _find_point_match(self, series_id: str, names: list[str]) -> dict[str, Any] | None:
        normalized = {name.strip().casefold() for name in names if name.strip()}
        for point in self._point_index(series_id):
            candidates = {point["name"].casefold(), *[alias.casefold() for alias in point["aliases"]]}
            if normalized.intersection(candidates):
                return self._point_details(series_id, [point["id"]])[0]
        return None

    def _analysis_corpus_signature(
        self, series: Any, mode: str = "quality", vectorize: bool = False,
    ) -> str:
        payload = {
            # A completed volume is immutable input to later stages.  Chapter
            # classification is reader metadata and must not create a new
            # analysis run; only adding/removing a volume changes the corpus.
            "pipeline": "analysis-manifest-v6-direct-quality-aggressive-fast",
            "mode": mode,
            "vectorize": bool(vectorize),
            "volumes": [{"id": volume.id, "number": volume.number} for volume in series.volumes],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _ensure_analysis_manifest(
        self, run_id: str, series: Any, mode: str = "quality", vectorize: bool = False,
    ) -> None:
        ordinal = 10
        embedding_state = self.embeddings.status(series.id)
        for volume in series.volumes:
            digest = hashlib.sha256(json.dumps({
                "volume_id": volume.id,
                "embedding_model": embedding_state.model,
                "vectorize": bool(vectorize),
            }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            volume_label = series.title if len(series.volumes) == 1 else f"第 {volume.number} 卷"
            step_key = f"vector:{volume.id}"
            self._upsert_analysis_step(
                run_id, step_key, "vector", volume.id,
                f"向量化{volume_label}", ordinal, digest,
            )
            if self.embeddings.volume_complete(series.id, volume.id):
                self._set_analysis_step(
                    run_id, step_key, "completed", artifact_type="embedding_volume",
                    artifact_id=embedding_state.index_id,
                    validation={"message": "该卷向量索引已完成"},
                )
            elif not vectorize:
                self._set_analysis_step(
                    run_id, step_key, "skipped",
                    validation={"message": "本轮未启用向量化"},
                )
            ordinal += 10
        ordinal = 1000
        for volume in series.volumes:
            digest = hashlib.sha256(json.dumps(
                {"mode": mode, "volume_id": volume.id},
                ensure_ascii=False, sort_keys=True,
            ).encode()).hexdigest()
            volume_label = series.title if len(series.volumes) == 1 else f"第 {volume.number} 卷"
            self._upsert_analysis_step(run_id, f"volume:{volume.id}", "volume", volume.id,
                                       f"校验并生成{volume_label}报告", ordinal, digest)
            ordinal += 10
        self._upsert_analysis_step(run_id, "wiki:architect", "architect", series.id,
                                   "校验并规划 Wiki", 100000, self._analysis_corpus_signature(series, mode, vectorize))
        self._upsert_analysis_step(run_id, "wiki:final", "final", series.id,
                                   "最终 Wiki 验收", 1000000, self._analysis_corpus_signature(series, mode, vectorize))
        self._recount_analysis_run(run_id)

    def _upsert_analysis_step(
        self, run_id: str, step_key: str, kind: str, scope_id: str | None,
        label: str, ordinal: int, input_hash: str,
    ) -> None:
        with self.library.connect() as db:
            existing = db.execute(
                "SELECT id,input_hash FROM analysis_steps WHERE run_id=? AND step_key=?",
                (run_id, step_key),
            ).fetchone()
            if existing and existing["input_hash"] != input_hash:
                db.execute(
                    "UPDATE analysis_steps SET kind=?,scope_id=?,label=?,ordinal=?,input_hash=?,status='pending',artifact_type=NULL,artifact_id=NULL,validation_json='{}',error=NULL,completed_at=NULL WHERE id=?",
                    (kind, scope_id, label, ordinal, input_hash, existing["id"]),
                )
            elif existing:
                db.execute("UPDATE analysis_steps SET label=?,ordinal=? WHERE id=?", (label, ordinal, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO analysis_steps(id,run_id,step_key,kind,scope_id,label,ordinal,input_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, run_id, step_key, kind, scope_id, label, ordinal, input_hash),
                )

    def _log_analysis(
        self, run_id: str, step_key: str | None, level: str, message: str,
        input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO analysis_run_logs(run_id,step_key,level,message,input_tokens,output_tokens) VALUES(?,?,?,?,?,?)",
                (run_id, step_key, level, message, input_tokens, output_tokens),
            )

    def _set_analysis_step(self, run_id: str, step_key: str, status: str, *,
                           artifact_type: str | None = None, artifact_id: str | None = None,
                           validation: dict[str, Any] | None = None, error: str | None = None,
                           input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self.library.connect() as db:
            db.execute(
                "UPDATE analysis_steps SET status=?,artifact_type=COALESCE(?,artifact_type),artifact_id=COALESCE(?,artifact_id),"
                "validation_json=?,error=?,attempts=attempts+CASE WHEN ?='running' THEN 1 ELSE 0 END,"
                "started_at=CASE WHEN ?='running' THEN CURRENT_TIMESTAMP ELSE started_at END,"
                "completed_at=CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP ELSE completed_at END "
                "WHERE run_id=? AND step_key=?",
                (status, artifact_type, artifact_id, json.dumps(validation or {}, ensure_ascii=False), error,
                 status, status, status, run_id, step_key),
            )
            if input_tokens or output_tokens:
                db.execute(
                    "UPDATE analysis_runs SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                    (input_tokens, output_tokens, run_id),
                )
        self._recount_analysis_run(run_id)

    def _recount_analysis_run(self, run_id: str) -> None:
        with self.library.connect() as db:
            counts = db.execute(
                "SELECT COUNT(*) total,SUM(CASE WHEN status IN ('completed','skipped') THEN 1 ELSE 0 END) completed FROM analysis_steps WHERE run_id=?",
                (run_id,),
            ).fetchone()
            db.execute(
                "UPDATE analysis_runs SET total_steps=?,completed_steps=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(counts["total"] or 0), int(counts["completed"] or 0), run_id),
            )

    def _call_with_api_retries(
        self, run_id: str, step_key: str | None, label: str,
        operation: Callable[[], Any], *, pause_on_exhaustion: bool,
    ) -> Any:
        for attempt in range(1, API_MAX_ATTEMPTS + 1):
            try:
                return operation()
            except AnalysisPaused:
                raise
            except MiniMaxAPIError as error:
                if not error.retryable:
                    self._log_analysis(run_id, step_key, "error", f"{label}请求被拒绝，不再重复发送：{str(error)[:500]}")
                    raise AnalysisStabilityPaused(f"{label}请求被拒绝，已暂停：{str(error)[:500]}") from error
                self._log_analysis(
                    run_id, step_key, "warning",
                    f"{label} API 调用失败（{attempt}/{API_MAX_ATTEMPTS}）：{str(error)[:500]}",
                )
                if attempt >= API_MAX_ATTEMPTS:
                    if pause_on_exhaustion:
                        raise AnalysisStabilityPaused(
                            f"{label}连续 {API_MAX_ATTEMPTS} 次 API 调用失败，已自动暂停"
                        ) from error
                    raise
                time.sleep(attempt)

    def _mark_step_skipped(self, run_id: str, step_key: str, error: Exception) -> None:
        with self.library.connect() as db:
            db.execute(
                "UPDATE analysis_steps SET status='skipped',artifact_type=NULL,artifact_id=NULL,"
                "validation_json=?,error=?,completed_at=CURRENT_TIMESTAMP WHERE run_id=? AND step_key=?",
                (json.dumps({
                    "message": f"API 连续 {API_MAX_ATTEMPTS} 次失败，已跳过",
                    "api_error": str(error)[:1000],
                }, ensure_ascii=False), str(error)[:1500], run_id, step_key),
            )
        self._recount_analysis_run(run_id)

    def _restore_skipped_steps(self, run_id: str, step_keys: list[str]) -> None:
        if not step_keys:
            return
        placeholders = ",".join("?" for _ in step_keys)
        with self.library.connect() as db:
            db.execute(
                f"UPDATE analysis_steps SET status='pending',artifact_type=NULL,artifact_id=NULL,"
                f"validation_json='{{}}',error=NULL,completed_at=NULL "
                f"WHERE run_id=? AND status='skipped' AND step_key IN ({placeholders})",
                (run_id, *step_keys),
            )
        self._recount_analysis_run(run_id)

    def _run_analysis_manifest(self, run_id: str) -> None:
        current_key: str | None = None
        try:
            with self.library.connect() as db:
                run = db.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
                db.execute("UPDATE analysis_runs SET status='running',message='正在校验已有产物',error=NULL WHERE id=?", (run_id,))
            series = self.library.get_series(run["series_id"])
            mode = run["mode"]
            vectorize = bool(run["vectorize"])
            # Both product modes now use direct whole-volume analysis. Quality
            # keeps the former fast budgets; fast uses more aggressive budgets.
            report_variant: HarnessVariant = "fast_volume_direct"
            self._ensure_analysis_manifest(run_id, series, mode, vectorize)
            if vectorize:
                for volume in series.volumes:
                    self._raise_if_pause_requested(run_id)
                    current_key = f"vector:{volume.id}"
                    volume_label = series.title if len(series.volumes) == 1 else f"第 {volume.number} 卷"
                    if self.embeddings.volume_complete(series.id, volume.id):
                        self._log_analysis(run_id, current_key, "success", f"{volume_label}向量索引已完成，直接复用。")
                        continue
                    self._set_run_current(run_id, current_key, f"正在向量化{volume_label}")
                    self._set_analysis_step(run_id, current_key, "running")
                    self._log_analysis(run_id, current_key, "info", f"开始为{volume_label}建立原文向量切片。")
                    result = self.embeddings.index_volume(series.id, volume.id)
                    self._set_analysis_step(
                        run_id, current_key, "completed", artifact_type="embedding_volume",
                        artifact_id=str(result["index_id"]),
                        validation={"message": f"已写入 {result['chunks']} 个向量切片"},
                    )
                    self._log_analysis(run_id, current_key, "success", f"{volume_label}向量化完成：{result['chunks']} 个切片。")
                self.embeddings.finalize_series(series.id)
            for volume in series.volumes:
                volume_label = series.title if len(series.volumes) == 1 else f"第 {volume.number} 卷"
                self._raise_if_pause_requested(run_id)
                current_key = f"volume:{volume.id}"
                self._set_run_current(run_id, current_key, f"正在检查{volume_label}")
                valid, reason, report = self._validate_volume_checkpoint(volume, report_variant)
                if valid and report:
                    self._set_analysis_step(run_id, current_key, "completed", artifact_type="volume_report",
                                            artifact_id=report.id, validation={"message": reason})
                    self._log_analysis(run_id, current_key, "success", f"{volume_label}：{reason}")
                    self._raise_if_pause_requested(run_id)
                    continue
                self._set_analysis_step(run_id, current_key, "running")
                self._log_analysis(run_id, current_key, "info", f"{volume_label}需要生成：{reason}")
                generated = self._call_with_api_retries(
                    run_id, current_key, f"{volume_label}报告",
                    lambda: self.synthesize_volume(
                        volume.id, report_variant, analysis_run_id=run_id,
                        analysis_mode=mode,
                    ),
                    pause_on_exhaustion=True,
                )
                valid, reason, checked = self._validate_volume_checkpoint(volume, report_variant)
                if not valid or not checked:
                    raise ValueError(f"{volume_label}报告未通过校验：{reason}")
                self._set_analysis_step(
                    run_id, current_key, "completed", artifact_type="volume_report", artifact_id=checked.id,
                    validation={"message": reason}, input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                )
                self._log_analysis(run_id, current_key, "success", f"{volume_label}报告生成并校验通过。")
                self._raise_if_pause_requested(run_id)

            # Volume Agents may have reclassified obvious copyright pages,
            # afterwords or other non-story material. Refresh before creating
            # preview/Wiki manifests so those chapters are excluded immediately.
            series = self.library.get_series(series.id)

            current_key = "wiki:architect"
            self._raise_if_pause_requested(run_id)
            self._set_run_current(run_id, current_key, "正在检查 Wiki 规划")
            valid, reason, blueprint = self._validate_blueprint_checkpoint(series, report_variant)
            if not valid:
                self._set_analysis_step(run_id, current_key, "running")
                self._log_analysis(run_id, current_key, "info", f"Wiki Architect 需要运行：{reason}")
                blueprint = self._call_with_api_retries(
                    run_id, current_key, "Wiki Blueprint",
                    lambda: self.architect(
                        series.id, allow_partial=False,
                        event_run_id=f"analysis:{run_id}:architect", analysis_run_id=run_id,
                        report_variant=report_variant, analysis_mode=mode,
                    ),
                    pause_on_exhaustion=True,
                )
                valid, reason, blueprint = self._validate_blueprint_checkpoint(series, report_variant)
                if not valid or not blueprint:
                    raise ValueError(f"Wiki Blueprint 未通过校验：{reason}")
                self._set_analysis_step(
                    run_id, current_key, "completed", artifact_type="wiki_blueprint", artifact_id=blueprint.id,
                    validation={"message": reason}, input_tokens=blueprint.input_tokens,
                    output_tokens=blueprint.output_tokens,
                )
                self._log_analysis(run_id, current_key, "success", "Wiki Blueprint 生成并校验通过。")
            else:
                self._set_analysis_step(run_id, current_key, "completed", artifact_type="wiki_blueprint",
                                        artifact_id=blueprint.id, validation={"message": reason})
                self._log_analysis(run_id, current_key, "success", f"Wiki Architect：{reason}")

            self._raise_if_pause_requested(run_id)
            self._ensure_wiki_manifest(run_id, series, blueprint, report_variant)
            self._run_wiki_steps(run_id, series, blueprint, report_variant, mode)
            current_key = "wiki:final"
            self._set_run_current(run_id, current_key, "正在执行最终 Wiki 验收")
            valid, reason = self._validate_final_wiki(run_id, series, blueprint)
            if not valid:
                raise ValueError(reason)
            self._set_analysis_step(run_id, current_key, "completed", artifact_type="wiki", artifact_id=series.id,
                                    validation={"message": reason})
            self._log_analysis(run_id, current_key, "success", reason)
            with self.library.connect() as db:
                db.execute(
                    "UPDATE analysis_runs SET status='completed',pause_requested=0,current_step=NULL,message=?,error=NULL,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    ("向量库与 Wiki 已完整生成并通过校验" if vectorize else "Wiki 已完整生成并通过校验", run_id),
                )
        except AnalysisPaused as error:
            with self.library.connect() as db:
                current = db.execute(
                    "SELECT current_step FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
                # More than one step can be running during parallel Reader/Composer
                # work. Completed artifacts stay completed; only unfinished steps
                # return to the resumable queue.
                db.execute(
                    "UPDATE analysis_steps SET status='pending',error=NULL WHERE run_id=? AND status='running'",
                    (run_id,),
                )
            pause_key = current["current_step"] if current and current["current_step"] else current_key
            pause_message = str(error).strip() or "已暂停，可随时从断点恢复"
            self._log_analysis(run_id, pause_key, "warning", f"分析已在可恢复保存点暂停：{pause_message}")
            with self.library.connect() as db:
                db.execute(
                    "UPDATE analysis_runs SET status='paused',pause_requested=0,message=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (pause_message, run_id),
                )
        except Exception as error:
            with self.library.connect() as db:
                current = db.execute(
                    "SELECT current_step FROM analysis_runs WHERE id=?", (run_id,)
                ).fetchone()
            failed_key = current["current_step"] if current and current["current_step"] else current_key
            if failed_key:
                self._set_analysis_step(run_id, failed_key, "failed", error=str(error)[:1500])
            self._log_analysis(run_id, failed_key, "error", f"分析中断：{type(error).__name__}: {str(error)[:800]}")
            with self.library.connect() as db:
                db.execute(
                    "UPDATE analysis_runs SET status='failed',pause_requested=0,message='等待从断点继续',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"{type(error).__name__}: {str(error)[:1000]}", run_id),
                )

    def _set_run_current(self, run_id: str, step_key: str, message: str) -> None:
        with self.library.connect() as db:
            db.execute(
                "UPDATE analysis_runs SET current_step=?,message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (step_key, message, run_id),
            )

    def _validate_volume_checkpoint(self, volume: Any, variant: HarnessVariant = DEFAULT_HARNESS_VARIANT) -> tuple[bool, str, VolumeReportRecord | None]:
        report = self.latest_volume_report(volume.id, variant)
        if not report or not report.report:
            return False, "没有已完成的卷报告", None
        expected = [item.id for item in volume.chapters if item.is_body]
        actual = [item.chapter_id for item in report.report.chapter_summaries]
        if actual != expected:
            return True, f"已有卷报告已冻结；忽略当前章节分类差异（{len(actual)}/{len(expected)}）", report
        return True, f"已有卷报告已完成并冻结，覆盖 {len(actual)}/{len(expected)} 个正文章节", report

    def _validate_blueprint_checkpoint(
        self, series: Any, report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
    ) -> tuple[bool, str, WikiBlueprintRecord | None]:
        blueprint = self.latest_blueprint(series.id)
        if not blueprint:
            return False, "尚无 Wiki Blueprint", None
        expected = [volume.id for volume in series.volumes]
        if blueprint.blueprint.coverage_volume_ids != expected or not blueprint.blueprint.complete_work_coverage:
            return False, "Blueprint 未覆盖全部导入卷", blueprint
        with self.library.connect() as db:
            newest_report = db.execute(
                "SELECT MAX(created_at) created_at FROM volume_reports "
                "WHERE series_id=? AND harness_variant=? AND status='completed'",
                (series.id, report_variant),
            ).fetchone()["created_at"]
        if newest_report and blueprint.created_at < newest_report:
            return False, f"Blueprint 早于 {report_variant} 的最新卷报告", blueprint
        return True, f"已有 Blueprint 校验通过，覆盖 {len(expected)} 卷", blueprint

    def _ensure_wiki_manifest(self, run_id: str, series: Any, blueprint: WikiBlueprintRecord, report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT) -> None:
        # Spoiler decisions are now part of the Composer's atomic entry payload.
        # Remove obsolete standalone steps left by a briefly used earlier schema.
        with self.library.connect() as db:
            db.execute("DELETE FROM analysis_steps WHERE run_id=? AND kind='spoiler'", (run_id,))
        ordinal = 2000
        for plan in wiki_compilation_plans(blueprint.blueprint):
            if plan.action not in {"create", "update", "rebuild"}:
                continue
            digest = hashlib.sha256((blueprint.id + plan.model_dump_json()).encode()).hexdigest()
            self._upsert_analysis_step(run_id, f"entry:{plan.slug}", "entry", plan.slug,
                                       f"编译词条：{plan.title}", ordinal, digest)
            ordinal += 1
        report_ids = {report.volume_id: report.id for report in self.list_volume_reports(series.id)
                      if report.status == "completed" and report.harness_variant == report_variant}
        for volume in series.volumes:
            for chapter in volume.chapters:
                if not chapter.is_body:
                    continue
                digest = hashlib.sha256(f"{chapter.id}:{chapter.character_count}:{report_ids.get(volume.id,'')}".encode()).hexdigest()
                self._upsert_analysis_step(run_id, f"preview:{chapter.id}", "preview", chapter.id,
                                           f"生成轻览：{chapter.title}", ordinal, digest)
                ordinal += 1
        self._recount_analysis_run(run_id)

    def _run_wiki_steps(
        self, run_id: str, series: Any, blueprint: WikiBlueprintRecord,
        report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
        analysis_mode: str = "quality",
    ) -> None:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT * FROM analysis_steps WHERE run_id=? AND kind IN ('entry','preview') ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        steps = [dict(row) for row in rows]
        entry_steps = [step for step in steps if step["kind"] == "entry"]
        preview_steps = [step for step in steps if step["kind"] == "preview"]
        stability_lock = threading.Lock()
        skipped_since_success: list[str] = []

        def note_api_success() -> None:
            with stability_lock:
                skipped_since_success.clear()

        def register_skipped_step(step_key: str) -> list[str] | None:
            with stability_lock:
                skipped_since_success.append(step_key)
                if len(skipped_since_success) < CONSECUTIVE_SKIP_PAUSE_THRESHOLD:
                    return None
                failed_streak = list(skipped_since_success)
                skipped_since_success.clear()
                return failed_streak

        def pause_after_failed_streak(step_keys: list[str]) -> AnalysisStabilityPaused:
            self._restore_skipped_steps(run_id, step_keys)
            labels = [
                step["label"] for step in steps if step["step_key"] in set(step_keys)
            ]
            self._log_analysis(
                run_id, step_keys[-1], "warning",
                f"连续跳过 {len(step_keys)} 个单项任务且期间没有成功 API 请求；"
                f"已撤销本轮跳过状态：{'、'.join(labels)}。",
            )
            return AnalysisStabilityPaused(
                f"连续 {len(step_keys)} 个词条或轻览 API 失败，已撤销跳过并自动暂停；手动恢复后会重试"
            )

        def compile_entry_step(step: dict[str, Any]) -> tuple[str, Exception | None]:
            key = step["step_key"]
            usage = {"input": 0, "output": 0}
            try:
                self._raise_if_pause_requested(run_id)
                if step["status"] == "skipped":
                    return key, None
                valid, reason, entry_id = self._validate_entry_checkpoint(series.id, step["scope_id"], blueprint.id)
                if not valid:
                    self._set_analysis_step(run_id, key, "running")
                    self._log_analysis(run_id, key, "info", f"{step['label']}：{reason}")
                    compiled = self._call_with_api_retries(
                        run_id, key, step["label"],
                        lambda: self.compile_entry(
                            series.id, step["scope_id"], "auto",
                            analysis_run_id=run_id, analysis_step_key=key,
                            report_variant=report_variant,
                            analysis_mode=analysis_mode,
                            api_success_callback=note_api_success,
                        ),
                        pause_on_exhaustion=False,
                    )
                    note_api_success()
                    compile_run_id = compiled.metadata.get("compile_run_id")
                    if compile_run_id:
                        with self.library.connect() as db:
                            metrics = db.execute(
                                "SELECT input_tokens,output_tokens FROM wiki_compile_runs WHERE id=?",
                                (compile_run_id,),
                            ).fetchone()
                        if metrics:
                            usage = {"input": metrics["input_tokens"], "output": metrics["output_tokens"]}
                    valid, reason, entry_id = self._validate_entry_checkpoint(series.id, step["scope_id"], blueprint.id)
                    if not valid:
                        raise ValueError(reason)
                self._raise_if_pause_requested(run_id)
                self._set_analysis_step(run_id, key, "completed", artifact_type="wiki_entry",
                                        artifact_id=entry_id, validation={"message": reason},
                                        input_tokens=usage["input"], output_tokens=usage["output"])
                self._log_analysis(run_id, key, "success", f"{step['label']}：校验通过")
                return key, None
            except AnalysisPaused as error:
                return key, error
            except MiniMaxAPIError as error:
                self._mark_step_skipped(run_id, key, error)
                self._log_analysis(
                    run_id, key, "warning",
                    f"{step['label']}连续 {API_MAX_ATTEMPTS} 次 API 失败，本次已跳过。",
                )
                failed_streak = register_skipped_step(key)
                return key, pause_after_failed_streak(failed_streak) if failed_streak else None
            except Exception as error:
                self._set_analysis_step(run_id, key, "failed", error=str(error)[:1500])
                self._log_analysis(
                    run_id, key, "error",
                    f"{step['label']}失败：{type(error).__name__}: {str(error)[:500]}",
                )
                return key, error

        if entry_steps:
            configured_entry_concurrency = (
                self.composer_concurrency
                if self.composer_concurrency != 3 else WIKI_ENTRY_CONCURRENCY
            )
            self._set_run_current(
                run_id, entry_steps[0]["step_key"],
                f"正在按公共前缀分组编译 {len(entry_steps)} 个 Wiki 词条（并发 {configured_entry_concurrency}）",
            )
            self._log_analysis(
                run_id, None, "info",
                f"Wiki Compiler 开始按模型、工具协议与上下文层级分组；每组先预热 1 个词条，"
                f"随后最多并发 {configured_entry_concurrency} 个。",
            )
            failures: list[tuple[str, Exception]] = []
            plan_by_slug = {
                plan.slug: plan for plan in wiki_compilation_plans(blueprint.blueprint)
            } if getattr(blueprint, "blueprint", None) is not None else {}
            grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for step in entry_steps:
                plan = plan_by_slug.get(step["scope_id"])
                if plan is None:
                    group_key = ("default",)
                else:
                    writer_model = self.model_id if plan.writer_tier == "core" else self.fast_model_id
                    protocol = "agentic" if plan.compile_mode == "agentic" and plan.writer_tier != "local" else "context"
                    group_key = (writer_model, protocol, plan.writer_tier, analysis_mode)
                grouped.setdefault(group_key, []).append(step)

            for group_key, group_steps in grouped.items():
                if failures or self._is_pause_requested(run_id):
                    break
                warm_step = next((step for step in group_steps if step["status"] != "completed"), group_steps[0])
                self._log_analysis(
                    run_id, warm_step["step_key"], "info",
                    f"Composer 前缀组 {'/'.join(group_key)}：先预热 {warm_step['label']}。",
                )
                key, error = compile_entry_step(warm_step)
                if error:
                    failures.append((key, error))
                    break
                remaining = [step for step in group_steps if step is not warm_step]
                if not remaining:
                    continue
                worker_count = min(configured_entry_concurrency, len(remaining))
                next_index = 0
                stop_scheduling = False
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures: dict[Any, dict[str, Any]] = {}

                    def fill_workers() -> None:
                        nonlocal next_index
                        while not stop_scheduling and len(futures) < worker_count and next_index < len(remaining):
                            step = remaining[next_index]
                            next_index += 1
                            futures[executor.submit(compile_entry_step, step)] = step

                    fill_workers()
                    while futures:
                        done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                        for future in done:
                            futures.pop(future, None)
                            key, error = future.result()
                            if error:
                                failures.append((key, error))
                                stop_scheduling = True
                        if self._is_pause_requested(run_id):
                            stop_scheduling = True
                        fill_workers()
            if failures:
                pause_failure = next((item for item in failures if isinstance(item[1], AnalysisPaused)), None)
                if pause_failure:
                    raise pause_failure[1]
                failed_key, error = failures[0]
                self._set_run_current(run_id, failed_key, "Wiki 词条编译中断，等待续跑")
                raise RuntimeError(f"{failed_key}: {type(error).__name__}: {error}")
            self._raise_if_pause_requested(run_id)

        def compile_preview_step(step: dict[str, Any]) -> tuple[str, Exception | None]:
            key = step["step_key"]
            usage = {"input": 0, "output": 0}
            try:
                self._raise_if_pause_requested(run_id)
                if step["status"] == "skipped":
                    return key, None
                valid, reason = self._validate_preview_checkpoint(step["scope_id"])
                if not valid:
                    self._set_analysis_step(run_id, key, "running")
                    self._log_analysis(run_id, key, "info", f"{step['label']}：{reason}")
                    self._call_with_api_retries(
                        run_id, key, step["label"],
                        lambda: self.compile_chapter_preview(
                            step["scope_id"], report_variant,
                            api_success_callback=note_api_success,
                            analysis_mode=analysis_mode,
                        ),
                        pause_on_exhaustion=False,
                    )
                    note_api_success()
                    with self.library.connect() as db:
                        metrics = db.execute(
                            "SELECT input_tokens,output_tokens FROM chapter_preview_runs "
                            "WHERE chapter_id=? ORDER BY created_at DESC LIMIT 1", (step["scope_id"],),
                        ).fetchone()
                    if metrics:
                        usage = {"input": metrics["input_tokens"], "output": metrics["output_tokens"]}
                    valid, reason = self._validate_preview_checkpoint(step["scope_id"])
                    if not valid:
                        raise ValueError(reason)
                self._raise_if_pause_requested(run_id)
                self._set_analysis_step(run_id, key, "completed", artifact_type="chapter_preview",
                                        artifact_id=step["scope_id"], validation={"message": reason},
                                        input_tokens=usage["input"], output_tokens=usage["output"])
                self._log_analysis(run_id, key, "success", f"{step['label']}：校验通过")
                return key, None
            except AnalysisPaused as error:
                return key, error
            except MiniMaxAPIError as error:
                self._mark_step_skipped(run_id, key, error)
                self._log_analysis(run_id, key, "warning", f"{step['label']}连续 {API_MAX_ATTEMPTS} 次 API 失败，本次已跳过。")
                failed_streak = register_skipped_step(key)
                return key, pause_after_failed_streak(failed_streak) if failed_streak else None
            except Exception as error:
                self._set_analysis_step(run_id, key, "failed", error=str(error)[:1500])
                return key, error

        if preview_steps:
            configured_preview_concurrency = (
                self.preview_concurrency if self.preview_concurrency != 3 else PREVIEW_CONCURRENCY
            )
            self._set_run_current(
                run_id, preview_steps[0]["step_key"],
                f"正在并行生成 {len(preview_steps)} 个章节轻览（并发 {configured_preview_concurrency}）",
            )
            failures = []
            worker_count = min(configured_preview_concurrency, len(preview_steps))
            next_index = 0
            stop_scheduling = False
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures: dict[Any, dict[str, Any]] = {}

                def fill_preview_workers() -> None:
                    nonlocal next_index
                    while not stop_scheduling and len(futures) < worker_count and next_index < len(preview_steps):
                        step = preview_steps[next_index]
                        next_index += 1
                        futures[executor.submit(compile_preview_step, step)] = step

                fill_preview_workers()
                while futures:
                    done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        futures.pop(future, None)
                        key, error = future.result()
                        if error:
                            failures.append((key, error))
                            stop_scheduling = True
                    if self._is_pause_requested(run_id):
                        stop_scheduling = True
                    fill_preview_workers()
            if failures:
                pause_failure = next((item for item in failures if isinstance(item[1], AnalysisPaused)), None)
                if pause_failure:
                    raise pause_failure[1]
                failed_key, error = failures[0]
                raise RuntimeError(f"{failed_key}: {type(error).__name__}: {error}")
            self._raise_if_pause_requested(run_id)

        appearance_count = self._refresh_appearance_tracks(series.id, blueprint)
        self._log_analysis(
            run_id, preview_steps[-1]["step_key"] if preview_steps else "wiki:final", "success",
            f"已根据公共要点证据与章节轻览链接刷新 {appearance_count} 个出场章节标记。",
        )

    def _validate_entry_checkpoint(self, series_id: str, slug: str, blueprint_id: str) -> tuple[bool, str, str | None]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT id,body_markdown,metadata_json,blueprint_id FROM wiki_entries WHERE series_id=? AND slug=?",
                (series_id, slug),
            ).fetchone()
        if not row:
            return False, "词条尚未生成", None
        metadata = json.loads(row["metadata_json"])
        if row["blueprint_id"] != blueprint_id or metadata.get("compile_state") != "compiled":
            return False, "词条不是由当前 Blueprint 编译", row["id"]
        if not row["body_markdown"].strip():
            return False, "词条正文为空", row["id"]
        return True, "当前词条产物有效", row["id"]

    def _validate_preview_checkpoint(self, chapter_id: str) -> tuple[bool, str]:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT status,intro FROM chapter_overviews WHERE chapter_id=?", (chapter_id,)
            ).fetchone()
        if not row or row["status"] != "ready" or not (row["intro"] or "").strip():
            return False, "章节轻览尚未生成"
        return True, "章节轻览有效"

    def _validate_final_wiki(self, run_id: str, series: Any, blueprint: WikiBlueprintRecord) -> tuple[bool, str]:
        with self.library.connect() as db:
            incomplete = db.execute(
                "SELECT step_key,label,status FROM analysis_steps WHERE run_id=? AND step_key!='wiki:final' "
                "AND status NOT IN ('completed','skipped') ORDER BY ordinal",
                (run_id,),
            ).fetchall()
            home = db.execute(
                "SELECT id,body_markdown,blocks_json,blueprint_id FROM wiki_entries WHERE series_id=? AND slug='home'",
                (series.id,),
            ).fetchone()
        if incomplete:
            return False, f"仍有 {len(incomplete)} 个步骤未完成：{incomplete[0]['label']}"
        home_has_content = False
        if home:
            try:
                home_has_content = bool(home["body_markdown"].strip() or json.loads(home["blocks_json"] or "[]"))
            except (TypeError, ValueError):
                home_has_content = bool(home["body_markdown"].strip())
        if not home or home["blueprint_id"] != blueprint.id or not home_has_content:
            return False, "Wiki 首页未由当前 Blueprint 完整生成"
        return True, "全部卷报告、Wiki 规划、带剧透规则的词条和章节轻览均已通过校验。"

    def list_volume_reports(self, series_id: str) -> list[VolumeReportRecord]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT * FROM volume_reports WHERE series_id=? ORDER BY volume_number, created_at DESC",
                (series_id,),
            ).fetchall()
        return [self._report_record(row) for row in rows]

    def latest_volume_report(
        self, volume_id: str, variant: HarnessVariant = DEFAULT_HARNESS_VARIANT
    ) -> VolumeReportRecord | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM volume_reports WHERE volume_id=? AND harness_variant=? "
                "AND status='completed' ORDER BY created_at DESC LIMIT 1",
                (volume_id, variant),
            ).fetchone()
        return self._report_record(row) if row else None

    def volume_report_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT id,step,event_type,tool_name,status,arguments_json,result_preview,result_size,metrics_json,created_at "
                "FROM agent_run_events WHERE run_id=? ORDER BY id", (run_id,),
            ).fetchall()
        return [{
            "id": row["id"], "step": row["step"], "event_type": row["event_type"],
            "tool_name": row["tool_name"], "status": row["status"],
            "arguments": json.loads(row["arguments_json"]), "result_preview": row["result_preview"],
            "result_size": row["result_size"], "metrics": json.loads(row["metrics_json"]),
            "created_at": row["created_at"],
        } for row in rows]

    def _record_agent_event(self, run_id: str, event: dict[str, Any]) -> None:
        metrics = {key: event[key] for key in (
            "input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens", "tool_names", "result_stats"
        ) if key in event}
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO agent_run_events(run_id,step,event_type,tool_name,status,arguments_json,result_preview,result_size,metrics_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, int(event.get("step", 0)), str(event.get("event_type", "event")),
                 event.get("tool_name"), str(event.get("status", "completed")),
                 json.dumps(event.get("arguments") or {}, ensure_ascii=False),
                 str(event.get("result_preview") or ""), int(event.get("result_size", 0)),
                json.dumps(metrics, ensure_ascii=False)),
            )


    def synthesize_volume(
        self, volume_id: str, variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
        progress_callback: Callable[[str], None] | None = None,
        analysis_run_id: str | None = None,
        analysis_mode: str = "quality",
    ) -> VolumeReportRecord:
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        volume, series = self._get_volume(volume_id)
        body_chapters = [chapter for chapter in volume.chapters if chapter.is_body]
        if variant == "chapter_packet_dag":
            missing = [
                chapter.id for chapter in body_chapters
                if self.harness.artifacts(chapter.id).packet is None
            ]
            if missing:
                raise ValueError(f"Narrative Packets missing for {len(missing)} chapters")

        run_id = uuid.uuid4().hex
        prompt_version = "volume-agent-v11-minimal-report"
        with self.library.connect() as db:
            resumable = db.execute(
                "SELECT id FROM volume_reports WHERE volume_id=? AND harness_variant=? "
                "AND prompt_version=? "
                "AND status IN ('draft','failed') AND agent_checkpoint_json IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (volume.id, variant, prompt_version),
            ).fetchone()
            if resumable:
                run_id = resumable["id"]
                db.execute(
                    "UPDATE volume_reports SET status='draft',prompt_version=?,error=NULL WHERE id=?",
                    (prompt_version, run_id),
                )
            else:
                db.execute(
                    "INSERT INTO volume_reports(id,series_id,volume_id,volume_number,harness_variant,model,prompt_version,status) "
                    "VALUES(?,?,?,?,?,?,?,'draft')",
                    (run_id, series.id, volume.id, volume.number, variant, self.model_id, prompt_version),
                )

        analysis_step_key = f"volume:{volume.id}"
        volume_label = series.title if len(series.volumes) == 1 else f"第 {volume.number} 卷"
        def record_event(event: dict[str, Any]) -> None:
            self._record_agent_event(run_id, event)
            if not analysis_run_id:
                return
            arguments = event.get("arguments") or {}
            metrics = {key: event.get(key, 0) for key in (
                "input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens"
            )}
            message = summarize_analysis_event(
                str(event.get("event_type", "event")), event.get("tool_name"), arguments,
                str(event.get("result_preview") or ""),
                int(event.get("step", 0)), int(event.get("max_steps", 0)),
                event.get("result_stats") or {},
            )
            level = "error" if event.get("status") in {"failed", "rejected"} else "info"
            self._log_analysis(
                analysis_run_id, analysis_step_key, level, f"{volume_label} · {message}",
                int(metrics["input_tokens"]), int(metrics["output_tokens"]),
            )

        started = time.perf_counter()
        try:
            previous = self._previous_reports(series.id, volume.number, variant)
            source_material = "## 本卷完整原文\n" + self._variant_source(variant, body_chapters)
            record_event({
                "step": 5, "event_type": "volume_context_ready", "status": "completed",
                "arguments": {"chapters": len(body_chapters), "characters": volume.character_count},
                "result_preview": "已装载整卷材料；卷 Agent 将只维护公共要点、卷总结和逐章总结。",
            })
            return self._synthesize_shared_points(
                run_id, series, volume, body_chapters, previous, source_material,
                started, record_event, analysis_run_id, analysis_mode,
            )
        except AnalysisPaused:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE volume_reports SET status='draft',duration_ms=?,error='分析已暂停，等待恢复' WHERE id=?",
                    (round((time.perf_counter() - started) * 1000), run_id),
                )
            raise
        except Exception as error:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE volume_reports SET status='failed', duration_ms=?, error=? WHERE id=?",
                    (round((time.perf_counter() - started) * 1000), str(error)[:1500], run_id),
                )
            raise
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM volume_reports WHERE id=?", (run_id,)).fetchone()
        return self._report_record(row)

    def _synthesize_shared_points(
        self, run_id: str, series: Any, volume: Any, chapters: list[Any],
        previous: list[dict[str, Any]], source_material: str,
        started: float, record_event: Callable[[dict[str, Any]], None],
        analysis_run_id: str | None = None,
        analysis_mode: str = "quality",
    ) -> VolumeReportRecord:
        requested_point_ids: set[str] = set()
        with self.library.connect() as db:
            prior_reads = db.execute(
                "SELECT arguments_json FROM agent_run_events WHERE run_id=? AND tool_name='get_knowledge_points' ORDER BY id",
                (run_id,),
            ).fetchall()
            checkpoint_row = db.execute(
                "SELECT agent_checkpoint_json FROM volume_reports WHERE id=?", (run_id,),
            ).fetchone()
        for row in prior_reads:
            try:
                requested_point_ids.update(str(value) for value in json.loads(row["arguments_json"]).get("point_ids") or [])
            except (TypeError, ValueError, AttributeError):
                continue
        raw_resume_state = json.loads(checkpoint_row["agent_checkpoint_json"]) if checkpoint_row and checkpoint_row["agent_checkpoint_json"] else None
        resume_state = compact_agent_resume_state(raw_resume_state)
        if raw_resume_state and resume_state:
            before = len(json.dumps(raw_resume_state, ensure_ascii=False))
            after = len(json.dumps(resume_state, ensure_ascii=False))
            if after < before:
                with self.library.connect() as db:
                    db.execute(
                        "UPDATE volume_reports SET agent_checkpoint_json=? WHERE id=?",
                        (json.dumps(resume_state, ensure_ascii=False), run_id),
                    )
                record_event({
                    "step": int(resume_state.get("next_step", 1)),
                    "event_type": "agent_checkpoint_compacted", "status": "completed",
                    "arguments": {"before_chars": before, "after_chars": after},
                    "result_preview": f"断点历史已从 {before:,} 字符压缩至 {after:,} 字符；查询资料与工具协议保持不变。",
                    "result_size": after,
                })

        def get_points(args: dict[str, Any]) -> dict[str, Any]:
            ids = [str(item) for item in args.get("point_ids") or []][:50]
            details = self._point_details(series.id, ids)
            requested_point_ids.update(item["id"] for item in details)
            return {"points": serialize_point_context(details), "missing_ids": [item for item in ids if item not in {point['id'] for point in details}]}

        def compact_evidence(item: dict[str, Any]) -> list[dict[str, str]]:
            return [{
                "locator": str(value.get("l") or value.get("locator") or ""),
                "note": str(value.get("n") or value.get("note") or ""),
            } for value in item.get("e") or item.get("evidence") or [] if isinstance(value, dict)]

        def create_points(args: dict[str, Any]) -> dict[str, Any]:
            results = []
            for item in (args.get("items") or [])[:50]:
                if not isinstance(item, dict):
                    results.append({"accepted": False, "error": "item must be an object"}); continue
                name = str(item.get("n") or item.get("name") or "").strip()
                aliases = list(dict.fromkeys(str(value).strip() for value in item.get("a") or item.get("aliases") or [] if str(value).strip() and str(value).strip() != name))
                if not name:
                    results.append({"accepted": False, "error": "name is required"}); continue
                match = self._find_point_match(series.id, [name, *aliases])
                if match:
                    results.append({"accepted": False, "error": "命中已有要点，请读取后决定是否追加", "existing": match}); continue
                point_id = uuid.uuid4().hex
                point_type = str(item.get("t") or item.get("type") or "未分类").strip() or "未分类"
                content = normalize_point_update(item.get("u") or item.get("initial_update"))
                with self.library.connect() as db:
                    db.execute(
                        "INSERT INTO knowledge_points(id,series_id,canonical_name,point_type,aliases_json,created_volume_number) VALUES(?,?,?,?,?,?)",
                        (point_id, series.id, name, point_type, json.dumps(aliases, ensure_ascii=False), volume.number),
                    )
                if content:
                    self._upsert_point_update(
                        point_id, volume.id, volume.number, content, compact_evidence(item),
                    )
                requested_point_ids.add(point_id)
                results.append({
                    "accepted": True,
                    "point": {"id": point_id, "name": name, "type": point_type, "aliases": aliases},
                })
            return {"results": results}

        def append_points(args: dict[str, Any]) -> dict[str, Any]:
            results = []
            for item in (args.get("items") or [])[:50]:
                if not isinstance(item, dict):
                    results.append({"accepted": False, "error": "item must be an object"}); continue
                point_id = str(item.get("id") or item.get("point_id") or "")
                if point_id not in requested_point_ids:
                    results.append({"accepted": False, "point_id": point_id, "error": "提交前必须先用 get_knowledge_points 读取该要点"}); continue
                details = self._point_details(series.id, [point_id])
                if not details:
                    results.append({"accepted": False, "point_id": point_id, "error": "point not found"}); continue
                content = normalize_point_update(item.get("u") or item.get("content"))
                aliases = [str(value).strip() for value in item.get("a") or item.get("aliases") or [] if str(value).strip()]
                with self.library.connect() as db:
                    row = db.execute("SELECT aliases_json FROM knowledge_points WHERE id=?", (point_id,)).fetchone()
                    merged_aliases = list(dict.fromkeys([*json.loads(row["aliases_json"] or "[]"), *aliases]))
                    db.execute("UPDATE knowledge_points SET aliases_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (json.dumps(merged_aliases, ensure_ascii=False), point_id))
                replaced = self._upsert_point_update(
                    point_id, volume.id, volume.number, content, compact_evidence(item),
                ) if content else False
                point = details[0]
                results.append({
                    "accepted": True,
                    "point": {
                        "id": point_id,
                        "name": point.get("name") or point.get("canonical_name"),
                        "type": point.get("type") or point.get("point_type"),
                        "aliases": merged_aliases,
                    },
                    "replaced_current_volume_update": replaced,
                })
            return {"results": results}

        def add_aliases(args: dict[str, Any]) -> dict[str, Any]:
            return append_points({"items": [
                {"id": item.get("id") or item.get("point_id"), "a": item.get("a") or item.get("aliases") or [], "u": ""}
                for item in args.get("items") or [] if isinstance(item, dict)
            ]})

        evidence_schema = {"type": "array", "items": {"type": "object", "properties": {
            "l": {"type": "string", "description": "lr:// locator"}, "n": {"type": "string", "description": "可选短注"},
        }, "required": ["l"]}}
        point_create_schema = {"type": "object", "properties": {
            "n": {"type": "string", "description": "正式名"}, "t": {"type": "string", "description": "类型"},
            "a": {"type": "array", "items": {"type": "string"}, "description": "别名"},
            "u": {"type": "string", "description": "本卷增量"}, "e": evidence_schema,
        }, "required": ["n", "t"]}
        point_update_schema = {"type": "object", "properties": {
            "id": {"type": "string", "description": "要点 ID"}, "u": {"type": "string", "description": "本卷增量"},
            "a": {"type": "array", "items": {"type": "string"}, "description": "新增别名"},
            "e": evidence_schema,
        }, "required": ["id"]}
        chapter_summary_schema = {"type": "object", "properties": {
            "chapter_id": {"type": "string"}, "summary": {"type": "string"},
        }, "required": ["chapter_id", "summary"]}
        final_properties = {
            "summary": {"type": "string"},
            "chapter_summaries": {"type": "array", "items": chapter_summary_schema},
            "reference_chapter_ids": {"type": "array", "items": {"type": "string"}},
        }
        first_volume = volume.number == 1
        tools = [
            function_tool("create_knowledge_points", "批量创建确认不存在的新要点", {"items": {"type": "array", "items": point_create_schema}}, ["items"]),
            function_tool("append_knowledge_points", "批量向已经读取的既有要点追加本卷更新，也可同时追加别名", {"items": {"type": "array", "items": point_update_schema}}, ["items"]),
            function_tool("add_point_aliases", "批量为已经读取的要点追加别名", {"items": {"type": "array", "items": point_update_schema}}, ["items"]),
            function_tool("submit_volume_summary", "要点维护完成后提交必要的本卷总结", final_properties,
                          ["summary", "chapter_summaries", "reference_chapter_ids"]),
            function_tool(
                "search_original_text",
                "仅在需要准确章节/段落定位与引用时搜索本卷原文；不用于重复阅读或总结",
                {"query": {"type": "string"}, "limit": {"type": "integer"},
                 "mode": self.original_search_mode_schema(series.id)}, ["query"],
            ),
        ]
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "create_knowledge_points": create_points,
            "append_knowledge_points": append_points, "add_point_aliases": add_aliases,
            "search_original_text": lambda args: self.search_original_tool(
                series.id, args.get("query", ""), int(args.get("limit", 12)), volume.id,
                mode=str(args.get("mode") or "hybrid"),
            ),
        }
        if not first_volume:
            tools.insert(0, function_tool(
                "get_knowledge_points", "一次调用批量读取所有相关既有要点的完整追加历史",
                {"point_ids": {"type": "array", "items": {"type": "string"}}}, ["point_ids"],
            ))
            handlers["get_knowledge_points"] = get_points
        with self.library.connect() as db:
            has_wiki = bool(previous) and bool(db.execute(
                "SELECT 1 FROM wiki_entries WHERE series_id=? AND status='published' LIMIT 1", (series.id,),
            ).fetchone())
        if has_wiki and not first_volume:
            tools.insert(-1, function_tool("search_wiki", "新增卷分析时按需搜索已有 Wiki", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]))
            handlers["search_wiki"] = lambda args: self.search_wiki(series.id, args.get("query", ""), int(args.get("limit", 12)))

        point_index = self._point_index(series.id)
        current_volume_updates = self._current_volume_point_updates(series.id, volume.id)
        schedule = volume_agent_schedule(volume.number, analysis_mode)
        later_volume_guidance = (
            "这是快速分析：首轮一次批量读取全部相关既有要点，随后立即批量创建、更新并提交卷总结；"
            "不要把调查、维护或提交拆成额外轮次。"
            if analysis_mode == "fast" else
            "请先梳理本卷发生了什么，再用一次批量读取覆盖所有与本卷有关的既有要点；无关要点不得读取或维护。"
            "要点读取只在前三轮开放；原文搜索仅供必要的精确定位。查询、要点维护和卷总结可以在同一回复中并行调用，"
            "执行器会按查询、创建、更新、最终提交的依赖顺序处理。原则上在第 3—4 轮完成创建、更新和别名维护，"
            "之后依据已有信息提交卷总结。覆盖完整即可提前提交，不必耗尽轮次。"
        )
        volume_identity = analysis_volume_identity(series, volume)
        prompt = (
            f"series_id: {series.id}\nvolume_id: {volume.id}\n{volume_identity}"
            f"volume_title: {volume.title}\nchapter_ids_and_titles: "
            f"{json.dumps([{'chapter_id': item.id, 'title': item.title} for item in chapters], ensure_ascii=False)}\n\n"
            f"前面各卷的卷总结（不含章报告与要点全文）：\n{json.dumps(previous, ensure_ascii=False)}\n\n"
            f"当前卷材料（承接前卷总结）：\n{source_material}\n\n"
            f"公共要点索引固定快照（紧凑格式：@ID | 类型 | 正式名 | 别名）：\n{serialize_point_index(point_index)}\n\n"
            f"本卷此前已持久化的要点更新（再次提交同一要点会覆盖本卷这一条）：\n{serialize_current_point_updates(current_volume_updates)}\n\n"
            + (
                "这是第一卷：它负责初始化作品的要点体系，通常涉及最多的新人物、地点、组织、事件和世界观。"
                "当前不存在需要调查的前卷要点历史，因此不提供要点查询工具。原文搜索只用于必要的精确定位。请先梳理整卷，再直接依据完整卷材料分批初始化，"
                "每轮尽量提交一组覆盖明确的要点，全部覆盖后提交卷总结。"
                if first_volume else
                later_volume_guidance
            )
        )
        if resume_state and resume_state.get("base_user_prompt"):
            prompt = str(resume_state["base_user_prompt"])
            record_event({
                "step": int(resume_state.get("next_step", 1)), "max_steps": int(schedule["max_steps"]) + 1,
                "event_type": "agent_checkpoint_resumed", "status": "completed",
                "arguments": {"next_round": int(resume_state.get("next_step", 1))},
                "result_preview": f"恢复到第 {int(resume_state.get('next_step', 1))}/{int(schedule['max_steps']) + 1} 轮，保留既有工具调用历史。",
            })

        def save_checkpoint(state: dict[str, Any]) -> None:
            state["base_user_prompt"] = prompt
            with self.library.connect() as db:
                db.execute(
                    "UPDATE volume_reports SET agent_checkpoint_json=?,input_tokens=?,output_tokens=?,tool_trace_json=? WHERE id=?",
                    (json.dumps(state, ensure_ascii=False), int(state["input_tokens"]),
                     int(state["output_tokens"]), json.dumps(state["tool_trace"], ensure_ascii=False), run_id),
                )
            if analysis_run_id:
                self._raise_if_pause_requested(analysis_run_id)
        result = run_tool_agent(
            client=self.provider, model=self.model_id, system_prompt=VOLUME_POINT_AGENT_SYSTEM,
            user_prompt=prompt, tools=tools, handlers=handlers,
            final_tool_name="submit_volume_summary", max_steps=int(schedule["max_steps"]),
            max_tokens=VOLUME_AGENT_MAX_TOKENS,
            event_callback=record_event, force_final_round=False, error_recovery_round=True,
            tool_budget_steps=int(schedule["query_steps"]),
            always_available_tool_names={
                "create_knowledge_points", "append_knowledge_points", "add_point_aliases",
                "search_original_text",
            },
            final_payload_validator=lambda payload: validate_compact_volume_submission(
                payload, [item.id for item in chapters]
            ),
            resume_state=resume_state, checkpoint_callback=save_checkpoint,
            round_plan=list(schedule["round_plan"]),
            reasoning_effort=analysis_reasoning_effort(analysis_mode),
        )
        final = result.payload
        reference_chapter_ids = list(dict.fromkeys(
            str(item) for item in final.get("reference_chapter_ids") or []
        ))
        reference_chapter_id_set = set(reference_chapter_ids)
        submitted = {str(item.get("chapter_id")): item for item in final.get("chapter_summaries") or [] if isinstance(item, dict)}
        resolved_chapters = []
        for chapter in chapters:
            if chapter.id in reference_chapter_id_set:
                continue
            item = submitted.get(chapter.id, {})
            resolved_chapters.append(VolumeChapterSummary(
                chapter_id=chapter.id, title=chapter.title,
                summary=str(item.get("summary") or "本章内容已纳入卷总结。"),
            ))

        raw_payload = {
            "summary": final.get("summary") or "",
            "chapter_summaries": [item.model_dump() for item in resolved_chapters],
        }
        story_chapter_ids = [item.id for item in chapters if item.id not in reference_chapter_id_set]
        payload = normalize_volume_report(raw_payload, series.id, volume.id, volume.number, volume.title, story_chapter_ids)
        report = VolumeReportData.model_validate(payload)
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        with self.library.connect() as db:
            self._mark_reference_chapters(db, volume.id, reference_chapter_ids)
            db.execute(
                "UPDATE volume_reports SET status='completed',report_json=?,input_tokens=?,output_tokens=?,duration_ms=?,tool_trace_json=?,agent_checkpoint_json=NULL WHERE id=?",
                (report.model_dump_json(), input_tokens, output_tokens,
                 round((time.perf_counter() - started) * 1000), json.dumps(result.tool_trace, ensure_ascii=False), run_id),
            )
            row = db.execute("SELECT * FROM volume_reports WHERE id=?", (run_id,)).fetchone()
        record_event({"step": 99, "event_type": "volume_report_accepted", "status": "accepted",
                      "arguments": {"points": len(self._current_volume_point_updates(series.id, volume.id)), "chapters": len(resolved_chapters)},
                      "result_preview": "公共要点追加与卷总结均已保存。"})
        return self._report_record(row)

    @staticmethod
    def _mark_reference_chapters(db: Any, volume_id: str, chapter_ids: list[str]) -> None:
        if not chapter_ids:
            return
        placeholders = ",".join("?" for _ in chapter_ids)
        params = (volume_id, *chapter_ids)
        db.execute(
            f"UPDATE chapters SET is_body=0 WHERE volume_id=? AND id IN ({placeholders})",
            params,
        )
        db.execute(
            f"UPDATE chapter_overviews SET status='skipped' WHERE chapter_id IN ("
            f"SELECT id FROM chapters WHERE volume_id=? AND id IN ({placeholders})) AND status='pending'",
            params,
        )

    def _current_volume_point_updates(self, series_id: str, volume_id: str) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT kp.id,kp.canonical_name,kp.point_type,kp.aliases_json,kpu.content,kpu.evidence_json "
                "FROM knowledge_point_updates kpu JOIN knowledge_points kp ON kp.id=kpu.point_id "
                "WHERE kp.series_id=? AND kpu.volume_id=? ORDER BY kp.point_type,kp.canonical_name,kpu.created_at",
                (series_id, volume_id),
            ).fetchall()
        return [{
            "id": row["id"], "name": row["canonical_name"], "type": row["point_type"],
            "aliases": json.loads(row["aliases_json"] or "[]"), "content": row["content"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
        } for row in rows]

    def architect(
        self, series_id: str, allow_partial: bool = False,
        event_run_id: str | None = None,
        analysis_run_id: str | None = None,
        report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
        analysis_mode: str = "quality",
    ) -> WikiBlueprintRecord:
        """Build a complete Wiki Blueprint with one resumable incremental Agent."""
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        series = self.library.get_series(series_id)
        if series is None:
            raise LookupError("Series not found")
        reports: list[VolumeReportRecord] = []
        missing: list[str] = []
        for volume in series.volumes:
            report = self.latest_volume_report(volume.id, report_variant)
            if report is None:
                missing.append(volume.id)
            else:
                reports.append(report)
        if missing and not allow_partial:
            raise ValueError(f"Wiki Architect requires reports for all volumes; missing {len(missing)}")
        if not reports:
            raise ValueError("No completed volume reports are available")

        report_payloads = architect_report_context([item.report for item in reports if item.report])
        all_volume_ids = [volume.id for volume in series.volumes]
        covered_ids = [report.volume_id for report in reports]
        existing_blueprint = self.latest_blueprint(series_id)
        with self.library.connect() as db:
            existing_rows = db.execute(
                "SELECT slug,title,kind,level,summary,status,revision FROM wiki_entries "
                "WHERE series_id=? ORDER BY kind,title", (series_id,),
            ).fetchall()
        existing_entries = [dict(row) for row in existing_rows]
        existing_slugs = {str(row.get("slug") or "") for row in existing_entries}
        point_index = self._point_index(series_id)
        point_details = self._point_details(series_id, [item["id"] for item in point_index])
        point_context = serialize_point_context(point_details)
        context_prompt = (
            f"series_id: {series_id}\nall_imported_volume_ids: {json.dumps(all_volume_ids)}\n"
            f"covered_volume_ids: {json.dumps(covered_ids)}\n"
            f"各卷概览、叙事结构与全部章节总结：\n{json.dumps(report_payloads, ensure_ascii=False)}\n\n"
            f"公共要点完整表（@ 后为可绑定 ID，含逐卷更新与证据）：\n{point_context}\n\n"
            f"已有 Wiki Blueprint（可以保留合理内容并覆盖更新）：\n"
            f"{existing_blueprint.blueprint.model_dump_json() if existing_blueprint else '{}'}\n\n"
            f"已有 Wiki 词条：\n{json.dumps(existing_entries, ensure_ascii=False)}\n\n"
            "请从总体框架开始，随后批量提交词条、专题、类目与首页草稿；每轮依据 TODO 收敛。"
        )
        context_hash = hashlib.sha256((
            f"wiki-architect-v9-page-contract-{analysis_mode}\n"
            + self.model_id + "\n" + WIKI_ARCHITECT_AGENT_SYSTEM + "\n" + context_prompt
        ).encode("utf-8")).hexdigest()
        architect_run_id = event_run_id or uuid.uuid4().hex

        def record_event(event: dict[str, Any]) -> None:
            self._record_agent_event(architect_run_id, event)
            if analysis_run_id:
                self._log_analysis(
                    analysis_run_id, "wiki:architect",
                    "error" if event.get("status") in {"failed", "rejected"} else "info",
                    f"Wiki Architect · {summarize_analysis_event(str(event.get('event_type', 'event')), event.get('tool_name'), event.get('arguments') or {}, str(event.get('result_preview') or ''), int(event.get('step', 0)), int(event.get('max_steps', 0)), event.get('result_stats') or {})}",
                    int(event.get("input_tokens", 0)), int(event.get("output_tokens", 0)),
                )

        with self.library.connect() as db:
            run_row = db.execute("SELECT * FROM wiki_architect_runs WHERE id=?", (architect_run_id,)).fetchone()
            if run_row and run_row["context_hash"] != context_hash:
                db.execute("DELETE FROM wiki_architect_runs WHERE id=?", (architect_run_id,))
                run_row = None
            if run_row and run_row["status"] == "completed" and run_row["blueprint_id"]:
                return self.get_blueprint(run_row["blueprint_id"])
            if run_row:
                draft = json.loads(run_row["draft_json"] or "{}")
                touched = json.loads(run_row["touched_json"] or "{}")
                draft.setdefault("navigation_boxes", {})
                touched.setdefault("navigation_boxes", [])
                resume_state = json.loads(run_row["agent_checkpoint_json"] or "null")
            else:
                draft = architect_draft_from_blueprint(existing_blueprint.blueprint if existing_blueprint else None)
                touched = {"framework": False, "home": False, "categories": [], "entry_plans": [], "specials": [], "navigation_boxes": []}
                resume_state = None
                db.execute(
                    "INSERT INTO wiki_architect_runs(id,series_id,context_hash,status,draft_json,touched_json) "
                    "VALUES(?,?,?,'running',?,?)",
                    (architect_run_id, series_id, context_hash, json.dumps(draft, ensure_ascii=False),
                     json.dumps(touched, ensure_ascii=False)),
                )

        def persist_draft() -> None:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_architect_runs SET draft_json=?,touched_json=?,status='running',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(draft, ensure_ascii=False), json.dumps(touched, ensure_ascii=False), architect_run_id),
                )

        def mark_touched(group: str, key: str | None = None) -> None:
            if key is None:
                touched[group] = True
            else:
                values = set(touched.get(group) or [])
                values.add(key)
                touched[group] = sorted(values)

        valid_point_ids = {item["id"] for item in point_index}

        def mutable_framework() -> dict[str, Any]:
            framework = draft.get("framework")
            if not isinstance(framework, dict):
                framework = {
                    "work_profile": {}, "templates": {}, "template_metadata": {}, "infobox_schemas": {},
                    "architecture_notes": [], "deprecated_entry_slugs": [],
                }
                draft["framework"] = framework
            return framework

        def submit_work_profile(args: dict[str, Any]) -> dict[str, Any]:
            profile = {
                "narrative_form": str(args.get("narrative_form") or "").strip(),
                "world_entry": str(args.get("world_entry") or "").strip(),
                "wiki_focus": [str(item).strip() for item in args.get("wiki_focus") or [] if str(item).strip()][:8],
                "organization_strategy": str(args.get("organization_strategy") or "").strip(),
                "coverage_notes": str(args.get("coverage_notes") or "").strip(),
            }
            if not profile["narrative_form"] or not profile["world_entry"]:
                return {"accepted": False, "error": "narrative_form and world_entry are required"}
            framework = mutable_framework()
            framework["work_profile"] = profile
            if "deprecated_entry_slugs" in args:
                framework["deprecated_entry_slugs"] = [
                    str(item) for item in args.get("deprecated_entry_slugs") or []
                ]
            mark_touched("framework"); persist_draft()
            return {"accepted": True, "work_profile_keys": list(profile)}

        def upsert_wiki_templates(args: dict[str, Any]) -> dict[str, Any]:
            framework = mutable_framework()
            templates = framework.setdefault("templates", {})
            metadata = framework.setdefault("template_metadata", {})
            schemas = framework.setdefault("infobox_schemas", {})
            results = []
            for raw in args.get("items") or []:
                template_id = str(raw.get("template_id") or "").strip()
                kind = str(raw.get("kind") or "").strip()
                label = str(raw.get("label") or kind).strip()
                if not template_id or not kind:
                    results.append({"accepted": False, "error": "template_id and kind are required"})
                    continue
                sections = [str(item).strip() for item in raw.get("template_sections") or [] if str(item).strip()][:10]
                fields = []
                for field in raw.get("infobox_fields") or []:
                    key = str(field.get("key") or "").strip()
                    field_label = str(field.get("label") or "").strip()
                    if key and field_label:
                        fields.append({
                            "key": key, "label": field_label,
                            "type": str(field.get("value_type") or "text").strip(),
                        })
                replaced = template_id in templates
                templates[template_id] = sections
                schemas[template_id] = fields[:18]
                metadata[template_id] = {
                    "kind": kind,
                    "label": label,
                    "suitable_for": str(raw.get("suitable_for") or "").strip(),
                }
                results.append({"accepted": True, "id": template_id, "kind": kind, "label": label, "replaced": replaced})
            if any(item["accepted"] for item in results):
                mark_touched("framework"); persist_draft()
            return {"results": results, "template_count": len(templates)}

        def upsert_entries(args: dict[str, Any]) -> dict[str, Any]:
            raw_items = args.get("items") or []
            results = []
            for raw in raw_items:
                try:
                    raw = dict(raw)
                    level = str(raw.get("level") or "standard")
                    tier = {"featured": "core", "standard": "supporting", "stub": "local"}.get(level, "supporting")
                    template_id = str(raw.get("template") or "").strip()
                    reason = str(raw.get("reason") or "").strip()
                    if not reason:
                        raise ValueError("reason is required; explain in one short sentence why this deserves an entry")
                    framework = mutable_framework()
                    template_meta = (framework.get("template_metadata") or {}).get(template_id)
                    if not template_id or template_id not in (framework.get("templates") or {}):
                        raise ValueError(f"unknown template: {template_id or '(empty)'}; create it with upsert_wiki_templates first")
                    entry_kind = str((template_meta or {}).get("kind") or raw.get("kind") or "other").strip()
                    raw["kind"] = entry_kind
                    raw["reason"] = reason[:240]
                    raw["action"] = "update" if str(raw.get("slug") or "") in existing_slugs else "create"
                    raw.update({
                        "writer_tier": tier,
                        "compile_mode": "context_only" if tier == "local" else "agentic",
                        "tool_budget": {"core": 4, "supporting": 2, "local": 0}[tier],
                        "source_refs": [], "changed_sections": [],
                    })
                    item = WikiEntryPlan.model_validate(raw).model_dump()
                    item["knowledge_point_ids"] = [value for value in item["knowledge_point_ids"] if value in valid_point_ids]
                    # Keep entry plans compact. Composer resolves the named
                    # template at runtime instead of duplicating its schema.
                    item["infobox_schema"] = []
                    replaced = item["slug"] in draft["entry_plans"]
                    draft["entry_plans"][item["slug"]] = item
                    mark_touched("entry_plans", item["slug"])
                    results.append({"accepted": True, "slug": item["slug"], "replaced": replaced})
                except Exception as error:
                    results.append({"accepted": False, "error": str(error)[:500]})
            if any(item["accepted"] for item in results):
                persist_draft()
            guidance = architect_entry_count_guidance(len(point_details))
            response = {"results": results, "draft_count": len(draft["entry_plans"])}
            if len(draft["entry_plans"]) > guidance["maximum"]:
                response["scale_warning"] = (
                    f"当前普通词条 {len(draft['entry_plans'])} 个，超过建议上界 {guidance['maximum']}；"
                    "请停止新增，合并材料并用 remove_entry_blueprints 移除低价值、重复或可归入其他页面的计划。"
                )
            return response

        def remove_entries(args: dict[str, Any]) -> dict[str, Any]:
            slugs = list(dict.fromkeys(
                str(item).strip() for item in args.get("slugs") or [] if str(item).strip()
            ))
            removed: list[str] = []
            missing_slugs: list[str] = []
            for slug in slugs:
                if draft["entry_plans"].pop(slug, None) is None:
                    missing_slugs.append(slug)
                else:
                    removed.append(slug)
            removed_set = set(removed)
            cleaned_links = 0
            if removed_set:
                for special in (draft.get("specials") or {}).values():
                    before = list(special.get("related_entry_slugs") or [])
                    special["related_entry_slugs"] = [slug for slug in before if slug not in removed_set]
                    cleaned_links += len(before) - len(special["related_entry_slugs"])
                for box in (draft.get("navigation_boxes") or {}).values():
                    for group in box.get("groups") or []:
                        before = list(group.get("entry_slugs") or [])
                        group["entry_slugs"] = [slug for slug in before if slug not in removed_set]
                        cleaned_links += len(before) - len(group["entry_slugs"])
                page_blocks = [
                    *((draft.get("home") or {}).get("home_sections") or []),
                    *(block for category in (draft.get("categories") or {}).values()
                      for block in category.get("blocks") or []),
                ]
                for block in page_blocks:
                    if not isinstance(block, dict) or not isinstance(block.get("data"), dict):
                        continue
                    items = block["data"].get("items")
                    if not isinstance(items, list):
                        continue
                    kept = [item for item in items if not (
                        isinstance(item, dict) and str(item.get("slug") or "") in removed_set
                    )]
                    cleaned_links += len(items) - len(kept)
                    block["data"]["items"] = kept
                persist_draft()
            return {
                "accepted": bool(removed), "removed": removed, "not_found": missing_slugs,
                "cleaned_links": cleaned_links, "draft_count": len(draft["entry_plans"]),
                "reason": str(args.get("reason") or "").strip(),
            }

        def upsert_specials(args: dict[str, Any]) -> dict[str, Any]:
            results = []
            for raw in args.get("items") or []:
                try:
                    raw = dict(raw)
                    raw.update({"source_refs": [], "changed_sections": [], "tool_budget": 4})
                    item = WikiSpecialPlan.model_validate(raw).model_dump()
                    item["knowledge_point_ids"] = [value for value in item["knowledge_point_ids"] if value in valid_point_ids]
                    replaced = item["slug"] in draft["specials"]
                    draft["specials"][item["slug"]] = item
                    mark_touched("specials", item["slug"])
                    results.append({"accepted": True, "slug": item["slug"], "replaced": replaced})
                except Exception as error:
                    results.append({"accepted": False, "error": str(error)[:500]})
            if any(item["accepted"] for item in results):
                persist_draft()
            return {"results": results, "draft_count": len(draft["specials"])}

        def upsert_categories(args: dict[str, Any]) -> dict[str, Any]:
            results = []
            for raw in args.get("items") or []:
                try:
                    item = WikiCategory.model_validate(raw).model_dump()
                    replaced = item["id"] in draft["categories"]
                    draft["categories"][item["id"]] = item
                    mark_touched("categories", item["id"])
                    results.append({"accepted": True, "id": item["id"], "replaced": replaced})
                except Exception as error:
                    results.append({"accepted": False, "error": str(error)[:500]})
            if any(item["accepted"] for item in results):
                persist_draft()
            return {"results": results, "draft_count": len(draft["categories"])}

        def upsert_navigation_boxes(args: dict[str, Any]) -> dict[str, Any]:
            results = []
            for raw in (args.get("items") or [])[:6]:
                try:
                    item = WikiNavigationBox.model_validate(raw).model_dump()
                    if not item["groups"]:
                        raise ValueError("navigation box requires at least one group")
                    if not any(group["entry_slugs"] for group in item["groups"]):
                        raise ValueError("navigation box requires linked entries")
                    replaced = item["id"] in draft["navigation_boxes"]
                    draft["navigation_boxes"][item["id"]] = item
                    mark_touched("navigation_boxes", item["id"])
                    results.append({"accepted": True, "id": item["id"], "replaced": replaced})
                except Exception as error:
                    results.append({"accepted": False, "error": str(error)[:500]})
            if any(item["accepted"] for item in results):
                persist_draft()
            return {"results": results, "draft_count": len(draft["navigation_boxes"])}

        def submit_home(args: dict[str, Any]) -> dict[str, Any]:
            home = normalize_home_submission(args)
            if home["home_action"] not in {"keep", "update", "rebuild"}:
                return {"accepted": False, "error": "home_action 必须为 keep、update 或 rebuild"}
            missing_fields = [
                field for field in ("home_title", "home_intro", "home_body_markdown")
                if not home[field]
            ]
            if missing_fields:
                return {
                    "accepted": False,
                    "error": "首页必须同时包含标题、简短 Intro 和完整 Markdown 正文；缺少："
                    + ", ".join(missing_fields),
                }
            replaced = draft.get("home") is not None
            draft["home"] = home
            mark_touched("home"); persist_draft()
            return {"accepted": True, "replaced": replaced, "sections": len(home["home_sections"])}

        def search_original(args: dict[str, Any]) -> dict[str, Any]:
            queries = [str(item).strip() for item in args.get("queries") or [] if str(item).strip()][:12]
            limit = max(1, min(int(args.get("limit_per_query") or 6), 12))
            mode = str(args.get("mode") or "hybrid")
            return {"queries": [{
                "query": query,
                "matches": self.search_original_tool(
                    series_id, query, limit, volume_ids=covered_ids, mode=mode,
                ).get("results", []),
            } for query in queries]}

        architect_module_item_schema = {
            "type": "object", "properties": {
                "slug": {"type": "string"}, "title": {"type": "string"},
                "description": {"type": "string"}, "summary": {"type": "string"},
                "volume_id": {"type": "string"}, "volume_number": {"type": "number"},
            },
        }
        architect_page_block_schema = {
            "type": "object", "properties": {
                "type": {"type": "string", "enum": [
                    "prose", "markdown", "quote", "info_cards", "module_buttons", "volume_cards",
                ]},
                "title": {"type": "string"},
                "data": {"type": "object", "properties": {
                    "markdown": {"type": "string"}, "text": {"type": "string"},
                    "source": {"type": "string"}, "locator": {"type": "string"},
                    "items": {"type": "array", "items": architect_module_item_schema},
                }},
            }, "required": ["type", "data"],
        }
        category_page_schema = WikiCategory.model_json_schema()
        category_page_schema["properties"]["blocks"]["items"] = architect_page_block_schema

        tools = [
            function_tool("search_original_text", "批量搜索原文，仅用于核查精确引用或定位", {
                "queries": {"type": "array", "items": {"type": "string"}},
                "limit_per_query": {"type": "integer"},
                "mode": self.original_search_mode_schema(series_id),
            }, ["queries"]),
            function_tool("get_knowledge_points", "按 ID 批量复核公共要点", {
                "point_ids": {"type": "array", "items": {"type": "string"}},
            }, ["point_ids"]),
            function_tool("submit_work_profile", "提交简短的作品画像和 Wiki 组织方向；不要包含词条细节", {
                "narrative_form": {"type": "string"},
                "world_entry": {"type": "string"},
                "wiki_focus": {"type": "array", "items": {"type": "string"}},
                "organization_strategy": {"type": "string"},
                "coverage_notes": {"type": "string"},
                "deprecated_entry_slugs": {"type": "array", "items": {"type": "string"}},
            }, ["narrative_form", "world_entry", "wiki_focus"]),
            function_tool("upsert_wiki_templates", "分批提交可复用的命名页面模板；同一词条类型可按职能设计 2—3 种模板", {
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "template_id": {"type": "string"},
                    "kind": {"type": "string"}, "label": {"type": "string"},
                    "suitable_for": {"type": "string"},
                    "template_sections": {"type": "array", "items": {"type": "string"}},
                    "infobox_fields": {"type": "array", "items": {"type": "object", "properties": {
                        "key": {"type": "string"}, "label": {"type": "string"},
                        "value_type": {"type": "string"},
                    }, "required": ["key", "label"]}},
                }, "required": ["template_id", "kind", "label", "template_sections"]}},
            }, ["items"]),
            function_tool("upsert_entry_blueprints", "批量新增或覆盖筛选、整合后的普通词条蓝图；按语义相关性组织批次，不设人为条数门槛", {
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "slug": {"type": "string"}, "title": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "template": {"type": "string"},
                    "level": {"type": "string", "enum": ["stub", "standard", "featured"]},
                    "reason": {"type": "string"},
                    "subcategory": {"type": "string"},
                    "needs_narrative_track": {"type": "boolean"},
                    "knowledge_point_ids": {"type": "array", "items": {"type": "string"}},
                }, "required": ["slug", "title", "template", "level", "reason", "knowledge_point_ids"]}},
            }, ["items"]),
            function_tool("remove_entry_blueprints", "移除重复、琐碎、缺少独立查询价值或应并入其他页面的普通词条计划；系统会同步清理页面与导航中的相关链接", {
                "slugs": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            }, ["slugs", "reason"]),
            function_tool("upsert_special_blueprints", "批量新增或覆盖多个专题蓝图", {
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "slug": {"type": "string"}, "title": {"type": "string"},
                    "introduction": {"type": "string"}, "angle": {"type": "string"},
                    "section_brief": {"type": "array", "items": {"type": "string"}},
                    "related_entry_slugs": {"type": "array", "items": {"type": "string"}},
                    "knowledge_point_ids": {"type": "array", "items": {"type": "string"}},
                    "recommended_on_home": {"type": "boolean"},
                    "action": {"type": "string", "enum": ["create", "keep", "update", "rebuild"]},
                }, "required": ["slug", "title", "introduction"]}},
            }, ["items"]),
            function_tool("upsert_category_pages", "批量新增或覆盖完整类目页", {
                "items": {"type": "array", "items": category_page_schema},
            }, ["items"]),
            function_tool("upsert_navigation_boxes", "提交或覆盖作品导航盒；按人物阵营、种族、组织、世界观、概念等分组，只链接已规划页面", {
                "items": {"type": "array", "maxItems": 6, "items": WikiNavigationBox.model_json_schema()},
            }, ["items"]),
            function_tool("submit_wiki_home", "提交或覆盖完整 Wiki 首页", {
                "home_title": {"type": "string"}, "home_intro": {"type": "string"},
                "body_markdown": {"type": "string"},
                "home_sections": {"type": "array", "items": architect_page_block_schema},
                "home_action": {"type": "string", "enum": ["keep", "update", "rebuild"]},
            }, ["home_title", "home_intro", "body_markdown", "home_sections"]),
            function_tool("finalize_wiki_blueprint", "提交自审结论；结构完整后发布当前草稿", {
                "completion_summary": {"type": "string"},
                "reviewed_areas": {"type": "array", "items": {"type": "string", "enum": [
                    "coverage", "entry_selection", "categories", "home", "navigation", "duplicates_and_links",
                ]}},
                "remaining_risks": {"type": "array", "items": {"type": "string"}},
            }, ["completion_summary", "reviewed_areas", "remaining_risks"]),
        ]
        handlers = {
            "search_original_text": search_original,
            "get_knowledge_points": lambda args: {"points": self._point_details(
                series_id, [str(item) for item in args.get("point_ids") or []][:100]
            )},
            "submit_work_profile": submit_work_profile,
            "upsert_wiki_templates": upsert_wiki_templates,
            "upsert_entry_blueprints": upsert_entries,
            "remove_entry_blueprints": remove_entries,
            "upsert_special_blueprints": upsert_specials,
            "upsert_category_pages": upsert_categories,
            "upsert_navigation_boxes": upsert_navigation_boxes,
            "submit_wiki_home": submit_home,
        }
        always_available = {
            "submit_work_profile", "upsert_wiki_templates", "upsert_entry_blueprints", "remove_entry_blueprints", "upsert_special_blueprints",
            "upsert_category_pages", "upsert_navigation_boxes", "submit_wiki_home",
        }

        def validate_final(arguments: dict[str, Any]) -> str | None:
            quality = architect_quality_report(draft, len(point_details))
            blockers = list(quality.get("blockers") or [])
            if blockers:
                return "Wiki Blueprint 尚有结构阻塞项：" + "；".join(blockers)
            required_reviews = {
                "coverage", "entry_selection", "categories", "home", "navigation", "duplicates_and_links",
            }
            reviewed = {str(item) for item in arguments.get("reviewed_areas") or []}
            missing_reviews = sorted(required_reviews - reviewed)
            if missing_reviews:
                return "最终自审范围不完整，缺少：" + "、".join(missing_reviews)
            if not str(arguments.get("completion_summary") or "").strip():
                return "completion_summary 不能为空"
            return None

        def save_checkpoint(state: dict[str, Any]) -> None:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_architect_runs SET draft_json=?,touched_json=?,agent_checkpoint_json=?,"
                    "tool_trace_json=?,input_tokens=?,output_tokens=?,status='running',"
                    "error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(draft, ensure_ascii=False), json.dumps(touched, ensure_ascii=False),
                     json.dumps(state, ensure_ascii=False), json.dumps(state.get("tool_trace") or [], ensure_ascii=False),
                     int(state.get("input_tokens") or 0), int(state.get("output_tokens") or 0), architect_run_id),
                )
            if analysis_run_id:
                self._raise_if_pause_requested(analysis_run_id)

        architect_schedule = wiki_architect_schedule(analysis_mode)
        next_step = int((resume_state or {}).get("next_step", 1))
        if next_step > int(architect_schedule["max_steps"]):
            raise ValueError(
                f"Wiki Architect 已达到 {int(architect_schedule['max_steps'])} 次行动的安全上限；"
                "请检查草稿并回退 Blueprint 后重新运行"
            )

        record_event({
            "step": 0, "event_type": "architect_context_ready", "status": "completed",
            "arguments": {"volumes": len(report_payloads), "points": len(point_details), "resumed": bool(resume_state)},
            "result_preview": (
                f"{len(report_payloads)} 卷、{len(point_details)} 个要点；固定上下文 {len(context_prompt):,} 字符；"
                f"{'恢复既有草稿' if resume_state else '建立新草稿'}。"
            ),
        })
        if resume_state:
            record_event({
                "step": next_step, "event_type": "agent_checkpoint_resumed", "status": "completed",
                "max_steps": int(architect_schedule["visible_steps"]),
                "result_preview": f"从第 {next_step} 轮继续；此前提交的蓝图草稿保持不变。",
            })
        try:
            result = run_tool_agent(
                client=self.provider, system_prompt=WIKI_ARCHITECT_AGENT_SYSTEM,
                user_prompt=context_prompt, tools=tools, handlers=handlers,
                final_tool_name="finalize_wiki_blueprint",
                max_steps=int(architect_schedule["max_steps"]),
                max_tokens=WIKI_ARCHITECT_AGENT_MAX_TOKENS,
                event_callback=record_event,
                tool_budget_steps=None,
                force_final_round=False,
                error_recovery_round=False,
                always_available_tool_names=always_available,
                final_payload_validator=validate_final,
                resume_state=resume_state,
                checkpoint_callback=save_checkpoint,
                round_plan=None,
                round_context_callback=lambda step, total: architect_todo_text(
                    draft, touched, len(point_details), step, total,
                ),
                visible_total_steps=int(architect_schedule["visible_steps"]),
                final_tool_min_step=int(architect_schedule["final_tool_min_step"]),
                provisional_final_step=None,
                expose_round_limits_to_model=False,
                require_final_on_last_step=False,
                model=self.model_id,
                reasoning_effort=analysis_reasoning_effort(analysis_mode),
            )
        except AnalysisPaused:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_architect_runs SET status='paused',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (architect_run_id,),
                )
            raise
        except Exception as error:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_architect_runs SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"{type(error).__name__}: {error}", architect_run_id),
                )
            raise

        framework = draft.get("framework") or {}
        home = draft.get("home") or {}
        raw_payload = {
            "series_id": series_id,
            "coverage_volume_ids": covered_ids,
            "complete_work_coverage": not missing,
            "work_profile": framework.get("work_profile") or {},
            "home_title": home.get("home_title") or series.title,
            "home_intro": home.get("home_intro") or "",
            "home_body_markdown": home.get("home_body_markdown") or "",
            "home_sections": home.get("home_sections") or [],
            "categories": list((draft.get("categories") or {}).values()),
            "entry_plans": list((draft.get("entry_plans") or {}).values()),
            "specials": list((draft.get("specials") or {}).values()),
            "navigation_boxes": list((draft.get("navigation_boxes") or {}).values()),
            "templates": framework.get("templates") or {},
            "template_metadata": framework.get("template_metadata") or {},
            "home_action": home.get("home_action") or "update",
            "deprecated_entry_slugs": framework.get("deprecated_entry_slugs") or [],
            "architecture_notes": framework.get("architecture_notes") or [],
            "infobox_schemas": framework.get("infobox_schemas") or {},
        }
        for box in raw_payload["navigation_boxes"]:
            if isinstance(box, dict) and str(box.get("title") or "").replace(" ", "") in {"作品导航", "导航盒"}:
                box["title"] = series.title
        payload = normalize_blueprint(raw_payload, series_id, covered_ids, not missing,
                                      {row["slug"] for row in existing_entries})
        payload = bind_blueprint_knowledge_points(payload, point_details)
        blueprint = WikiBlueprintData.model_validate(payload)
        with self.library.connect() as db:
            version = int(db.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM wiki_blueprints WHERE series_id=?",
                (series_id,),
            ).fetchone()["version"])
            blueprint_id = uuid.uuid4().hex
            status = "publishable" if blueprint.complete_work_coverage else "draft"
            db.execute(
                "INSERT INTO wiki_blueprints(id,series_id,version,status,model,blueprint_json,tool_trace_json,input_tokens,output_tokens) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (blueprint_id, series_id, version, status, self.model_id, blueprint.model_dump_json(),
                 json.dumps(result.tool_trace, ensure_ascii=False), result.input_tokens, result.output_tokens),
            )
            db.execute(
                "UPDATE wiki_architect_runs SET status='completed',blueprint_id=?,draft_json=?,"
                "agent_checkpoint_json=NULL,tool_trace_json=?,input_tokens=?,output_tokens=?,"
                "error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (blueprint_id, json.dumps(draft, ensure_ascii=False), json.dumps(result.tool_trace, ensure_ascii=False),
                 result.input_tokens, result.output_tokens, architect_run_id),
            )
        record_event({
            "step": 99, "event_type": "wiki_blueprint_accepted", "status": "accepted",
            "arguments": architect_quality_report(draft, len(point_details))["counts"],
            "result_preview": "增量草稿通过质量检查，已发布为完整 Wiki Blueprint。",
        })
        self._upsert_home_entry(blueprint_id, blueprint, status == "publishable")
        self._upsert_page_skeletons(blueprint_id, blueprint)
        return self.get_blueprint(blueprint_id)

    def compile_entry(
        self, series_id: str, slug: str, mode: str = "auto",
        analysis_run_id: str | None = None, analysis_step_key: str | None = None,
        report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
        analysis_mode: str = "quality",
        api_success_callback: Callable[[], None] | None = None,
    ) -> WikiEntryRecord:
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        blueprint_record = self.latest_blueprint(series_id)
        if blueprint_record is None:
            raise ValueError("Wiki Blueprint is not available")
        plan = next((item for item in wiki_compilation_plans(blueprint_record.blueprint) if item.slug == slug), None)
        if plan is None:
            raise LookupError("Entry plan not found")
        resolved_mode = plan.compile_mode if mode == "auto" else mode
        writer_model = self.model_id if plan.writer_tier == "core" else self.fast_model_id
        report_models = [report.report for report in self.list_volume_reports(series_id) if report.status == "completed" and report.harness_variant == report_variant and report.report]
        point_index = self._point_index(series_id)
        assigned_points = self._point_details(series_id, plan.knowledge_point_ids)
        report_context = self._compiler_report_context(report_models, plan, assigned_points)
        special_plan = next(
            (item for item in blueprint_record.blueprint.specials if item.slug == slug), None,
        )
        selected_template = {
            "id": plan.template,
            "metadata": blueprint_record.blueprint.template_metadata.get(plan.template, {}),
            "sections": blueprint_record.blueprint.templates.get(
                plan.template, blueprint_record.blueprint.templates.get(plan.kind, []),
            ),
            "infobox_fields": blueprint_record.blueprint.infobox_schemas.get(
                plan.template, blueprint_record.blueprint.infobox_schemas.get(plan.kind, []),
            ),
        }
        wiki_directory = sorted(({
            "slug": item.slug, "title": item.title, "kind": item.kind,
            "level": "special" if item.kind == "special" else item.level,
        } for item in wiki_compilation_plans(blueprint_record.blueprint)), key=lambda item: (item["kind"], item["title"]))
        valid_entry_slugs = {item["slug"] for item in wiki_directory}
        local_volume_numbers = {
            float(update.get("volume_number"))
            for point in assigned_points for update in point.get("updates") or []
            if update.get("volume_number") is not None
        }
        local_volume_summaries = [{
            "volume_id": report.volume_id,
            "volume_number": report.volume_number,
            "title": report.title,
            "summary": report.summary,
        } for report in report_models if report.volume_number in local_volume_numbers]
        stable_work_summary = json.dumps({
            "work_profile": blueprint_record.blueprint.work_profile,
            "categories": [c.model_dump() for c in blueprint_record.blueprint.categories],
        }, ensure_ascii=False)
        stable_directory = json.dumps(wiki_directory, ensure_ascii=False)
        # Keep work-wide, deterministic material first so adjacent Composer
        # requests share the longest possible provider-cache prefix.  The
        # entry-specific plan and evidence deliberately come last.
        common_context = (
            f"Wiki Blueprint 的全作品稳定摘要：\n"
            f"{stable_work_summary}\n\n"
            + ("" if plan.writer_tier == "local" else (
                "全作品卷章索引（只用于定位；需要全文时再批量读取）：\n"
                f"{json.dumps(report_context['work_index'], ensure_ascii=False)}\n\n"
                "全作品公共要点索引（紧凑格式：@ID | 类型 | 正式名 | 别名）：\n"
                f"{serialize_point_index(point_index)}\n\n"
            ))
            + f"可链接 Wiki 词条目录（所有 related_entry_slugs、卡片 slug 与信息表链接只能使用这些精确 slug）：\n"
            f"{stable_directory}\n\n"
            f"当前词条计划：\n{plan.model_dump_json()}\n\n"
            f"当前词条选用的页面模板（按此组织正文小节与信息表；未确认字段可省略）：\n"
            f"{json.dumps(selected_template, ensure_ascii=False)}\n\n"
            + (f"当前专题写作框架：\n{special_plan.model_dump_json()}\n\n" if special_plan else "")
            + f"当前编译模式：{resolved_mode}；写作等级：{plan.writer_tier}\n\n"
            f"蓝图为当前词条绑定的公共要点完整内容（含逐卷更新与证据）：\n"
            f"{serialize_point_context(assigned_points)}\n\n"
            + (
                f"当前词条相关卷总结（快速模式不提供逐章总结）：\n{json.dumps(local_volume_summaries, ensure_ascii=False)}\n\n"
                if plan.writer_tier == "local" and analysis_mode == "fast" else
                f"当前词条的高命中章报告：\n{json.dumps(report_context['relevant_chapters'], ensure_ascii=False)}\n\n"
            )
        )
        schedule = wiki_composer_schedule(plan, resolved_mode, analysis_mode)
        research_rounds = int(schedule["research_rounds"])
        with self.library.connect() as db:
            checkpoint_row = db.execute(
                "SELECT id,agent_checkpoint_json FROM wiki_compile_runs "
                "WHERE series_id=? AND slug=? AND blueprint_id=? AND mode=? "
                "AND status IN ('running','paused','failed') AND agent_checkpoint_json IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (series_id, slug, blueprint_record.id, resolved_mode),
            ).fetchone()
        try:
            resume_state = json.loads(checkpoint_row["agent_checkpoint_json"]) if checkpoint_row else None
        except (TypeError, json.JSONDecodeError):
            resume_state = None
        if resume_state and resume_state.get("base_user_prompt"):
            common_context = str(resume_state["base_user_prompt"])
        run_id = str(checkpoint_row["id"]) if checkpoint_row else uuid.uuid4().hex

        def record_event(event: dict[str, Any]) -> None:
            self._record_agent_event(run_id, event)
            if event.get("event_type") == "model_completed" and api_success_callback:
                api_success_callback()
            if analysis_run_id:
                self._log_analysis(
                    analysis_run_id, analysis_step_key,
                    "error" if event.get("status") in {"failed", "rejected"} else "info",
                    f"{plan.title} · {summarize_analysis_event(str(event.get('event_type', 'event')), event.get('tool_name'), event.get('arguments') or {}, str(event.get('result_preview') or ''), int(event.get('step', 0)), int(event.get('max_steps', 0)))}",
                    int(event.get("input_tokens", 0)), int(event.get("output_tokens", 0)),
                )
        started = time.perf_counter()
        with self.library.connect() as db:
            if checkpoint_row:
                db.execute("UPDATE wiki_compile_runs SET status='running',error=NULL WHERE id=?", (run_id,))
            else:
                db.execute(
                    "INSERT INTO wiki_compile_runs(id,series_id,slug,blueprint_id,mode,status) VALUES(?,?,?,?,?,'running')",
                    (run_id, series_id, slug, blueprint_record.id, resolved_mode),
                )
        if resume_state:
            record_event({
                "step": int(resume_state.get("next_step", 1)),
                "max_steps": research_rounds + 1 if research_rounds else 1,
                "event_type": "wiki_composer_checkpoint_resumed", "status": "completed",
                "arguments": {"next_round": int(resume_state.get("next_step", 1))},
                "result_preview": f"恢复到第 {int(resume_state.get('next_step', 1))} 轮，保留既有工具结果。",
            })
        try:
            report_by_number = {item.volume_number: item for item in report_models}
            chapter_by_id = {
                chapter.chapter_id: (report, chapter)
                for report in report_models for chapter in report.chapter_summaries
            }

            def get_volume_summaries(args: dict[str, Any]) -> dict[str, Any]:
                numbers = list(dict.fromkeys(
                    float(item) for item in (args.get("volume_numbers") or [])
                    if str(item).replace(".", "", 1).isdigit()
                ))[:24]
                found = [report_by_number[number] for number in numbers if number in report_by_number]
                return {
                    "summaries": architect_report_context(found),
                    "missing": [number for number in numbers if number not in report_by_number],
                }

            def get_chapter_summaries(args: dict[str, Any]) -> dict[str, Any]:
                chapter_ids = list(dict.fromkeys(str(item) for item in (args.get("chapter_ids") or [])))[:40]
                found = []
                for chapter_id in chapter_ids:
                    item = chapter_by_id.get(chapter_id)
                    if item:
                        report, chapter = item
                        found.append({
                            "volume_id": report.volume_id, "volume_number": report.volume_number,
                            "volume_title": report.title, "chapter_summary": chapter.model_dump(),
                        })
                return {"chapters": found, "missing": [item for item in chapter_ids if item not in chapter_by_id]}

            def search_original_batch(args: dict[str, Any]) -> dict[str, Any]:
                queries = list(dict.fromkeys(str(item).strip() for item in (args.get("queries") or []) if str(item).strip()))[:8]
                limit = max(1, min(20, int(args.get("limit_per_query") or 8)))
                mode = str(args.get("mode") or "hybrid")
                return {"queries": [{
                    "query": query,
                    **self.search_original_tool(
                        series_id, query, limit,
                        volume_ids=blueprint_record.blueprint.coverage_volume_ids,
                        mode=mode,
                    ),
                } for query in queries]}

            def get_wiki_entries(args: dict[str, Any]) -> dict[str, Any]:
                slugs = list(dict.fromkeys(str(item) for item in (args.get("slugs") or [])))[:30]
                entries = []
                planned = []
                missing = []
                plan_by_slug = {
                    item.slug: item for item in wiki_compilation_plans(blueprint_record.blueprint)
                }
                for entry_slug in slugs:
                    entry = self.get_entry(series_id, entry_slug)
                    if entry and entry.metadata.get("compile_state") == "compiled":
                        entries.append(entry.model_dump())
                    elif entry_slug in plan_by_slug:
                        planned.append(plan_by_slug[entry_slug].model_dump(
                            include={"slug", "title", "kind", "level", "reason", "knowledge_point_ids"}
                        ))
                    else:
                        missing.append(entry_slug)
                return {"entries": entries, "planned_uncompiled": planned, "missing": missing}

            submit_schema = WikiCompiledPayload.model_json_schema()
            submit_tool = {
                "type": "function",
                "function": {
                    "name": "submit_wiki_entry",
                    "description": "提交当前词条的完整最终内容；提交成功即结束 Composer",
                    "parameters": submit_schema,
                },
            }
            research_tools = [
                function_tool(
                    "search_original_text", "按多个查询批量搜索原文证据与精确 locator",
                    {"queries": {"type": "array", "items": {"type": "string"}},
                     "limit_per_query": {"type": "integer"},
                     "mode": self.original_search_mode_schema(series_id)}, ["queries"],
                ),
                function_tool(
                    "get_volume_summaries", "按卷号批量读取卷总结和逐章总结",
                    {"volume_numbers": {"type": "array", "items": {"type": "integer"}}}, ["volume_numbers"],
                ),
                function_tool(
                    "get_chapter_summaries", "按 chapter_id 批量读取章总结",
                    {"chapter_ids": {"type": "array", "items": {"type": "string"}}}, ["chapter_ids"],
                ),
                function_tool(
                    "get_knowledge_points", "按 ID 批量读取固定上下文中未展开的公共要点",
                    {"point_ids": {"type": "array", "items": {"type": "string"}}}, ["point_ids"],
                ),
                function_tool(
                    "get_wiki_entries", "按精确 slug 批量读取已经编译完成的相关词条",
                    {"slugs": {"type": "array", "items": {"type": "string"}}}, ["slugs"],
                ),
            ]
            tools = ([*research_tools, submit_tool] if research_rounds else [submit_tool])
            handlers = {
                "search_original_text": search_original_batch,
                "get_volume_summaries": get_volume_summaries,
                "get_chapter_summaries": get_chapter_summaries,
                "get_knowledge_points": lambda args: {
                    "points": serialize_point_context(self._point_details(
                        series_id, [str(item) for item in args.get("point_ids") or []][:50],
                    ))
                },
                "get_wiki_entries": get_wiki_entries,
            }

            def validate_submission(payload: dict[str, Any]) -> str | None:
                normalized = normalize_wiki_compiled_payload(payload)
                payload.clear()
                payload.update(normalized)
                try:
                    compiled = WikiCompiledPayload.model_validate(payload)
                except Exception as error:
                    return format_wiki_compiled_validation_error(error)
                spoiler_error = validate_compiled_spoiler_policy(
                    compiled, set(chapter_by_id),
                )
                if spoiler_error:
                    return spoiler_error
                payload.clear()
                payload.update(compiled.model_dump())
                return None

            def save_checkpoint(state: dict[str, Any]) -> None:
                state["base_user_prompt"] = common_context
                with self.library.connect() as db:
                    db.execute(
                        "UPDATE wiki_compile_runs SET agent_checkpoint_json=?,input_tokens=?,output_tokens=?,tool_trace_json=? WHERE id=?",
                        (json.dumps(state, ensure_ascii=False), int(state["input_tokens"]),
                         int(state["output_tokens"]), json.dumps(state["tool_trace"], ensure_ascii=False), run_id),
                    )
                if analysis_run_id:
                    self._raise_if_pause_requested(analysis_run_id)

            agent_result = run_tool_agent(
                client=self.provider, model=writer_model,
                system_prompt=WIKI_COMPOSER_AGENT_SYSTEM, user_prompt=common_context,
                tools=tools, handlers=handlers, final_tool_name="submit_wiki_entry",
                max_steps=research_rounds if research_rounds else 1,
                max_tokens=WIKI_COMPOSER_MAX_TOKENS,
                event_callback=record_event,
                tool_budget_steps=research_rounds if research_rounds else None,
                force_final_round=research_rounds > 0,
                # A context-only Composer normally needs one call.  If that
                # submission is malformed or rejected, allow exactly one
                # correction round rather than turning a recoverable page into
                # a failed analysis run.  Successful calls remain single-shot.
                error_recovery_round=research_rounds == 0,
                final_payload_validator=validate_submission,
                resume_state=resume_state,
                checkpoint_callback=save_checkpoint,
                round_plan=list(schedule["round_plan"]),
                reasoning_effort=analysis_reasoning_effort(analysis_mode),
            )
            payload_model = WikiCompiledPayload.model_validate(
                normalize_wiki_compiled_payload(agent_result.payload)
            )
            payload = self._sanitize_wiki_links(payload_model.model_dump(), valid_entry_slugs)
            payload["metadata"] = {**payload["metadata"], "compiler_mode": resolved_mode, "writer_model": writer_model,
                                   "writer_tier": plan.writer_tier, "compile_run_id": run_id}
            input_tokens = agent_result.input_tokens
            output_tokens = agent_result.output_tokens
            raw_output = json.dumps(payload_model.model_dump(), ensure_ascii=False)
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_compile_runs SET status='completed',raw_output=?,tool_trace_json=?,agent_checkpoint_json=NULL,input_tokens=?,output_tokens=?,duration_ms=? WHERE id=?",
                    (raw_output, json.dumps(agent_result.tool_trace, ensure_ascii=False), input_tokens,
                     output_tokens, round((time.perf_counter() - started) * 1000), run_id),
                )
            record_event({
                "step": 90, "event_type": "wiki_entry_raw_output", "status": "completed",
                "result_preview": raw_output, "result_size": len(raw_output),
            })
            return self._save_compiled_entry(blueprint_record, plan, payload)
        except AnalysisPaused:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_compile_runs SET status='paused',error=NULL,duration_ms=? WHERE id=?",
                    (round((time.perf_counter() - started) * 1000), run_id),
                )
            raise
        except Exception as error:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE wiki_compile_runs SET status='failed',error=?,duration_ms=? WHERE id=?",
                    (str(error)[:1500], round((time.perf_counter() - started) * 1000), run_id),
                )
            raise

    @staticmethod
    def _sanitize_wiki_links(payload: dict[str, Any], valid_slugs: set[str]) -> dict[str, Any]:
        """Keep generated prose, but never publish a link to a nonexistent entry."""
        def clean(value: Any) -> Any:
            if isinstance(value, list):
                return [clean(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: clean(item) for key, item in value.items()}
            if "entry_slugs" in result and isinstance(result["entry_slugs"], list):
                result["entry_slugs"] = [slug for slug in result["entry_slugs"] if slug in valid_slugs]
            if "related_entry_slugs" in result and isinstance(result["related_entry_slugs"], list):
                result["related_entry_slugs"] = [slug for slug in result["related_entry_slugs"] if slug in valid_slugs]
            slug = result.get("slug")
            if isinstance(slug, str) and slug not in valid_slugs and not slug.startswith("category:"):
                result.pop("slug", None)
            return result
        return clean(payload)

    @staticmethod
    def _compiler_report_context(
        reports: list[VolumeReportData], plan: WikiEntryPlan,
        assigned_points: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Build a stable work index plus only the current entry's high-hit chapters.

        Full point histories are already supplied separately.  Repeating every
        complete volume and chapter report for a featured entry wastes context
        and weakens the shared prefix.  Composer can batch-fetch any omitted
        complete report through its read-only tools.
        """
        locators: list[str] = [str(item) for item in plan.source_refs]

        def collect_locators(value: Any) -> None:
            if isinstance(value, dict):
                locator = value.get("locator")
                if isinstance(locator, str):
                    locators.append(locator)
                for item in value.values():
                    collect_locators(item)
            elif isinstance(value, list):
                for item in value:
                    collect_locators(item)

        collect_locators(assigned_points)
        ref_chapters = {
            (int(match.group(1)), int(match.group(2)))
            for locator in locators
            if (match := re.search(r"/v(\d+)/c(\d+)", locator))
        }
        ref_volume_numbers = {
            int(match.group(1)) for locator in locators
            if (match := re.search(r"/v(\d+)", locator))
        }
        needles = [
            value.strip() for value in [plan.title, *plan.aliases]
            if isinstance(value, str) and len(value.strip()) >= 2
        ]
        work_index: list[dict[str, Any]] = []
        relevant: list[dict[str, Any]] = []
        for report in sorted(reports, key=lambda item: item.volume_number):
            chapter_index = [{
                "chapter_id": chapter.chapter_id,
                "title": chapter.title,
                "summary_hint": chapter.summary[:240],
            } for chapter in report.chapter_summaries]
            work_index.append({
                "volume_id": report.volume_id,
                "volume_number": report.volume_number,
                "title": report.title,
                "summary": report.summary[:2400],
                "chapters": chapter_index,
            })
            for chapter in report.chapter_summaries:
                serialized = json.dumps(chapter.model_dump(), ensure_ascii=False)
                chapter_match = re.search(r"(?:^|[-_/])v(\d+)(?:[-_/])c(\d+)", chapter.chapter_id)
                locator_match = bool(
                    chapter_match
                    and (int(chapter_match.group(1)), int(chapter_match.group(2))) in ref_chapters
                )
                keyword_match = any(needle in serialized for needle in needles)
                if locator_match or keyword_match:
                    relevant.append({
                        "volume_id": report.volume_id,
                        "volume_number": report.volume_number,
                        "volume_title": report.title,
                        "chapter_summary": chapter.model_dump(),
                    })
            # A volume-only source reference is useful, but should not expand
            # the entire volume. Its overview is already present in work_index.
            if report.volume_number in ref_volume_numbers and not any(
                item["volume_id"] == report.volume_id for item in relevant
            ):
                relevant.append({
                    "volume_id": report.volume_id,
                    "volume_number": report.volume_number,
                    "volume_title": report.title,
                    "note": "蓝图或要点引用了本卷；需要细节时使用章节总结、卷总结或原文检索工具读取。",
                })
        return {"work_index": work_index, "relevant_chapters": relevant}

    def compile_chapter_preview(
        self, chapter_id: str, report_variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
        api_success_callback: Callable[[], None] | None = None,
        analysis_mode: str = "quality",
    ) -> dict[str, Any]:
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        chapter = self.library.get_chapter(chapter_id)
        if chapter is None:
            raise LookupError("Chapter not found")
        with self.library.connect() as db:
            volume_row = db.execute(
                "SELECT v.id FROM chapters c JOIN volumes v ON v.id=c.volume_id WHERE c.id=?", (chapter_id,)
            ).fetchone()
        report = self.latest_volume_report(volume_row["id"], report_variant)
        if report is None or report.report is None:
            raise ValueError("Default volume report is required before chapter preview")
        chapter_summary = next((item for item in report.report.chapter_summaries if item.chapter_id == chapter_id), None)
        if chapter_summary is None:
            raise ValueError("Chapter summary is unavailable")
        source_text = "\n".join(
            f"[{paragraph.locator}] {paragraph.text}" for paragraph in chapter.paragraphs
        )
        summary_text = chapter_summary.summary
        wiki_entries = self.wiki_state(report.series_id).entries
        entry_index = [
            {"title": item.title, "slug": item.slug, "kind": item.kind}
            for item in wiki_entries if item.level != "home"
        ]
        user_prompt = (
            f"目标章节：{chapter.title}\n\n"
            f"本章总结：\n{summary_text}\n\n"
            f"全量 Wiki 词条索引（只在标题确实对应时使用 slug）：\n{json.dumps(entry_index, ensure_ascii=False)}\n\n"
            f"本章原文：\n{source_text}\n\n"
            "只输出一个紧凑 JSON object，字段严格为 intro、overview、people、places、concepts、events。"
            "intro 与 overview 各不超过 120 个汉字；people 最多 4 项，其余分类各最多 3 项；"
            "卡片 summary 不超过 50 个汉字。不要复述章报告，不要输出分析过程或额外字段。"
        )
        input_tokens = output_tokens = cache_hit_tokens = cache_miss_tokens = 0
        for attempt, budget in enumerate((4096, 8192)):
            try:
                result = self.provider.complete_json(
                    model=self.fast_model_id, system=CHAPTER_PREVIEW_SYSTEM, user=user_prompt,
                    max_tokens=budget, temperature=0.35, reasoning_split=False,
                    reasoning_effort="none", json_mode=True,
                )
                input_tokens += result.input_tokens
                output_tokens += result.output_tokens
                cache_hit_tokens += result.cache_hit_tokens
                cache_miss_tokens += result.cache_miss_tokens
                if api_success_callback:
                    api_success_callback()
                raw = result.content.strip().removeprefix("```json").removesuffix("```").strip()
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not all(key in payload for key in ("intro", "overview", "people", "places", "concepts", "events")):
                    raise ValueError("章节轻览缺少必要字段")
                if any(not isinstance(payload[key], list) for key in ("people", "places", "concepts", "events")):
                    raise ValueError("章节轻览要素必须为数组")
                normalized = normalize_chapter_preview_payload(payload)
                if not any(normalized[key] for key in ("people", "places", "concepts", "events")):
                    raise ValueError("章节轻览未生成任何要素，请根据原文补齐")
                break
            except MiniMaxOutputTruncated as error:
                input_tokens += error.input_tokens
                output_tokens += error.output_tokens
                cache_hit_tokens += error.cache_hit_tokens
                cache_miss_tokens += error.cache_miss_tokens
                if attempt == 1:
                    raise ValueError("章节轻览输出连续超限，未保存不完整结果") from error
            except (ValueError, json.JSONDecodeError) as error:
                if attempt == 1:
                    raise
                user_prompt += "\n上次输出不符合结构要求：" + str(error) + "。请输出完整 JSON 和本章中确实存在的要素。"
        with self.library.connect() as db:
            db.execute(
                "UPDATE chapter_overviews SET status='ready',intro=?,overview=?,people_json=?,events_json=?,places_json=?,concepts_json=? WHERE chapter_id=?",
                (
                    normalized["intro"], normalized["overview"],
                    json.dumps(normalized["people"], ensure_ascii=False),
                    json.dumps(normalized["events"], ensure_ascii=False),
                    json.dumps(normalized["places"], ensure_ascii=False),
                    json.dumps(normalized["concepts"], ensure_ascii=False), chapter_id,
                ),
            )
            db.execute(
                "INSERT INTO chapter_preview_runs(id,series_id,chapter_id,model,input_tokens,output_tokens) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, report.series_id, chapter_id, self.fast_model_id, input_tokens, output_tokens),
            )
        return normalized

    def wiki_state(self, series_id: str) -> WikiState:
        blueprint = self.latest_blueprint(series_id)
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT * FROM wiki_entries WHERE series_id=? ORDER BY CASE level WHEN 'home' THEN 0 WHEN 'featured' THEN 1 WHEN 'standard' THEN 2 ELSE 3 END, title",
                (series_id,),
            ).fetchall()
        entries = [self._entry_summary(row) for row in rows]
        planned_slugs = {plan.slug for plan in wiki_compilation_plans(blueprint.blueprint)} if blueprint else set()
        total_plans = len(planned_slugs)
        compiled_slugs = {
            row["slug"] for row in rows
            if row["slug"] in planned_slugs and row["level"] != "home" and (
                json.loads(row["metadata_json"] or "{}").get("compile_state") == "compiled"
                or bool(str(row["body_markdown"] or "").strip())
            )
        }
        return WikiState(
            series_id=series_id,
            blueprint=WikiBlueprintSummary(
                id=blueprint.id,
                version=blueprint.version,
                status=blueprint.status,
                home_title=blueprint.blueprint.home_title,
                complete_work_coverage=blueprint.blueprint.complete_work_coverage,
            ) if blueprint else None,
            entries=entries,
            categories=blueprint.blueprint.categories if blueprint else [],
            navigation_boxes=blueprint.blueprint.navigation_boxes if blueprint else [],
            compilation_status={
                "planned": total_plans,
                "compiled": len(compiled_slugs),
                "remaining": max(0, total_plans - len(compiled_slugs)),
                "complete_work_coverage": blueprint.blueprint.complete_work_coverage if blueprint else False,
            },
        )

    def wiki_mention_index(self, series_id: str) -> list[dict[str, Any]]:
        """Resolve knowledge-point aliases to published Wiki entries once.

        Selection highlighting can then stay entirely local and immediate.  A
        point is bound through the Architect blueprint first; exact canonical
        title matching is only a conservative fallback.
        """
        blueprint = self.latest_blueprint(series_id)
        point_to_slugs: dict[str, list[str]] = {}
        if blueprint:
            for plan in wiki_compilation_plans(blueprint.blueprint):
                for point_id in plan.knowledge_point_ids:
                    point_to_slugs.setdefault(point_id, []).append(plan.slug)
        with self.library.connect() as db:
            entries = db.execute(
                "SELECT slug,title,kind,theme_color,metadata_json FROM wiki_entries "
                "WHERE series_id=? AND level!='home' ORDER BY title", (series_id,),
            ).fetchall()
            points = db.execute(
                "SELECT id,canonical_name,aliases_json FROM knowledge_points "
                "WHERE series_id=? ORDER BY canonical_name", (series_id,),
            ).fetchall()
        by_slug = {row["slug"]: row for row in entries}
        merged: dict[str, dict[str, Any]] = {}

        def normalized(value: Any) -> str:
            return re.sub(r"[\s·・—_\-:：,，。;；/\\()（）\[\]【】]+", "", str(value or "")).casefold()

        def literal_score(canonical: str, title: str) -> float:
            left, right = normalized(canonical), normalized(title)
            if left == right and left:
                return 1
            if len(left) < 2 or len(right) < 2:
                return 0
            sequence = SequenceMatcher(None, left, right).ratio()
            containment = min(len(left), len(right)) / max(len(left), len(right)) if left in right or right in left else 0
            return max(sequence, containment)

        def add_term(target_slug: str, value: str, canonical: str, kind: str, score: float) -> None:
            value = str(value).strip()
            if not normalized(value):
                return
            item = merged[target_slug]
            candidate = {
                "value": value, "canonical_name": canonical,
                "match_kind": kind, "score": round(score, 4),
            }
            key = normalized(value)
            current = next((term for term in item["terms"] if normalized(term["value"]) == key), None)
            if current is None:
                item["terms"].append(candidate)
            elif (kind == "literal", score) > (current["match_kind"] == "literal", current["score"]):
                item["terms"][item["terms"].index(current)] = candidate
            if kind == "literal":
                item["direct_match"] = True

        for row in entries:
            metadata = json.loads(row["metadata_json"] or "{}")
            merged[row["slug"]] = {
                "slug": row["slug"], "title": row["title"], "kind": row["kind"],
                "theme_color": row["theme_color"], "terms": [],
                "direct_match": False,
            }
            add_term(row["slug"], row["title"], row["title"], "literal", 1)
            for alias in metadata.get("aliases", []):
                add_term(row["slug"], alias, row["title"], "literal", 1)
        for point in points:
            canonical = str(point["canonical_name"]).strip()
            aliases = [str(value).strip() for value in json.loads(point["aliases_json"] or "[]") if str(value).strip()]
            ranked = sorted(
                ((literal_score(canonical, row["title"]), row) for row in entries),
                key=lambda item: (-item[0], len(normalized(item[1]["title"])), str(item[1]["title"])),
            )
            literal_target = ranked[0][1] if ranked and ranked[0][0] >= 0.64 else None
            literal_target_slug = literal_target["slug"] if literal_target else None
            if literal_target:
                for value in [canonical, *aliases]:
                    add_term(literal_target_slug, value, canonical, "literal", ranked[0][0])
            for slug in point_to_slugs.get(point["id"], []):
                if slug not in by_slug or slug == literal_target_slug:
                    continue
                for value in [canonical, *aliases]:
                    add_term(slug, value, canonical, "related", 0)
        return [item for item in merged.values() if item["terms"]]

    def get_entry(self, series_id: str, slug: str) -> WikiEntryRecord | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM wiki_entries WHERE series_id=? AND slug=?", (series_id, slug)
            ).fetchone()
        return self._entry_record(row) if row else None

    def create_special(
        self, series_id: str, title: str, introduction: str, related_slugs: list[str]
    ) -> WikiEntryRecord:
        slug = unique_slug(title)
        payload = {
            "title": title,
            "summary": introduction,
            "body_markdown": introduction,
            "metadata": {"related_entry_slugs": related_slugs},
            "blocks": [
                {"type": "prose", "title": "导言", "data": {"markdown": introduction}},
                {"type": "info_cards", "title": "相关词条", "data": {"entry_slugs": related_slugs}},
            ],
            "track_items": [],
        }
        plan = WikiEntryPlan(
            slug=slug, title=title, kind="special", level="standard", reason="用户创建的特辑",
            source_refs=related_slugs, template="special", needs_narrative_track=False,
        )
        blueprint = self.latest_blueprint(series_id)
        if blueprint is None:
            raise ValueError("Wiki Blueprint is not available")
        entry = self._save_compiled_entry(blueprint, plan, payload, user_created=True, level_override="special")
        return entry

    def delete_special(self, series_id: str, slug: str) -> bool:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT id FROM wiki_entries WHERE series_id=? AND slug=? AND user_created=1 AND level='special'",
                (series_id, slug),
            ).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM wiki_entries WHERE id=?", (row["id"],))
        return True

    def get_blueprint(self, blueprint_id: str) -> WikiBlueprintRecord:
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM wiki_blueprints WHERE id=?", (blueprint_id,)).fetchone()
        if not row:
            raise LookupError("Blueprint not found")
        return self._blueprint_record(row)

    def latest_blueprint(self, series_id: str) -> WikiBlueprintRecord | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM wiki_blueprints WHERE series_id=? ORDER BY version DESC LIMIT 1",
                (series_id,),
            ).fetchone()
        return self._blueprint_record(row) if row else None

    def search_original(
        self, series_id: str, query: str, limit: int = 20,
        volume_id: str | None = None, volume_ids: list[str] | None = None,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        requested = mode if mode in {"lexical", "semantic", "hybrid"} else "hybrid"
        available = self.embeddings.status(series_id).available
        effective = requested if available else "lexical"
        if effective == "lexical":
            results = self._search_original_lexical(series_id, query, limit, volume_id, volume_ids)
            for item in results:
                item.setdefault("retrieval_source", "lexical")
            return results
        if effective == "semantic":
            try:
                return self.embeddings.semantic_search(series_id, query, limit, volume_id, volume_ids)
            except ValueError:
                results = self._search_original_lexical(series_id, query, limit, volume_id, volume_ids)
                for item in results:
                    item.update(retrieval_source="lexical", semantic_fallback=True)
                return results
        lexical = self._search_original_lexical(series_id, query, max(limit * 3, 24), volume_id, volume_ids)
        try:
            semantic = self.embeddings.semantic_search(series_id, query, max(limit * 3, 24), volume_id, volume_ids)
        except ValueError:
            semantic = []
            for item in lexical:
                item["semantic_fallback"] = True
        return self._fuse_original_results(lexical, semantic, limit)

    def _search_original_lexical(
        self, series_id: str, query: str, limit: int = 20,
        volume_id: str | None = None, volume_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        chapter_markers = re.findall(r"第[0-9一二三四五六七八九十百零〇两]+章", query)
        # Models sometimes copy paragraph ids from an earlier result into their next
        # lexical query.  Those ids are metadata, not source text, and could otherwise
        # consume the complete term budget before the meaningful words are considered.
        raw_terms = [item for item in re.split(r"[\s,，、;；|/：:]+", query.strip()) if item]
        metadata_pattern = re.compile(r"^(?:p\d{3,}|c\d{1,3}|v\d{1,3}|[\w-]+-v\d{2,}(?:-c\d{2,})?(?:-p\d{3,})?)$", re.I)
        terms = []
        for item in raw_terms:
            item = item.strip("'\"“”‘’「」『』【】()（）")
            if not item:
                continue
            if item in chapter_markers or metadata_pattern.fullmatch(item):
                continue
            if item not in terms:
                terms.append(item)
            if len(terms) == 8:
                break
        if not terms:
            return []
        with self.library.connect() as db:
            patterns = [f"%{term}%" for term in terms]
            # Longer terms carry more information than generic one-character terms.
            score_sql = " + ".join(
                f"CASE WHEN p.text LIKE ? THEN {max(1, min(len(term), 8))} ELSE 0 END"
                for term in terms
            )
            match_sql = " OR ".join("p.text LIKE ?" for _ in terms)
            params: list[Any] = [*patterns, series_id, *patterns]
            # Multi-term queries are usually generated from an event description.
            # Require at least one distinctive term so a short generic word such as
            # “指导” cannot fill Top-N by itself.  A deliberate single-word search
            # keeps the original broad substring behavior.
            anchor_terms = [term for term in terms if len(term) >= 3] if len(terms) > 1 else []
            anchor_clause = ""
            if anchor_terms:
                anchor_clause = " AND (" + " OR ".join("p.text LIKE ?" for _ in anchor_terms) + ")"
                params.extend(f"%{term}%" for term in anchor_terms)
            volume_clause = ""
            scoped_volume_ids = [volume_id] if volume_id else (volume_ids or [])
            if scoped_volume_ids:
                volume_clause = f" AND v.id IN ({','.join('?' for _ in scoped_volume_ids)})"
                params.extend(scoped_volume_ids)
            chapter_clause = ""
            if chapter_markers:
                chapter_clause = " AND (" + " OR ".join("c.title LIKE ?" for _ in chapter_markers) + ")"
                params.extend(f"%{marker}%" for marker in chapter_markers)
            params.append(limit)
            rows = db.execute(
                f"SELECT p.id,p.locator,p.text,c.id chapter_id,c.title chapter_title,c.chapter_index,"
                f"p.paragraph_index source_index,v.id volume_id,v.number,({score_sql}) match_score "
                "FROM paragraphs p JOIN chapters c ON c.id=p.chapter_id JOIN volumes v ON v.id=c.volume_id "
                f"WHERE v.series_id=? AND ({match_sql}){anchor_clause}{volume_clause}{chapter_clause} "
                "ORDER BY match_score DESC,v.number,c.chapter_index,p.paragraph_index LIMIT ?",
                params,
            ).fetchall()
        results = []
        for relevance_rank, row in enumerate(rows, start=1):
            item = dict(row)
            item["matched_terms"] = [term for term in terms if term in row["text"]]
            item["relevance_rank"] = relevance_rank
            results.append(item)
        results.sort(key=self._original_source_order)
        return results

    @staticmethod
    def _original_source_order(item: dict[str, Any]) -> tuple[float, int, int, str]:
        return (
            float(item.get("number") or 0),
            int(item.get("chapter_index") or 0),
            int(item.get("source_index") or 0),
            str(item.get("locator") or ""),
        )

    def _fuse_original_results(
        self, lexical: list[dict[str, Any]], semantic: list[dict[str, Any]], limit: int,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        lexical_by_relevance = sorted(lexical, key=lambda item: int(item.get("relevance_rank") or 10**9))
        semantic_by_relevance = sorted(semantic, key=lambda item: int(item.get("relevance_rank") or 10**9))
        for rank, raw in enumerate(lexical_by_relevance, start=1):
            item = dict(raw)
            item.update(retrieval_source="lexical", lexical_rank=rank, fused_score=1.0 / (60 + rank))
            candidates.append(item)
        for rank, raw in enumerate(semantic_by_relevance, start=1):
            matched = next((item for item in candidates if (
                item.get("chapter_id") == raw.get("chapter_id")
                and str(raw.get("start_locator") or raw.get("locator")) <= str(item.get("locator"))
                and str(item.get("locator")) <= str(raw.get("end_locator") or raw.get("locator"))
            )), None)
            if matched:
                matched["retrieval_source"] = "hybrid"
                matched["semantic_rank"] = rank
                matched["semantic_score"] = raw.get("semantic_score")
                matched["fused_score"] = float(matched["fused_score"]) + 1.0 / (60 + rank)
            else:
                item = dict(raw)
                item.update(semantic_rank=rank, fused_score=1.0 / (60 + rank))
                candidates.append(item)
        candidates.sort(key=lambda item: (
            -float(item.get("fused_score") or 0), float(item.get("number") or 0), str(item.get("locator") or ""),
        ))
        selected, relevance_cutoff = _select_ranked_results(
            candidates, limit, "fused_score",
        )
        if semantic and not any(item.get("retrieval_source") in {"semantic", "hybrid"} for item in selected):
            # Preserve one genuinely strong semantic recall when lexical hits occupy
            # every Top-N slot.  The absolute cosine floor prevents diversity from
            # promoting an unrelated semantic candidate merely because it ranked first.
            semantic_candidate = next((item for item in candidates if (
                item.get("retrieval_source") == "semantic"
                and float(item.get("fused_score") or 0) >= relevance_cutoff
                and float(item.get("semantic_score") or 0) >= 0.25
            )), None)
            if semantic_candidate and selected:
                selected[-1] = semantic_candidate
        for relevance_rank, item in enumerate(selected, start=1):
            item["relevance_rank"] = relevance_rank
            item["relevance_cutoff"] = round(relevance_cutoff, 8)
        selected.sort(key=self._original_source_order)
        return selected

    def original_search_mode_schema(self, series_id: str) -> dict[str, Any]:
        if self.embeddings.status(series_id).available:
            return {"type": "string", "enum": ["hybrid", "semantic", "lexical"], "default": "hybrid"}
        return {"type": "string", "enum": ["lexical"], "default": "lexical"}

    def search_original_tool(
        self, series_id: str, query: str, limit: int = 20,
        volume_id: str | None = None, volume_ids: list[str] | None = None,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        """Return evidence plus the query interpretation used by agent tools.

        Keeping diagnostics outside the evidence list lets the trace explain an empty
        result without inventing a fake paragraph that a writer might cite.
        """
        raw_terms = [item for item in re.split(r"[\s,，、;；|/：:]+", query.strip()) if item]
        available = self.embeddings.status(series_id).available
        requested_mode = mode if mode in {"lexical", "semantic", "hybrid"} else "hybrid"
        effective_mode = requested_mode if available else "lexical"
        results = self.search_original(series_id, query, limit, volume_id, volume_ids, effective_mode)
        if any(item.get("semantic_fallback") for item in results):
            effective_mode = "lexical"
        chapter_markers = re.findall(r"第[0-9一二三四五六七八九十百零〇两]+章", query)
        matched = sorted({term for item in results for term in item.get("matched_terms", [])})
        diagnostic = (
            f"找到 {len(results)} 个段落；命中词：{'、'.join(matched)}。"
            if results else
            ("没有找到相关语义或字面结果；请调整查询描述。" if effective_mode != "lexical" else
             "没有字面命中。当前使用词法检索；请改用原文中可能出现的实体名或更短关键词，必要时读取章节原文。")
        )
        return {
            "query": query,
            "query_parts": raw_terms,
            "chapter_scope": chapter_markers,
            "volume_scope": [volume_id] if volume_id else (volume_ids or []),
            "result_count": len(results),
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "semantic_available": available,
            "diagnostic": diagnostic,
            "results": results,
        }

    def search_wiki(self, series_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        terms = [item for item in re.split(r"[\s,，、;；|/：:]+", query.strip()) if item][:8]
        if not terms:
            return []
        indexed_aliases: dict[str, list[str]] = {}
        for entry in self.wiki_mention_index(series_id):
            title_key = str(entry.get("title") or "").strip().casefold()
            aliases = list(dict.fromkeys(
                str(term.get("value") or "").strip()
                for term in entry.get("terms") or []
                if term.get("match_kind") == "literal"
                and str(term.get("value") or "").strip()
                and str(term.get("value") or "").strip().casefold() != title_key
            ))
            if aliases:
                indexed_aliases[str(entry["slug"])] = aliases
        alias_hit_slugs = [
            slug for slug, aliases in indexed_aliases.items()
            if any(query.strip().casefold() == alias.casefold() for alias in aliases)
        ]
        patterns = [f"%{term}%" for term in terms]
        score_sql = " + ".join(
            f"CASE WHEN title LIKE ? THEN {len(term) + 6} WHEN metadata_json LIKE ? THEN {len(term) + 5} "
            f"WHEN summary LIKE ? THEN {len(term) + 2} WHEN body_markdown LIKE ? THEN {max(1, len(term))} ELSE 0 END" for term in terms
        )
        score_params = [pattern for pattern in patterns for _ in range(4)]
        match_sql = " OR ".join("title LIKE ? OR metadata_json LIKE ? OR summary LIKE ? OR body_markdown LIKE ?" for _ in terms)
        match_params = [pattern for pattern in patterns for _ in range(4)]
        with self.library.connect() as db:
            rows = list(db.execute(
                f"SELECT slug,title,kind,level,summary,body_markdown,metadata_json,theme_color,({score_sql}) match_score "
                f"FROM wiki_entries WHERE series_id=? AND ({match_sql}) "
                "ORDER BY match_score DESC,CASE level WHEN 'featured' THEN 0 WHEN 'standard' THEN 1 ELSE 2 END,title LIMIT ?",
                (*score_params, series_id, *match_params, limit),
            ).fetchall())
            existing_slugs = {str(row["slug"]) for row in rows}
            missing_alias_slugs = [slug for slug in alias_hit_slugs if slug not in existing_slugs]
            if missing_alias_slugs:
                placeholders = ",".join("?" for _ in missing_alias_slugs)
                rows.extend(db.execute(
                    "SELECT slug,title,kind,level,summary,body_markdown,metadata_json,theme_color,0 match_score "
                    f"FROM wiki_entries WHERE series_id=? AND slug IN ({placeholders})",
                    (series_id, *missing_alias_slugs),
                ).fetchall())
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            title = item["title"] or ""
            summary = item["summary"] or ""
            body = item.pop("body_markdown", "") or ""
            metadata = json.loads(item.pop("metadata_json", "{}") or "{}")
            aliases = list(dict.fromkeys([
                *[str(value).strip() for value in metadata.get("aliases", []) if str(value).strip()],
                *indexed_aliases.get(str(item["slug"]), []),
            ]))
            query_key = query.strip().casefold()
            title_key = title.strip().casefold()
            title_exact = query_key == title_key
            title_partial = bool(query_key) and query_key in title_key and not title_exact
            alias_matches = [
                alias for alias in aliases
                if query_key == alias.casefold()
            ]
            alias_matches.sort(key=lambda alias: (query.strip().casefold() != alias.casefold(), len(alias)))
            if title_exact:
                title_match_kind = "exact_title"
                matched_title = title
            elif alias_matches:
                title_match_kind = "exact_alias"
                matched_title = alias_matches[0]
            elif title_partial:
                title_match_kind = "partial_title"
                matched_title = title
            else:
                title_match_kind = "none"
                matched_title = None
            if not matched_title and not any(
                term.casefold() in summary.casefold() or term.casefold() in body.casefold()
                for term in terms
            ):
                continue
            source = summary
            matched_term = next((term for term in terms if term.casefold() in summary.casefold()), "")
            if not matched_title and not matched_term:
                source = body
                matched_term = next((term for term in terms if term.casefold() in body.casefold()), "")
            start = source.casefold().find(matched_term.casefold()) if matched_term else -1
            if start >= 0:
                excerpt_start = max(0, start - 34)
                excerpt_end = min(len(source), start + len(matched_term) + 58)
                excerpt = source[excerpt_start:excerpt_end].replace("\n", " ").strip()
                match_start = start - excerpt_start
                match_end = match_start + len(matched_term)
            else:
                excerpt = summary or title
                match_start = 0
                match_end = 0
            item.update(
                excerpt=excerpt,
                match_start=match_start,
                match_end=match_end,
                title_match=bool(matched_title),
                title_match_kind=title_match_kind,
                matched_title=matched_title,
            )
            if alias_matches:
                item["match_score"] = int(item.get("match_score") or 0) + 100
            results.append(item)
        wiki_title_priority = {"exact_title": 0, "exact_alias": 1, "partial_title": 2, "none": 3}
        results.sort(key=lambda item: (wiki_title_priority[str(item["title_match_kind"])], -int(item.get("match_score") or 0), str(item["title"])))
        return results[:limit]

    def _variant_source(self, variant: HarnessVariant, chapters: list[Any]) -> str:
        if variant == "chapter_packet_dag":
            packets = [self.harness.artifacts(chapter.id).packet.model_dump() for chapter in chapters]
            return json.dumps(packets, ensure_ascii=False)
        if variant in {"full_volume_flash_map", "fast_volume_direct", "long_context_direct"}:
            parts = []
            for chapter_summary in chapters:
                chapter = self.library.get_chapter(chapter_summary.id)
                parts.append(f"\n## {chapter.title}\n" + "\n".join(f"[{p.locator}] {p.text}" for p in chapter.paragraphs))
            return "\n".join(parts)
        return json.dumps(
            [{"chapter_id": chapter.id, "title": chapter.title, "characters": chapter.character_count} for chapter in chapters],
            ensure_ascii=False,
        ) + "\n本变体不预先提供正文或 Narrative Packet，必须使用检索工具主动阅读。"


    def _previous_reports(
        self, series_id: str, current_number: int | float,
        variant: HarnessVariant = DEFAULT_HARNESS_VARIANT,
    ) -> list[dict[str, Any]]:
        reports = self.list_volume_reports(series_id)
        candidates: dict[int, list[VolumeReportRecord]] = {}
        for record in reports:
            if (record.volume_number < current_number and record.status == "completed"
                    and record.report):
                candidates.setdefault(record.volume_number, []).append(record)
        result = []
        for volume_number in sorted(candidates):
            records = candidates[volume_number]
            # Reports are already ordered newest-first within each volume.
            # Prefer the current pipeline, but allow a quality/fast hand-off
            # when the preceding volume was produced by the other mode.
            record = next((item for item in records if item.harness_variant == variant), records[0])
            assert record.report is not None
            previous = {
                "volume_number": record.volume_number, "title": record.report.title,
                "summary": record.report.summary,
            }
            result.append(previous)
        return result

    def _get_volume(self, volume_id: str):
        with self.library.connect() as db:
            row = db.execute("SELECT series_id FROM volumes WHERE id=?", (volume_id,)).fetchone()
        if not row:
            raise LookupError("Volume not found")
        series = self.library.get_series(row["series_id"])
        volume = next(item for item in series.volumes if item.id == volume_id)
        return volume, series

    def _upsert_home_entry(self, blueprint_id: str, blueprint: WikiBlueprintData, published: bool) -> None:
        authored_sections = [normalize_wiki_block(section) for section in blueprint.home_sections if section.get("type")]
        blocks = authored_sections or [WikiBlock(type="prose", title="从这里开始", data={"markdown": blueprint.home_intro})]
        # A structured home page owns its reading order through home_sections.
        # Keeping the same narrative in body_markdown would render it once
        # before the modules and then repeat it inside the ordered blocks.
        rendered_body_markdown = "" if authored_sections else (blueprint.home_body_markdown or blueprint.home_intro)
        category_by_name = {
            str(name).strip().casefold(): category
            for category in blueprint.categories for name in (category.id, category.label)
        }
        category_by_id = {category.id: category for category in blueprint.categories}
        linked_categories: set[str] = set()
        for block in blocks:
            if block.type != "module_buttons" or not isinstance(block.data.get("items"), list):
                continue
            repaired = []
            for raw_item in block.data["items"]:
                item = dict(raw_item) if isinstance(raw_item, dict) else {"label": str(raw_item)}
                name = str(item.get("label") or item.get("title") or "").strip().casefold()
                raw_slug = str(item.get("slug") or "")
                category = category_by_id.get(raw_slug.removeprefix("category:")) if raw_slug.startswith("category:") else None
                category = category or category_by_name.get(name)
                if category:
                    item["slug"] = f"category:{category.id}"
                    linked_categories.add(category.id)
                repaired.append(item)
            block.data["items"] = repaired
        # Authored home_sections are the complete homepage. Do not append legacy
        # "helpful" modules after Architect has deliberately chosen its pacing.
        if not authored_sections:
            missing_categories = [category for category in blueprint.categories if category.id not in linked_categories]
            if missing_categories:
                blocks.append(WikiBlock(type="module_buttons", title="浏览分类", data={"items": [
                    {"label": category.label, "description": category.description, "slug": f"category:{category.id}"}
                    for category in missing_categories[:8]
                ]}))
            featured = [plan for plan in blueprint.entry_plans if plan.writer_tier == "core"][:8]
            if featured:
                blocks.append(WikiBlock(type="info_cards", title="主要人物与要点", data={"items": [
                    {"title": plan.title, "summary": plan.reason, "slug": plan.slug,
                     "kind": plan.kind, "theme_color": theme_for_kind(plan.kind)}
                    for plan in featured
                ]}))
            blocks.append(WikiBlock(type="volume_cards", title="卷目导览", data={"items": []}))
        valid_page_slugs = {plan.slug for plan in wiki_compilation_plans(blueprint)}
        cleaned = self._sanitize_wiki_links(
            {"blocks": [block.model_dump() for block in blocks]}, valid_page_slugs,
        )["blocks"]
        blocks = [WikiBlock.model_validate(block) for block in cleaned]
        entry_id = f"wiki-{blueprint.series_id}-home"
        with self.library.connect() as db:
            existing = db.execute("SELECT revision FROM wiki_entries WHERE id=?", (entry_id,)).fetchone()
            revision = int(existing["revision"] + 1) if existing else 1
            db.execute(
                "INSERT INTO wiki_entries(id,series_id,slug,title,kind,level,theme_color,summary,body_markdown,metadata_json,infobox_json,blocks_json,status,revision,blueprint_id) "
                "VALUES(?,?,?,?,?,'home','#667c68',?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title,summary=excluded.summary,body_markdown=excluded.body_markdown,metadata_json=excluded.metadata_json,infobox_json=excluded.infobox_json,blocks_json=excluded.blocks_json,status=excluded.status,revision=excluded.revision,blueprint_id=excluded.blueprint_id,updated_at=CURRENT_TIMESTAMP",
                (
                    entry_id, blueprint.series_id, "home", blueprint.home_title, "home",
                    blueprint.home_intro, rendered_body_markdown,
                    json.dumps({"work_profile": blueprint.work_profile}, ensure_ascii=False),
                    json.dumps({}, ensure_ascii=False),
                    json.dumps([block.model_dump() for block in blocks], ensure_ascii=False),
                    "published" if published else "draft", revision, blueprint_id,
                ),
            )

    def _upsert_page_skeletons(self, blueprint_id: str, blueprint: WikiBlueprintData) -> None:
        with self.library.connect() as db:
            for plan in wiki_compilation_plans(blueprint):
                entry_id = f"wiki-{blueprint.series_id}-{plan.slug}"
                existing = db.execute("SELECT id FROM wiki_entries WHERE id=?", (entry_id,)).fetchone()
                if existing:
                    db.execute("UPDATE wiki_entries SET blueprint_id=? WHERE id=?", (blueprint_id, entry_id))
                    continue
                blocks = [WikiBlock(
                    type="callout", title="词条待编译",
                    data={"text": plan.reason},
                )]
                db.execute(
                    "INSERT INTO wiki_entries(id,series_id,slug,title,kind,level,theme_color,summary,body_markdown,metadata_json,infobox_json,blocks_json,status,revision,user_created,blueprint_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry_id, blueprint.series_id, plan.slug, plan.title, plan.kind,
                     "special" if plan.kind == "special" else plan.level,
                     theme_for_entry(plan.kind, plan.slug), plan.reason, "",
                     json.dumps({"compile_state": "planned", "action": plan.action,
                                 "changed_sections": plan.changed_sections, "subcategory": plan.subcategory,
                                 "writer_tier": plan.writer_tier,
                                 "template": plan.template,
                                 "knowledge_point_ids": plan.knowledge_point_ids}, ensure_ascii=False),
                     json.dumps({}, ensure_ascii=False),
                     json.dumps([block.model_dump() for block in blocks], ensure_ascii=False),
                     "draft", 0, 0, blueprint_id),
                )

    def _save_compiled_entry(
        self,
        blueprint: WikiBlueprintRecord,
        plan: WikiEntryPlan,
        payload: dict[str, Any],
        user_created: bool = False,
        level_override: str | None = None,
    ) -> WikiEntryRecord:
        entry_id = f"wiki-{blueprint.series_id}-{plan.slug}"
        entry_title = str(payload.get("title") or plan.title).strip()
        body_markdown = strip_duplicate_entry_title(
            str(payload.get("body_markdown") or ""), entry_title
        )
        allowed_blocks = {"prose","module_buttons","info_cards","fact_table","quote","timeline","narrative_track","entity_list","chapter_list","event_list","related_entries","callout","markdown","volume_cards","chapter_cards"}
        blocks = []
        for raw in payload.get("blocks") or []:
            if raw.get("type") in allowed_blocks:
                blocks.append(WikiBlock.model_validate(raw))
        tracks_raw = self._augment_track_items(blueprint.series_id, plan, payload)
        spoiler_policy = WikiSpoilerPolicy.model_validate(
            payload.get("spoiler_policy") or {}
        )
        with self.library.connect() as db:
            existing = db.execute("SELECT revision FROM wiki_entries WHERE id=?", (entry_id,)).fetchone()
            revision = int(existing["revision"] + 1) if existing else 1
            db.execute(
                "INSERT INTO wiki_entries(id,series_id,slug,title,kind,level,theme_color,summary,body_markdown,metadata_json,infobox_json,blocks_json,spoiler_json,status,revision,user_created,blueprint_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,kind=excluded.kind,level=excluded.level,theme_color=excluded.theme_color,summary=excluded.summary,body_markdown=excluded.body_markdown,metadata_json=excluded.metadata_json,infobox_json=excluded.infobox_json,blocks_json=excluded.blocks_json,spoiler_json=excluded.spoiler_json,status=excluded.status,revision=excluded.revision,user_created=excluded.user_created,blueprint_id=excluded.blueprint_id,updated_at=CURRENT_TIMESTAMP",
                (
                    entry_id, blueprint.series_id, plan.slug, entry_title,
                    plan.kind, level_override or ("special" if plan.kind == "special" else plan.level), theme_for_entry(plan.kind, plan.slug),
                    payload.get("summary") or "", body_markdown,
                    json.dumps({**(payload.get("metadata") or {}), "compile_state": "compiled"}, ensure_ascii=False),
                    json.dumps(payload.get("infobox") or {}, ensure_ascii=False),
                    json.dumps([block.model_dump() for block in blocks], ensure_ascii=False),
                    spoiler_policy.model_dump_json(),
                    "published" if blueprint.status in {"publishable", "published"} else "draft",
                    revision, int(user_created), blueprint.id,
                ),
            )
            db.execute("DELETE FROM narrative_tracks WHERE entry_id=?", (entry_id,))
            for index, raw in enumerate(tracks_raw, start=1):
                if not raw.get("start_locator"):
                    continue
                db.execute(
                    "INSERT INTO narrative_tracks(id,entry_id,kind,category,title,note,start_locator,end_locator,importance,evidence_json,source_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{entry_id}-track-{index:03d}", entry_id,
                        "range" if raw.get("end_locator") else "point", raw.get("category") or "story",
                        raw.get("title") or "剧情节点", raw.get("note") or "", raw["start_locator"],
                        raw.get("end_locator"), float(raw.get("importance", 0.5)),
                        json.dumps(raw.get("evidence") or [], ensure_ascii=False),
                        raw.get("source_type") or "ai",
                    ),
                )
        return self.get_entry(blueprint.series_id, plan.slug)

    def _augment_track_items(
        self, series_id: str, plan: WikiEntryPlan, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Compile deterministic citation and appearance tracks beside AI-curated tracks."""
        tracks = [dict(item) for item in (payload.get("track_items") or [])]
        seen = {
            (item.get("start_locator"), item.get("end_locator"), item.get("source_type", "ai"))
            for item in tracks
        }
        serialized = json.dumps(
            {"body": payload.get("body_markdown") or "", "blocks": payload.get("blocks") or []},
            ensure_ascii=False,
        )
        citation_pattern = r"lr://[^\s)\]>'\"，。；]+"
        citations_by_chapter: dict[str, list[str]] = {}
        for locator in dict.fromkeys(re.findall(citation_pattern, serialized)):
            chapter_match = re.match(r"(.+/v\d+(?:\.\d+)?/c\d+)(?:/p\d+)?$", locator)
            chapter_key = chapter_match.group(1) if chapter_match else locator
            citations_by_chapter.setdefault(chapter_key, []).append(locator)
        with self.library.connect() as db:
            for chapter_key, locators in citations_by_chapter.items():
                row = db.execute(
                    "SELECT c.title chapter_title,MIN(p.locator) start_locator,"
                    "MAX(p.locator) end_locator FROM paragraphs p "
                    "JOIN chapters c ON c.id=p.chapter_id WHERE p.locator LIKE ? "
                    "GROUP BY c.id,c.title",
                    (f"{chapter_key}/p%",),
                ).fetchone()
                start_locator = row["start_locator"] if row else min(locators)
                end_locator = row["end_locator"] if row else max(locators)
                key = (start_locator, end_locator, "citation")
                if key in seen:
                    continue
                chapter_title = row["chapter_title"] if row else "引用章节"
                tracks.append({
                    "kind": "range", "category": "citation", "title": chapter_title,
                    "note": f"当前词条在《{chapter_title}》引用了 {len(locators)} 处原文。",
                    "start_locator": start_locator, "end_locator": end_locator,
                    "importance": 0.42,
                    "evidence": [{"locator": locator} for locator in locators],
                    "source_type": "citation",
                })
                seen.add(key)

        appearance_tracks = self._appearance_track_items(
            series_id, plan.slug, str(payload.get("title") or plan.title),
            plan.knowledge_point_ids,
        )
        for item in appearance_tracks:
            key = (item["start_locator"], item["end_locator"], "appearance")
            if key in seen:
                continue
            tracks.append(item)
            seen.add(key)
        return tracks

    @staticmethod
    def _track_name_key(value: Any) -> str:
        return re.sub(
            r"[\s·・—_\-:：,，。;；/\\()（）\[\]【】《》「」『』]+", "", str(value or ""),
        ).casefold()

    @staticmethod
    def _track_chapter_key(locator: str) -> tuple[float, int] | None:
        match = re.search(r"/v(\d+(?:\.\d+)?)/c(\d+)", str(locator or ""))
        return (float(match.group(1)), int(match.group(2))) if match else None

    def _appearance_track_context(self, series_id: str) -> dict[str, Any]:
        """Load the work-wide point and preview indexes once for a track refresh."""
        with self.library.connect() as db:
            point_rows = db.execute(
                "SELECT id,canonical_name,aliases_json FROM knowledge_points WHERE series_id=?",
                (series_id,),
            ).fetchall()
            update_rows = db.execute(
                "SELECT kpu.point_id,kpu.evidence_json FROM knowledge_point_updates kpu "
                "JOIN knowledge_points kp ON kp.id=kpu.point_id WHERE kp.series_id=? "
                "ORDER BY kpu.volume_number,kpu.created_at",
                (series_id,),
            ).fetchall()
            chapter_rows = db.execute(
                "SELECT c.id chapter_id,c.title chapter_title,c.chapter_index,v.number volume_number,"
                "MIN(p.locator) start_locator,MAX(p.locator) end_locator,"
                "o.people_json,o.events_json,o.places_json,o.concepts_json "
                "FROM chapters c JOIN volumes v ON v.id=c.volume_id "
                "JOIN chapter_overviews o ON o.chapter_id=c.id "
                "LEFT JOIN paragraphs p ON p.chapter_id=c.id "
                "WHERE v.series_id=? GROUP BY c.id,c.title,c.chapter_index,v.number,"
                "o.people_json,o.events_json,o.places_json,o.concepts_json "
                "ORDER BY v.number,c.chapter_index",
                (series_id,),
            ).fetchall()
        evidence_by_point: dict[str, list[dict[str, str]]] = {}
        for update in update_rows:
            try:
                evidence = json.loads(update["evidence_json"] or "[]")
            except json.JSONDecodeError:
                evidence = []
            evidence_by_point.setdefault(str(update["point_id"]), []).extend(normalize_evidence(evidence))
        points = []
        for point in point_rows:
            try:
                aliases = json.loads(point["aliases_json"] or "[]")
            except json.JSONDecodeError:
                aliases = []
            points.append({
                "id": str(point["id"]), "canonical_name": str(point["canonical_name"]),
                "aliases": aliases, "evidence": evidence_by_point.get(str(point["id"]), []),
            })
        chapters = []
        for source in chapter_rows:
            row = dict(source)
            slugs: set[str] = set()
            for column in ("people_json", "events_json", "places_json", "concepts_json"):
                try:
                    cards = json.loads(row[column] or "[]")
                except json.JSONDecodeError:
                    cards = []
                for card in cards if isinstance(cards, list) else []:
                    if not isinstance(card, dict):
                        continue
                    slug = str(card.get("slug") or "").strip()
                    if slug.startswith("lr://wiki/"):
                        slug = slug[len("lr://wiki/"):].split("?", 1)[0].split("#", 1)[0]
                    if slug:
                        slugs.add(slug)
            chapters.append({"row": row, "slugs": slugs})
        return {"points": points, "chapters": chapters}

    def _appearance_track_items(
        self, series_id: str, entry_slug: str, entry_title: str,
        bound_point_ids: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Merge canonical point evidence with explicit links from chapter previews.

        Point evidence owns the optional note. A preview link only contributes
        chapter membership, so it cannot overwrite a note already attached to
        evidence for the same chapter.
        """
        title_key = self._track_name_key(entry_title)
        if not title_key:
            return []
        bound_ids = set(bound_point_ids or [])
        prepared = context or self._appearance_track_context(series_id)
        candidates: list[tuple[int, int, str, Any]] = []
        for point in prepared["points"]:
            canonical_match = self._track_name_key(point["canonical_name"]) == title_key
            alias_match = any(self._track_name_key(alias) == title_key for alias in point["aliases"])
            if canonical_match or alias_match:
                candidates.append((
                    0 if canonical_match else 1,
                    0 if point["id"] in bound_ids else 1,
                    str(point["id"]), point,
                ))
        candidates.sort(key=lambda item: item[:3])
        point = candidates[0][3] if candidates else None
        chapter_rows = [item["row"] for item in prepared["chapters"]]

        chapter_by_key = {
            (float(row["volume_number"]), int(row["chapter_index"])): row
            for row in chapter_rows if row["start_locator"] and row["end_locator"]
        }
        appearances: dict[tuple[float, int], dict[str, Any]] = {}

        def ensure(chapter_key: tuple[float, int]) -> dict[str, Any] | None:
            row = chapter_by_key.get(chapter_key)
            if row is None:
                return None
            return appearances.setdefault(chapter_key, {"row": row, "notes": [], "evidence": []})

        for raw in point["evidence"] if point else []:
            chapter_key = self._track_chapter_key(raw.get("locator", ""))
            target = ensure(chapter_key) if chapter_key else None
            if target is None:
                continue
            locator = str(raw.get("locator") or "")
            note = str(raw.get("note") or "").strip()
            existing = next((item for item in target["evidence"] if item["locator"] == locator), None)
            if existing is None:
                target["evidence"].append({"locator": locator, "note": note})
            elif note and not existing.get("note"):
                existing["note"] = note
            if note and note not in target["notes"]:
                target["notes"].append(note)

        for chapter in prepared["chapters"]:
            row = chapter["row"]
            if entry_slug in chapter["slugs"]:
                ensure((float(row["volume_number"]), int(row["chapter_index"])))

        result = []
        for chapter_key in sorted(appearances):
            item = appearances[chapter_key]
            row = item["row"]
            result.append({
                "kind": "range", "category": "appearance", "title": row["chapter_title"],
                "note": "；".join(item["notes"]),
                "start_locator": row["start_locator"], "end_locator": row["end_locator"],
                "importance": 0.3, "evidence": item["evidence"],
                "source_type": "appearance",
            })
        return result

    def _refresh_appearance_tracks(
        self, series_id: str, blueprint: WikiBlueprintRecord,
    ) -> int:
        """Rebuild appearance tracks after previews have finished for this run."""
        if not hasattr(blueprint, "blueprint"):
            return 0
        bound_by_slug = {
            plan.slug: plan.knowledge_point_ids
            for plan in wiki_compilation_plans(blueprint.blueprint)
        }
        with self.library.connect() as db:
            entries = db.execute(
                "SELECT id,slug,title FROM wiki_entries WHERE series_id=? AND level!='home'",
                (series_id,),
            ).fetchall()
        context = self._appearance_track_context(series_id)
        compiled: list[tuple[Any, list[dict[str, Any]]]] = []
        for entry in entries:
            compiled.append((entry, self._appearance_track_items(
                series_id, entry["slug"], entry["title"], bound_by_slug.get(entry["slug"], []), context,
            )))
        with self.library.connect() as db:
            for entry, tracks in compiled:
                db.execute(
                    "DELETE FROM narrative_tracks WHERE entry_id=? AND source_type='appearance'",
                    (entry["id"],),
                )
                for index, raw in enumerate(tracks, start=1):
                    db.execute(
                        "INSERT INTO narrative_tracks(id,entry_id,kind,category,title,note,start_locator,end_locator,importance,evidence_json,source_type) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"{entry['id']}-appearance-{index:03d}", entry["id"], "range",
                            "appearance", raw["title"], raw["note"], raw["start_locator"],
                            raw["end_locator"], raw["importance"],
                            json.dumps(raw["evidence"], ensure_ascii=False), "appearance",
                        ),
                    )
        return sum(len(tracks) for _entry, tracks in compiled)

    def _entry_record(self, row: Any) -> WikiEntryRecord:
        metadata = json.loads(row["metadata_json"])
        with self.library.connect() as db:
            tracks = db.execute(
                "SELECT * FROM narrative_tracks WHERE entry_id=? ORDER BY start_locator", (row["id"],)
            ).fetchall()
            blueprint_row = db.execute("SELECT blueprint_json FROM wiki_blueprints WHERE id=?", (row["blueprint_id"],)).fetchone()
        if blueprint_row:
            blueprint = json.loads(blueprint_row["blueprint_json"])
            plan = next((item for item in blueprint.get("entry_plans", []) if item.get("slug") == row["slug"]), {})
            schemas = blueprint.get("infobox_schemas", {})
            fields = plan.get("infobox_schema") or schemas.get(plan.get("template")) or schemas.get(plan.get("kind")) or []
            metadata["infobox_labels"] = {str(field["key"]): str(field["label"]) for field in fields if isinstance(field, dict) and field.get("key") and field.get("label")}
        return WikiEntryRecord(
            id=row["id"], series_id=row["series_id"], slug=row["slug"], title=row["title"],
            kind=row["kind"], level=row["level"], theme_color=row["theme_color"],
            summary=row["summary"], body_markdown=row["body_markdown"],
            metadata=metadata,
            infobox=json.loads(row["infobox_json"]),
            blocks=[WikiBlock.model_validate(item) for item in json.loads(row["blocks_json"])],
            spoiler_policy=WikiSpoilerPolicy.model_validate_json(row["spoiler_json"] or "{}"),
            track_items=[
                NarrativeTrackRecord(
                    id=item["id"], entry_id=item["entry_id"], kind=item["kind"], category=item["category"],
                    title=item["title"], note=item["note"], start_locator=item["start_locator"],
                    end_locator=item["end_locator"], importance=item["importance"],
                    evidence=[EvidenceRef.model_validate(e) for e in json.loads(item["evidence_json"])],
                    source_type=item["source_type"],
                ) for item in tracks
            ],
            status=row["status"], revision=row["revision"], user_created=bool(row["user_created"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _entry_summary(row: Any) -> Any:
        from .models import WikiEntrySummary
        return WikiEntrySummary(
            id=row["id"], series_id=row["series_id"], slug=row["slug"], title=row["title"],
            kind=row["kind"], level=row["level"], theme_color=row["theme_color"],
            summary=row["summary"], metadata={
                key: value for key, value in json.loads(row["metadata_json"] or "{}").items()
                if key in {"aliases", "subcategory"}
            },
            spoiler_policy=WikiSpoilerPolicy.model_validate_json(row["spoiler_json"] or "{}"),
            status=row["status"], revision=row["revision"], user_created=bool(row["user_created"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _report_record(row: Any) -> VolumeReportRecord:
        return VolumeReportRecord(
            id=row["id"], series_id=row["series_id"], volume_id=row["volume_id"], volume_number=row["volume_number"],
            harness_variant=row["harness_variant"], model=row["model"], prompt_version=row["prompt_version"], status=row["status"],
            report=VolumeReportData.model_validate_json(row["report_json"]) if row["report_json"] else None,
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
            duration_ms=row["duration_ms"], error=row["error"], created_at=row["created_at"],
        )

    @staticmethod
    def _blueprint_record(row: Any) -> WikiBlueprintRecord:
        return WikiBlueprintRecord(
            id=row["id"], series_id=row["series_id"], version=row["version"], status=row["status"],
            model=row["model"], blueprint=WikiBlueprintData.model_validate_json(row["blueprint_json"]),
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"], created_at=row["created_at"],
        )


def spoiler_targets_for_content(
    body_markdown: str, infobox: dict[str, Any], blocks: list[WikiBlock],
) -> set[str]:
    targets = {"title", "summary"}
    targets.update(f"infobox:{key}" for key in infobox)
    for index, paragraph in enumerate(re.split(r"\n\s*\n", body_markdown.strip())):
        if paragraph.strip():
            targets.add(f"body:{index}")
    for block_index, block in enumerate(blocks):
        if block.title or block.data.get("title"):
            targets.add(f"block:{block_index}:title")
        markdown = str(block.data.get("markdown") or block.data.get("text") or "").strip()
        if block.type in {"prose", "markdown"} and markdown:
            for paragraph_index, paragraph in enumerate(re.split(r"\n\s*\n", markdown)):
                if paragraph.strip():
                    targets.add(f"block:{block_index}:body:{paragraph_index}")
        else:
            targets.add(f"block:{block_index}:data")
    return targets


def spoiler_targets_for_entry(entry: WikiEntryRecord) -> set[str]:
    return spoiler_targets_for_content(entry.body_markdown, entry.infobox, entry.blocks)


SPOILER_MARKER_RE = re.compile(r"⟦SP:([A-Za-z][A-Za-z0-9_-]{0,31})⟧([\s\S]*?)⟦/SP:\1⟧")


def spoiler_marker_ids(payload: WikiCompiledPayload) -> set[str]:
    values = [payload.body_markdown, json.dumps(payload.infobox, ensure_ascii=False)]
    values.extend(str(block.title or "") for block in payload.blocks)
    values.extend(json.dumps(block.data, ensure_ascii=False) for block in payload.blocks)
    return {match.group(1) for value in values for match in SPOILER_MARKER_RE.finditer(value)}


def validate_compiled_spoiler_policy(
    payload: WikiCompiledPayload, chapter_ids: set[str],
) -> str | None:
    policy = payload.spoiler_policy
    if "⟦SP:" in payload.title or "⟦SP:" in payload.summary:
        return "词条标题和简介必须改写为无剧透文本，不能使用局部剧透标记"
    marked_ids = spoiler_marker_ids(payload)
    raw_markers = "\n".join([
        payload.body_markdown, json.dumps(payload.infobox, ensure_ascii=False),
        *(str(block.title or "") + json.dumps(block.data, ensure_ascii=False) for block in payload.blocks),
    ])
    open_ids = set(re.findall(r"⟦SP:([A-Za-z][A-Za-z0-9_-]{0,31})⟧", raw_markers))
    close_ids = set(re.findall(r"⟦/SP:([A-Za-z][A-Za-z0-9_-]{0,31})⟧", raw_markers))
    if open_ids != close_ids or open_ids != marked_ids:
        return "剧透标记必须使用编号一致且完整闭合的 ⟦SP:s1⟧…⟦/SP:s1⟧"
    valid_targets = spoiler_targets_for_content(
        payload.body_markdown, payload.infobox, payload.blocks,
    )
    if policy.entry.mode == "progress" and policy.entry.unlock_after_chapter_id not in chapter_ids:
        return f"spoiler_policy.entry 使用了非法解锁章节：{policy.entry.unlock_after_chapter_id}"
    if policy.entry.mode != "progress":
        policy.entry.unlock_after_chapter_id = None
    seen: set[str] = set()
    for fragment in policy.fragments:
        fragment_key = fragment.id or fragment.target
        if not fragment_key:
            return "spoiler_policy.fragments 每项必须填写对应标记 id"
        if fragment.mode == "open":
            return f"spoiler_policy.fragments 不应提交 open 规则：{fragment_key}"
        if fragment.id:
            if fragment.id not in marked_ids:
                return f"spoiler_policy 使用了正文中不存在的标记 id：{fragment.id}"
            if fragment.id in seen:
                return f"spoiler_policy 标记 id 重复：{fragment.id}"
        else:
            # Stable structural targets remain readable for legacy entries.
            if fragment.target in {"title", "summary"}:
                return f"{fragment.target} 涉及剧透时必须提升为 entry 整词条规则"
            if fragment.target not in valid_targets:
                return f"spoiler_policy 使用了不存在的 target：{fragment.target}"
        if fragment.mode == "progress" and fragment.unlock_after_chapter_id not in chapter_ids:
            return f"spoiler_policy 使用了非法解锁章节：{fragment.unlock_after_chapter_id}"
        if fragment.mode != "progress":
            fragment.unlock_after_chapter_id = None
        seen.add(fragment_key)
    return None


def normalize_volume_report(payload: dict[str, Any], series_id: str, volume_id: str, number: int | float, title: str, chapter_ids: list[str]) -> dict[str, Any]:
    for wrapper in ("report", "volume_report", "data", "result"):
        if isinstance(payload.get(wrapper), dict):
            payload = payload[wrapper]
            break
    summaries = payload.get("chapter_summaries")
    if not isinstance(summaries, list):
        summaries = []
    allowed_chapters = set(chapter_ids)
    normalized_summaries = []
    for item in summaries:
        if not isinstance(item, dict):
            continue
        chapter_id = str(item.get("chapter_id") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if chapter_id and summary and (not allowed_chapters or chapter_id in allowed_chapters):
            normalized_summaries.append({
                "chapter_id": chapter_id,
                "title": str(item.get("title") or "").strip(),
                "summary": summary,
            })
    return {
        "series_id": series_id,
        "volume_id": volume_id,
        "volume_number": number,
        "title": title,
        "summary": str(payload.get("summary") or "").strip(),
        "chapter_summaries": normalized_summaries,
    }


def validate_volume_report_structure(payload: dict[str, Any], chapter_ids: list[str]) -> str | None:
    for wrapper in ("report", "volume_report", "data", "result"):
        if isinstance(payload.get(wrapper), dict):
            payload = payload[wrapper]
            break
    if not str(payload.get("summary") or "").strip():
        return "summary 为空。"
    summaries = payload.get("chapter_summaries")
    if not isinstance(summaries, list):
        return "chapter_summaries 必须是数组。"
    covered = {str(item.get("chapter_id")) for item in summaries if isinstance(item, dict) and str(item.get("summary") or "").strip()}
    missing = [chapter_id for chapter_id in chapter_ids if chapter_id not in covered]
    if missing:
        return f"章节覆盖不完整，缺少 {len(missing)} 章：{', '.join(missing[:5])}。"
    return None


def validate_compact_volume_submission(payload: dict[str, Any], chapter_ids: list[str]) -> str | None:
    unexpected = set(payload) - {"summary", "chapter_summaries", "reference_chapter_ids"}
    if unexpected:
        return f"submit_volume_summary 包含不允许的字段：{', '.join(sorted(unexpected))}"
    if not str(payload.get("summary") or "").strip():
        return "summary 不能为空"
    summaries = payload.get("chapter_summaries")
    if not isinstance(summaries, list):
        return "chapter_summaries 必须是数组"
    references = payload.get("reference_chapter_ids")
    if not isinstance(references, list):
        return "reference_chapter_ids 必须是数组"
    reference_ids = list(dict.fromkeys(str(item) for item in references))
    invalid = [chapter_id for chapter_id in reference_ids if chapter_id not in chapter_ids]
    if invalid:
        return f"reference_chapter_ids 包含当前卷不存在的章节：{', '.join(invalid[:5])}"
    covered: set[str] = set()
    for item in summaries:
        if not isinstance(item, dict):
            return "chapter_summaries 的每一项必须是对象"
        extra_fields = set(item) - {"chapter_id", "summary"}
        if extra_fields:
            return f"chapter_summaries 包含不允许的字段：{', '.join(sorted(extra_fields))}"
        chapter_id = str(item.get("chapter_id") or "")
        if chapter_id not in chapter_ids:
            return f"chapter_summaries 包含当前卷不存在的章节：{chapter_id}"
        if chapter_id in covered:
            return f"chapter_summaries 重复提交章节：{chapter_id}"
        if str(item.get("summary") or "").strip():
            covered.add(chapter_id)
    missing = [chapter_id for chapter_id in chapter_ids if chapter_id not in covered and chapter_id not in reference_ids]
    if missing:
        return f"chapter_summaries 缺少 {len(missing)} 个正文章节：{', '.join(missing[:5])}"
    return None


def analysis_volume_identity(series: Any, volume: Any) -> str:
    """Keep single-volume works work-shaped in every model-facing prompt."""
    return (
        f"work_title: {series.title}\n"
        if len(series.volumes) == 1 else f"volume_number: {volume.number}\n"
    )


def normalize_wiki_block(section: dict[str, Any]) -> WikiBlock:
    raw_type = str(section.get("type") or "markdown")
    mapping = {
        "featured": "callout", "gallery": "info_cards", "index": "module_buttons",
        "notice": "callout", "cards": "info_cards", "links": "module_buttons",
        "hero": "quote", "introduction": "prose", "world_overview": "prose",
        "character_cards": "info_cards", "exploration_entrance": "module_buttons",
    }
    block_type = mapping.get(raw_type, raw_type)
    allowed = {
        "prose", "module_buttons", "info_cards", "fact_table", "quote", "timeline",
        "narrative_track", "entity_list", "chapter_list", "event_list", "related_entries",
        "callout", "markdown",
        "volume_cards", "chapter_cards",
    }
    if block_type not in allowed:
        block_type = "markdown"
    data = dict(section.get("data") or {})
    if "content" in section:
        if block_type in {"prose", "markdown"}:
            data.setdefault("markdown", section["content"])
        else:
            data.setdefault("text", section["content"])
    if "items" in section:
        data.setdefault("items", section["items"])
    if "characters" in section:
        data.setdefault("items", section["characters"])
    if "cards" in section:
        data.setdefault("items", section["cards"])
    if "buttons" in section:
        data.setdefault("items", section["buttons"])
    if "volumes" in section:
        data.setdefault("items", section["volumes"])
    if "links" in section:
        data.setdefault("items", section["links"])
    if section.get("source_ref"):
        data.setdefault("locator", section["source_ref"])
    if section.get("attribution"):
        data.setdefault("attribution", section["attribution"])
    if section.get("source"):
        data.setdefault("source", section["source"])
    if section.get("locator"):
        data.setdefault("locator", section["locator"])
    if block_type == "info_cards" and isinstance(data.get("items"), list):
        data["items"] = [
            ({**item, "title": item.get("title") or item.get("image") or item.get("name"),
              "summary": item.get("summary") or item.get("caption") or item.get("description") or item.get("note") or item.get("reason", "")}
             if isinstance(item, dict) else item)
            for item in data["items"]
        ]
    if block_type == "module_buttons" and isinstance(data.get("items"), list):
        data["items"] = [
            ({**item, "title": item.get("title") or item.get("label") or item.get("name"),
              "description": item.get("description") or item.get("summary") or item.get("note") or item.get("reason", "")}
             if isinstance(item, dict) else item)
            for item in data["items"]
        ]
    return WikiBlock(type=block_type, title=section.get("title"), data=data)


def wiki_compilation_plans(blueprint: WikiBlueprintData) -> list[WikiEntryPlan]:
    """Expose ordinary entries and Architect specials through one compiler path."""
    plans = list(blueprint.entry_plans)
    plans.extend(WikiEntryPlan(
        slug=special.slug,
        title=special.title,
        kind="special",
        level="featured",
        reason="；".join(part for part in (special.introduction, special.angle) if part),
        source_refs=special.source_refs,
        template="special_feature",
        action=special.action,
        changed_sections=special.changed_sections,
        writer_tier="core",
        compile_mode="agentic",
        tool_budget=special.tool_budget,
        knowledge_point_ids=special.knowledge_point_ids,
    ) for special in blueprint.specials)
    return plans


def normalize_blueprint(
    payload: dict[str, Any], series_id: str, covered_ids: list[str], complete: bool,
    existing_slugs: set[str] | None = None,
) -> dict[str, Any]:
    existing_slugs = existing_slugs or set()
    payload["series_id"] = series_id
    payload["coverage_volume_ids"] = covered_ids
    payload["complete_work_coverage"] = complete
    payload.setdefault("work_profile", {})
    payload.setdefault("home_title", "作品首页")
    payload.setdefault("home_intro", "")
    payload.setdefault("home_body_markdown", payload.get("home_intro", ""))
    payload.setdefault("home_sections", [])
    payload.setdefault("categories", [])
    payload.setdefault("entry_plans", [])
    payload.setdefault("specials", [])
    payload.setdefault("templates", {})
    payload.setdefault("template_metadata", {})
    payload.setdefault("home_action", "update")
    if payload["home_action"] == "create":
        payload["home_action"] = "update"
    payload.setdefault("deprecated_entry_slugs", [])
    payload.setdefault("architecture_notes", [])
    payload.setdefault("infobox_schemas", {})
    payload.setdefault("navigation_boxes", [])
    used_slugs: set[str] = set()
    for index, item in enumerate(payload["categories"], start=1):
        item.setdefault("id", unique_slug(item.get("label") or f"category-{index}"))
        item.setdefault("parent_kind", "other")
        item.setdefault("description", "")
        item.setdefault("theme_color", theme_for_kind(item.get("parent_kind", "other")))
        item.setdefault("introduction", item.get("description", ""))
        item.setdefault("body_markdown", item.get("introduction") or item.get("description", ""))
        item.setdefault("blocks", [])
        item["blocks"] = [
            normalize_wiki_block(block).model_dump()
            for block in item["blocks"] if isinstance(block, dict) and block.get("type")
        ]
        item.setdefault("subcategories", [])
    normalized_plans = []
    for item in payload["entry_plans"]:
        title = item.get("title") or item.get("name")
        if not title:
            continue
        slug = item.get("slug") or unique_slug(title)
        base = slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base}-{suffix}"; suffix += 1
        used_slugs.add(slug)
        item.update({"slug": slug, "title": title})
        item.setdefault("aliases", [])
        item.setdefault("kind", "other")
        if item.get("level") not in {"stub","standard","featured"}:
            item["level"] = "standard"
        item.setdefault("reason", "")
        item.setdefault("source_refs", [])
        if not item.get("template"):
            # Legacy blueprints keyed templates directly by kind. Keep those
            # readable while all new Architect output uses named template IDs.
            item["template"] = item["kind"] if item["kind"] in payload["templates"] else "standard_entry"
        item.setdefault("needs_narrative_track", False)
        if item.get("action") not in {"create", "keep", "update", "rebuild"}:
            item["action"] = "keep" if slug in existing_slugs else "create"
        elif item["action"] == "keep" and slug not in existing_slugs:
            item["action"] = "create"
        item.setdefault("changed_sections", [])
        item.setdefault("subcategory", "")
        tier = item.get("writer_tier")
        if tier not in {"core", "supporting", "local"}:
            tier = {"featured": "core", "standard": "supporting", "stub": "local"}[item["level"]]
        item["writer_tier"] = tier
        if tier == "core":
            item.setdefault("preferred_model", "")
            item.setdefault("compile_mode", "agentic")
            item.setdefault("tool_budget", 4)
        elif tier == "supporting":
            item.setdefault("preferred_model", "")
            item.setdefault("compile_mode", "agentic")
            item.setdefault("tool_budget", 2)
        else:
            item.setdefault("preferred_model", "")
            item.setdefault("compile_mode", "context_only")
            item.setdefault("tool_budget", 0)
        # The schema lives once in the shared template library. Keeping this
        # empty prevents every entry plan from duplicating it; Composer resolves
        # template -> schema when compiling the entry.
        item["infobox_schema"] = []
        item.setdefault("knowledge_point_ids", [])
        normalized_plans.append(item)
    payload["entry_plans"] = normalized_plans
    normalized_specials = []
    for item in payload["specials"]:
        title = item.get("title")
        if not title:
            continue
        slug = item.get("slug") or unique_slug(title)
        base = slug
        suffix = 2
        while slug in used_slugs:
            slug = f"{base}-{suffix}"; suffix += 1
        used_slugs.add(slug)
        item["slug"] = slug
        item.setdefault("introduction", "")
        item.setdefault("angle", "")
        item.setdefault("section_brief", [])
        item.setdefault("related_entry_slugs", [])
        item.setdefault("source_refs", [])
        item.setdefault("knowledge_point_ids", [])
        if item.get("action") not in {"create", "keep", "update", "rebuild"}:
            item["action"] = "keep" if slug in existing_slugs else "create"
        elif item["action"] == "keep" and slug not in existing_slugs:
            item["action"] = "create"
        item.setdefault("changed_sections", [])
        item.setdefault("tool_budget", 4)
        item.setdefault("recommended_on_home", True)
        normalized_specials.append(item)
    payload["specials"] = normalized_specials
    valid_related_slugs = {str(plan["slug"]) for plan in normalized_plans}
    for item in normalized_specials:
        item["related_entry_slugs"] = [
            str(slug) for slug in item["related_entry_slugs"] if str(slug) in valid_related_slugs
        ]
    valid_page_slugs = valid_related_slugs | {str(item["slug"]) for item in normalized_specials}
    valid_category_slugs = {f"category:{item['id']}" for item in payload["categories"]}

    categories_by_kind: dict[str, list[dict[str, Any]]] = {}
    category_targets: dict[str, dict[str, str]] = {}
    for category in payload["categories"]:
        categories_by_kind.setdefault(str(category.get("parent_kind") or "").casefold(), []).append(category)
        category_targets[f"category:{category['id']}"] = {
            "title": str(category.get("label") or category["id"]),
            "description": str(category.get("description") or category.get("introduction") or ""),
        }
    for plan in normalized_plans:
        candidate_categories = categories_by_kind.get(str(plan.get("kind") or "").casefold(), [])
        subcategories = [
            item for category in candidate_categories
            for item in category.get("subcategories") or [] if isinstance(item, dict)
        ]
        raw_subcategory = str(plan.get("subcategory") or "").strip()
        matched_subcategory = next((
            str(item.get("id") or "") for item in subcategories
            if raw_subcategory and raw_subcategory in {str(item.get("id") or ""), str(item.get("label") or "")}
        ), "")
        if matched_subcategory:
            plan["subcategory"] = matched_subcategory
        elif not raw_subcategory and len(subcategories) == 1:
            plan["subcategory"] = str(subcategories[0].get("id") or "")

    page_targets = {
        str(item["slug"]): {
            "title": str(item.get("title") or item["slug"]),
            "description": str(item.get("reason") or ""),
        }
        for item in normalized_plans
    }
    page_targets.update({
        str(item["slug"]): {
            "title": str(item.get("title") or item["slug"]),
            "description": str(item.get("introduction") or item.get("angle") or ""),
        }
        for item in normalized_specials
    })
    page_targets.update(category_targets)

    def hydrate_page_blocks(blocks: list[Any]) -> list[Any]:
        hydrated: list[Any] = []
        for raw_block in blocks:
            if not isinstance(raw_block, dict):
                continue
            block = normalize_wiki_block(raw_block).model_dump()
            data = block.get("data") if isinstance(block.get("data"), dict) else {}
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if block.get("type") in {"module_buttons", "info_cards"}:
                normalized_items = []
                for raw_item in items:
                    item = dict(raw_item) if isinstance(raw_item, dict) else {"title": str(raw_item)}
                    target = page_targets.get(str(item.get("slug") or ""), {})
                    item["title"] = str(item.get("title") or item.get("label") or item.get("name") or target.get("title") or "").strip()
                    description = str(item.get("description") or item.get("summary") or item.get("note") or item.get("reason") or target.get("description") or "").strip()
                    if block.get("type") == "info_cards":
                        item["summary"] = description
                    else:
                        item["description"] = description
                    normalized_items.append(item)
                data["items"] = normalized_items
            elif block.get("type") == "volume_cards":
                for item in items:
                    if not isinstance(item, dict) or item.get("volume_number") is not None:
                        continue
                    match = re.search(r"-v(\d+(?:\.\d+)?)$", str(item.get("volume_id") or ""), re.IGNORECASE)
                    if match:
                        item["volume_number"] = float(match.group(1))
            block["data"] = data
            hydrated.append(block)
        return hydrated

    payload["home_sections"] = hydrate_page_blocks(payload["home_sections"])
    for category in payload["categories"]:
        category["blocks"] = hydrate_page_blocks(category["blocks"])

    normalized_navigation_boxes: list[dict[str, Any]] = []
    used_navigation_ids: set[str] = set()
    valid_navigation_targets = valid_page_slugs | valid_category_slugs | {"home"}
    for index, box in enumerate(payload["navigation_boxes"]):
        if not isinstance(box, dict):
            continue
        title = str(box.get("title") or "").strip()
        box_id = str(box.get("id") or unique_slug(title or f"navigation-{index + 1}")).strip()
        if not title or not box_id or box_id in used_navigation_ids:
            continue
        groups: list[dict[str, Any]] = []
        for group in box.get("groups") or []:
            if not isinstance(group, dict):
                continue
            label = str(group.get("label") or "").strip()
            member_slugs = list(dict.fromkeys(
                str(slug) for slug in group.get("entry_slugs") or []
                if str(slug) in valid_page_slugs
            ))
            if not label or not member_slugs:
                continue
            normalized_group: dict[str, Any] = {"label": label, "entry_slugs": member_slugs}
            group_slug = str(group.get("slug") or "").strip()
            if group_slug in valid_navigation_targets:
                normalized_group["slug"] = group_slug
            groups.append(normalized_group)
        if not groups:
            continue
        normalized_box: dict[str, Any] = {"id": box_id, "title": title, "groups": groups}
        title_slug = str(box.get("title_slug") or "").strip()
        if title_slug in valid_navigation_targets:
            normalized_box["title_slug"] = title_slug
        normalized_navigation_boxes.append(normalized_box)
        used_navigation_ids.add(box_id)
    payload["navigation_boxes"] = normalized_navigation_boxes[:6]

    def clean_page_links(value: Any) -> Any:
        if isinstance(value, list):
            return [clean_page_links(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: clean_page_links(item) for key, item in value.items()}
        for key in ("entry_slugs", "related_entry_slugs"):
            if isinstance(result.get(key), list):
                result[key] = [slug for slug in result[key] if slug in valid_page_slugs]
        if isinstance(result.get("slug"), str):
            slug = result["slug"]
            if slug not in valid_page_slugs and slug not in valid_category_slugs:
                result.pop("slug")
        return result

    payload["home_sections"] = clean_page_links(payload["home_sections"])
    for category in payload["categories"]:
        category["blocks"] = clean_page_links(category["blocks"])
    return payload


def bind_blueprint_knowledge_points(
    payload: dict[str, Any], points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate Architect bindings and supplement them by name/alias overlap.

    The blueprint stores stable point IDs; full point histories stay canonical
    in the point tables and are expanded when a Composer starts.
    """
    by_id = {str(point.get("id")): point for point in points if point.get("id")}

    def keys(values: list[Any]) -> set[str]:
        result: set[str] = set()
        for value in values:
            key = re.sub(r"[\s·・—_\-:：,，。;；/\\()（）\[\]【】]+", "", str(value or "")).casefold()
            if len(key) >= 2 and (not key.isascii() or len(key) >= 3):
                result.add(key)
        return result

    point_keys = {
        point_id: keys([point.get("name"), *(point.get("aliases") or [])])
        for point_id, point in by_id.items()
    }
    for plan in [*(payload.get("entry_plans") or []), *(payload.get("specials") or [])]:
        if not isinstance(plan, dict):
            continue
        bound = [
            str(point_id) for point_id in plan.get("knowledge_point_ids") or []
            if str(point_id) in by_id
        ]
        entry_keys = keys([plan.get("title"), *(plan.get("aliases") or [])])
        for point_id, candidates in point_keys.items():
            if any(
                entry_key in point_key or point_key in entry_key
                for entry_key in entry_keys for point_key in candidates
            ):
                bound.append(point_id)
        plan["knowledge_point_ids"] = list(dict.fromkeys(bound))
    return payload


def architect_report_context(reports: list[VolumeReportData]) -> list[dict[str, Any]]:
    """Expose only canonical volume and chapter summaries to Architect."""
    return [{
        "series_id": report.series_id,
        "volume_id": report.volume_id,
        "volume_number": report.volume_number,
        "title": report.title,
        "summary": report.summary,
        "chapter_summaries": [chapter.model_dump() for chapter in report.chapter_summaries],
    } for report in reports]


def serialize_point_context(points: list[dict[str, Any]]) -> str:
    """Serialize complete point histories without repeated JSON field names."""
    lines: list[str] = []
    for point in points:
        aliases = [str(alias) for alias in point.get("aliases") or [] if str(alias).strip()]
        header = f"@{point.get('id')} | {point.get('type') or '未分类'} | {point.get('name') or '未命名'}"
        if aliases:
            header += f" | 别名：{'、'.join(aliases)}"
        lines.append(header)
        for update in point.get("updates") or []:
            evidence = update.get("evidence") or []
            locators = list(dict.fromkeys(
                str(item.get("locator")) for item in evidence
                if isinstance(item, dict) and item.get("locator")
            ))
            notes = [
                str(item.get("note")) for item in evidence
                if isinstance(item, dict) and item.get("note")
            ]
            suffix = f" [证据：{'、'.join(locators)}]" if locators else ""
            if notes:
                suffix += f" [注：{'；'.join(notes)}]"
            lines.append(
                f"  V{update.get('volume_number')}: {normalize_point_update(update.get('content'))}{suffix}"
            )
    return "\n".join(lines)


def serialize_point_index(points: list[dict[str, Any]]) -> str:
    """Compact name/type/alias-only point index shared by Volume and Composer."""
    lines = []
    for point in points:
        line = f"@{point.get('id')} | {point.get('type') or '未分类'} | {point.get('name') or '未命名'}"
        aliases = [str(value) for value in point.get("aliases") or [] if str(value).strip()]
        if aliases:
            line += " | " + "、".join(aliases)
        lines.append(line)
    return "\n".join(lines)


def serialize_current_point_updates(points: list[dict[str, Any]]) -> str:
    """Compact the canonical point/update rows without inventing fixed categories."""
    lines = []
    for point in points:
        line = f"{point.get('type') or '未分类'} | {point.get('name') or '未命名'}"
        aliases = [str(value) for value in point.get("aliases") or [] if str(value).strip()]
        if aliases:
            line += " | " + "、".join(aliases)
        lines.append(line)
        content = normalize_point_update(point.get("content"))
        if content:
            lines.append("  " + content)
        locators = [
            str(item.get("locator")) for item in point.get("evidence") or []
            if isinstance(item, dict) and item.get("locator")
        ]
        if locators:
            lines.append("  证据：" + "、".join(dict.fromkeys(locators)))
    return "\n".join(lines) or "（无）"


def normalize_evidence(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value or []:
        if isinstance(item, str) and item.startswith("lr://"):
            result.append({"locator": item, "note": ""})
        elif isinstance(item, dict) and item.get("locator"):
            result.append({"locator": item["locator"], "note": item.get("note", ""), "marker": item.get("marker", "")})
    return result


def normalize_point_update(value: Any) -> str:
    """Store point updates as one readable line without model-specific markup."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_duplicate_entry_title(markdown: str, title: str) -> str:
    """Remove a leading title line when a composer repeats the page heading."""
    body = str(markdown or "").lstrip()
    if not body or not title.strip():
        return body
    lines = body.splitlines()
    first = lines[0].strip()

    def normalized(value: str) -> str:
        value = re.sub(r"^#{1,6}\s*", "", value.strip())
        value = re.sub(r"^(?:\*\*|__)(.*)(?:\*\*|__)$", r"\1", value)
        return re.sub(r"[\s`*_#《》「」『』【】\-—:：]", "", value).casefold()

    if normalized(first) != normalized(title):
        return body
    remainder = "\n".join(lines[1:]).lstrip("\n")
    return remainder


def normalize_preview_cards(value: Any) -> list[str | dict[str, Any]]:
    result: list[str | dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict) and (item.get("title") or item.get("name")):
            result.append({
                "title": str(item.get("title") or item.get("name")),
                "slug": str(item.get("slug") or ""),
                "summary": str(item.get("summary") or item.get("description") or ""),
                "importance": "primary" if item.get("importance") == "primary" else "secondary",
            })
    return result


def normalize_chapter_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "intro": str(payload.get("intro") or "").strip()[:240],
        "overview": str(payload.get("overview") or "").strip()[:240],
        "people": normalize_preview_cards(payload.get("people"))[:4],
        "places": normalize_preview_cards(payload.get("places"))[:3],
        "concepts": normalize_preview_cards(payload.get("concepts"))[:3],
        "events": normalize_preview_cards(payload.get("events"))[:3],
    }


def volume_report_tool_properties() -> dict[str, Any]:
    return {key: {"type": type_name} for key, type_name in {
        "summary": "string", "chapter_summaries": "array",
    }.items()}


def blueprint_tool_properties() -> dict[str, Any]:
    return {key: {"type": type_name} for key, type_name in {
        "work_profile": "object", "home_title": "string", "home_intro": "string", "home_sections": "array",
        "categories": "array", "entry_plans": "array", "specials": "array", "templates": "object",
        "template_metadata": "object", "infobox_schemas": "object", "navigation_boxes": "array",
    }.items()}


def wiki_entry_tool_properties() -> dict[str, Any]:
    return {key: {"type": type_name} for key, type_name in {
        "title": "string", "summary": "string", "body_markdown": "string",
        "metadata": "object", "infobox": "object", "blocks": "array", "track_items": "array",
        "spoiler_policy": "object",
    }.items()}


def unique_slug(value: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if ascii_slug:
        return ascii_slug
    return "entry-" + uuid.uuid5(uuid.NAMESPACE_URL, value).hex[:10]


def theme_for_kind(kind: str) -> str:
    known = {
        "character": "#6d806e", "place": "#667f91", "nation": "#667f91", "group": "#88765f",
        "event": "#936d5a", "clue": "#8a6d76", "object": "#7d768e", "concept": "#756e92",
        "special": "#8b705c", "人物": "#6d806e", "地点": "#667f91", "国家": "#667f91",
        "组织": "#88765f", "事件": "#936d5a", "线索": "#8a6d76", "物品": "#7d768e",
        "概念": "#756e92",
    }
    if kind in known:
        return known[kind]
    palette = ["#667c68", "#6b7890", "#85705f", "#7c6881", "#5f7d7a", "#8a6a66"]
    return palette[int(hashlib.sha256(kind.encode("utf-8")).hexdigest()[:8], 16) % len(palette)]


def theme_for_entry(kind: str, slug: str) -> str:
    base = theme_for_kind(kind)
    variants = {
        "#6d806e": ["#647c69", "#71856f", "#5f786d"],
        "#667f91": ["#637d91", "#6f8392", "#5e7888"],
        "#88765f": ["#846f5c", "#907a62", "#7d715f"],
        "#936d5a": ["#916a58", "#9a725e", "#876959"],
        "#8a6d76": ["#876a76", "#90717b", "#806b78"],
        "#7d768e": ["#7a728d", "#837b94", "#746f87"],
        "#756e92": ["#726b90", "#7d7597", "#6e6b8b"],
    }.get(base, [base, "#6b7890", "#85705f"])
    digest = int(hashlib.sha256(f"{kind}:{slug}".encode("utf-8")).hexdigest()[:8], 16)
    return variants[digest % len(variants)]
