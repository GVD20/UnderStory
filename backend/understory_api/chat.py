from __future__ import annotations

import json
import random
import re
import time
import uuid
from difflib import SequenceMatcher
from typing import Any, Callable

from .agentic import AgentPause, function_tool, run_tool_agent
from .deepseek import MiniMaxClient
from .epub_importer import format_volume_number
from .knowledge import KnowledgeService, normalize_evidence
from .knowledge_prompts import CHAT_AGENT_SYSTEM
from .library import LibraryRepository
from .models import (
    ChatCreateRequest,
    ChatMessageRecord,
    ChatSendRequest,
    ChatSpoilerDecisionRequest,
    ChatSpoilerConfirmation,
    ChatThreadDetail,
    ChatThreadRecord,
    EvidenceRef,
    ReadingSettingsSnapshot,
)
from .settings import SettingsRepository


class ChatService:
    def __init__(
        self,
        library: LibraryRepository,
        knowledge: KnowledgeService,
        provider: Any | None = None,
    ) -> None:
        self.library = library
        self.knowledge = knowledge
        settings = SettingsRepository(library.workspace, library.data_dir).load()
        self.provider = provider or (MiniMaxClient(settings.api_key, timeout_seconds=240, base_url=settings.base_url, model=settings.advanced_model, reasoning_split=settings.reasoning_split) if settings.api_key else None)
        self.model_id = getattr(self.provider, "model", settings.advanced_model)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.library.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_threads (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    cards_json TEXT NOT NULL DEFAULT '[]',
                    actions_json TEXT NOT NULL DEFAULT '[]',
                    thinking_json TEXT NOT NULL DEFAULT '[]',
                    thinking_seconds REAL NOT NULL DEFAULT 0,
                    full_chapter_calls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS chat_messages_thread_idx
                    ON chat_messages(thread_id, created_at);
                CREATE TABLE IF NOT EXISTS chat_spoiler_confirmations (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE REFERENCES chat_threads(id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    current_chapter_id TEXT,
                    current_paragraph_id TEXT,
                    thinking_json TEXT NOT NULL DEFAULT '[]',
                    thinking_seconds REAL NOT NULL DEFAULT 0,
                    full_chapter_calls INTEGER NOT NULL DEFAULT 0,
                    full_chapter_characters INTEGER NOT NULL DEFAULT 0,
                    reading_settings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_messages)")}
            if "thinking_json" not in columns:
                db.execute("ALTER TABLE chat_messages ADD COLUMN thinking_json TEXT NOT NULL DEFAULT '[]'")
            if "thinking_seconds" not in columns:
                db.execute("ALTER TABLE chat_messages ADD COLUMN thinking_seconds REAL NOT NULL DEFAULT 0")
            confirmation_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_spoiler_confirmations)")}
            if "thinking_seconds" not in confirmation_columns:
                db.execute("ALTER TABLE chat_spoiler_confirmations ADD COLUMN thinking_seconds REAL NOT NULL DEFAULT 0")
            if "full_chapter_calls" not in confirmation_columns:
                db.execute("ALTER TABLE chat_spoiler_confirmations ADD COLUMN full_chapter_calls INTEGER NOT NULL DEFAULT 0")
            if "full_chapter_characters" not in confirmation_columns:
                db.execute("ALTER TABLE chat_spoiler_confirmations ADD COLUMN full_chapter_characters INTEGER NOT NULL DEFAULT 0")
            if "reading_settings_json" not in confirmation_columns:
                db.execute("ALTER TABLE chat_spoiler_confirmations ADD COLUMN reading_settings_json TEXT NOT NULL DEFAULT '{}'")

    def create_thread(self, request: ChatCreateRequest) -> ChatThreadDetail:
        if self.library.get_series(request.series_id) is None:
            raise LookupError("Series not found")
        thread_id = uuid.uuid4().hex
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO chat_threads(id,series_id,title) VALUES(?,?,?)",
                (thread_id, request.series_id, request.title),
            )
        return self.get_thread(thread_id)

    def list_threads(self, series_id: str) -> list[ChatThreadRecord]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT t.* FROM chat_threads t WHERE t.series_id=? AND EXISTS ("
                "SELECT 1 FROM chat_messages m WHERE m.thread_id=t.id AND m.role='user'"
                ") ORDER BY t.updated_at DESC", (series_id,)
            ).fetchall()
        return [ChatThreadRecord(**dict(row)) for row in rows]

    def get_thread(self, thread_id: str) -> ChatThreadDetail:
        with self.library.connect() as db:
            thread = db.execute("SELECT * FROM chat_threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                raise LookupError("Thread not found")
            rows = db.execute(
                "SELECT * FROM chat_messages WHERE thread_id=? ORDER BY created_at", (thread_id,)
            ).fetchall()
        return ChatThreadDetail(
            **dict(thread),
            messages=[self._message(row) for row in rows],
            pending_spoiler_confirmation=self._pending_confirmation(thread_id),
        )

    def delete_thread(self, thread_id: str) -> bool:
        with self.library.connect() as db:
            cursor = db.execute("DELETE FROM chat_threads WHERE id=?", (thread_id,))
        return cursor.rowcount > 0

    def send(
        self, thread_id: str, request: ChatSendRequest,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        *, persist_user: bool = True, allow_spoiler_confirmation: bool = True,
        spoiler_decision: str | None = None, prior_thinking: list[dict[str, Any]] | None = None,
        prior_thinking_seconds: float = 0,
        prior_full_chapter_calls: int = 0,
        prior_full_chapter_characters: int = 0,
    ) -> ChatThreadDetail:
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        started = time.perf_counter()
        thinking: list[dict[str, Any]] = list(prior_thinking or [])
        thread = self.get_thread(thread_id)
        if persist_user and thread.pending_spoiler_confirmation:
            raise ValueError("请先处理当前回答的剧透确认")
        series = self.library.get_series(thread.series_id)
        if series is None:
            raise LookupError("Series not found")
        first_exchange = thread.title == "新对话" and not any(message.role == "assistant" for message in thread.messages)
        if persist_user:
            user_id = uuid.uuid4().hex
            with self.library.connect() as db:
                db.execute(
                    "INSERT INTO chat_messages(id,thread_id,role,content) VALUES(?,?,'user',?)",
                    (user_id, thread_id, request.content),
                )

        wiki_state = self.knowledge.wiki_state(thread.series_id)
        wiki_blueprint = self.knowledge.latest_blueprint(thread.series_id)
        current_context = self._current_context(request.current_chapter_id, request.current_paragraph_id)
        history = [
            {"role": message.role, "content": message.content}
            for message in thread.messages[-18:]
            if message.role in {"user", "assistant"}
        ]
        overall = {
            "title": series.title,
            "volume_count": len(series.volumes),
            "character_count": series.total_characters,
            "volume_catalog": [
                {
                    "volume_id": volume.id, "volume_number": volume.number,
                    "title": volume.title, "chapter_count": len(volume.chapters),
                    "character_count": volume.character_count,
                }
                for volume in series.volumes
            ],
            "home_intro": wiki_blueprint.blueprint.home_intro if wiki_blueprint else "",
            "categories": [category.label for category in wiki_state.categories],
            "available_entries": [{"slug": entry.slug, "title": entry.title, "kind": entry.kind} for entry in wiki_state.entries[:120]],
        }
        spoiler_instruction = {
            "full": "读者已明确选择忽略剧透警告。第一轮最初判断应明确说明“我会忽略剧透警告，提供完整答案”，再按需核验并完整回答；不要再次请求剧透确认。",
            "minimize": "读者要求继续回答但尽量减少剧透。第一轮最初判断应明确说明“我会重新构思一份尽量不剧透的答案”；保留有用结论，省略关键身份、结局、死亡、背叛、真相与重大转折；不要再次请求剧透确认。",
        }.get(spoiler_decision, "")
        user_prompt = (
            f"作品整体上下文：{json.dumps(overall, ensure_ascii=False)}\n"
            f"读者当前位置：{json.dumps(current_context, ensure_ascii=False)}\n"
            f"读者附加的选中原文：{request.selected_text or '未附加'}\n"
            f"之前对话：{json.dumps(history, ensure_ascii=False)}\n"
            f"读者新问题：{request.content}\n"
            f"剧透回答策略：{spoiler_instruction or '尚未获得额外授权；严重剧透时必须先请求确认。'}\n"
            "请按需调用工具，并调用 submit_answer。"
        )
        entry_by_slug = {entry.slug: entry for entry in wiki_state.entries if entry.level != "home"}
        retrieved_cards: dict[str, dict[str, Any]] = {}
        report_by_number: dict[float, Any] = {}
        for record in (self.knowledge.list_volume_reports(thread.series_id) if hasattr(self.knowledge, "list_volume_reports") else []):
            number = float(record.volume_number)
            if record.status == "completed" and record.report and number not in report_by_number:
                report_by_number[number] = record.report
        chapter_by_number = {
            self._chapter_number(chapter.volume, chapter.index): chapter
            for volume in series.volumes for chapter in volume.chapters
        }
        chapter_report_by_id = {
            chapter.chapter_id: chapter
            for report in report_by_number.values() for chapter in report.chapter_summaries
        }
        full_chapter_calls = max(0, int(prior_full_chapter_calls))
        full_chapter_characters = max(0, int(prior_full_chapter_characters))
        original_reading_settings = request.reading_settings.model_dump() if request.reading_settings else None
        pending_reading_settings = dict(original_reading_settings) if original_reading_settings else None
        semantic_retrieval = bool(request.reading_settings and request.reading_settings.semantic_retrieval)
        retrieval_activity = {"derived": 0, "original": 0}

        def describe_reading_settings_change(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
            labels = {
                "theme": ("主题", {"paper": "明亮", "dark": "深色", "system": "跟随系统"}),
                "font_size": ("字号", {}),
                "line_height": ("行距", {}),
                "epub_features": ("EPUB 特性", {"off": "关", "basic": "基础", "advanced": "高级"}),
                "paper_color": ("纸张颜色", {"standard": "标准", "warm": "纸色"}),
                "illustrations": ("插图", {"off": "关", "original": "原图", "immersive": "沉浸"}),
                "spoiler_shield": ("剧透屏蔽", {"off": "关", "auto": "自动", "strict": "严格"}),
                "semantic_retrieval": ("语义化检索", {False: "关", True: "开"}),
            }
            changes = []
            for key, (label, choices) in labels.items():
                if before.get(key) == after.get(key):
                    continue
                format_value = lambda value: choices.get(value, f"{value}px" if key == "font_size" else f"{float(value):.2f}" if key == "line_height" else str(value))
                changes.append({
                    "key": key, "label": label,
                    "before": before.get(key), "after": after.get(key),
                    "before_label": format_value(before.get(key)),
                    "after_label": format_value(after.get(key)),
                })
            return changes

        def get_reading_settings(_args: dict[str, Any]) -> dict[str, Any]:
            if pending_reading_settings is None:
                return {"available": False, "error": "当前客户端没有提供阅读设置快照，无法读取或修改。"}
            return {"available": True, "settings": pending_reading_settings}

        def update_reading_settings(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal pending_reading_settings
            if original_reading_settings is None or pending_reading_settings is None:
                return {"accepted": False, "error": "当前客户端没有提供阅读设置快照，不能提交修改。"}
            patch = args.get("settings")
            if not isinstance(patch, dict) or not patch:
                return {"accepted": False, "error": "settings 必须包含至少一个需要修改的设置字段。"}
            allowed = set(ReadingSettingsSnapshot.model_fields)
            unknown = sorted(set(patch) - allowed)
            if unknown:
                return {"accepted": False, "error": f"不支持的设置字段：{', '.join(unknown)}"}
            try:
                pending_reading_settings = ReadingSettingsSnapshot.model_validate({
                    **pending_reading_settings, **patch,
                }).model_dump()
            except Exception as error:
                return {"accepted": False, "error": f"设置值无效：{str(error)[:240]}"}
            changes = describe_reading_settings_change(original_reading_settings, pending_reading_settings)
            return {
                "accepted": True, "deferred_until_answer_complete": True,
                "settings": pending_reading_settings, "changes": changes,
            }

        def remember_card(raw: Any) -> None:
            slug = str(getattr(raw, "slug", "") or (raw.get("slug") if isinstance(raw, dict) else "")).strip()
            entry = entry_by_slug.get(slug)
            if not entry or slug in retrieved_cards:
                return
            retrieved_cards[slug] = {
                "slug": entry.slug, "title": entry.title, "kind": entry.kind,
                "summary": entry.summary, "theme_color": entry.theme_color,
            }

        def search_wiki(args: dict[str, Any]) -> dict[str, Any]:
            retrieval_activity["derived"] += 1
            results = self.knowledge.search_wiki(
                thread.series_id, args.get("query", ""), int(args.get("limit", 12)),
            )
            for item in results[:4]:
                remember_card(item)
            return {"results": results}

        def get_wiki_entry(args: dict[str, Any]) -> dict[str, Any]:
            retrieval_activity["derived"] += 1
            payload = self._entry_payload(thread.series_id, args.get("slug", ""))
            remember_card(payload)
            return payload

        def get_content_summaries(args: dict[str, Any]) -> dict[str, Any]:
            retrieval_activity["derived"] += 1
            results = []
            for request_index, request_item in enumerate((args.get("requests") or [])[:48]):
                if not isinstance(request_item, dict):
                    results.append({"request_index": request_index, "status": "error", "error": "请求项必须是对象"})
                    continue
                try:
                    volume_number = float(request_item.get("volume_number"))
                except (TypeError, ValueError):
                    results.append({"request_index": request_index, "status": "error", "error": "卷号无效"})
                    continue
                chapter_numbers = list(dict.fromkeys(
                    self._normalize_chapter_number(str(item))
                    for item in (request_item.get("chapter_numbers") or [])
                ))[:40]
                report = report_by_number.get(volume_number)
                if not chapter_numbers:
                    if not report:
                        results.append({
                            "request_index": request_index, "type": "volume", "volume_number": volume_number,
                            "status": "error", "error": "卷总结不存在或尚未完成",
                        })
                        continue
                    results.append({
                        "request_index": request_index, "type": "volume", "status": "ok",
                        "volume_number": report.volume_number, "title": report.title, "summary": report.summary,
                    })
                    continue
                for chapter_number in chapter_numbers:
                    chapter = chapter_by_number.get(chapter_number)
                    chapter_report = chapter_report_by_id.get(chapter.id) if chapter else None
                    if not chapter or float(chapter.volume) != volume_number:
                        results.append({
                            "request_index": request_index, "type": "chapter", "chapter_number": chapter_number,
                            "volume_number": volume_number, "status": "error", "error": "章节不存在或不属于指定卷",
                        })
                    elif not chapter_report:
                        results.append({
                            "request_index": request_index, "type": "chapter", "chapter_number": chapter_number,
                            "volume_number": volume_number, "status": "error", "error": "章总结不存在或尚未完成",
                        })
                    else:
                        results.append({
                            "request_index": request_index, "type": "chapter", "status": "ok",
                            "chapter_number": chapter_number, "volume_number": chapter.volume,
                            "chapter_index": chapter.index, "title": chapter.title,
                            "summary": chapter_report.summary,
                        })
            return {
                "results": results,
                "returned": sum(item.get("status") == "ok" for item in results),
                "errors": sum(item.get("status") == "error" for item in results),
            }

        def search_knowledge_points(args: dict[str, Any]) -> dict[str, Any]:
            retrieval_activity["derived"] += 1
            return self.knowledge.search_knowledge_points(
                thread.series_id,
                [str(item) for item in (args.get("queries") or [])],
                int(args.get("limit_per_query") or 6),
            )

        def search_original_text(args: dict[str, Any]) -> dict[str, Any]:
            retrieval_activity["original"] += 1
            try:
                requested_limit = int(args.get("limit") or 12)
            except (TypeError, ValueError):
                requested_limit = 12
            return self.knowledge.search_original_tool(
                thread.series_id,
                args.get("query", ""),
                max(4, min(requested_limit, 24)),
                mode=str(args.get("mode") or "hybrid") if semantic_retrieval else "lexical",
            )

        def get_chapter_lists(args: dict[str, Any]) -> dict[str, Any]:
            requested = list(dict.fromkeys(
                float(item) for item in (args.get("volume_numbers") or [])
                if str(item).replace(".", "", 1).isdigit()
            ))[:24]
            volume_by_number = {float(volume.number): volume for volume in series.volumes}
            results = []
            for number in requested:
                volume = volume_by_number.get(number)
                if not volume:
                    results.append({"volume_number": number, "status": "error", "error": "卷不存在"})
                    continue
                results.append({
                    "volume_id": volume.id, "volume_number": volume.number, "title": volume.title,
                    "status": "ok", "character_count": volume.character_count,
                    "chapters": [{
                        "chapter_number": self._chapter_number(chapter.volume, chapter.index),
                        "chapter_id": chapter.id, "chapter_index": chapter.index, "title": chapter.title,
                        "character_count": chapter.character_count, "paragraph_count": chapter.paragraph_count,
                        "is_body": chapter.is_body,
                    } for chapter in volume.chapters],
                })
            return {
                "volumes": results,
                "returned": sum(item.get("status") == "ok" for item in results),
                "errors": sum(item.get("status") == "error" for item in results),
            }

        def search_chapter_titles(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query") or "").strip()
            normalized_query = self._normalize_title(query)
            limit = max(1, min(30, int(args.get("limit") or 20)))
            matches = []
            for number, chapter in chapter_by_number.items():
                normalized_title = self._normalize_title(chapter.title)
                contains = bool(normalized_query and normalized_query in normalized_title)
                score = 1.0 if normalized_query == normalized_title else .92 if contains else SequenceMatcher(None, normalized_query, normalized_title).ratio()
                if normalized_query and not contains and score < .28:
                    continue
                matches.append({
                    "chapter_number": number, "title": chapter.title,
                    "volume_number": chapter.volume, "chapter_index": chapter.index,
                    "chapter_id": chapter.id, "is_body": chapter.is_body,
                    "match_score": round(score, 3),
                })
            matches.sort(key=lambda item: (-item["match_score"], float(item["volume_number"]), item["chapter_index"]))
            return {"query": query, "results": matches[:limit], "returned": min(limit, len(matches))}

        def get_full_chapter(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal full_chapter_calls, full_chapter_characters
            if full_chapter_calls >= 3 and full_chapter_characters >= 30_000:
                return {
                    "disabled": True, "used": full_chapter_calls,
                    "characters_used": full_chapter_characters,
                    "call_threshold": 3, "character_threshold": 30_000,
                    "error": "本轮对话的完整章节读取额度已用完；该工具现已禁用。请改用 search_original_text。",
                }
            full_chapter_calls += 1
            number = self._normalize_chapter_number(str(args.get("chapter_number") or ""))
            summary = chapter_by_number.get(number)
            chapter = self.library.get_chapter(summary.id) if summary else None
            if not chapter:
                return {
                    "chapter_number": number, "missing": True,
                    "used": full_chapter_calls, "characters_used": full_chapter_characters,
                    "call_threshold": 3, "character_threshold": 30_000,
                    "disabled_after_this_call": full_chapter_calls >= 3 and full_chapter_characters >= 30_000,
                }
            content = "\n\n".join(f"[{item.locator}]\n{item.text}" for item in chapter.paragraphs)
            full_chapter_characters += sum(len(item.text) for item in chapter.paragraphs)
            return {
                "chapter_number": number, "title": chapter.title,
                "volume_number": chapter.volume, "chapter_index": chapter.index,
                "character_count": chapter.character_count,
                "content": content,
                "used": full_chapter_calls, "characters_used": full_chapter_characters,
                "call_threshold": 3, "character_threshold": 30_000,
                "disabled_after_this_call": full_chapter_calls >= 3 and full_chapter_characters >= 30_000,
                "notice": "完整章节正文上下文开销很大；后续优先使用 search_original_text。",
            }

        def handle_agent_event(event: dict[str, Any]) -> None:
            compact = chat_activity_event(event)
            if not compact:
                return
            if compact["event_type"] in {"chat_initial", "chat_tool", "chat_thinking"}:
                thinking.append(compact)
            if event_callback:
                event_callback(compact)
        tools = [
            function_tool("search_wiki", "搜索 Wiki 标题、摘要与正文；搜索结果可以作为回答中的相关词条卡片", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
            function_tool("get_wiki_entry", "读取完整 Wiki 词条，并允许把该词条作为相关链接卡片嵌入回答", {"slug": {"type": "string"}}, ["slug"]),
            function_tool("get_content_summaries", "统一批量读取卷总结与章总结。每个请求项指定 volume_number；不提供 chapter_numbers 时返回该卷总结，提供时返回指定章总结。各项独立返回，单项错误不影响同批其他结果。", {"requests": {"type": "array", "maxItems": 48, "items": {"type": "object", "properties": {"volume_number": {"type": "number"}, "chapter_numbers": {"type": "array", "items": {"type": "string"}, "maxItems": 40}}, "required": ["volume_number"]}}}, ["requests"]),
            function_tool("search_knowledge_points", "批量搜索公共跨卷要点表。仅检索正式名和别名，直接返回命中要点的类型、别名、逐卷详细内容与证据。", {"queries": {"type": "array", "items": {"type": "string"}, "maxItems": 16}, "limit_per_query": {"type": "integer"}}, ["queries"]),
            function_tool("get_chapter_lists", "获取一卷或多卷的章节表，包含卷与章节稳定编号、标题、字数、段落数及剧情章节标记。可用于了解作品结构和定位完整章节。", {"volume_numbers": {"type": "array", "items": {"type": "number"}, "maxItems": 24}}, ["volume_numbers"]),
            function_tool("search_chapter_titles", "模糊检索章节标题。章节可能重名，因此返回包含稳定章节编号、所属卷与卷内章序的列表。", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
            function_tool("get_full_chapter", "按 vXXcXX 编号读取一个章节的完整正文，禁止批量使用。仅当本轮累计调用达到 3 次且返回正文累计达到 30000 字时才禁用；任一条件未达到都仍可继续调用。完整正文会大量占用上下文，仅在必须进行细颗粒度整章分析时使用；一般事实核查必须优先使用 search_original_text。", {"chapter_number": {"type": "string"}}, ["chapter_number"]),
            function_tool("search_original_text", "搜索带定位原文。普通事件与关系核验默认 hybrid；只有核对独特名称或精确短句时使用 lexical。一个查询只核验一个事实，使用少量独特实体或事件短语，避免泛词。系统按匹配强弱自适应返回约 4–24 条，不会为凑数加入明显弱相关结果；最后按原文顺序排列。默认上限 12 条。", {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 4, "maximum": 24, "default": 12}, "mode": self.knowledge.original_search_mode_schema(thread.series_id) if semantic_retrieval and hasattr(self.knowledge, "original_search_mode_schema") else {"type": "string", "enum": ["lexical"], "default": "lexical"}}, ["query"]),
            function_tool("create_special", "按读者明确要求创建可删除的 Wiki 特辑；只重组已有词条", {
                "title": {"type": "string"}, "introduction": {"type": "string"},
                "related_entry_slugs": {"type": "array", "items": {"type": "string"}},
            }, ["title", "introduction", "related_entry_slugs"]),
            function_tool("get_reading_settings", "读取读者当前的阅读设置。处理相对修改（如调大字号、切换夜间主题）前先读取。仅在设置相关请求中使用。", {}),
            function_tool("update_reading_settings", "暂存阅读设置修改，等本轮最终回答完整输出后由客户端统一应用。只有读者明确要求修改设置时才能调用；不得把偏好讨论、效果咨询或模糊暗示当成授权。", {
                "settings": {"type": "object", "additionalProperties": False, "properties": {
                    "theme": {"type": "string", "enum": ["paper", "dark", "system"]},
                    "font_size": {"type": "integer", "minimum": 14, "maximum": 23},
                    "line_height": {"type": "number", "minimum": 1.55, "maximum": 2.4},
                    "epub_features": {"type": "string", "enum": ["off", "basic", "advanced"]},
                    "paper_color": {"type": "string", "enum": ["standard", "warm"]},
                    "illustrations": {"type": "string", "enum": ["off", "original", "immersive"]},
                    "spoiler_shield": {"type": "string", "enum": ["off", "auto", "strict"]},
                    "semantic_retrieval": {"type": "boolean"},
                }},
            }, ["settings"]),
        ]
        tools.append(function_tool(
            "confirm_serious_spoiler",
            (
                "仅当研究完成、准备正式回答，且完整回答很可能泄露关键身份、死亡、背叛、结局、谜底或重大转折时调用。调用后本轮暂停等待读者选择；不要与其他写入或最终提交工具同时调用。普通情节信息不要调用。"
                if allow_spoiler_confirmation else
                "读者已经处理过本次剧透警告，本工具已关闭。不要再次调用；直接按照读者选择组织并提交回答。"
            ),
            {},
        ))
        tools.append(function_tool("submit_answer", "提交最终回答、证据、编号 Wiki/正文卡片和其他动作。卡片可通过 answer 中独立段落 {{cards:1,2}} 块级嵌入。", {
                "answer": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "object", "properties": {
                    "locator": {"type": "string"}, "note": {"type": "string"}, "marker": {"type": "string"},
                }, "required": ["locator"]}},
                "cards": {"type": "array", "items": {"type": "object", "properties": {
                    "number": {"type": "integer"},
                    "type": {"type": "string", "enum": ["wiki", "source"]},
                    "label": {"type": "string"},
                    "slug": {"type": "string"},
                    "locator": {"type": "string"},
                }, "required": ["number", "type"]}}, "actions": {"type": "array"},
            }, ["answer"]))
        handlers = {
            "search_wiki": search_wiki,
            "get_wiki_entry": get_wiki_entry,
            "get_content_summaries": get_content_summaries,
            "search_knowledge_points": search_knowledge_points,
            "get_chapter_lists": get_chapter_lists,
            "search_chapter_titles": search_chapter_titles,
            "get_full_chapter": get_full_chapter,
            "search_original_text": search_original_text,
            "create_special": lambda args: self.knowledge.create_special(
                thread.series_id, str(args.get("title", "读者特辑")),
                str(args.get("introduction", "")), [str(item) for item in args.get("related_entry_slugs", [])],
            ).model_dump(),
            "get_reading_settings": get_reading_settings,
            "update_reading_settings": update_reading_settings,
        }
        if allow_spoiler_confirmation:
            def confirm_serious_spoiler(_args: dict[str, Any]) -> None:
                raise AgentPause("serious_spoiler_confirmation")
            handlers["confirm_serious_spoiler"] = confirm_serious_spoiler
        else:
            handlers["confirm_serious_spoiler"] = lambda _args: {
                "confirmed": True,
                "decision": spoiler_decision,
                "instruction": spoiler_instruction,
                "message": "剧透确认已经关闭。直接按读者选择提交回答，不要再次请求确认。",
            }
        try:
            result = run_tool_agent(
                client=self.provider,
                model=self.model_id,
                system_prompt=(
                    CHAT_AGENT_SYSTEM
                    if not spoiler_decision else
                    CHAT_AGENT_SYSTEM + "\n\n本轮剧透确认已经完成并永久关闭：" + spoiler_instruction
                ),
                user_prompt=user_prompt,
                tools=tools,
                handlers=handlers,
                final_tool_name="submit_answer",
                max_steps=10,
                max_tokens=16000,
                event_callback=handle_agent_event,
                error_recovery_round=True,
                expose_round_limits_to_model=False,
                final_payload_validator=lambda payload: validate_chat_submission(
                    payload,
                    request.content,
                    require_original_evidence=(
                        semantic_retrieval
                        and retrieval_activity["derived"] > 0
                        and retrieval_activity["original"] == 0
                    ),
                ),
                reasoning_effort=lambda step: "none" if step == 1 else "low",
            )
        except AgentPause as pause:
            if pause.reason != "serious_spoiler_confirmation":
                raise
            self._save_pending_confirmation(
                thread_id, request, thinking, round(time.perf_counter() - started, 1),
                full_chapter_calls, full_chapter_characters,
            )
            return self.get_thread(thread_id)
        normalized_citations = [
            {**item, "locator": normalize_chat_locator(item.get("locator"), thread.series_id)}
            for item in normalize_evidence(result.payload.get("citations"))
        ]
        citations = [
            EvidenceRef.model_validate({**item, "marker": str(index)})
            for index, item in enumerate((item for item in normalized_citations if item["locator"]), start=1)
        ]
        answer = ensure_inline_citations(str(result.payload.get("answer") or ""), citations)
        cards = normalize_chat_cards(
            result.payload.get("cards"), entry_by_slug, retrieved_cards, series, self.library,
        )
        thinking_seconds = round(prior_thinking_seconds + time.perf_counter() - started, 1)
        assistant_id = uuid.uuid4().hex
        actions = [
            item for item in (result.payload.get("actions") or [])
            if isinstance(item, dict) and item.get("type") != "reading_settings_change"
        ]
        if original_reading_settings and pending_reading_settings:
            setting_changes = describe_reading_settings_change(original_reading_settings, pending_reading_settings)
            if setting_changes:
                actions.append({
                    "type": "reading_settings_change",
                    "before": original_reading_settings,
                    "after": pending_reading_settings,
                    "changes": setting_changes,
                })
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO chat_messages(id,thread_id,role,content,citations_json,cards_json,actions_json,thinking_json,thinking_seconds) VALUES(?,?,'assistant',?,?,?,?,?,?)",
                (
                    assistant_id, thread_id, answer,
                    json.dumps([item.model_dump() for item in citations], ensure_ascii=False),
                    json.dumps(cards, ensure_ascii=False),
                    json.dumps(actions, ensure_ascii=False),
                    json.dumps(thinking, ensure_ascii=False), thinking_seconds,
                ),
            )
            db.execute("UPDATE chat_threads SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (thread_id,))
        if first_exchange:
            title = self._generate_title(request.content, str(result.payload.get("answer") or ""))
            with self.library.connect() as db:
                db.execute("UPDATE chat_threads SET title=? WHERE id=?", (title, thread_id))
        return self.get_thread(thread_id)

    def resolve_spoiler_confirmation(
        self, thread_id: str, request: ChatSpoilerDecisionRequest,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ChatThreadDetail:
        pending = self._pending_row(thread_id)
        if not pending:
            raise LookupError("Spoiler confirmation not found")
        if request.decision == "stop":
            assistant_id = uuid.uuid4().hex
            thinking = json.loads(pending["thinking_json"] or "[]")
            with self.library.connect() as db:
                db.execute(
                    "INSERT INTO chat_messages(id,thread_id,role,content,actions_json,thinking_json,thinking_seconds) VALUES(?,?,'assistant','',?,?,?)",
                    (
                        assistant_id, thread_id,
                        json.dumps([{"type": "answer_stopped"}], ensure_ascii=False),
                        json.dumps(thinking, ensure_ascii=False),
                        float(pending["thinking_seconds"] or 0),
                    ),
                )
                db.execute("DELETE FROM chat_spoiler_confirmations WHERE thread_id=?", (thread_id,))
                db.execute("UPDATE chat_threads SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (thread_id,))
            return self.get_thread(thread_id)
        original = ChatSendRequest(
            content=pending["question"],
            current_chapter_id=pending["current_chapter_id"],
            current_paragraph_id=pending["current_paragraph_id"],
            reading_settings=ReadingSettingsSnapshot.model_validate(json.loads(pending["reading_settings_json"])) if json.loads(pending["reading_settings_json"] or "{}") else None,
        )
        self._delete_pending_confirmation(thread_id)
        return self.send(
            thread_id, original, event_callback,
            persist_user=False, allow_spoiler_confirmation=False,
            spoiler_decision=request.decision,
            prior_thinking=json.loads(pending["thinking_json"] or "[]"),
            prior_thinking_seconds=float(pending["thinking_seconds"] or 0),
            prior_full_chapter_calls=int(pending["full_chapter_calls"] or 0),
            prior_full_chapter_characters=int(pending["full_chapter_characters"] or 0),
        )

    def _pending_row(self, thread_id: str) -> Any | None:
        with self.library.connect() as db:
            return db.execute(
                "SELECT * FROM chat_spoiler_confirmations WHERE thread_id=?", (thread_id,),
            ).fetchone()

    def _pending_confirmation(self, thread_id: str) -> ChatSpoilerConfirmation | None:
        row = self._pending_row(thread_id)
        if not row:
            return None
        return ChatSpoilerConfirmation(
            id=row["id"], thread_id=row["thread_id"],
            thinking=json.loads(row["thinking_json"] or "[]"),
            thinking_seconds=float(row["thinking_seconds"] or 0),
            created_at=row["created_at"],
        )

    def _save_pending_confirmation(
        self, thread_id: str, request: ChatSendRequest, thinking: list[dict[str, Any]],
        thinking_seconds: float, full_chapter_calls: int, full_chapter_characters: int,
    ) -> None:
        with self.library.connect() as db:
            db.execute(
                """INSERT INTO chat_spoiler_confirmations(
                    id,thread_id,question,current_chapter_id,current_paragraph_id,thinking_json,thinking_seconds,full_chapter_calls,full_chapter_characters,reading_settings_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    question=excluded.question,current_chapter_id=excluded.current_chapter_id,
                    current_paragraph_id=excluded.current_paragraph_id,
                    thinking_json=excluded.thinking_json,thinking_seconds=excluded.thinking_seconds,
                    full_chapter_calls=excluded.full_chapter_calls,
                    full_chapter_characters=excluded.full_chapter_characters,
                    reading_settings_json=excluded.reading_settings_json,
                    created_at=CURRENT_TIMESTAMP""",
                (
                    uuid.uuid4().hex, thread_id, request.content,
                    request.current_chapter_id, request.current_paragraph_id,
                    json.dumps(thinking, ensure_ascii=False), thinking_seconds, full_chapter_calls, full_chapter_characters,
                    json.dumps(request.reading_settings.model_dump() if request.reading_settings else {}, ensure_ascii=False),
                ),
            )

    def _delete_pending_confirmation(self, thread_id: str) -> None:
        with self.library.connect() as db:
            db.execute("DELETE FROM chat_spoiler_confirmations WHERE thread_id=?", (thread_id,))

    def _generate_title(self, question: str, answer: str) -> str:
        try:
            result = self.provider.complete_json(
                model=self.model_id,
                system="根据首次问答生成简洁、具体的中文会话标题。只输出 JSON：{\"title\":\"...\"}。标题不超过18字，不使用书名号。",
                user=f"问题：{question}\n回答：{answer[:1500]}", max_tokens=120,
            )
            title = str(json.loads(result.content).get("title") or "").strip()
            return title[:24] or question[:18]
        except Exception:
            return question[:18] or "新对话"

    def _search_wiki(self, series_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        return self.knowledge.search_wiki(series_id, query, limit)

    def _entry_payload(self, series_id: str, slug: str) -> dict[str, Any]:
        entry = self.knowledge.get_entry(series_id, slug)
        return entry.model_dump() if entry else {"error": "entry not found"}

    def _volume_payload(self, series_id: str, volume_number: int | float) -> dict[str, Any]:
        reports = self.knowledge.list_volume_reports(series_id)
        report = next((item for item in reports if item.volume_number == volume_number and item.status == "completed" and item.report), None)
        return report.report.model_dump() if report and report.report else {"error": "report not found"}

    @staticmethod
    def _chapter_number(volume_number: int | float, chapter_index: int) -> str:
        return f"v{format_volume_number(volume_number)}c{int(chapter_index):02d}"

    @classmethod
    def _normalize_chapter_number(cls, value: str) -> str:
        match = re.search(r"v\s*(\d+(?:\.\d+)?)\s*[-_/]?\s*c\s*(\d+)", value, re.IGNORECASE)
        if not match:
            return value.strip().casefold()
        return cls._chapter_number(float(match.group(1)), int(match.group(2)))

    @staticmethod
    def _normalize_title(value: str) -> str:
        return re.sub(r"[\s\-—_·・:：,，。.!！?？『』「」【】\[\]()（）]+", "", value).casefold()

    def _current_context(self, chapter_id: str | None, paragraph_id: str | None) -> dict[str, Any]:
        if not chapter_id:
            return {}
        chapter = self.library.get_chapter(chapter_id)
        if chapter is None:
            return {}
        paragraph = next((item for item in chapter.paragraphs if item.id == paragraph_id), None)
        return {
            "chapter_id": chapter.id,
            "chapter_title": chapter.title,
            "volume": chapter.volume,
            "paragraph_id": paragraph.id if paragraph else None,
            "paragraph_locator": paragraph.locator if paragraph else None,
            "paragraph_text": paragraph.text if paragraph else None,
            "chapter_intro": chapter.chapter_overview.intro,
        }

    @staticmethod
    def _message(row: Any) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=row["id"], thread_id=row["thread_id"], role=row["role"], content=row["content"],
            citations=[EvidenceRef.model_validate(item) for item in json.loads(row["citations_json"])],
            cards=json.loads(row["cards_json"]), actions=json.loads(row["actions_json"]),
            thinking=json.loads(row["thinking_json"] or "[]"),
            thinking_seconds=float(row["thinking_seconds"] or 0),
            created_at=row["created_at"],
        )


CHAT_THINKING_LABELS = (
    "正在烧烤", "正在偷吃Token", "正在量子速读", "正在啃书",
    "正在给脑细胞热身", "正在把线索串成烤串", "正在翻书包", "正在捞重点",
    "正在和标点商量", "正在给答案加点盐", "正在搅拌脑浆", "正在挠电子脑袋",
    "正在书里探头探脑", "正在搬运知识小砖头", "正在把思路捋成面条", "正在给灵感充电",
    "正在蹲守灵感", "正在把问号熬成汤", "正在给逻辑打结再解开", "正在捕捉野生线索",
    "正在偷偷翻小抄", "正在压缩脑内弹幕", "正在给结论做拉伸", "正在给文字排队",
    "正在知识海里狗刨", "正在把脑回路接上电", "正在和书页对暗号", "正在给答案抛光",
    "正在给思绪梳毛", "正在搭建纸上宇宙", "正在追捕逃跑的重点", "正在把碎片拼成饼",
    "正在脑内开小会", "正在给书页翻面", "正在把线索抖出来", "正在加载一点聪明",
    "正在给思路系鞋带", "正在给答案烤个脆边", "正在潜入字里行间", "正在把信息晒干",
    "正在给脑内仓鼠加餐", "正在对着书本施法", "正在给知识拧螺丝", "正在搓一颗答案",
    "正在把伏笔捞上岸", "正在赶走脑内乱码", "正在给段落做体操", "正在把灵感装盘",
    "正在点燃电子小灯泡", "正在给问题拆快递", "正在给脑洞补墙", "正在和文字掰手腕",
)


def chat_activity_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    step = int(event.get("step") or 0)
    if event_type == "model_started":
        return {
            "event_type": "chat_preparing" if step <= 1 else "chat_thinking",
            "kind": "thinking",
            "label": (
                "正在理解问题" if step <= 1 else random.choice(CHAT_THINKING_LABELS)
            ),
        }
    if event_type == "model_completed" and step == 1:
        content = str((event.get("arguments") or {}).get("content") or "").strip()
        if content:
            return {"event_type": "chat_initial", "kind": "initial", "label": content}
        return None
    if event_type != "tool_result":
        return None
    tool_name = str(event.get("tool_name") or "")
    stats = event.get("result_stats") or {}
    returned = int(stats.get("returned") or (1 if tool_name == "get_wiki_entry" else 0))
    labels = {
        "search_original_text": f"检索了 {returned} 个原文片段",
        "search_wiki": f"检索了 {returned} 个 Wiki 词条",
        "search_knowledge_points": f"检索了 {returned} 个公共要点",
        "get_wiki_entry": f"浏览了 {returned} 个 Wiki 内容",
        "get_content_summaries": f"浏览了 {returned} 份内容总结",
        "get_chapter_lists": f"浏览了 {returned} 份章节表",
        "search_chapter_titles": f"找到 {returned} 个章节标题结果",
        "get_full_chapter": (
            f"读取了 {returned} 个完整章节；工具已禁用"
            if stats.get("disabled") else f"读取了 {returned} 个完整章节"
        ),
        "create_special": "创建了 Wiki 特辑",
        "get_reading_settings": "读取了当前阅读设置",
        "update_reading_settings": "已准备阅读设置修改",
    }
    return {
        "event_type": "chat_tool", "kind": "tool", "tool_name": tool_name,
        "count": returned, "label": labels.get(tool_name, "完成了一项资料检索"),
    }


def normalize_chat_cards(
    requested: Any, entries: dict[str, Any], retrieved: dict[str, dict[str, Any]],
    series: Any, library: LibraryRepository,
) -> list[dict[str, Any]]:
    requested_items = requested if isinstance(requested, list) else []
    chosen: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()
    seen_numbers: set[int] = set()
    for fallback_number, raw in enumerate(requested_items, start=1):
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("number") or fallback_number)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen_numbers:
            continue
        card_type = str(raw.get("type") or ("wiki" if raw.get("slug") else "source")).strip().casefold()
        label = str(raw.get("label") or raw.get("relation") or "").strip()[:60]
        if card_type == "wiki":
            slug = str(raw.get("slug") or "").strip()
            entry = entries.get(slug)
            target = ("wiki", slug)
            if not entry or target in seen_targets:
                continue
            card = {
                "number": number, "type": "wiki", "slug": entry.slug,
                "title": entry.title, "kind": entry.kind, "summary": entry.summary,
                "theme_color": entry.theme_color, "label": label or "相关 Wiki 词条",
            }
        elif card_type == "source":
            card = normalize_source_card(raw.get("locator"), number, label, series, library)
            if not card:
                continue
            target = ("source", str(card["locator"]))
            if target in seen_targets:
                continue
        else:
            continue
        seen_numbers.add(number)
        seen_targets.add(target)
        chosen.append(card)
    if not chosen:
        for slug, card in list(retrieved.items())[:3]:
            target = ("wiki", slug)
            if target in seen_targets:
                continue
            number = len(chosen) + 1
            chosen.append({**card, "number": number, "type": "wiki", "label": "检索到的相关词条"})
            seen_targets.add(target)
    return chosen[:8]


def validate_chat_submission(
    payload: dict[str, Any], question: str, *, require_original_evidence: bool = False,
) -> str | None:
    """Reject incomplete chat submissions while leaving ordinary short answers valid."""
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        return "answer 为空。正式回答必须完整放在 submit_answer.answer 中。"
    if len(question.strip()) >= 16 and (
        len(answer) < 32 or ("\n" not in answer and not re.search(r"[。！？.!?]", answer))
    ):
        return "answer 看起来只有标题或提纲。请把完整回答放入 submit_answer.answer，不要写在普通 content 中。"
    if require_original_evidence:
        return (
            "已使用 Wiki、知识点或总结建立结论，但尚未核验原文。请先调用一次 "
            "search_original_text：普通事实使用简洁的 hybrid 查询；只有精确名称或短句复核才使用 lexical。"
        )

    cards = [item for item in (payload.get("cards") or []) if isinstance(item, dict)]
    numbers = {
        int(item.get("number")) for item in cards
        if str(item.get("number") or "").isdigit() and int(item.get("number")) > 0
    }
    embedded = {
        int(number)
        for match in re.finditer(r"^\s*\{\{\s*cards\s*:\s*([0-9]+(?:\s*[,，]\s*[0-9]+)*)\s*\}\}\s*$", answer, re.MULTILINE | re.IGNORECASE)
        for number in re.split(r"[,，]", match.group(1))
    }
    unknown = embedded - numbers
    if unknown:
        return f"卡片占位符引用了未提交的编号：{', '.join(map(str, sorted(unknown)))}。"

    asks_for_location = bool(re.search(
        r"(?:首次|第一次|正式).{0,8}(?:出现|登场)|(?:出现|登场).{0,8}(?:哪里|哪[一卷章节]|第几卷)|"
        r"(?:哪里|在哪[里]?|哪[一卷章节]|第几卷|第几章|具体位置|原文位置|跳转)",
        question,
    ))
    unable_to_locate = bool(re.search(r"(?:未能|无法|没有|尚未).{0,8}(?:定位|找到|确认)", answer))
    source_numbers = {
        int(item.get("number")) for item in cards
        if item.get("type") == "source" and str(item.get("number") or "").isdigit()
    }
    if asks_for_location and not unable_to_locate:
        if not source_numbers:
            return "读者明确询问原文位置或首次登场；请提交至少一张最精确的正文卡片。"
        if not (source_numbers & embedded):
            return "位置类问题的正文卡片必须在相关结论后用独立 {{cards:N}} 段落内嵌。"
    return None


def normalize_source_card(
    raw_locator: Any, number: int, label: str, series: Any, library: LibraryRepository,
) -> dict[str, Any] | None:
    value = str(raw_locator or "").strip()
    compact = re.fullmatch(r"v\s*(\d+(?:\.\d+)?)\s*(?:c\s*(\d+))?\s*(?:p\s*(\d+))?", value, re.IGNORECASE)
    routed = re.search(r"/v(\d+(?:\.\d+)?)(?:/c(\d+))?(?:/p(\d+))?/?$", value, re.IGNORECASE)
    match = compact or routed
    if not match:
        return None
    volume_number = float(match.group(1))
    chapter_index = int(match.group(2)) if match.group(2) else None
    paragraph_index = int(match.group(3)) if match.group(3) else None
    volume = next((item for item in series.volumes if float(item.number) == volume_number), None)
    if not volume or (paragraph_index is not None and chapter_index is None):
        return None
    volume_segment = f"v{format_volume_number(volume.number)}"
    base = f"lr://series/{series.id}/{volume_segment}"
    display_volume = f"第 {format_volume_number(volume.number).lstrip('0') or '0'} 卷"
    if chapter_index is None:
        return {
            "number": number, "type": "source", "scale": "volume", "locator": base,
            "title": volume.title or display_volume, "summary": f"{len(volume.chapters)} 篇 · {volume.character_count:,} 字",
            "label": label or "打开整卷", "source_label": f"{display_volume} · {volume_segment}",
            "cover_url": volume.cover_url, "theme_color": "#8b6f47",
        }
    chapter_summary = next((item for item in volume.chapters if int(item.index) == chapter_index), None)
    if not chapter_summary:
        return None
    chapter_locator = f"{base}/c{chapter_index:02d}"
    if paragraph_index is None:
        return {
            "number": number, "type": "source", "scale": "chapter", "locator": chapter_locator,
            "title": chapter_summary.title, "summary": f"{display_volume} · {chapter_summary.character_count:,} 字",
            "label": label or "打开章节", "source_label": f"{display_volume} · {volume_segment}c{chapter_index:02d}",
            "theme_color": "#64758a",
        }
    chapter = library.get_chapter(chapter_summary.id)
    paragraph_locator = f"{chapter_locator}/p{paragraph_index:04d}"
    paragraph = next((item for item in (chapter.paragraphs if chapter else []) if item.locator == paragraph_locator), None)
    if not paragraph:
        return None
    excerpt = re.sub(r"\s+", " ", paragraph.text).strip()
    return {
        "number": number, "type": "source", "scale": "paragraph", "locator": paragraph_locator,
        "title": chapter_summary.title, "summary": excerpt[:96] + ("…" if len(excerpt) > 96 else ""),
        "label": label or "查看原文片段",
        "source_label": f"{display_volume} · {chapter_summary.title} · {volume_segment}c{chapter_index:02d}p{paragraph_index:04d}",
        "theme_color": "#736683",
    }


def normalize_chat_locator(raw_locator: Any, series_id: str) -> str:
    value = str(raw_locator or "").strip()
    if value.startswith("lr://"):
        return value
    match = re.fullmatch(
        r"v\s*(\d+(?:\.\d+)?)\s*(?:[/_-]?\s*c\s*(\d+))?\s*(?:[/_-]?\s*p\s*(\d+))?",
        value, re.IGNORECASE,
    )
    if not match:
        return ""
    locator = f"lr://series/{series_id}/v{format_volume_number(float(match.group(1)))}"
    if match.group(2):
        locator += f"/c{int(match.group(2)):02d}"
    if match.group(3):
        locator += f"/p{int(match.group(3)):04d}"
    return locator


def ensure_inline_citations(answer: str, citations: list[EvidenceRef]) -> str:
    """Keep citations near claims even when the model forgets to place markers."""
    superscript_digits = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    for citation in citations:
        marker = str(citation.marker or "")
        if not marker:
            continue
        superscript = marker.translate(superscript_digits)
        answer = re.sub(rf"\[{re.escape(marker)}\]\(lr://[^)]+\)", f"[{marker}]", answer)
        answer = re.sub(rf"\[{re.escape(superscript)}\](?:\(lr://[^)]+\))?", f"[{marker}]", answer)
        answer = re.sub(rf"(?<![\w\]]){re.escape(superscript)}\(lr://[^)]+\)", f"[{marker}]", answer)
    missing = [citation for citation in citations if f"[{citation.marker}]" not in answer]
    if not missing:
        return answer
    lines = answer.splitlines()
    candidates = [index for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith(("#", "-", "*", ">"))]
    if not candidates:
        candidates = [0]
        lines = [answer]
    for offset, citation in enumerate(missing):
        index = candidates[min(offset, len(candidates) - 1)]
        lines[index] = lines[index].rstrip() + f" [{citation.marker}]"
    return "\n".join(lines)
