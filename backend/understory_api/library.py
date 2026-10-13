from __future__ import annotations

import json
import hashlib
import math
import mimetypes
import re
import shutil
import sqlite3
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import quote

from .epub_importer import ImportedVolume, format_volume_number, import_epub
from .models import (
    Chapter,
    ChapterOverview,
    ChapterSummary,
    LibraryResponse,
    Paragraph,
    ReadingProgress,
    SearchResponse,
    SearchResult,
    SeriesSummary,
    VolumeSummary,
)

def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if slug:
        return slug
    digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:10]
    return f"series-{digest}"


class LibraryRepository:
    def __init__(
        self,
        workspace: Path | None = None,
        db_path: Path | None = None,
        storage_dir: Path | None = None,
    ) -> None:
        self.workspace = workspace or Path(__file__).resolve().parents[3]
        self.data_dir = storage_dir or self.workspace / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or self.data_dir / "lightrocket.db"
        self.source_dir = self.data_dir / "library" / "sources"
        self.asset_dir = self.data_dir / "library" / "assets"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS series (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    subtitle TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS volumes (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    character_count INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    cover_path TEXT,
                    cover_media_type TEXT,
                    UNIQUE(series_id, number)
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    id TEXT PRIMARY KEY,
                    volume_id TEXT NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
                    chapter_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    is_body INTEGER NOT NULL,
                    character_count INTEGER NOT NULL,
                    paragraph_count INTEGER NOT NULL,
                    UNIQUE(volume_id, chapter_index)
                );
                CREATE TABLE IF NOT EXISTS paragraphs (
                    id TEXT PRIMARY KEY,
                    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                    paragraph_index INTEGER NOT NULL,
                    locator TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    html TEXT NOT NULL,
                    role TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS paragraphs_chapter_idx
                    ON paragraphs(chapter_id, paragraph_index);
                CREATE TABLE IF NOT EXISTS chapter_overviews (
                    chapter_id TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    intro TEXT,
                    overview TEXT,
                    people_json TEXT NOT NULL DEFAULT '[]',
                    events_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS reading_progress (
                    series_id TEXT PRIMARY KEY REFERENCES series(id) ON DELETE CASCADE,
                    chapter_id TEXT NOT NULL,
                    paragraph_id TEXT,
                    progress REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            overview_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(chapter_overviews)").fetchall()
            }
            if "places_json" not in overview_columns:
                db.execute("ALTER TABLE chapter_overviews ADD COLUMN places_json TEXT NOT NULL DEFAULT '[]'")
            if "concepts_json" not in overview_columns:
                db.execute("ALTER TABLE chapter_overviews ADD COLUMN concepts_json TEXT NOT NULL DEFAULT '[]'")
            for table in ('series', 'volumes'):
                if 'authors_json' not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN authors_json TEXT NOT NULL DEFAULT '[]'")

    def _has_series(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM series LIMIT 1").fetchone())

    def import_paths(
        self,
        paths: list[Path],
        series_id: str | None = None,
        series_title: str | None = None,
        subtitle: str = "本地 EPUB 书库",
        token_counts: dict[int | float, int] | None = None,
        volume_numbers: list[int | float] | None = None,
    ) -> tuple[SeriesSummary, list[str]]:
        if not paths:
            raise ValueError("No EPUB files were supplied")
        imported = [import_epub(path, index) for index, path in enumerate(paths, start=1)]
        if volume_numbers is not None:
            if len(volume_numbers) != len(imported):
                raise ValueError("手动卷号数量与 EPUB 文件数量不一致")
            for volume, value in zip(imported, volume_numbers, strict=True):
                try:
                    numeric = float(value)
                except (TypeError, ValueError) as error:
                    raise ValueError("卷号必须是有效数字") from error
                if not math.isfinite(numeric) or numeric <= 0:
                    raise ValueError("卷号必须是大于 0 的有效数字")
                volume.number = int(numeric) if numeric.is_integer() else numeric
        resolved_title = series_title or imported[0].title
        resolved_id = series_id or slugify(resolved_title)
        imported_numbers = [volume.number for volume in imported]
        if len(imported_numbers) != len(set(imported_numbers)):
            raise ValueError("导入文件包含重复卷号，请检查 EPUB 文件名中的卷号")
        imported_ids: list[str] = []
        with self.connect() as db:
            existing = db.execute('SELECT authors_json FROM series WHERE id=?', (resolved_id,)).fetchone()
            authors = list(dict.fromkeys([*(json.loads(existing['authors_json']) if existing else []), *(author for volume in imported for author in volume.authors)]))
            existing_numbers = {
                row["number"]
                for row in db.execute(
                    "SELECT number FROM volumes WHERE series_id=?", (resolved_id,)
                ).fetchall()
            }
            conflicts = sorted(existing_numbers.intersection(imported_numbers))
            if conflicts:
                labels = "、".join(str(number) for number in conflicts)
                raise ValueError(f"第 {labels} 卷已经存在；作品内导入只会新增卷，不会覆盖已有内容")
            db.execute(
                "INSERT INTO series(id, title, subtitle) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title",
                (resolved_id, resolved_title, subtitle),
            )
            db.execute('UPDATE series SET authors_json=? WHERE id=?', (json.dumps(authors, ensure_ascii=False), resolved_id))
            for source_path, volume in zip(paths, imported, strict=True):
                imported_ids.append(self._store_volume(db, resolved_id, source_path, volume, token_counts or {}))
        series = self.get_series(resolved_id)
        if series is None:
            raise RuntimeError("Imported series could not be read back")
        return series, imported_ids

    def _store_volume(
        self,
        db: sqlite3.Connection,
        series_id: str,
        source_path: Path,
        volume: ImportedVolume,
        token_counts: dict[int | float, int],
    ) -> str:
        number_label = format_volume_number(volume.number)
        volume_id = f"{series_id}-v{number_label}"
        permanent_source = self.source_dir / series_id / f"v{number_label}.epub"
        permanent_source.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != permanent_source.resolve():
            shutil.copy2(source_path, permanent_source)
        elif not permanent_source.exists():
            shutil.copy2(source_path, permanent_source)

        cover_path: str | None = None
        if volume.cover_bytes:
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }.get(volume.cover_media_type or "", ".bin")
            cover_file = self.asset_dir / series_id / f"v{number_label}{extension}"
            cover_file.parent.mkdir(parents=True, exist_ok=True)
            cover_file.write_bytes(volume.cover_bytes)
            cover_path = str(cover_file)

        character_count = sum(sum(len(paragraph.text) for paragraph in chapter.paragraphs) for chapter in volume.chapters)
        token_count = token_counts.get(volume.number, round(character_count * 0.62))
        db.execute(
            "INSERT INTO volumes(id, series_id, number, title, token_count, character_count, source_path, cover_path, cover_media_type, authors_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                volume_id,
                series_id,
                volume.number,
                volume.title,
                token_count,
                character_count,
                str(permanent_source),
                cover_path,
                volume.cover_media_type,
                json.dumps(volume.authors, ensure_ascii=False),
            ),
        )
        for chapter_index, chapter in enumerate(volume.chapters, start=1):
            chapter_id = f"{volume_id}-c{chapter_index:02d}"
            paragraph_count = len(chapter.paragraphs)
            chapter_characters = sum(len(paragraph.text) for paragraph in chapter.paragraphs)
            db.execute(
                "INSERT INTO chapters(id, volume_id, chapter_index, title, kind, is_body, character_count, paragraph_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chapter_id,
                    volume_id,
                    chapter_index,
                    chapter.title,
                    chapter.kind,
                    int(chapter.is_body),
                    chapter_characters,
                    paragraph_count,
                ),
            )
            overview_status = "pending" if chapter.is_body else "skipped"
            db.execute(
                "INSERT INTO chapter_overviews(chapter_id, status) VALUES (?, ?)",
                (chapter_id, overview_status),
            )
            for paragraph_index, paragraph in enumerate(chapter.paragraphs, start=1):
                paragraph_id = f"{chapter_id}-p{paragraph_index:04d}"
                locator = (
                    f"lr://series/{series_id}/v{number_label}/"
                    f"c{chapter_index:02d}/p{paragraph_index:04d}"
                )
                db.execute(
                    "INSERT INTO paragraphs(id, chapter_id, paragraph_index, locator, text, html, role) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (paragraph_id, chapter_id, paragraph_index, locator, paragraph.text, paragraph.html, paragraph.role),
                )
        return volume_id

    def list_library(self) -> LibraryResponse:
        with self.connect() as db:
            ids = [row["id"] for row in db.execute("SELECT id FROM series ORDER BY created_at")]
        return LibraryResponse(series=[series for series_id in ids if (series := self.get_series(series_id))])

    def delete_series(self, series_id: str) -> bool:
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM series WHERE id=?", (series_id,)).fetchone()
            if not exists:
                return False
            db.execute("DELETE FROM series WHERE id=?", (series_id,))
        for base in (self.source_dir, self.asset_dir):
            target = (base / series_id).resolve()
            if target.is_relative_to(base.resolve()) and target.exists():
                shutil.rmtree(target)
        return True

    def rename_series(self, series_id: str, title: str) -> SeriesSummary:
        clean = title.strip()
        if not clean:
            raise ValueError("作品名称不能为空")
        with self.connect() as db:
            result = db.execute("UPDATE series SET title=? WHERE id=?", (clean, series_id))
            if result.rowcount == 0:
                raise LookupError("Series not found")
        series = self.get_series(series_id)
        if series is None:
            raise LookupError("Series not found")
        return series

    def reparse_series(self, series_id: str) -> SeriesSummary:
        """Rebuild deterministic chapter structure from preserved source EPUBs."""
        with self.connect() as db:
            series_row = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
            volume_rows = db.execute(
                "SELECT * FROM volumes WHERE series_id=? ORDER BY number", (series_id,),
            ).fetchall()
        if not series_row:
            raise LookupError("Series not found")
        parsed = [
            (row, Path(row["source_path"]), import_epub(Path(row["source_path"]), row["number"]))
            for row in volume_rows
        ]
        with self.connect() as db:
            for row, source_path, volume in parsed:
                db.execute("DELETE FROM volumes WHERE id=?", (row["id"],))
                self._store_volume(
                    db, series_id, source_path, volume,
                    {row["number"]: int(row["token_count"])},
                )
        result = self.get_series(series_id)
        if result is None:
            raise RuntimeError("Reparsed series could not be read back")
        return result

    def get_series(self, series_id: str) -> SeriesSummary | None:
        with self.connect() as db:
            series_row = db.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
            if not series_row:
                return None
            volume_rows = db.execute(
                "SELECT * FROM volumes WHERE series_id = ? ORDER BY number", (series_id,)
            ).fetchall()
            volumes: list[VolumeSummary] = []
            for volume in volume_rows:
                chapter_rows = db.execute(
                    "SELECT c.*, o.status AS overview_status FROM chapters c "
                    "JOIN chapter_overviews o ON o.chapter_id = c.id "
                    "WHERE c.volume_id = ? ORDER BY c.chapter_index",
                    (volume["id"],),
                ).fetchall()
                chapters = [self._chapter_summary(row, volume["number"]) for row in chapter_rows]
                volumes.append(
                    VolumeSummary(
                        id=volume["id"],
                        number=volume["number"],
                        title=volume["title"],
                        authors=json.loads(volume['authors_json']),
                        token_count=volume["token_count"],
                        character_count=volume["character_count"],
                        cover_url=f"/api/volumes/{volume['id']}/cover" if volume["cover_path"] else None,
                        chapters=chapters,
                    )
                )
            return SeriesSummary(
                id=series_row["id"],
                title=series_row["title"],
                subtitle=series_row["subtitle"],
                authors=json.loads(series_row['authors_json']),
                total_tokens=sum(volume.token_count for volume in volumes),
                total_characters=sum(volume.character_count for volume in volumes),
                volumes=volumes,
            )

    @staticmethod
    def _chapter_summary(row: sqlite3.Row, volume_number: int | float) -> ChapterSummary:
        return ChapterSummary(
            id=row["id"],
            volume=volume_number,
            index=row["chapter_index"],
            title=row["title"],
            kind=row["kind"],
            is_body=bool(row["is_body"]),
            character_count=row["character_count"],
            paragraph_count=row["paragraph_count"],
            overview_status=row["overview_status"],
        )

    def get_chapter(self, chapter_id: str) -> Chapter | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT c.*, v.number AS volume_number, o.status AS overview_status, "
                "o.intro, o.overview, o.people_json, o.events_json, o.places_json, o.concepts_json "
                "FROM chapters c JOIN volumes v ON v.id = c.volume_id "
                "JOIN chapter_overviews o ON o.chapter_id = c.id WHERE c.id = ?",
                (chapter_id,),
            ).fetchone()
            if not row:
                return None
            paragraph_rows = db.execute(
                "SELECT * FROM paragraphs WHERE chapter_id = ? ORDER BY paragraph_index",
                (chapter_id,),
            ).fetchall()
            summary = self._chapter_summary(row, row["volume_number"])
            return Chapter(
                **summary.model_dump(),
                paragraphs=[
                    Paragraph(
                        id=item["id"], locator=item["locator"], text=item["text"],
                        html=self._reader_html(item["html"], row["volume_id"]),
                        role=item["role"] or "paragraph",
                    )
                    for item in paragraph_rows
                ],
                chapter_overview=ChapterOverview(
                    status=row["overview_status"],
                    intro=row["intro"],
                    overview=row["overview"],
                    people=json.loads(row["people_json"]),
                    events=json.loads(row["events_json"]),
                    places=json.loads(row["places_json"]),
                    concepts=json.loads(row["concepts_json"]),
                ),
            )

    @staticmethod
    def _reader_html(value: str | None, volume_id: str) -> str | None:
        if not value:
            return None
        prefix = f"/api/volumes/{volume_id}/resource?path="
        return re.sub(
            r"epub-resource:([^\"']+)",
            lambda match: prefix + quote(match.group(1), safe="/"),
            value,
        )

    def get_epub_resource(self, volume_id: str, member_path: str) -> tuple[bytes, str] | None:
        with self.connect() as db:
            row = db.execute("SELECT source_path FROM volumes WHERE id=?", (volume_id,)).fetchone()
        member = PurePosixPath(member_path)
        if not row or member.is_absolute() or ".." in member.parts:
            return None
        try:
            with zipfile.ZipFile(Path(row["source_path"])) as archive:
                info = archive.getinfo(member.as_posix())
                if info.file_size > 30 * 1024 * 1024:
                    return None
                media_type = mimetypes.guess_type(info.filename)[0] or "application/octet-stream"
                if not (media_type.startswith("image/") or media_type.startswith("font/")):
                    return None
                return archive.read(info), media_type
        except (OSError, KeyError, zipfile.BadZipFile):
            return None

    def update_chapter_body(self, chapter_id: str, is_body: bool) -> Chapter | None:
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
            if not exists:
                return None
            db.execute("UPDATE chapters SET is_body = ? WHERE id = ?", (int(is_body), chapter_id))
        return self.get_chapter(chapter_id)

    def search(self, series_id: str, query: str, limit: int = 30) -> SearchResponse:
        needle = query.strip()
        if not needle:
            return SearchResponse(query=query, results=[])
        with self.connect() as db:
            chapter_rows = db.execute(
                "SELECT c.id AS chapter_id, c.title AS chapter_title, v.number AS volume_number, "
                "p.id AS paragraph_id, p.locator, o.intro "
                "FROM chapters c JOIN volumes v ON v.id = c.volume_id "
                "LEFT JOIN paragraphs p ON p.chapter_id = c.id AND p.paragraph_index = 1 "
                "LEFT JOIN chapter_overviews o ON o.chapter_id = c.id "
                "WHERE v.series_id = ? AND c.title LIKE ? "
                "ORDER BY v.number, c.chapter_index LIMIT ?",
                (series_id, f"%{needle}%", limit),
            ).fetchall()
            remaining = max(0, limit - len(chapter_rows))
            paragraph_rows = db.execute(
                "SELECT p.*, c.title AS chapter_title, c.id AS chapter_id, v.number AS volume_number "
                "FROM paragraphs p JOIN chapters c ON c.id = p.chapter_id "
                "JOIN volumes v ON v.id = c.volume_id "
                "WHERE v.series_id = ? AND p.text LIKE ? ORDER BY v.number, c.chapter_index, p.paragraph_index LIMIT ?",
                (series_id, f"%{needle}%", remaining),
            ).fetchall() if remaining else []
        results = [
            SearchResult(
                result_type="chapter",
                chapter_id=row["chapter_id"],
                chapter_title=row["chapter_title"],
                volume=row["volume_number"],
                locator=row["locator"] or f"lr://series/{series_id}/v{format_volume_number(row['volume_number'])}",
                paragraph_id=row["paragraph_id"],
                excerpt=row["intro"] or "从本章标题进入对应正文。",
                match_start=max(0, row["chapter_title"].casefold().find(needle.casefold())),
                match_end=max(0, row["chapter_title"].casefold().find(needle.casefold())) + len(needle),
                title_match=True,
                title_match_kind=(
                    "exact_title" if row["chapter_title"].strip().casefold() == needle.casefold()
                    else "partial_title"
                ),
            )
            for row in chapter_rows
        ]
        for row in paragraph_rows:
            start = row["text"].casefold().find(needle.casefold())
            excerpt_start = max(0, start - 34)
            excerpt_end = min(len(row["text"]), start + len(needle) + 58)
            results.append(
                SearchResult(
                    chapter_id=row["chapter_id"],
                    chapter_title=row["chapter_title"],
                    volume=row["volume_number"],
                    locator=row["locator"],
                    paragraph_id=row["id"],
                    excerpt=row["text"][excerpt_start:excerpt_end],
                    match_start=start - excerpt_start,
                    match_end=start - excerpt_start + len(needle),
                )
            )
        return SearchResponse(query=query, results=results)

    def get_cover(self, volume_id: str) -> tuple[Path, str] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT cover_path, cover_media_type FROM volumes WHERE id = ?", (volume_id,)
            ).fetchone()
        if not row or not row["cover_path"]:
            return None
        return Path(row["cover_path"]), row["cover_media_type"]

    def get_progress(self, series_id: str) -> ReadingProgress | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM reading_progress WHERE series_id = ?", (series_id,)
            ).fetchone()
        return ReadingProgress(**dict(row)) if row else None

    def save_progress(self, progress: ReadingProgress) -> ReadingProgress:
        with self.connect() as db:
            db.execute(
                "INSERT INTO reading_progress(series_id, chapter_id, paragraph_id, progress) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(series_id) DO UPDATE SET chapter_id=excluded.chapter_id, "
                "paragraph_id=excluded.paragraph_id, progress=excluded.progress, updated_at=CURRENT_TIMESTAMP",
                (progress.series_id, progress.chapter_id, progress.paragraph_id, progress.progress),
            )
        return progress
