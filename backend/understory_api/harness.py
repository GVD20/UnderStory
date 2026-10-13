from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .deepseek import MODEL_ID, MiniMaxClient, load_minimax_base_url, load_minimax_key
from .library import LibraryRepository
from .models import (
    HarnessArtifacts,
    HarnessCompileRequest,
    HarnessCompileResponse,
    HarnessConfig,
    HarnessRun,
    NarrativePacket,
    RagChunk,
)
from .prompts import PROMPT_VERSIONS, build_chapter_prompt, get_system_prompt
from .settings import SettingsRepository


class HarnessService:
    def __init__(self, library: LibraryRepository, provider: Any | None = None) -> None:
        self.library = library
        settings = SettingsRepository(library.workspace, library.data_dir).load()
        self.api_key = settings.api_key
        self.provider = provider or (MiniMaxClient(self.api_key, base_url=settings.base_url, model=settings.fast_model, reasoning_split=settings.reasoning_split) if self.api_key else None)
        self.model_id = getattr(self.provider, "model", settings.fast_model)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.library.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS harness_runs (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    system_fingerprint TEXT NOT NULL DEFAULT '',
                    raw_response TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS harness_runs_chapter_idx
                    ON harness_runs(chapter_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS harness_runs_hash_idx
                    ON harness_runs(input_hash, status);
                CREATE TABLE IF NOT EXISTS narrative_packets (
                    chapter_id TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES harness_runs(id),
                    prompt_version TEXT NOT NULL,
                    packet_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS harness_run_packets (
                    run_id TEXT PRIMARY KEY REFERENCES harness_runs(id) ON DELETE CASCADE,
                    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                    packet_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    id TEXT PRIMARY KEY,
                    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    start_paragraph_id TEXT NOT NULL,
                    end_paragraph_id TEXT NOT NULL,
                    start_locator TEXT NOT NULL,
                    end_locator TEXT NOT NULL,
                    text TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    approximate_tokens INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    UNIQUE(chapter_id, chunk_index)
                );
                """
            )

    def config(self) -> HarnessConfig:
        return HarnessConfig(
            configured=self.provider is not None,
            models=[self.model_id],
            prompt_versions=PROMPT_VERSIONS,
        )

    def verify_provider(self) -> list[str]:
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")
        return self.provider.list_models()

    def list_runs(self, series_id: str, limit: int = 30) -> list[HarnessRun]:
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT r.*, c.title AS chapter_title FROM harness_runs r "
                "JOIN chapters c ON c.id = r.chapter_id "
                "WHERE r.series_id = ? ORDER BY r.created_at DESC LIMIT ?",
                (series_id, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def artifacts(self, chapter_id: str) -> HarnessArtifacts:
        packet: NarrativePacket | None = None
        with self.library.connect() as db:
            packet_row = db.execute(
                "SELECT packet_json FROM narrative_packets WHERE chapter_id = ?", (chapter_id,)
            ).fetchone()
            if packet_row:
                packet = NarrativePacket.model_validate_json(packet_row["packet_json"])
            chunk_rows = db.execute(
                "SELECT * FROM rag_chunks WHERE chapter_id = ? ORDER BY chunk_index", (chapter_id,)
            ).fetchall()
            run_rows = db.execute(
                "SELECT r.*, c.title AS chapter_title FROM harness_runs r "
                "JOIN chapters c ON c.id = r.chapter_id WHERE r.chapter_id = ? "
                "ORDER BY r.created_at DESC LIMIT 20",
                (chapter_id,),
            ).fetchall()
        chunks = [
            RagChunk(
                id=row["id"], chapter_id=row["chapter_id"], chunk_index=row["chunk_index"],
                start_paragraph_id=row["start_paragraph_id"], end_paragraph_id=row["end_paragraph_id"],
                start_locator=row["start_locator"], end_locator=row["end_locator"], text=row["text"],
                character_count=row["character_count"], approximate_tokens=row["approximate_tokens"],
            )
            for row in chunk_rows
        ]
        return HarnessArtifacts(
            chapter_id=chapter_id,
            packet=packet,
            chunks=chunks,
            runs=[self._row_to_run(row) for row in run_rows],
        )

    def packet_for_run(self, run_id: str) -> NarrativePacket | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT p.packet_json, r.chapter_id, r.raw_response FROM harness_runs r "
                "LEFT JOIN harness_run_packets p ON p.run_id = r.id WHERE r.id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        if row["packet_json"]:
            return NarrativePacket.model_validate_json(row["packet_json"])
        if not row["raw_response"]:
            return None
        chapter = self.library.get_chapter(row["chapter_id"])
        if chapter is None:
            return None
        packet = self._validate_packet(
            row["raw_response"], chapter.id, [paragraph.locator for paragraph in chapter.paragraphs]
        )
        with self.library.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO harness_run_packets(run_id, chapter_id, packet_json) VALUES (?, ?, ?)",
                (run_id, chapter.id, packet.model_dump_json()),
            )
        return packet

    def compile_chapter(self, request: HarnessCompileRequest) -> HarnessCompileResponse:
        chapter = self.library.get_chapter(request.chapter_id)
        if chapter is None:
            raise LookupError("Chapter not found")
        if not chapter.is_body:
            raise ValueError("Non-body chapters are excluded from corpus compilation")
        if self.provider is None:
            raise ValueError("模型 API Key 尚未配置")

        prompt = build_chapter_prompt(
            chapter.id,
            chapter.title,
            [(paragraph.locator, paragraph.text) for paragraph in chapter.paragraphs],
        )
        system_prompt = get_system_prompt(request.prompt_version)
        input_hash = hashlib.sha256(
            f"{self.model_id}\0{request.prompt_version}\0{system_prompt}\0{prompt}".encode("utf-8")
        ).hexdigest()
        if not request.force:
            cached = self._cached_run(input_hash)
            if cached:
                cached.cached = True
                return HarnessCompileResponse(run=cached, artifacts=self.artifacts(chapter.id))

        run_id = uuid.uuid4().hex
        series_id = chapter.id.rsplit("-v", 1)[0]
        with self.library.connect() as db:
            db.execute(
                "INSERT INTO harness_runs(id, series_id, chapter_id, model, prompt_version, status, input_hash) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (run_id, series_id, chapter.id, self.model_id, request.prompt_version, input_hash),
            )

        started = time.perf_counter()
        raw_response: str | None = None
        provider_result: Any | None = None
        try:
            result = self.provider.complete_json(
                model=self.model_id,
                system=system_prompt,
                user=prompt,
                max_tokens=16000 if request.prompt_version == "chapter-packet-v3-faithful" else 6000,
            )
            provider_result = result
            raw_response = result.content
            packet = self._validate_packet(result.content, chapter.id, [p.locator for p in chapter.paragraphs])
            chunks = build_rag_chunks(chapter.id, chapter.paragraphs)
            duration_ms = round((time.perf_counter() - started) * 1000)
            people = unique([entity.name for entity in packet.entities if entity.entity_type == "character"])
            events = unique([event.title for event in packet.events if event.importance != "minor"])
            with self.library.connect() as db:
                db.execute(
                    "INSERT INTO narrative_packets(chapter_id, run_id, prompt_version, packet_json) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(chapter_id) DO UPDATE SET "
                    "run_id=excluded.run_id, prompt_version=excluded.prompt_version, "
                    "packet_json=excluded.packet_json, updated_at=CURRENT_TIMESTAMP",
                    (chapter.id, run_id, request.prompt_version, packet.model_dump_json()),
                )
                db.execute(
                    "INSERT OR REPLACE INTO harness_run_packets(run_id, chapter_id, packet_json) VALUES (?, ?, ?)",
                    (run_id, chapter.id, packet.model_dump_json()),
                )
                db.execute("DELETE FROM rag_chunks WHERE chapter_id = ?", (chapter.id,))
                db.executemany(
                    "INSERT INTO rag_chunks(id, chapter_id, chunk_index, start_paragraph_id, end_paragraph_id, "
                    "start_locator, end_locator, text, character_count, approximate_tokens, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            chunk.id, chunk.chapter_id, chunk.chunk_index, chunk.start_paragraph_id,
                            chunk.end_paragraph_id, chunk.start_locator, chunk.end_locator, chunk.text,
                            chunk.character_count, chunk.approximate_tokens,
                            hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                        )
                        for chunk in chunks
                    ],
                )
                db.execute(
                    "UPDATE chapter_overviews SET status='ready', intro=?, overview=?, people_json=?, events_json=?, places_json='[]', concepts_json='[]' "
                    "WHERE chapter_id = ?",
                    (packet.chapter_intro, packet.summary_short, json.dumps(people, ensure_ascii=False), json.dumps(events, ensure_ascii=False), chapter.id),
                )
                db.execute(
                    "UPDATE harness_runs SET status='completed', input_tokens=?, output_tokens=?, "
                    "cache_hit_tokens=?, cache_miss_tokens=?, duration_ms=?, "
                    "system_fingerprint=?, raw_response=? WHERE id=?",
                    (
                        result.input_tokens, result.output_tokens, result.cache_hit_tokens,
                        result.cache_miss_tokens, duration_ms, result.system_fingerprint,
                        result.content, run_id,
                    ),
                )
        except (ValidationError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self._mark_failed(run_id, started, str(error), raw_response, provider_result, request.model)
            raise ValueError(f"Narrative packet validation failed: {error}") from error
        except Exception as error:
            message = safe_provider_error(error)
            self._mark_failed(run_id, started, message, raw_response, provider_result, request.model)
            raise RuntimeError(message) from error

        run = self._get_run(run_id)
        if run is None:
            raise RuntimeError("Completed harness run could not be read back")
        return HarnessCompileResponse(run=run, artifacts=self.artifacts(chapter.id))

    def _validate_packet(self, raw: str, chapter_id: str, allowed_locators: list[str]) -> NarrativePacket:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        payload = json.loads(cleaned)
        payload = normalize_packet_payload(payload, allowed_locators[0])
        payload["chapter_id"] = chapter_id
        packet = NarrativePacket.model_validate(payload)
        allowed = set(allowed_locators)
        fallback_start, fallback_end = allowed_locators[0], allowed_locators[-1]
        for scene in packet.scenes:
            if scene.start_locator not in allowed:
                scene.start_locator = fallback_start
            if scene.end_locator not in allowed:
                scene.end_locator = fallback_end
        evidence_groups = [entity.evidence for entity in packet.entities]
        evidence_groups += [event.evidence for event in packet.events]
        evidence_groups += [relation.evidence for relation in packet.relations]
        evidence_groups += [fragment.evidence for fragment in packet.setting_fragments]
        evidence_groups += [packet.key_details, packet.dialogue_highlights]
        for group in evidence_groups:
            group[:] = [evidence for evidence in group if evidence.locator in allowed]
        packet.candidate_track_items[:] = [
            item for item in packet.candidate_track_items if item.locator in allowed
        ]
        return packet

    def _mark_failed(
        self,
        run_id: str,
        started: float,
        error: str,
        raw_response: str | None = None,
        provider_result: Any | None = None,
        model: str = MODEL_ID,
    ) -> None:
        input_tokens = int(getattr(provider_result, "input_tokens", 0))
        output_tokens = int(getattr(provider_result, "output_tokens", 0))
        cache_hit_tokens = int(getattr(provider_result, "cache_hit_tokens", 0))
        cache_miss_tokens = int(getattr(provider_result, "cache_miss_tokens", input_tokens))
        with self.library.connect() as db:
            db.execute(
                "UPDATE harness_runs SET status='failed', duration_ms=?, error=?, raw_response=?, "
                "input_tokens=?, output_tokens=?, cache_hit_tokens=?, cache_miss_tokens=?, "
                "system_fingerprint=? WHERE id=?",
                (
                    round((time.perf_counter() - started) * 1000), error[:1200], raw_response,
                    input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens,
                    getattr(provider_result, "system_fingerprint", ""), run_id,
                ),
            )

    def _cached_run(self, input_hash: str) -> HarnessRun | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT r.*, c.title AS chapter_title FROM harness_runs r "
                "JOIN chapters c ON c.id = r.chapter_id "
                "WHERE r.input_hash=? AND r.status='completed' ORDER BY r.created_at DESC LIMIT 1",
                (input_hash,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def _get_run(self, run_id: str) -> HarnessRun | None:
        with self.library.connect() as db:
            row = db.execute(
                "SELECT r.*, c.title AS chapter_title FROM harness_runs r "
                "JOIN chapters c ON c.id = r.chapter_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    @staticmethod
    def _row_to_run(row: Any) -> HarnessRun:
        return HarnessRun(
            id=row["id"], series_id=row["series_id"], chapter_id=row["chapter_id"],
            chapter_title=row["chapter_title"], model=row["model"], prompt_version=row["prompt_version"],
            status=row["status"], input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
            cache_hit_tokens=row["cache_hit_tokens"], cache_miss_tokens=row["cache_miss_tokens"],
            duration_ms=row["duration_ms"],
            error=row["error"], created_at=row["created_at"],
        )


def build_rag_chunks(chapter_id: str, paragraphs: list[Any], target_characters: int = 1800) -> list[RagChunk]:
    chunks: list[RagChunk] = []
    current: list[Any] = []
    current_characters = 0

    def flush() -> None:
        nonlocal current, current_characters
        if not current:
            return
        text = "\n".join(paragraph.text for paragraph in current)
        index = len(chunks) + 1
        chunks.append(
            RagChunk(
                id=f"{chapter_id}-rag-{index:04d}", chapter_id=chapter_id, chunk_index=index,
                start_paragraph_id=current[0].id, end_paragraph_id=current[-1].id,
                start_locator=current[0].locator, end_locator=current[-1].locator,
                text=text, character_count=len(text), approximate_tokens=max(1, round(len(text) * 0.62)),
            )
        )
        current = []
        current_characters = 0

    for paragraph in paragraphs:
        size = len(paragraph.text) + (1 if current else 0)
        if current and current_characters + size > target_characters:
            flush()
        current.append(paragraph)
        current_characters += size
    flush()
    return chunks


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value.strip()))


def safe_provider_error(error: Exception) -> str:
    text = str(error)
    if "401" in text:
        return "MiniMax rejected the API credential"
    if "402" in text:
        return "MiniMax Token Plan quota is unavailable"
    if "429" in text:
        return "MiniMax rate limit reached"
    if "timeout" in text.casefold():
        return "MiniMax request timed out"
    return f"MiniMax request failed: {type(error).__name__}"


def normalize_packet_payload(payload: dict[str, Any], fallback_locator: str) -> dict[str, Any]:
    entity_types = {"character", "place", "group", "object", "concept", "other"}
    for entity in payload.get("entities", []):
        if entity.get("entity_type") not in entity_types:
            entity["entity_type"] = "other"
        normalize_evidence(entity)
    for event in payload.get("events", []):
        if event.get("importance") not in {"major", "supporting", "minor"}:
            event["importance"] = "supporting"
        normalize_evidence(event)
    for relation in payload.get("relations", []):
        relation["object"] = relation.get("object") or relation.get("target") or ""
        normalize_evidence(relation)
    for fragment in payload.get("setting_fragments", []):
        fragment["category"] = fragment.get("category") or fragment.get("type") or fragment.get("kind") or "setting"
        fragment["description"] = (
            fragment.get("description") or fragment.get("summary") or fragment.get("content")
            or fragment.get("value") or fragment.get("note") or ""
        )
        normalize_evidence(fragment)
    normalized_tracks = []
    for item in payload.get("candidate_track_items", []):
        evidence = item.get("evidence") or []
        evidence_locator = evidence[0].get("locator") if evidence and isinstance(evidence[0], dict) else None
        item["title"] = item.get("title") or item.get("name") or "轨迹候选"
        item["category"] = item.get("category") or item.get("type") or item.get("kind") or "appearance"
        item["note"] = item.get("note") or item.get("summary") or item.get("description") or ""
        item["locator"] = item.get("locator") or evidence_locator or fallback_locator
        item["importance"] = item.get("importance", 0.5)
        normalized_tracks.append(item)
    payload["candidate_track_items"] = normalized_tracks
    return payload


def normalize_evidence(item: dict[str, Any]) -> None:
    normalized = []
    for evidence in item.get("evidence") or []:
        if isinstance(evidence, str):
            normalized.append({"locator": evidence, "note": ""})
        elif isinstance(evidence, dict) and evidence.get("locator"):
            normalized.append({"locator": evidence["locator"], "note": evidence.get("note", "")})
    item["evidence"] = normalized
