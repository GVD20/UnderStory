from __future__ import annotations

import hashlib
import json
import math
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from .harness import build_rag_chunks
from .library import LibraryRepository
from .settings import EmbeddingSettings, EmbeddingSettingsRepository


EMBEDDING_INDEX_VERSION = "rag-embedding-v1"


@dataclass(slots=True)
class EmbeddingIndexStatus:
    configured: bool
    available: bool
    complete: bool
    index_id: str | None
    model: str
    dimensions: int
    completed_volumes: int
    total_volumes: int
    embedded_chunks: int
    total_chunks: int

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "available": self.available,
            "complete": self.complete,
            "index_id": self.index_id,
            "model": self.model,
            "dimensions": self.dimensions,
            "completed_volumes": self.completed_volumes,
            "total_volumes": self.total_volumes,
            "embedded_chunks": self.embedded_chunks,
            "total_chunks": self.total_chunks,
        }


class EmbeddingClient:
    def __init__(self, settings: EmbeddingSettings, timeout_seconds: float = 120) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        try:
            request_payload: dict[str, Any] = {"model": self.settings.model, "input": texts}
            if query:
                # `user` belongs to the OpenAI embeddings contract.  Generic
                # providers may ignore it; the bundled local service uses it to
                # apply Qwen's recommended retrieval-query instruction only to
                # queries, never to indexed documents.
                request_payload["user"] = "query"
            response = httpx.post(
                f"{self.settings.base_url}/embeddings",
                headers=headers,
                json=request_payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ValueError("向量化服务连接超时，请检查 API 地址或减小批量大小") from error
        except httpx.NetworkError as error:
            raise ValueError(f"无法连接向量化服务：{type(error).__name__}") from error
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:400]
            if response.status_code in {401, 403}:
                raise ValueError(f"Embedding API Key 认证失败：{detail}")
            raise ValueError(f"向量化接口请求失败（HTTP {response.status_code}）：{detail}")
        try:
            payload = response.json()
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise TypeError("data is not an array")
            ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("向量化接口响应不符合 /v1/embeddings 格式") from error
        if len(vectors) != len(texts) or not vectors or any(not vector for vector in vectors):
            raise ValueError("向量化接口返回的向量数量或维度无效")
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise ValueError("向量化接口在同一批次返回了不同维度")
        return [_normalize(vector) for vector in vectors]


class EmbeddingService:
    def __init__(self, library: LibraryRepository) -> None:
        self.library = library
        self.settings_repository = EmbeddingSettingsRepository(library.workspace, library.data_dir)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.library.connect() as db:
            db.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS embedding_indexes (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    config_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    dimensions INTEGER NOT NULL DEFAULT 0,
                    index_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    active INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(series_id, config_hash)
                );
                CREATE INDEX IF NOT EXISTS embedding_indexes_series_idx
                    ON embedding_indexes(series_id, active DESC, updated_at DESC);
                CREATE TABLE IF NOT EXISTS embedding_volume_status (
                    index_id TEXT NOT NULL REFERENCES embedding_indexes(id) ON DELETE CASCADE,
                    volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                    source_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    embedded_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(index_id, volume_id)
                );
                CREATE TABLE IF NOT EXISTS rag_embeddings (
                    index_id TEXT NOT NULL REFERENCES embedding_indexes(id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(index_id, chunk_id)
                );
                CREATE INDEX IF NOT EXISTS rag_embeddings_index_idx
                    ON rag_embeddings(index_id);
                """
            )

    def status(self, series_id: str) -> EmbeddingIndexStatus:
        series = self.library.get_series(series_id)
        if series is None:
            raise LookupError("Series not found")
        settings = self.settings_repository.load()
        configured = settings.configured
        config_hash = self._config_hash(settings) if configured else ""
        with self.library.connect() as db:
            row = db.execute(
                "SELECT * FROM embedding_indexes WHERE series_id=? AND config_hash=? ORDER BY updated_at DESC LIMIT 1",
                (series_id, config_hash),
            ).fetchone() if configured else None
            statuses = db.execute(
                "SELECT volume_id,status,chunk_count,embedded_count FROM embedding_volume_status WHERE index_id=?",
                (row["id"],),
            ).fetchall() if row else []
        by_volume = {item["volume_id"]: item for item in statuses}
        completed = sum(
            1 for volume in series.volumes
            if volume.id in by_volume and by_volume[volume.id]["status"] == "completed"
        )
        total_chunks = sum(int(by_volume[volume.id]["chunk_count"]) for volume in series.volumes if volume.id in by_volume)
        embedded_chunks = sum(int(by_volume[volume.id]["embedded_count"]) for volume in series.volumes if volume.id in by_volume)
        complete = bool(series.volumes) and completed == len(series.volumes) and bool(row) and row["status"] == "completed"
        return EmbeddingIndexStatus(
            configured=configured,
            available=complete,
            complete=complete,
            index_id=row["id"] if row else None,
            model=settings.model if configured else "",
            dimensions=int(row["dimensions"] or 0) if row else 0,
            completed_volumes=completed,
            total_volumes=len(series.volumes),
            embedded_chunks=embedded_chunks,
            total_chunks=total_chunks,
        )

    def index_history(self) -> list[dict[str, object]]:
        settings = self.settings_repository.load()
        current_hash = self._config_hash(settings) if settings.configured else ""
        with self.library.connect() as db:
            indexes = db.execute(
                "SELECT ei.*,s.title series_title FROM embedding_indexes ei "
                "JOIN series s ON s.id=ei.series_id ORDER BY ei.updated_at DESC,ei.created_at DESC"
            ).fetchall()
            volumes = db.execute(
                "SELECT series_id,COUNT(*) total FROM volumes GROUP BY series_id"
            ).fetchall()
            volume_totals = {str(row["series_id"]): int(row["total"]) for row in volumes}
            status_rows = db.execute(
                "SELECT index_id,status,chunk_count,embedded_count FROM embedding_volume_status"
            ).fetchall()
        by_index: dict[str, list[Any]] = {}
        for row in status_rows:
            by_index.setdefault(str(row["index_id"]), []).append(row)
        result = []
        for row in indexes:
            statuses = by_index.get(str(row["id"]), [])
            result.append({
                "id": str(row["id"]), "series_id": str(row["series_id"]),
                "series_title": str(row["series_title"]), "model": str(row["model"]),
                "base_url": str(row["base_url"]), "dimensions": int(row["dimensions"] or 0),
                "status": str(row["status"]), "active": bool(row["active"]),
                "current_configuration": bool(current_hash and row["config_hash"] == current_hash),
                "completed_volumes": sum(1 for item in statuses if item["status"] == "completed"),
                "total_volumes": volume_totals.get(str(row["series_id"]), 0),
                "embedded_chunks": sum(int(item["embedded_count"] or 0) for item in statuses),
                "total_chunks": sum(int(item["chunk_count"] or 0) for item in statuses),
                "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
            })
        return result

    def select_configuration(self) -> None:
        settings = self.settings_repository.load()
        with self.library.connect() as db:
            db.execute("UPDATE embedding_indexes SET active=0")
            if not settings.configured:
                return
            config_hash = self._config_hash(settings)
            db.execute(
                "UPDATE embedding_indexes SET active=1 WHERE config_hash=? AND status='completed' "
                "AND EXISTS (SELECT 1 FROM volumes v WHERE v.series_id=embedding_indexes.series_id) "
                "AND NOT EXISTS (SELECT 1 FROM volumes v WHERE v.series_id=embedding_indexes.series_id "
                "AND NOT EXISTS (SELECT 1 FROM embedding_volume_status evs WHERE evs.index_id=embedding_indexes.id "
                "AND evs.volume_id=v.id AND evs.status='completed'))",
                (config_hash,),
            )

    def volume_complete(self, series_id: str, volume_id: str) -> bool:
        status = self.status(series_id)
        if not status.index_id:
            return False
        with self.library.connect() as db:
            row = db.execute(
                "SELECT status FROM embedding_volume_status WHERE index_id=? AND volume_id=?",
                (status.index_id, volume_id),
            ).fetchone()
        return bool(row and row["status"] == "completed")

    def index_volume(self, series_id: str, volume_id: str, batch_size: int = 32) -> dict[str, object]:
        settings = self.settings_repository.load()
        if not settings.configured:
            raise ValueError("尚未配置可用的向量化模型")
        series = self.library.get_series(series_id)
        if series is None:
            raise LookupError("Series not found")
        volume = next((item for item in series.volumes if item.id == volume_id), None)
        if volume is None:
            raise LookupError("Volume not found")
        index_id = self._ensure_index(series_id, settings)
        chunks = self._sync_volume_chunks(volume)
        source_hash = hashlib.sha256(
            "\0".join(str(item["content_hash"]) for item in chunks).encode("utf-8")
        ).hexdigest()
        with self.library.connect() as db:
            ready = db.execute(
                "SELECT status,source_hash,chunk_count,embedded_count FROM embedding_volume_status "
                "WHERE index_id=? AND volume_id=?",
                (index_id, volume_id),
            ).fetchone()
            if ready and ready["status"] == "completed" and ready["source_hash"] == source_hash:
                self.finalize_series(series_id)
                return {"index_id": index_id, "volume_id": volume_id, "chunks": int(ready["chunk_count"]), "reused": True}
            db.execute(
                "INSERT INTO embedding_volume_status(index_id,volume_id,source_hash,status,chunk_count,embedded_count,error) "
                "VALUES(?,?,?,'running',?,0,NULL) ON CONFLICT(index_id,volume_id) DO UPDATE SET "
                "source_hash=excluded.source_hash,status='running',chunk_count=excluded.chunk_count,embedded_count=0,error=NULL,updated_at=CURRENT_TIMESTAMP",
                (index_id, volume_id, source_hash, len(chunks)),
            )
            db.execute("UPDATE embedding_indexes SET status='indexing',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (index_id,))

        client = EmbeddingClient(settings)
        try:
            missing = self._missing_chunks(index_id, chunks)
            completed = len(chunks) - len(missing)
            for batch in _batches(missing, max(1, min(int(batch_size), 128))):
                vectors = client.embed([self._embedding_text(item) for item in batch])
                dimensions = len(vectors[0])
                self._store_vectors(index_id, batch, vectors, dimensions)
                completed += len(batch)
                with self.library.connect() as db:
                    db.execute(
                        "UPDATE embedding_volume_status SET embedded_count=?,updated_at=CURRENT_TIMESTAMP WHERE index_id=? AND volume_id=?",
                        (completed, index_id, volume_id),
                    )
            with self.library.connect() as db:
                db.execute(
                    "UPDATE embedding_volume_status SET status='completed',embedded_count=?,error=NULL,updated_at=CURRENT_TIMESTAMP "
                    "WHERE index_id=? AND volume_id=?",
                    (len(chunks), index_id, volume_id),
                )
            self.finalize_series(series_id)
            return {"index_id": index_id, "volume_id": volume_id, "chunks": len(chunks), "reused": not missing}
        except Exception as error:
            with self.library.connect() as db:
                db.execute(
                    "UPDATE embedding_volume_status SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE index_id=? AND volume_id=?",
                    (str(error)[:1000], index_id, volume_id),
                )
                db.execute(
                    "UPDATE embedding_indexes SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(error)[:1000], index_id),
                )
            raise

    def finalize_series(self, series_id: str) -> bool:
        series = self.library.get_series(series_id)
        settings = self.settings_repository.load()
        if series is None or not settings.configured:
            return False
        index_id = self._ensure_index(series_id, settings)
        with self.library.connect() as db:
            completed = int(db.execute(
                "SELECT COUNT(*) FROM embedding_volume_status WHERE index_id=? AND status='completed' "
                f"AND volume_id IN ({','.join('?' for _ in series.volumes)})",
                (index_id, *[volume.id for volume in series.volumes]),
            ).fetchone()[0]) if series.volumes else 0
            if completed != len(series.volumes):
                return False
            db.execute("UPDATE embedding_indexes SET active=0 WHERE series_id=?", (series_id,))
            db.execute(
                "UPDATE embedding_indexes SET active=1,status='completed',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (index_id,),
            )
        return True

    def semantic_search(
        self, series_id: str, query: str, limit: int = 20,
        volume_id: str | None = None, volume_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        state = self.status(series_id)
        if not state.available or not state.index_id or not query.strip():
            return []
        settings = self.settings_repository.load()
        query_vector = EmbeddingClient(settings).embed([query.strip()], query=True)[0]
        if len(query_vector) != state.dimensions:
            raise ValueError(f"查询向量维度为 {len(query_vector)}，作品索引维度为 {state.dimensions}，请重新建立索引")
        scoped = [volume_id] if volume_id else list(volume_ids or [])
        params: list[Any] = [state.index_id, series_id]
        volume_clause = ""
        if scoped:
            volume_clause = f" AND v.id IN ({','.join('?' for _ in scoped)})"
            params.extend(scoped)
        with self.library.connect() as db:
            rows = db.execute(
                "SELECT rc.id,rc.start_locator locator,rc.start_locator,rc.end_locator,rc.text,rc.chapter_id,"
                "c.title chapter_title,c.chapter_index,rc.chunk_index source_index,"
                "v.id volume_id,v.number,re.dimensions,re.embedding "
                "FROM rag_embeddings re JOIN rag_chunks rc ON rc.id=re.chunk_id "
                "JOIN chapters c ON c.id=rc.chapter_id JOIN volumes v ON v.id=c.volume_id "
                f"WHERE re.index_id=? AND v.series_id=?{volume_clause}",
                params,
            ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            vector = _unpack_vector(row["embedding"], int(row["dimensions"]))
            score = sum(left * right for left, right in zip(query_vector, vector))
            item = {key: row[key] for key in (
                "id", "locator", "start_locator", "end_locator", "text", "chapter_id",
                "chapter_title", "chapter_index", "source_index", "volume_id", "number",
            )}
            item.update(semantic_score=round(score, 6), retrieval_source="semantic", matched_terms=[])
            scored.append(item)
        scored.sort(key=lambda item: (-float(item["semantic_score"]), float(item["number"]), str(item["locator"])))
        selected, relevance_cutoff = _select_semantic_results(scored, limit)
        for relevance_rank, item in enumerate(selected, start=1):
            item["relevance_rank"] = relevance_rank
            item["relevance_cutoff"] = round(relevance_cutoff, 6)
        selected.sort(key=lambda item: (
            float(item.get("number") or 0), int(item.get("chapter_index") or 0),
            int(item.get("source_index") or 0), str(item.get("locator") or ""),
        ))
        return selected

    def _ensure_index(self, series_id: str, settings: EmbeddingSettings) -> str:
        config_hash = self._config_hash(settings)
        with self.library.connect() as db:
            row = db.execute(
                "SELECT id FROM embedding_indexes WHERE series_id=? AND config_hash=?",
                (series_id, config_hash),
            ).fetchone()
            if row:
                return str(row["id"])
            index_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO embedding_indexes(id,series_id,config_hash,model,base_url,index_version) VALUES(?,?,?,?,?,?)",
                (index_id, series_id, config_hash, settings.model, settings.base_url, EMBEDDING_INDEX_VERSION),
            )
        return index_id

    def _config_hash(self, settings: EmbeddingSettings) -> str:
        return hashlib.sha256(json.dumps({
            "model": settings.model,
            "base_url": settings.base_url,
            "index_version": EMBEDDING_INDEX_VERSION,
        }, sort_keys=True).encode()).hexdigest()

    def _sync_volume_chunks(self, volume: Any) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for chapter_summary in volume.chapters:
            chapter = self.library.get_chapter(chapter_summary.id)
            if chapter is None:
                continue
            for chunk in build_rag_chunks(chapter.id, chapter.paragraphs):
                chunks.append({
                    **chunk.model_dump(),
                    "content_hash": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                    "chapter_title": chapter.title,
                    "volume_title": volume.title,
                    "volume_number": volume.number,
                })
        chapter_ids = [chapter.id for chapter in volume.chapters]
        with self.library.connect() as db:
            if chapter_ids:
                placeholders = ",".join("?" for _ in chapter_ids)
                desired_ids = {str(item["id"]) for item in chunks}
                existing = db.execute(
                    f"SELECT id FROM rag_chunks WHERE chapter_id IN ({placeholders})", chapter_ids,
                ).fetchall()
                for row in existing:
                    if row["id"] not in desired_ids:
                        db.execute("DELETE FROM rag_chunks WHERE id=?", (row["id"],))
            db.executemany(
                "INSERT INTO rag_chunks(id,chapter_id,chunk_index,start_paragraph_id,end_paragraph_id,start_locator,end_locator,text,character_count,approximate_tokens,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "chunk_index=excluded.chunk_index,start_paragraph_id=excluded.start_paragraph_id,end_paragraph_id=excluded.end_paragraph_id,"
                "start_locator=excluded.start_locator,end_locator=excluded.end_locator,text=excluded.text,character_count=excluded.character_count,"
                "approximate_tokens=excluded.approximate_tokens,content_hash=excluded.content_hash",
                [(
                    item["id"], item["chapter_id"], item["chunk_index"], item["start_paragraph_id"], item["end_paragraph_id"],
                    item["start_locator"], item["end_locator"], item["text"], item["character_count"], item["approximate_tokens"], item["content_hash"],
                ) for item in chunks],
            )
        return chunks

    def _missing_chunks(self, index_id: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.library.connect() as db:
            stored = {
                row["chunk_id"]: row["content_hash"] for row in db.execute(
                    "SELECT chunk_id,content_hash FROM rag_embeddings WHERE index_id=?", (index_id,),
                ).fetchall()
            }
        return [item for item in chunks if stored.get(item["id"]) != item["content_hash"]]

    def _embedding_text(self, chunk: dict[str, Any]) -> str:
        return (
            f"作品卷册：{chunk['volume_title']}（第 {chunk['volume_number']} 卷）\n"
            f"章节：{chunk['chapter_title']}\n原文：{chunk['text']}"
        )

    def _store_vectors(
        self, index_id: str, chunks: list[dict[str, Any]], vectors: list[list[float]], dimensions: int,
    ) -> None:
        with self.library.connect() as db:
            existing = int(db.execute("SELECT dimensions FROM embedding_indexes WHERE id=?", (index_id,)).fetchone()[0] or 0)
            if existing and existing != dimensions:
                raise ValueError(f"向量维度发生变化：索引为 {existing}，接口返回 {dimensions}")
            db.execute("UPDATE embedding_indexes SET dimensions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (dimensions, index_id))
            db.executemany(
                "INSERT INTO rag_embeddings(index_id,chunk_id,content_hash,dimensions,embedding) VALUES(?,?,?,?,?) "
                "ON CONFLICT(index_id,chunk_id) DO UPDATE SET content_hash=excluded.content_hash,dimensions=excluded.dimensions,embedding=excluded.embedding,created_at=CURRENT_TIMESTAMP",
                [(
                    index_id, chunk["id"], chunk["content_hash"], dimensions, _pack_vector(vector),
                ) for chunk, vector in zip(chunks, vectors)],
            )


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("向量化接口返回了无法归一化的向量")
    normalized = [value / norm for value in vector]
    if any(not math.isfinite(value) for value in normalized):
        raise ValueError("向量化接口返回了非有限数值")
    return normalized


def _select_semantic_results(
    scored: list[dict[str, Any]], limit: int,
) -> tuple[list[dict[str, Any]], float]:
    """Select a coherent semantic band without padding weak results to Top-N."""
    return _select_ranked_results(
        scored, limit, "semantic_score", minimum_score=0.25, minimum_gap=0.06,
    )


def _select_ranked_results(
    scored: list[dict[str, Any]], limit: int, score_key: str, minimum_score: float = 0.0,
    minimum_gap: float = 0.0,
) -> tuple[list[dict[str, Any]], float]:
    """Select an adaptive relevance band from items already sorted by score."""
    if not scored:
        return [], 0.0
    maximum = max(4, min(int(limit), 24))
    best_score = float(scored[0].get(score_key) or 0)
    strong_cutoff = max(minimum_score, best_score * 0.92)
    strong_count = sum(
        float(item.get(score_key) or 0) >= strong_cutoff
        for item in scored
    )
    relevance_cutoff = strong_cutoff
    if strong_count < 4:
        # A large early score gap usually means the query has only one or two
        # genuinely close matches.  Stop before that gap instead of forcing four.
        gap_threshold = max(minimum_gap, best_score * 0.10)
        natural_boundary = 0
        inspected = min(4, len(scored))
        for index in range(1, inspected):
            previous = float(scored[index - 1].get(score_key) or 0)
            current = float(scored[index].get(score_key) or 0)
            if previous - current >= gap_threshold:
                natural_boundary = index
                break
        target = natural_boundary or min(4, len(scored), maximum)
        if target:
            relevance_cutoff = max(
                minimum_score,
                float(scored[target - 1].get(score_key) or 0),
            )
    qualified = [
        item for item in scored
        if float(item.get(score_key) or 0) >= relevance_cutoff
    ]
    selected = qualified[:maximum]
    if len(qualified) > maximum and selected:
        relevance_cutoff = max(
            relevance_cutoff,
            float(selected[-1].get(score_key) or 0),
        )
    return selected, relevance_cutoff


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(payload: bytes, dimensions: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dimensions}f", payload)


def _batches(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
