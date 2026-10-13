"""One independent database and human-readable source tree per work."""
from __future__ import annotations
import json
import os
import sqlite3
import threading
import shutil
import time
from contextvars import ContextVar
from pathlib import Path
from .library import LibraryRepository, slugify
from .models import LibraryResponse

current_library: ContextVar[LibraryRepository | None] = ContextVar('library', default=None)

def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)

class WorkRepository(LibraryRepository):
    def __init__(self, folder: Path):
        self.folder = folder
        super().__init__(workspace=folder, db_path=folder / 'work.sqlite3', storage_dir=folder)
        self.source_dir = folder / 'sources'
        self.asset_dir = folder / 'assets'
        self.source_dir.mkdir(exist_ok=True)
        self.asset_dir.mkdir(exist_ok=True)
        self.restore_source_cache()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS desktop_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.export_lock = threading.RLock()
        self.restore_relative_paths()
        self.source_stamps = {}
        self.refresh_source()
        self.recover_authors()
        with self.connect() as db:
            db.execute('PRAGMA user_version=1')

    def restore_source_cache(self):
        metadata = self.folder / 'book.json'
        if not metadata.exists(): return
        with self.connect() as db:
            if db.execute('SELECT 1 FROM series').fetchone(): return
            book = json.loads(metadata.read_text(encoding='utf-8'))
            if book.get('schemaVersion') != 1: raise ValueError('Unsupported book schema')
            db.execute('INSERT INTO series(id,title,subtitle) VALUES(?,?,?)', (book['id'], book['title'], book.get('subtitle', '')))
            db.execute('UPDATE series SET authors_json=? WHERE id=?', (json.dumps(book.get('authors', []), ensure_ascii=False), book['id']))
            for entry in book['volumes']:
                source = (self.folder / entry['file']).resolve()
                if not source.is_relative_to(self.folder.resolve()): raise ValueError('Invalid volume file path')
                volume = json.loads(source.read_text(encoding='utf-8'))
                db.execute('INSERT INTO volumes(id,series_id,number,title,token_count,character_count,source_path,cover_path,cover_media_type) VALUES(?,?,?,?,?,?,?,?,?)', (volume['id'], book['id'], volume['number'], volume['title'], volume['token_count'], volume['character_count'], volume['source_path'], volume.get('cover_path'), volume.get('cover_media_type')))
                db.execute('UPDATE volumes SET authors_json=? WHERE id=?', (json.dumps(volume.get('authors', []), ensure_ascii=False), volume['id']))
                for chapter in volume['chapters']:
                    db.execute('INSERT INTO chapters(id,volume_id,chapter_index,title,kind,is_body,character_count,paragraph_count) VALUES(?,?,?,?,?,?,?,?)', tuple(chapter[key] for key in ('id','volume_id','chapter_index','title','kind','is_body','character_count','paragraph_count')))
                    db.execute('INSERT INTO chapter_overviews(chapter_id,status) VALUES(?,?)', (chapter['id'], 'pending' if chapter['is_body'] else 'skipped'))
                    for paragraph in chapter['paragraphs']:
                        db.execute('INSERT INTO paragraphs(id,chapter_id,paragraph_index,locator,text,html,role) VALUES(?,?,?,?,?,?,?)', tuple(paragraph[key] for key in ('id','chapter_id','paragraph_index','locator','text','html','role')))

    def refresh_source(self):
        """JSON is the editable source; SQLite body rows are a searchable cache.

        Existing stable IDs must be retained, so generated evidence stays valid.
        Structural additions are made by the EPUB importer.
        """
        with self.export_lock:
            metadata = self.folder / 'book.json'
            if metadata.exists():
                value = json.loads(metadata.read_text(encoding='utf-8'))
                if value.get('schemaVersion') != 1: raise ValueError('Unsupported book schema')
                with self.connect() as db:
                    db.execute('UPDATE series SET title=?,subtitle=? WHERE id=?', (value['title'], value.get('subtitle', ''), value['id']))
                    if 'authors' in value:
                        db.execute('UPDATE series SET authors_json=? WHERE id=?', (json.dumps(value['authors'], ensure_ascii=False), value['id']))
            for source in (self.folder / 'volumes').glob('*.json'):
                stamp = source.stat().st_mtime_ns
                if self.source_stamps.get(source) == stamp: continue
                volume = json.loads(source.read_text(encoding='utf-8'))
                if volume.get('schemaVersion') != 1: raise ValueError('Unsupported volume schema')
                with self.connect() as db:
                    if 'authors' in volume:
                        db.execute('UPDATE volumes SET authors_json=? WHERE id=?', (json.dumps(volume['authors'], ensure_ascii=False), volume['id']))
                    for chapter in volume['chapters']:
                        stored = db.execute('SELECT id,locator FROM paragraphs WHERE chapter_id=? ORDER BY paragraph_index', (chapter['id'],)).fetchall()
                        paragraphs = chapter['paragraphs']
                        if [(p['id'], p['locator']) for p in paragraphs] != [(p['id'], p['locator']) for p in stored]:
                            raise ValueError('修改正文时请保留段落 ID、顺序和 locator；新增卷请使用导入功能')
                        for paragraph in paragraphs:
                            db.execute('UPDATE paragraphs SET text=?,html=?,role=? WHERE id=?', (paragraph['text'], paragraph.get('html', ''), paragraph.get('role', 'paragraph'), paragraph['id']))
                        count = sum(len(p['text']) for p in paragraphs)
                        db.execute('UPDATE chapters SET title=?,is_body=?,character_count=? WHERE id=?', (chapter['title'], int(chapter['is_body']), count, chapter['id']))
                    count = sum(sum(len(p['text']) for p in c['paragraphs']) for c in volume['chapters'])
                    db.execute('UPDATE volumes SET title=?,character_count=?,token_count=? WHERE id=?', (volume['title'], count, round(count * .62), volume['id']))
                self.source_stamps[source] = stamp

    def restore_relative_paths(self):
        with self.connect() as db:
            for row in db.execute('SELECT id,series_id,number,source_path,cover_path FROM volumes').fetchall():
                source = self.source_dir / row['series_id'] / Path(row['source_path']).name
                cover = self.asset_dir / row['series_id'] / Path(row['cover_path']).name if row['cover_path'] else None
                db.execute('UPDATE volumes SET source_path=?,cover_path=? WHERE id=?', (str(source), str(cover) if cover else None, row['id']))

    def import_paths(self, *args, **kwargs):
        result = super().import_paths(*args, **kwargs)
        self.export_source()
        return result

    def delete_series(self, series_id):
        with self.connect() as db:
            try:
                active = db.execute("SELECT 1 FROM analysis_runs WHERE status IN ('pending','running') LIMIT 1").fetchone()
            except sqlite3.OperationalError: active = None
            if active: raise ValueError('请先暂停作品分析，再移除作品')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        trash = self.folder.parent / '.trash'
        trash.mkdir(exist_ok=True)
        shutil.move(str(self.folder), str(trash / f'{self.folder.name}-{time.time_ns()}'))
        catalog.repositories.pop(series_id, None)
        return True

    def rename_series(self, *args, **kwargs):
        result = super().rename_series(*args, **kwargs)
        self.export_metadata(result.id)
        return result

    def update_chapter_body(self, *args, **kwargs):
        result = super().update_chapter_body(*args, **kwargs)
        self.export_source()
        return result

    def export_metadata(self, series_id):
        series = self.get_series(series_id)
        atomic_json(self.folder / 'book.json', {'schemaVersion': 1, 'id': series.id, 'title': series.title, 'subtitle': series.subtitle, 'authors': series.authors, 'volumes': [{'id': v.id, 'number': v.number, 'file': f'volumes/{v.id}.json'} for v in series.volumes]})

    def recover_authors(self):
        """Fill the new metadata field for older imports, preserving source IDs and body flags."""
        metadata = self.folder / 'book.json'
        if not metadata.exists() or 'authors' in json.loads(metadata.read_text(encoding='utf-8')):
            return
        from .epub_importer import read_epub_authors
        with self.connect() as db:
            authors = []
            for volume in db.execute('SELECT id,source_path,authors_json FROM volumes ORDER BY number').fetchall():
                names = json.loads(volume['authors_json'])
                if not names:
                    try:
                        names = read_epub_authors(Path(volume['source_path']))
                    except Exception:
                        # Missing or damaged optional source files do not prevent reading a work.
                        names = []
                db.execute('UPDATE volumes SET authors_json=? WHERE id=?', (json.dumps(names, ensure_ascii=False), volume['id']))
                authors.extend(names)
            db.execute('UPDATE series SET authors_json=?', (json.dumps(list(dict.fromkeys(authors)), ensure_ascii=False),))
        self.export_source()

    def export_source(self):
        with self.export_lock, self.connect() as db:
            series = db.execute('SELECT id FROM series').fetchone()
            if not series: return
            for volume in db.execute('SELECT * FROM volumes ORDER BY number').fetchall():
                chapters = []
                for chapter in db.execute('SELECT * FROM chapters WHERE volume_id=? ORDER BY chapter_index', (volume['id'],)).fetchall():
                    value = dict(chapter)
                    value['paragraphs'] = [dict(p) for p in db.execute('SELECT * FROM paragraphs WHERE chapter_id=? ORDER BY paragraph_index', (chapter['id'],))]
                    chapters.append(value)
                value = dict(volume)
                value['authors'] = json.loads(value.pop('authors_json'))
                value['source_path'] = str(Path(value['source_path']).relative_to(self.folder)).replace('\\', '/')
                if value['cover_path']: value['cover_path'] = str(Path(value['cover_path']).relative_to(self.folder)).replace('\\', '/')
                value['schemaVersion'] = 1
                value['chapters'] = chapters
                atomic_json(self.folder / 'volumes' / (volume['id'] + '.json'), value)
            self.export_metadata(series['id'])

class LibraryCatalog:
    def __init__(self, root: Path):
        self.data_dir = root
        self.workspace = root
        root.mkdir(parents=True, exist_ok=True)
        self.repositories: dict[str, WorkRepository] = {}

    def works(self):
        for metadata in self.data_dir.glob('*/book.json'):
            value = json.loads(metadata.read_text(encoding='utf-8'))
            key = value['id']
            if key not in self.repositories: self.repositories[key] = WorkRepository(metadata.parent)
        return list(self.repositories.values())

    def list_library(self):
        return LibraryResponse(series=[s for work in self.works() for s in work.list_library().series])

    def import_paths(self, paths, series_id=None, series_title=None, **kwargs):
        from .epub_importer import import_epub
        title = series_title or import_epub(paths[0], 1).title
        key = series_id or slugify(title)
        if not __import__('re').fullmatch(r'[a-zA-Z0-9_-]+', key): raise ValueError('Invalid work ID')
        self.works()
        repository = self.repositories.get(key)
        if repository is None:
            repository = WorkRepository(self.data_dir / key)
            self.repositories[key] = repository
        return repository.import_paths(paths, series_id=key, series_title=title, **kwargs)

    def locate(self, identifier):
        for work in self.works():
            with work.connect() as db:
                for table in ('series', 'volumes', 'chapters', 'chat_threads', 'analysis_runs'):
                    try:
                        if db.execute(f'SELECT 1 FROM {table} WHERE id=?', (identifier,)).fetchone(): return work
                    except sqlite3.OperationalError: pass
        return None

catalog = LibraryCatalog(Path(os.environ.get('UNDERSTORY_LIBRARY', Path.home() / 'Documents' / 'UnderStory Library')))
