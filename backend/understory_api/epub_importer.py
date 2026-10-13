from __future__ import annotations

import re
import posixpath
import zipfile
import unicodedata
from copy import copy
from html import escape
from dataclasses import dataclass, field
from statistics import median
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .models import ChapterKind


HEADING_RE = re.compile(
    r"^(序章|终章|尾声|后记|作者后记|附录|间章|幕间|断章|特典|"
    r"第[一二三四五六七八九十百零〇0-9]+章)(?:\s+.*)?$"
)
VOLUME_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)(?!\d)")
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_MEMBER_BYTES = 30 * 1024 * 1024
SKIP_DOCUMENT_TITLES = {
    "封面", "插图", "目录", "目次", "制作人员", "版权信息", "版权页", "奥付",
}
SHORT_BODY_TITLE_RE = re.compile(
    r'序章|序幕|楔子|引子|终章|終章|终幕|尾声|尾聲|特典|番外|外传|外傳|'
    r'间章|間章|幕间|幕間|断章|斷章|短篇|后日谈|後日談|後日譚|'
    r'プロローグ|エピローグ|prologue|epilogue|interlude|bonus|sidestory',
    re.I,
)


@dataclass(slots=True)
class ImportedChapter:
    title: str
    kind: ChapterKind
    is_body: bool
    paragraphs: list["ImportedParagraph"]


@dataclass(slots=True)
class ImportedParagraph:
    text: str
    html: str
    role: str = "paragraph"


@dataclass(slots=True)
class ImportedVolume:
    number: int | float
    title: str
    chapters: list[ImportedChapter]
    cover_bytes: bytes | None
    cover_media_type: str | None
    authors: list[str] = field(default_factory=list)


def metadata_authors(opf: ET.Element) -> list[str]:
    roles: dict[str, list[str]] = {}
    for node in opf.iter():
        if local_name(node.tag) == 'meta' and node.attrib.get('property') == 'role':
            roles.setdefault(node.attrib.get('refines', '').lstrip('#'), []).append((node.text or '').strip().casefold())
    authors: list[str] = []
    for node in opf.iter():
        if local_name(node.tag) != 'creator':
            continue
        explicit_roles = roles.get(node.attrib.get('id', ''), []) + [value.casefold() for key, value in node.attrib.items() if local_name(key) == 'role']
        if explicit_roles and not any(role in {'aut', 'author', 'http://id.loc.gov/vocabulary/relators/aut'} for role in explicit_roles):
            continue
        name = re.sub(r'\s+', ' ', ''.join(node.itertext())).strip()
        if name and name not in authors:
            authors.append(name)
    return authors


def read_epub_authors(path: Path) -> list[str]:
    """Read metadata alone, without reparsing or renumbering existing chapters."""
    with zipfile.ZipFile(path) as zf:
        _validate_archive(zf)
        container = ET.fromstring(zf.read('META-INF/container.xml'))
        rootfile = next(node.attrib['full-path'] for node in container.iter() if local_name(node.tag) == 'rootfile')
        return metadata_authors(ET.fromstring(zf.read(rootfile)))


def mark_short_chapters(chapters: list[ImportedChapter]) -> None:
    counts = [(chapter, len(re.sub(r'\s+', '', ''.join(p.text for p in chapter.paragraphs)))) for chapter in chapters
              if chapter.is_body and chapter.kind not in {'prologue', 'epilogue'} and not is_short_body_title(chapter.title)]
    nonempty = [count for _, count in counts if count > 0]
    if len(nonempty) < 3:
        return
    threshold = min(300, median(nonempty) * .12)
    for chapter, count in counts:
        if count < threshold:
            chapter.is_body = False


def is_short_body_title(title: str) -> bool:
    normalized = ''.join(char for char in unicodedata.normalize('NFKC', title).casefold() if char.isalnum())
    return bool(SHORT_BODY_TITLE_RE.search(normalized))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def resolve_href(opf_path: str, href: str) -> str:
    return posixpath.normpath(str(PurePosixPath(opf_path).parent / href.split("#", 1)[0]))


def classify_chapter(title: str) -> tuple[ChapterKind, bool]:
    normalized = title.strip()
    if "后记" in normalized:
        return "afterword", False
    if normalized.startswith(("附录", "译注", "譯注", "译者注", "譯者注")):
        return "appendix", False
    if normalized.startswith("序章"):
        return "prologue", True
    if normalized.startswith(("终章", "尾声")):
        return "epilogue", True
    return "chapter", True


def guess_volume_number(filename: str, fallback: int = 1) -> int | float:
    candidates = VOLUME_NUMBER_RE.findall(Path(filename).stem)
    if not candidates:
        return fallback
    value = float(candidates[-1])
    return int(value) if value.is_integer() else value


def format_volume_number(value: int | float) -> str:
    """Format a volume number for stable IDs, paths and lr:// locators."""
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric):02d}"
    whole, fraction = format(numeric, "g").split(".", 1)
    return f"{int(whole):02d}.{fraction}"


def _validate_archive(zf: zipfile.ZipFile) -> None:
    total_size = 0
    for item in zf.infolist():
        member = PurePosixPath(item.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("EPUB contains an unsafe archive path")
        if item.file_size > MAX_MEMBER_BYTES:
            raise ValueError("EPUB contains an unexpectedly large member")
        total_size += item.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("EPUB expands beyond the supported size")


def _visible_lines(raw: bytes) -> list[str]:
    soup = BeautifulSoup(raw, "lxml-xml")
    for tag in soup(["script", "style", "nav", "head"]):
        tag.decompose()
    body = soup.find("body") or soup
    return [line.strip() for line in body.get_text("\n", strip=True).splitlines() if line.strip()]


SAFE_READER_TAGS = {
    "span", "strong", "b", "em", "i", "u", "s", "del", "ruby", "rt", "rp",
    "sup", "sub", "br", "small", "mark", "code", "kbd", "q", "img",
}
SAFE_STYLE_PROPERTIES = {
    "text-align", "text-indent", "font-weight", "font-style", "text-decoration",
    "vertical-align", "letter-spacing", "font-size", "font-family", "line-height",
    "white-space", "word-break", "margin-left", "margin-right", "color", "float",
}
INHERITED_STYLE_PROPERTIES = {
    "text-align", "font-weight", "font-style", "text-decoration", "letter-spacing",
    "font-size", "font-family", "line-height", "white-space", "word-break", "color",
}


@dataclass(slots=True)
class SafeCssRule:
    selector: str
    declarations: dict[str, str]
    specificity: tuple[int, int, int]
    order: int


def _safe_declarations(value: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        name, raw_value = (part.strip() for part in declaration.split(":", 1))
        name = name.casefold()
        raw_value = re.sub(r"\s*!important\s*$", "", raw_value, flags=re.I).strip()
        if name not in SAFE_STYLE_PROPERTIES or re.search(r"url|expression|javascript|behavior|@import", raw_value, re.I):
            continue
        if name == "font-size" and not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:px|em|rem|%)|xx-small|x-small|small|medium|large|x-large|xx-large|smaller|larger)", raw_value, re.I):
            continue
        if name in {"color"} and not re.fullmatch(r"(?:#[0-9a-f]{3,8}|rgba?\([^)]{1,60}\)|hsla?\([^)]{1,60}\)|[a-z]{3,20})", raw_value, re.I):
            continue
        if name == "float" and raw_value.casefold() not in {"left", "right", "none", "inline-start", "inline-end"}:
            continue
        declarations[name] = raw_value
    return declarations


def _safe_style(value: str) -> str:
    return ";".join(f"{name}:{raw_value}" for name, raw_value in _safe_declarations(value).items())


def _class_style(classes: list[str]) -> str:
    names = " ".join(classes).casefold()
    styles = []
    if any(token in names for token in ("center", "centre", "midashi", "align-c")):
        styles.append("text-align:center")
    elif any(token in names for token in ("right", "align-r")):
        styles.append("text-align:right")
    if "noindent" not in names and any(token in names for token in ("indent", "jisage", "paragraph")):
        styles.append("text-indent:2em")
    return ";".join(styles)


def _tag_classes(tag: object) -> list[str]:
    value = tag.get("class") or []
    return str(value).split() if isinstance(value, str) else [str(item) for item in value]


def _parse_css_rules(css: str, start_order: int = 0) -> list[SafeCssRule]:
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[SafeCssRule] = []
    order = start_order
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned):
        declarations = _safe_declarations(match.group(2))
        if not declarations:
            continue
        for raw_selector in match.group(1).split(","):
            selector = raw_selector.strip()
            # Pseudo-element declarations cannot be represented faithfully by
            # the sanitized inline-style format. Dropping them is safer than
            # accidentally applying a ::first-letter rule to the whole block.
            if not selector or selector.startswith("@") or ":" in selector or any(token in selector for token in ("[", "+", "~")):
                continue
            rules.append(SafeCssRule(
                selector=selector,
                declarations=declarations,
                specificity=(selector.count("#"), selector.count("."), len(re.findall(r"(?:^|[\s>])(?:[a-z][\w-]*)", selector, re.I))),
                order=order,
            ))
            order += 1
    return rules


def _matches_compound(tag: object, compound: str) -> bool:
    compound = compound.strip()
    if not compound or compound == "*":
        return True
    tag_name = local_name(str(getattr(tag, "name", ""))).casefold()
    expected_tag = re.match(r"^[a-z][\w-]*", compound, re.I)
    if expected_tag and tag_name != expected_tag.group(0).casefold():
        return False
    expected_id = re.search(r"#([\w-]+)", compound)
    if expected_id and str(tag.get("id") or "") != expected_id.group(1):
        return False
    classes = set(_tag_classes(tag))
    return all(item in classes for item in re.findall(r"\.([\w-]+)", compound))


def _matches_selector(tag: object, selector: str) -> bool:
    parts = [part for part in re.split(r"\s+|\s*>\s*", selector.strip()) if part and part != ">"]
    if not parts or not _matches_compound(tag, parts[-1]):
        return False
    ancestor = getattr(tag, "parent", None)
    for compound in reversed(parts[:-1]):
        while ancestor is not None and not _matches_compound(ancestor, compound):
            ancestor = getattr(ancestor, "parent", None)
        if ancestor is None:
            return False
        ancestor = getattr(ancestor, "parent", None)
    return True


def _computed_style(tag: object, rules: list[SafeCssRule], inherit: bool = True) -> str:
    declarations: dict[str, str] = {}
    if inherit:
        ancestors = list(getattr(tag, "parents", []))
        for ancestor in reversed(ancestors):
            inherited: dict[str, tuple[tuple[int, int, int], int, str]] = {}
            for rule in rules:
                if _matches_selector(ancestor, rule.selector):
                    for name, value in rule.declarations.items():
                        if name in INHERITED_STYLE_PROPERTIES:
                            previous = inherited.get(name)
                            if previous is None or (rule.specificity, rule.order) >= (previous[0], previous[1]):
                                inherited[name] = (rule.specificity, rule.order, value)
            declarations.update({name: item[2] for name, item in inherited.items()})
            declarations.update({
                name: value for name, value in _safe_declarations(str(ancestor.get("style") or "")).items()
                if name in INHERITED_STYLE_PROPERTIES
            })
    cascaded: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    for rule in rules:
        if _matches_selector(tag, rule.selector):
            for name, value in rule.declarations.items():
                previous = cascaded.get(name)
                if previous is None or (rule.specificity, rule.order) >= (previous[0], previous[1]):
                    cascaded[name] = (rule.specificity, rule.order, value)
    declarations.update({name: item[2] for name, item in cascaded.items()})
    declarations.update(_safe_declarations(str(tag.get("style") or "")))
    inferred = _safe_declarations(_class_style(_tag_classes(tag)))
    for name, value in inferred.items():
        declarations.setdefault(name, value)
    return ";".join(f"{name}:{value}" for name, value in declarations.items())


def _document_style_rules(raw: bytes, document_path: str, stylesheets: dict[str, str]) -> list[SafeCssRule]:
    soup = BeautifulSoup(raw, "lxml-xml")
    chunks = []
    for link in soup.find_all("link"):
        raw_rel = link.get("rel") or ""
        rel = (raw_rel if isinstance(raw_rel, str) else " ".join(str(item) for item in raw_rel)).casefold()
        href = str(link.get("href") or "")
        if "stylesheet" in rel and href:
            css = stylesheets.get(resolve_href(document_path, href))
            if css:
                chunks.append(css)
    chunks.extend(tag.get_text("\n") for tag in soup.find_all("style"))
    rules: list[SafeCssRule] = []
    for chunk in chunks:
        rules.extend(_parse_css_rules(chunk, len(rules)))
    return rules


def _sanitize_fragment(node: object, document_path: str, rules: list[SafeCssRule]) -> str:
    source_root_style = _computed_style(node, rules)
    root = copy(node)
    if source_root_style:
        root["style"] = source_root_style
    fragment = BeautifulSoup("", "lxml-xml")
    if not getattr(root, "name", None):
        return escape(str(node))
    for tag in list(root.find_all(True)):
        name = local_name(tag.name).casefold()
        if name == "image":
            href = tag.get("href") or tag.get("xlink:href")
            replacement = fragment.new_tag("img")
            if href:
                replacement["src"] = f"epub-resource:{resolve_href(document_path, str(href))}"
            replacement["alt"] = str(tag.get("aria-label") or "插图")
            tag.replace_with(replacement)
            continue
        if name not in SAFE_READER_TAGS:
            tag.unwrap()
            continue
        tag.name = name
        attributes: dict[str, str] = {}
        if name == "img":
            href = tag.get("src") or tag.get("href") or tag.get("xlink:href")
            if href:
                attributes["src"] = f"epub-resource:{resolve_href(document_path, str(href))}"
            attributes["alt"] = str(tag.get("alt") or tag.get("title") or "插图")[:200]
            if tag.get("title"):
                attributes["title"] = str(tag.get("title"))[:200]
        style = _computed_style(tag, rules, inherit=False)
        if style:
            attributes["style"] = style
            if name == "span" and "float" in _safe_declarations(style):
                attributes["class"] = "epub-inline-float"
        tag.attrs = attributes
    root_name = local_name(root.name).casefold()
    root_style = _computed_style(root, rules)
    if root_name in SAFE_READER_TAGS:
        attributes: dict[str, str] = {}
        if root_name == "img":
            href = root.get("src") or root.get("href") or root.get("xlink:href")
            if href:
                attributes["src"] = f"epub-resource:{resolve_href(document_path, str(href))}"
            attributes["alt"] = str(root.get("alt") or root.get("title") or "插图")[:200]
        if root_style:
            attributes["style"] = root_style
            if root_name == "span" and "float" in _safe_declarations(root_style):
                attributes["class"] = "epub-inline-float"
        root.attrs = attributes
        return str(root)
    content = "".join(str(item) for item in root.contents)
    return f'<span class="epub-block-style" style="{escape(root_style, quote=True)}">{content}</span>' if root_style else content


def _document_blocks(raw: bytes, document_path: str = "", rules: list[SafeCssRule] | None = None) -> tuple[str, list[tuple[str, ImportedParagraph]]]:
    soup = BeautifulSoup(raw, "lxml-xml")
    title_tag = soup.find("title")
    document_title = title_tag.get_text(" ", strip=True) if title_tag else ""
    for tag in soup(["script", "style", "nav", "head"]):
        tag.decompose()
    body = soup.find("body") or soup
    blocks = []
    candidates = body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li", "figure", "img"])
    for node in candidates:
        if any(local_name(parent.name).casefold() in {"p", "blockquote", "li", "figure"} for parent in node.parents if getattr(parent, "name", None)):
            continue
        name = local_name(node.name).casefold()
        text = re.sub(r"\s+", " ", node.get_text("", strip=True)).strip()
        has_image = bool(node.find(["img", "image"]) or name == "img")
        if not text and not has_image:
            continue
        role = f"heading-{name[1:]}" if re.fullmatch(r"h[1-6]", name) else "quote" if name == "blockquote" else "list-item" if name == "li" else "illustration" if has_image and not text else "paragraph"
        html = _sanitize_fragment(node, document_path, rules or [])
        if has_image and not text:
            text = str(node.get("alt") or "")
        blocks.append((name, ImportedParagraph(text=text, html=html, role=role)))
    return document_title, blocks


def _document_chapters(raw: bytes, document_path: str = "", rules: list[SafeCssRule] | None = None) -> list[tuple[str, list[ImportedParagraph]]]:
    """Prefer EPUB structure over title-pattern guessing.

    Many light novels use arbitrary story names, dates, interludes, or side-story
    labels instead of ``第N章``. A spine XHTML document and its leading heading
    are therefore stronger chapter boundaries than a language-specific regex.
    """
    effective_rules = rules if rules is not None else _document_style_rules(raw, document_path, {})
    document_title, blocks = _document_blocks(raw, document_path, effective_rules)
    structural_headings = [(name, block.text) for name, block in blocks if name in {"h1", "h2", "h3", "h4"} and block.text]
    leading_heading = structural_headings[0][1] if structural_headings else ""
    leading_level = structural_headings[0][0] if structural_headings else ""
    peer_boundaries = list(dict.fromkeys(text for name, text in structural_headings if name == leading_level))
    explicit_boundaries = list(dict.fromkeys(
        block.text for name, block in blocks
        if name in {"h1", "h2", "h3", "h4"} and block.text and HEADING_RE.match(block.text)
    ))
    # Same-level structural headings represent sibling chapters; lower-level
    # headings remain readable content inside the current chapter.
    boundaries = explicit_boundaries if len(explicit_boundaries) > 1 else (peer_boundaries if peer_boundaries else explicit_boundaries)
    if not boundaries and document_title:
        boundaries = [document_title]

    parts: list[tuple[str, list[ImportedParagraph]]] = []
    current_title = boundaries[0] if len(boundaries) == 1 else ""
    current_lines: list[ImportedParagraph] = []
    boundary_set = set(boundaries)
    for _, block in blocks:
        if block.text in boundary_set:
            if current_title and current_lines:
                parts.append((current_title, current_lines))
            current_title = block.text
            current_lines = []
            continue
        current_lines.append(block)
    if current_title and current_lines:
        parts.append((current_title, current_lines))

    result = []
    for title, paragraphs in parts:
        normalized = re.sub(r"\s+", " ", title).strip()
        while paragraphs and re.sub(r"\s+", " ", paragraphs[0].text).strip() == normalized:
            paragraphs.pop(0)
        paragraphs = _coalesce_illustrations(paragraphs)
        if not paragraphs:
            continue
        result.append((normalized, paragraphs))
    return result


def _coalesce_illustrations(paragraphs: list[ImportedParagraph]) -> list[ImportedParagraph]:
    """Keep media in reading order without consuming text locator numbers."""
    result: list[ImportedParagraph] = []
    pending: list[str] = []
    for paragraph in paragraphs:
        if paragraph.role == "illustration" and not paragraph.text:
            pending.append(paragraph.html)
            continue
        if pending:
            paragraph.html = "".join(pending) + paragraph.html
            pending = []
        result.append(paragraph)
    if pending and result:
        result[-1].html += "".join(pending)
    return result


def import_epub(path: Path, fallback_number: int | float = 1) -> ImportedVolume:
    with zipfile.ZipFile(path) as zf:
        _validate_archive(zf)
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = next(
            node.attrib["full-path"]
            for node in container.iter()
            if local_name(node.tag) == "rootfile"
        )
        opf = ET.fromstring(zf.read(rootfile))
        authors = metadata_authors(opf)

        metadata_title = path.stem
        for node in opf.iter():
            if local_name(node.tag) == "title" and (node.text or "").strip():
                metadata_title = (node.text or "").strip()
                break

        manifest: dict[str, dict[str, str]] = {}
        cover_id: str | None = None
        for node in opf.iter():
            name = local_name(node.tag)
            if name == "item":
                manifest[node.attrib["id"]] = {
                    "path": resolve_href(rootfile, node.attrib["href"]),
                    "media_type": node.attrib.get("media-type", "application/octet-stream"),
                    "properties": node.attrib.get("properties", ""),
                }
                if "cover-image" in node.attrib.get("properties", "").split():
                    cover_id = node.attrib["id"]
            elif name == "meta" and node.attrib.get("name") == "cover":
                cover_id = node.attrib.get("content")

        spine_ids = [
            node.attrib["idref"]
            for node in opf.iter()
            if local_name(node.tag) == "itemref" and node.attrib.get("idref") in manifest
        ]
        stylesheets = {
            item["path"]: zf.read(item["path"]).decode("utf-8", "ignore")
            for item in manifest.values()
            if item["media_type"] == "text/css"
        }

        chapter_parts: list[tuple[str, list[ImportedParagraph]]] = []
        pending_illustrations: list[ImportedParagraph] = []
        for item_id in spine_ids:
            item = manifest[item_id]
            if item["media_type"] in {"application/xhtml+xml", "text/html"}:
                raw = zf.read(item["path"])
                rules = _document_style_rules(raw, item["path"], stylesheets)
                document_title, blocks = _document_blocks(raw, item["path"], rules)
                if document_title.strip() in SKIP_DOCUMENT_TITLES:
                    pending_illustrations.extend(block for _, block in blocks if block.role == "illustration")
                    continue
                parts = _document_chapters(raw, item["path"], rules)
                if pending_illustrations and parts:
                    if parts[0][1]:
                        parts[0][1][0].html = "".join(item.html for item in pending_illustrations) + parts[0][1][0].html
                    pending_illustrations = []
                chapter_parts.extend(parts)

        cover_bytes = None
        cover_media_type = None
        if cover_id and cover_id in manifest:
            cover_item = manifest[cover_id]
            if cover_item["media_type"].startswith("image/"):
                cover_bytes = zf.read(cover_item["path"])
                cover_media_type = cover_item["media_type"]

    if not chapter_parts:
        raise ValueError("No readable chapters were found in this EPUB")

    chapters = []
    for title, paragraphs in chapter_parts:
        kind, is_body = classify_chapter(title)
        chapters.append(
            ImportedChapter(
                title=title,
                kind=kind,
                is_body=is_body,
                paragraphs=paragraphs,
            )
        )

    mark_short_chapters(chapters)
    return ImportedVolume(
        number=guess_volume_number(path.name, fallback_number),
        title=metadata_title,
        chapters=chapters,
        cover_bytes=cover_bytes,
        cover_media_type=cover_media_type,
        authors=authors,
    )
