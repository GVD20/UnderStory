import os
import secrets
from .desktop_library import catalog, current_library
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
import json
import queue
import threading
import time

from .library import LibraryRepository
from .knowledge import KnowledgeService
from .chat import ChatService
from .models import (
    Chapter,
    ChapterUpdate,
    WikiEntryRecord,
    WikiSpecialCreateRequest,
    WikiState,
    ChatCreateRequest,
    ChatSendRequest,
    ChatSpoilerDecisionRequest,
    ChatThreadDetail,
    ChatThreadRecord,
    ImportResponse,
    LibraryResponse,
    ReadingProgress,
    ReadingMarkerUpdate,
    SearchResponse,
    SearchResult,
    AnalysisStartRequest,
    AnalysisJobRecord,
    SeriesSummary,
    SeriesUpdate,
    ModelSettingsUpdate,
    EmbeddingSettingsUpdate,
)
from .settings import EmbeddingSettingsRepository, SettingsRepository
from .embeddings import EmbeddingService


MAX_UPLOAD_BYTES = 60 * 1024 * 1024


def get_library():
    return current_library.get() or catalog

@lru_cache(maxsize=32)
def knowledge_for(library):
    return KnowledgeService(library)

def get_knowledge():
    return knowledge_for(get_library())

@lru_cache(maxsize=32)
def chat_for(library):
    return ChatService(library, knowledge_for(library))

def get_chat():
    return chat_for(get_library())


def get_settings() -> SettingsRepository:
    library = get_library()
    return SettingsRepository(library.workspace, library.data_dir)


def get_embedding_settings() -> EmbeddingSettingsRepository:
    library = get_library()
    return EmbeddingSettingsRepository(library.workspace, library.data_dir)


app = FastAPI(title="UnderStory API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "null"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/library", response_model=LibraryResponse)
def list_library() -> LibraryResponse:
    return get_library().list_library()


@app.get("/api/series/{series_id}", response_model=SeriesSummary)
def get_series(series_id: str) -> SeriesSummary:
    series = get_library().get_series(series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


@app.delete("/api/series/{series_id}")
def delete_series(series_id: str) -> dict[str, bool]:
    try:
        if not get_library().delete_series(series_id):
            raise HTTPException(status_code=404, detail="Series not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    knowledge_for.cache_clear()
    chat_for.cache_clear()
    return {"deleted": True}


@app.patch("/api/series/{series_id}", response_model=SeriesSummary)
def rename_series(series_id: str, update: SeriesUpdate) -> SeriesSummary:
    try:
        return get_library().rename_series(series_id, update.title)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/settings/model")
def read_model_settings() -> dict[str, object]:
    return get_settings().public()


@app.put("/api/settings/model")
def update_model_settings(update: ModelSettingsUpdate) -> dict[str, object]:
    try:
        result = get_settings().save(update.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    knowledge_for.cache_clear()
    chat_for.cache_clear()
    return result


@app.post("/api/settings/model/models")
def list_configured_models(update: ModelSettingsUpdate) -> dict[str, object]:
    try:
        models = get_settings().list_models(update.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"models": models, "count": len(models)}


@app.post("/api/settings/model/test")
def test_model_connection(update: ModelSettingsUpdate) -> dict[str, object]:
    try:
        model = get_settings().test_connection(update.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "model": model, "message": f"连接成功，{model} 已返回响应"}


@app.get("/api/settings/embedding")
def read_embedding_settings() -> dict[str, object]:
    return get_embedding_settings().public()


@app.put("/api/settings/embedding")
def update_embedding_settings(update: EmbeddingSettingsUpdate) -> dict[str, object]:
    try:
        result = get_embedding_settings().save(update.model_dump())
        [EmbeddingService(work).select_configuration() for work in catalog.works()]
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    knowledge_for.cache_clear()
    chat_for.cache_clear()
    return result


@app.get("/api/settings/embedding/indexes")
def embedding_index_history() -> dict[str, object]:
    indexes = [item for work in catalog.works() for item in EmbeddingService(work).index_history()]
    return {"indexes": indexes, "count": len(indexes)}


@app.post("/api/settings/embedding/models")
def list_embedding_models(update: EmbeddingSettingsUpdate) -> dict[str, object]:
    try:
        models = get_embedding_settings().list_models(update.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"models": models, "count": len(models)}


@app.post("/api/settings/embedding/test")
def test_embedding_connection(update: EmbeddingSettingsUpdate) -> dict[str, object]:
    try:
        model, dimensions = get_embedding_settings().test_connection(update.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"ok": True, "model": model, "dimensions": dimensions, "message": f"连接成功，返回 {dimensions} 维向量"}


@app.get("/api/embedding/{series_id}/status")
def embedding_status(series_id: str) -> dict[str, object]:
    try:
        return EmbeddingService(get_library()).status(series_id).as_dict()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/library/import", response_model=ImportResponse)
async def import_epubs(
    files: list[UploadFile] = File(...),
    series_id: str | None = Form(default=None),
    series_title: str | None = Form(default=None),
    volume_numbers: str | None = Form(default=None),
) -> ImportResponse:
    library = get_library()
    upload_dir = library.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    try:
        for index, upload in enumerate(files, start=1):
            filename = Path(upload.filename or f"volume-{index}.epub").name
            if Path(filename).suffix.casefold() != ".epub":
                raise HTTPException(status_code=400, detail=f"{filename} is not an EPUB file")
            content = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"{filename} is too large")
            target = upload_dir / filename
            target.write_bytes(content)
            saved_paths.append(target)
        parsed_numbers = None
        if volume_numbers is not None:
            try:
                parsed_numbers = json.loads(volume_numbers)
            except json.JSONDecodeError as error:
                raise ValueError("手动卷号格式无效") from error
            if not isinstance(parsed_numbers, list):
                raise ValueError("手动卷号必须是列表")
        series, imported_ids = library.import_paths(
            saved_paths,
            series_id=series_id,
            series_title=series_title,
            volume_numbers=parsed_numbers,
        )
        return ImportResponse(series=series, imported_volumes=imported_ids)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        for path in saved_paths:
            path.unlink(missing_ok=True)


@app.get("/api/chapters/{chapter_id}", response_model=Chapter)
def get_chapter(chapter_id: str) -> Chapter:
    chapter = get_library().get_chapter(chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@app.patch("/api/chapters/{chapter_id}", response_model=Chapter)
def update_chapter(chapter_id: str, update: ChapterUpdate) -> Chapter:
    chapter = get_library().update_chapter_body(chapter_id, update.is_body)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@app.get("/api/search", response_model=SearchResponse)
def search(
    series_id: str,
    q: str = Query(min_length=1, max_length=80),
    limit: int = Query(default=30, ge=1, le=60),
) -> SearchResponse:
    base = get_library().search(series_id, q, limit)
    for item in get_knowledge().search_wiki(series_id, q, limit):
        base.results.append(SearchResult(
            result_type="wiki", chapter_id="", chapter_title=item["title"], volume=0,
            locator=f"wiki://{item['slug']}", paragraph_id=None,
            excerpt=item["excerpt"], match_start=item["match_start"], match_end=item["match_end"],
            title_match=item["title_match"], title_match_kind=item["title_match_kind"],
            matched_title=item["matched_title"],
            theme_color=item["theme_color"], entry_kind=item.get("kind"),
        ))
    base.results = sorted(base.results, key=search_result_priority)[:limit]
    return base


def search_result_priority(item: SearchResult) -> int:
    if item.result_type == "wiki" and item.title_match_kind == "exact_title":
        return 0
    if item.result_type == "wiki" and item.title_match_kind == "exact_alias":
        return 1
    if item.result_type == "chapter" and item.title_match_kind == "exact_title":
        return 2
    if item.result_type == "wiki" and item.title_match_kind == "partial_title":
        return 3
    if item.result_type == "chapter" and item.title_match_kind == "partial_title":
        return 4
    return 5 if item.result_type == "wiki" else 6


@app.get("/api/volumes/{volume_id}/cover")
def get_volume_cover(volume_id: str) -> FileResponse:
    cover = get_library().get_cover(volume_id)
    if cover is None:
        raise HTTPException(status_code=404, detail="Cover not found")
    path, media_type = cover
    return FileResponse(path, media_type=media_type)


@app.get("/api/volumes/{volume_id}/resource")
def get_volume_resource(volume_id: str, path: str = Query(...)) -> Response:
    resource = get_library().get_epub_resource(volume_id, path)
    if resource is None:
        raise HTTPException(status_code=404, detail="EPUB resource not found")
    content, media_type = resource
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/progress/{series_id}", response_model=ReadingProgress | None)
def get_progress(series_id: str) -> ReadingProgress | None:
    return get_library().get_progress(series_id)


@app.put("/api/progress/{series_id}", response_model=ReadingProgress)
def save_progress(series_id: str, progress: ReadingProgress) -> ReadingProgress:
    if progress.series_id != series_id:
        raise HTTPException(status_code=400, detail="Series id does not match request path")
    return get_library().save_progress(progress)


@app.get("/api/reading-marker/{series_id}")
def get_reading_marker(series_id: str):
    library = get_library()
    path = library.folder / "reading-marker.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schemaVersion": 1, "completedChapterId": None, "updatedAt": None}


@app.put("/api/reading-marker/{series_id}")
def save_reading_marker(series_id: str, value: ReadingMarkerUpdate):
    from datetime import datetime, timezone
    from .desktop_library import atomic_json
    library = get_library()
    if value.completedChapterId is not None and library.get_chapter(value.completedChapterId) is None:
        raise HTTPException(422, "章节不存在")
    record = {"schemaVersion": 1, "completedChapterId": value.completedChapterId, "updatedAt": datetime.now(timezone.utc).isoformat()}
    atomic_json(library.folder / "reading-marker.json", record)
    return record


@app.post("/api/analysis/{series_id}/start", response_model=AnalysisJobRecord)
def start_analysis(series_id: str, request: AnalysisStartRequest) -> AnalysisJobRecord:
    try:
        return get_knowledge().start_analysis(series_id, request.mode, request.vectorize)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/analysis/{series_id}/latest", response_model=AnalysisJobRecord | None)
def latest_analysis(series_id: str) -> AnalysisJobRecord | None:
    return get_knowledge().latest_analysis_job(series_id)


@app.get("/api/analysis/jobs/{job_id}", response_model=AnalysisJobRecord)
def analysis_job(job_id: str) -> AnalysisJobRecord:
    try:
        return get_knowledge().get_analysis_job(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/analysis/jobs/{job_id}/pause", response_model=AnalysisJobRecord)
def pause_analysis_job(job_id: str) -> AnalysisJobRecord:
    try:
        return get_knowledge().request_analysis_pause(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/analysis/jobs/{job_id}/logs")
def analysis_job_logs(job_id: str, limit: int = Query(default=500, ge=20, le=1000)) -> dict:
    try:
        return {"logs": get_knowledge().analysis_logs(job_id, limit)}
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/wiki/{series_id}", response_model=WikiState)
def wiki_state(series_id: str) -> WikiState:
    if get_library().get_series(series_id) is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return get_knowledge().wiki_state(series_id)


@app.get("/api/wiki/{series_id}/entries/{slug}", response_model=WikiEntryRecord)
def wiki_entry(series_id: str, slug: str) -> WikiEntryRecord:
    entry = get_knowledge().get_entry(series_id, slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="Wiki entry not found")
    return entry


@app.get("/api/wiki/{series_id}/mention-index")
def wiki_mention_index(series_id: str) -> dict:
    if get_library().get_series(series_id) is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return {"entries": get_knowledge().wiki_mention_index(series_id)}


@app.post("/api/wiki/{series_id}/specials", response_model=WikiEntryRecord)
def create_special(series_id: str, request: WikiSpecialCreateRequest) -> WikiEntryRecord:
    try:
        return get_knowledge().create_special(series_id, request.title, request.introduction, request.related_entry_slugs)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/wiki/{series_id}/specials/{slug}")
def delete_special(series_id: str, slug: str) -> dict[str, bool]:
    if not get_knowledge().delete_special(series_id, slug):
        raise HTTPException(status_code=404, detail="User special not found")
    return {"deleted": True}


@app.post("/api/chat/threads", response_model=ChatThreadDetail)
def create_chat_thread(request: ChatCreateRequest) -> ChatThreadDetail:
    try:
        return get_chat().create_thread(request)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/chat/threads", response_model=list[ChatThreadRecord])
def list_chat_threads(series_id: str) -> list[ChatThreadRecord]:
    return get_chat().list_threads(series_id)


@app.get("/api/chat/threads/{thread_id}", response_model=ChatThreadDetail)
def get_chat_thread(thread_id: str) -> ChatThreadDetail:
    try:
        return get_chat().get_thread(thread_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/chat/threads/{thread_id}")
def delete_chat_thread(thread_id: str) -> dict[str, bool]:
    if not get_chat().delete_thread(thread_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"deleted": True}


@app.post("/api/chat/threads/{thread_id}/messages", response_model=ChatThreadDetail)
def send_chat_message(thread_id: str, request: ChatSendRequest) -> ChatThreadDetail:
    try:
        return get_chat().send(thread_id, request)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Chat agent failed: {type(error).__name__}") from error


@app.post("/api/chat/threads/{thread_id}/messages/stream")
def stream_chat_message(thread_id: str, request: ChatSendRequest) -> StreamingResponse:
    service = get_chat()
    def event_stream():
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def callback(event: dict[str, Any]) -> None:
            if event.get("event_type") == "chat_initial":
                content = str(event.get("label") or "")
                events.put(("initial_start", None))
                for index in range(0, len(content), 9):
                    events.put(("initial_delta", content[index:index + 9]))
                    time.sleep(0.01)
                events.put(("initial_end", None))
                return
            events.put(("status", event))

        def worker() -> None:
            try:
                detail = service.send(thread_id, request, callback)
                if detail.pending_spoiler_confirmation:
                    events.put(("confirmation_required", detail.pending_spoiler_confirmation.model_dump()))
                    events.put(("complete", detail.model_dump()))
                    return
                answer = detail.messages[-1].content if detail.messages else ""
                events.put(("answer_start", {
                    "thinking_seconds": detail.messages[-1].thinking_seconds if detail.messages else 0,
                }))
                for index in range(0, len(answer), 18):
                    events.put(("delta", answer[index:index + 18]))
                    time.sleep(0.012)
                events.put(("complete", detail.model_dump()))
            except Exception as error:
                events.put(("error", {"message": f"{type(error).__name__}: {str(error)[:300]}"}))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event_name, payload = events.get()
            if event_name == "done":
                break
            yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/chat/threads/{thread_id}/spoiler-confirmation/stream")
def resolve_chat_spoiler_confirmation(
    thread_id: str, request: ChatSpoilerDecisionRequest,
) -> StreamingResponse:
    service = get_chat()
    def event_stream():
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def callback(event: dict[str, Any]) -> None:
            if event.get("event_type") == "chat_initial":
                content = str(event.get("label") or "")
                events.put(("initial_start", None))
                for index in range(0, len(content), 9):
                    events.put(("initial_delta", content[index:index + 9]))
                    time.sleep(0.01)
                events.put(("initial_end", None))
                return
            events.put(("status", event))

        def worker() -> None:
            try:
                detail = service.resolve_spoiler_confirmation(thread_id, request, callback)
                if request.decision != "stop":
                    answer = detail.messages[-1].content if detail.messages else ""
                    events.put(("answer_start", {
                        "thinking_seconds": detail.messages[-1].thinking_seconds if detail.messages else 0,
                    }))
                    for index in range(0, len(answer), 18):
                        events.put(("delta", answer[index:index + 18]))
                        time.sleep(0.012)
                events.put(("complete", detail.model_dump()))
            except Exception as error:
                events.put(("error", {"message": f"{type(error).__name__}: {str(error)[:300]}"}))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()
        while True:
            event_name, payload = events.get()
            if event_name == "done":
                break
            yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.middleware("http")
async def desktop_request(request, call_next):
    token = os.environ.get("UNDERSTORY_TOKEN", "")
    if token and not secrets.compare_digest(request.headers.get("X-UnderStory-Token", ""), token):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    path = request.url.path.split("/")
    identifier = request.query_params.get("series_id")
    if len(path) > 3:
        if path[2] in ("series", "chapters", "volumes", "progress", "reading-marker", "wiki", "state", "embedding"): identifier = path[3]
        elif path[2] == "analysis": identifier = path[4] if path[3] == "jobs" and len(path)>4 else path[3]
        elif path[2] == "chat" and len(path)>4: identifier = path[4]
    if request.url.path == "/api/chat/threads" and request.method == "POST":
        identifier = (await request.json()).get("series_id")
    library = catalog.locate(identifier) if identifier else None
    if identifier and library is None:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "作品或记录不存在"}, status_code=404)
    if library:
        try: library.refresh_source()
        except (ValueError, KeyError, OSError) as error:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": str(error)}, status_code=422)
    selection = current_library.set(library)
    try:
        return await call_next(request)
    finally:
        current_library.reset(selection)

@app.get("/api/state/{series_id}")
def read_state(series_id: str):
    library = get_library()
    if library is catalog: raise HTTPException(404, "Work not found")
    with library.connect() as db:
        row = db.execute("SELECT value FROM desktop_state WHERE key='ui'").fetchone()
        return json.loads(row['value']) if row else {}

@app.put("/api/state/{series_id}")
def write_state(series_id: str, value: dict):
    library = get_library()
    if library is catalog: raise HTTPException(404, "Work not found")
    with library.connect() as db:
        db.execute("INSERT INTO desktop_state VALUES ('ui',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False),))
    return {"ok": True}

@app.post("/api/credentials")
def set_credentials(value: dict):
    from .settings import credential_cache
    for name in ("model", "embedding"):
        if name in value: credential_cache[name] = str(value[name])
    return {"ok": True}
