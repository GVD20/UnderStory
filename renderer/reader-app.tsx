import { UnderStoryLogo } from "./brand-logo";
import { usePanelVisibility } from "./panel-visibility";
import { AnimatedRegion } from "./animated-region";
import { SidebarHeader } from './sidebar';
import { safeReaderHtml } from "./safe-html";
import { storage, desktopConfig } from "./storage";
import { mentionContext, matchMentionTerm, wholeWordOccurrences } from "./mention-boundaries";


import {
  ArrowLeft, BookMarked, BookOpenText, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Clock3,
  BookOpen, EyeOff, FileText, ImageIcon, ImageOff, Images, Layers, LibraryBig,
  MessageCircle, Monitor, Moon, PanelLeftClose, PanelLeftOpen, PanelRightClose, Search,
  Settings2, Shield, ShieldAlert, ShieldOff, Sparkles, Sun, X,
} from "./icons";
import { FormEvent, Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { API_ORIGIN, libraryApi } from "./lib/api";
import type { AnalysisJobRecord, Chapter, ChapterSummary, EmbeddingIndexStatus, NarrativeTrackRecord, ReadingSettingsSnapshot, SearchResult, SideMode, Theme, View, WikiEntrySummary, WikiMentionMatch } from "./types";
import { WikiWorkspace } from "./wiki-workspace";
import { WikiSideViewer } from "./wiki-workspace";
import { ChatSidePane, ChatWorkspace } from "./chat-workspace";
import { AnalysisProgress } from "./analysis-progress";
import { ModalLayer } from "./modal-layer";

type EpubFeatureMode = "off" | "basic" | "advanced";
type PaperColor = "standard" | "warm";

type IllustrationMode = "off" | "original" | "immersive";

export function UnderStoryApp({ seriesId, onBackToLibrary }: { seriesId: string; onBackToLibrary: () => void }) {
  const [progressOpen, setProgressOpen] = useState(false);
  const [progressPinned, setProgressPinned] = useState(false);
  const [view, setView] = useState<View>("reader");
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [readingSettingsOpen, setReadingSettingsOpen] = useState(false);
  const [analysisCheck, setAnalysisCheck] = useState(0);
  const [analysisJob, setAnalysisJob] = useState<AnalysisJobRecord | null>(null);
  const [knowledgeRevision, setKnowledgeRevision] = useState(0);
  useEffect(() => { const open = () => setAnalysisOpen(true); window.addEventListener('understory-analysis', open); return () => window.removeEventListener('understory-analysis', open); }, []);
  const [series, setSeries] = useState<Awaited<ReturnType<typeof libraryApi.getSeries>> | null>(null);
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [activeChapterId, setActiveChapterId] = useState("");
  const [expandedVolumes, setExpandedVolumes] = useState<Set<number>>(new Set());
  const [tocVisible, setTocVisible, tocPanelRef, tocExpanded] = usePanelVisibility(true, "left");
  const [sideVisible, setSideVisible, sidePanelRef, sideExpanded] = usePanelVisibility(true, "right");
  const [sideMode, setSideMode] = useState<SideMode>("peek");
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const searchRequest = useRef(0);
  useEffect(() => () => { searchRequest.current += 1; }, [seriesId]);
  const [targetParagraphId, setTargetParagraphId] = useState("");
  const [readerProgress, setReaderProgress] = useState(0);
  const [completedChapterId, setCompletedChapterId] = useState<string | null>(null);
  const [sideHistory, setSideHistory] = useState<{ mode: SideMode; slug: string }[]>([]);
  const [theme, setTheme] = useState<Theme>("paper");
  const [fontSize, setFontSize] = useState(18);
  const [lineHeight, setLineHeight] = useState(2.05);
  const [epubFeatures, setEpubFeatures] = useState<EpubFeatureMode>("basic");
  const [paperColor, setPaperColor] = useState<PaperColor>((storage.getItem("lr:paper-color") ?? "standard") as PaperColor);
  const [illustrations, setIllustrations] = useState<IllustrationMode>("original");
  const [readerImage, setReaderImage] = useState<{ src: string; alt: string } | null>(null);
  const [spoilerShieldMode, setSpoilerShieldMode] = useState<"off" | "auto" | "strict">("auto");
  const [embeddingStatus, setEmbeddingStatus] = useState<EmbeddingIndexStatus | null>(null);
  const [semanticRetrieval, setSemanticRetrieval] = useState(false);
  const [spoilerUnlocks, setSpoilerUnlocks] = useState<Set<string>>(new Set());
  const [bookmarks, setBookmarks] = useState<Set<string>>(new Set());
  const [readerSelection, setReaderSelection] = useState<ReaderSelection | null>(null);
  const selectionPopoverRef = useRef<HTMLElement>(null);
  const readingSettings: ReadingSettingsSnapshot = useMemo(() => ({
    theme, font_size: fontSize, line_height: lineHeight,
    epub_features: epubFeatures, paper_color: paperColor, illustrations, spoiler_shield: spoilerShieldMode,
    semantic_retrieval: semanticRetrieval,
  }), [epubFeatures, paperColor, fontSize, illustrations, lineHeight, semanticRetrieval, spoilerShieldMode, theme]);
  const applyReadingSettings = useCallback((settings: ReadingSettingsSnapshot) => {
    const nextPaperColor = settings.paper_color ?? (storage.getItem("lr:paper-color") ?? "standard") as PaperColor;
    setPaperColor(nextPaperColor);
    storage.setItem("lr:paper-color", nextPaperColor);
    setTheme(settings.theme); setFontSize(settings.font_size); setLineHeight(settings.line_height);
    const nextSemanticRetrieval = Boolean(settings.semantic_retrieval && embeddingStatus?.configured);
    setEpubFeatures(settings.epub_features); setIllustrations(settings.illustrations); setSpoilerShieldMode(settings.spoiler_shield); setSemanticRetrieval(nextSemanticRetrieval);
    storage.setItem("lr:theme", settings.theme);
    storage.setItem("lr:reader-font-size", String(settings.font_size));
    storage.setItem("lr:reader-line-height", String(settings.line_height));
    storage.setItem("lr:epub-features", settings.epub_features);
    storage.setItem("lr:illustrations", settings.illustrations);
    storage.setItem("lr:spoiler-shield", settings.spoiler_shield);
    storage.setItem("lr:semantic-retrieval", nextSemanticRetrieval ? "1" : "0");
  }, [embeddingStatus?.configured]);

  useEffect(() => {
    const refresh = () => applyReadingSettings({ paper_color: (storage.getItem("lr:paper-color") ?? "standard") as PaperColor, theme: (storage.getItem("lr:theme") ?? "paper") as Theme, font_size: Number(storage.getItem("lr:reader-font-size") ?? 18), line_height: Number(storage.getItem("lr:reader-line-height") ?? 2.05), epub_features: (storage.getItem("lr:epub-features") ?? "basic") as EpubFeatureMode, illustrations: (storage.getItem("lr:illustrations") ?? "original") as IllustrationMode, spoiler_shield: (storage.getItem("lr:spoiler-shield") ?? "auto") as "off" | "auto" | "strict", semantic_retrieval: storage.getItem("lr:semantic-retrieval") === "1" });
    window.addEventListener("understory-preferences", refresh);
    return () => window.removeEventListener("understory-preferences", refresh);
  }, [applyReadingSettings]);
  const [selectionPrompt, setSelectionPrompt] = useState("");
  const [selectionChatRequest, setSelectionChatRequest] = useState(0);
  const [error, setError] = useState("");
  const [lastWikiSlug, setLastWikiSlug] = useState("home");
  const [wikiTrackItems, setWikiTrackItems] = useState<NarrativeTrackRecord[]>([]);
  const [wikiMentionIndex, setWikiMentionIndex] = useState<WikiMentionMatch[]>([]);
  const readingRef = useRef<HTMLElement>(null);
  const pendingReadingJump = useRef<{ chapterId: string; paragraphId: string; paragraphIndex: number | null; behavior: ScrollBehavior; restoreProgress?: number } | null>(null);
  const [readingJumpRevision, setReadingJumpRevision] = useState(0);
  const positionReadingJump = useCallback(() => {
    const viewport = readingRef.current;
    const jump = pendingReadingJump.current;
    if (!viewport || !jump || viewport.dataset.chapterId !== jump.chapterId) return;
    const paragraphs = [...viewport.querySelectorAll<HTMLElement>('p[data-locator]')];
    const target = paragraphs.find(paragraph => jump.paragraphId
      ? paragraph.id === jump.paragraphId
      : jump.paragraphIndex !== null && Number(paragraph.dataset.locator?.match(/\/p(\d+)(?:$|[?#])/i)?.[1]) === jump.paragraphIndex);
    if ((jump.paragraphId || jump.paragraphIndex !== null) && !target) return;
    // Earlier EPUB images can change the target's position after their intrinsic
    // dimensions load. Keep the jump pending until that layout is settled.
    if ([...viewport.querySelectorAll('img')].some(image => !image.complete && (jump.restoreProgress !== undefined || (target && Boolean(image.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING))))) return;
    if (target && jump.restoreProgress === undefined) setTargetParagraphId(target.id);
    const top = jump.restoreProgress !== undefined ? jump.restoreProgress * Math.max(0, viewport.scrollHeight - viewport.clientHeight) : target ? viewport.scrollTop + target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - (viewport.clientHeight - target.clientHeight) / 2 : 0;
    viewport.scrollTo({ top: Math.max(0, top), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : jump.behavior });
    pendingReadingJump.current = null;
    const progress = Math.min(1, Math.max(0, top) / Math.max(1, viewport.scrollHeight - viewport.clientHeight));
    setReaderProgress(progress);
    if (progressSaveTimer.current) clearTimeout(progressSaveTimer.current);
    void libraryApi.saveProgress(seriesId, jump.chapterId, progress, target?.id ?? paragraphs[0]?.id).catch(() => {});
  }, [seriesId]);
  const progressSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const analysisWelcomeChecked = useRef("");

  useEffect(() => {
    if (!tocVisible) return;
    const frame = requestAnimationFrame(() => document.querySelector(".chapter-list button.active")?.scrollIntoView({ block: "nearest" }));
    return () => cancelAnimationFrame(frame);
  }, [tocVisible, activeChapterId, series]);
  useEffect(() => {
    const narrow = matchMedia("(max-width: 1100px)");
    const compact = matchMedia("(max-width: 950px)");
    const adjust = () => { if (narrow.matches) setSideVisible(false); if (compact.matches) setTocVisible(false); };
    adjust(); narrow.addEventListener("change", adjust); compact.addEventListener("change", adjust);
    return () => { narrow.removeEventListener("change", adjust); compact.removeEventListener("change", adjust); };
  }, []);
  const flatChapters = useMemo(
    () => series?.volumes.flatMap((volume) => volume.chapters) ?? [],
    [series],
  );
  const activeIndex = flatChapters.findIndex((item) => item.id === activeChapterId);
  const activeSummary = flatChapters[activeIndex];
  const activeVolume = series?.volumes.find((volume) => volume.number === activeSummary?.volume);
  const spoilerView = {
    mode: spoilerShieldMode, activeChapterId: completedChapterId ?? "", readerProgress: completedChapterId ? 1 : 0, unlocked: spoilerUnlocks,
    onUnlock: (key: string) => setSpoilerUnlocks((current) => {
      if (key.endsWith(":entry")) return new Set(current).add(key);
      const next = new Set([...current].filter((item) => item.endsWith(":entry")));
      if (!current.has(key)) next.add(key);
      return next;
    }),
  };

  useEffect(() => {
    let live = true;
    Promise.all([libraryApi.getSeries(seriesId), libraryApi.getProgress(seriesId).catch(() => null), libraryApi.getReadingMarker(seriesId)]).then(([data, progress, marker]) => {
      if (!live) return;
      setSeries(data);
      setCompletedChapterId(marker.completedChapterId);
      setQuery("");
      setSubmittedQuery("");
      setSearchResults([]);
      const saved = progress?.chapter_id ?? storage.getItem(`lr:last-chapter:${seriesId}`);
      const first = data.volumes[0]?.chapters[0]?.id ?? "";
      const chapterId = data.volumes.some((volume) => volume.chapters.some((item) => item.id === saved))
        ? saved! : first;
      setChapter(null);
      setReaderProgress(0);
      pendingReadingJump.current = { chapterId, paragraphId: '', paragraphIndex: null, behavior: 'auto', restoreProgress: progress?.chapter_id === chapterId ? progress.progress : 0 };
      setActiveChapterId(chapterId);
      const volume = data.volumes.find((item) => item.chapters.some((entry) => entry.id === chapterId));
      if (volume) setExpandedVolumes(new Set([volume.number]));
      setBookmarks(new Set(JSON.parse(storage.getItem(`lr:bookmarks:${seriesId}`) ?? "[]") as string[]));
      const savedTheme = storage.getItem("lr:theme");
      setTheme(savedTheme === "paper" || savedTheme === "dark" || savedTheme === "system" ? savedTheme : "paper");
      const savedFontSize = Number(storage.getItem("lr:reader-font-size"));
      setFontSize(savedFontSize >= 14 && savedFontSize <= 23 ? savedFontSize : 18);
      const savedLineHeight = Number(storage.getItem("lr:reader-line-height"));
      setLineHeight(savedLineHeight >= 1.55 && savedLineHeight <= 2.4 ? savedLineHeight : 2.05);
      const savedEpubFeatures = storage.getItem("lr:epub-features");
      setEpubFeatures(savedEpubFeatures === "off" || savedEpubFeatures === "advanced" ? savedEpubFeatures : savedEpubFeatures === "basic" ? "basic" : storage.getItem("lr:epub-advanced-styles") === "1" ? "advanced" : "basic");
      const savedIllustrations = storage.getItem("lr:illustrations");
      setIllustrations(savedIllustrations === "off" || savedIllustrations === "immersive" ? savedIllustrations : savedIllustrations === "original" ? "original" : storage.getItem("lr:immersive-illustrations") === "1" ? "immersive" : "original");
      const savedSpoilerMode = storage.getItem("lr:spoiler-shield");
      setSpoilerShieldMode(savedSpoilerMode === "off" || savedSpoilerMode === "strict" ? savedSpoilerMode : "auto");
      setSpoilerUnlocks(new Set());
      setLastWikiSlug(storage.getItem(`lr:last-wiki-slug:${seriesId}`) ?? "home");
    }).catch(() => { if (live) setError("本地阅读服务连接失败，请重启应用或查看日志。"); });
    return () => { live = false; };
  }, [seriesId]);

  useEffect(() => {
    let cancelled = false;
    void libraryApi.getEmbeddingStatus(seriesId).then((status) => {
      if (cancelled) return;
      setEmbeddingStatus(status);
      setSemanticRetrieval(status.configured && storage.getItem("lr:semantic-retrieval") === "1");
    }).catch(() => {
      if (cancelled) return;
      setEmbeddingStatus(null);
      setSemanticRetrieval(false);
    });
    return () => { cancelled = true; };
  }, [seriesId]);

  useEffect(() => {
    if (!series || analysisWelcomeChecked.current === series.id) return;
    analysisWelcomeChecked.current = series.id;
    const seenKey = `lr:analysis-welcome:${series.id}`;
    if (storage.getItem(seenKey)) return;
    void Promise.all([
      libraryApi.getLatestAnalysis(series.id),
      libraryApi.getWikiState(series.id),
    ]).then(([latest, wiki]) => {
      if (!latest && !wiki.blueprint && wiki.entries.length === 0) {
        storage.setItem(seenKey, "1");
        setAnalysisOpen(true);
      }
    }).catch(() => undefined);
  }, [series]);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      const job = await libraryApi.getLatestAnalysis(seriesId).catch(() => null);
      if (!live) return;
      setAnalysisJob(job);
      if (job?.status === 'running' || job?.status === 'pending') timer = setTimeout(() => void check(), 5000);
      else if (job?.status === 'completed') {
        setKnowledgeRevision(value => value + 1);
        void libraryApi.getSeries(seriesId).then(data => { if (live) setSeries(data); });
      }
    };
    void check();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [seriesId, analysisCheck]);

  useEffect(() => {
    let live = true;
    void libraryApi.getWikiMentionIndex(seriesId).then((items) => { if (live) setWikiMentionIndex(items); }).catch(async () => {
      const state = await libraryApi.getWikiState(seriesId).catch(() => null);
      if (live) setWikiMentionIndex((state?.entries ?? []).map((entry) => ({
        slug: entry.slug, title: entry.title, kind: entry.kind, theme_color: entry.theme_color,
        terms: [
          { value: entry.title, canonical_name: entry.title, match_kind: "literal" as const, score: 1 },
          ...selectionValueStrings(entry.metadata.aliases).map((value) => ({ value, canonical_name: entry.title, match_kind: "related" as const, score: 0 })),
        ], direct_match: true,
      })));
    });
    return () => { live = false; };
  }, [seriesId, knowledgeRevision]);

  useEffect(() => {
    if (!readerSelection) return;
    const clearOutside = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (target?.closest(".reader-selection-popover, .right-sidebar, .toc-panel, .side-rail")) return;
      if (!readingRef.current?.contains(target)) {
        setReaderSelection(null);
        setSelectionPrompt("");
      }
    };
    document.addEventListener("pointerdown", clearOutside);
    return () => document.removeEventListener("pointerdown", clearOutside);
  }, [readerSelection]);

  useEffect(() => {
    if (!activeChapterId) return;
    let live = true;
    libraryApi.getChapter(activeChapterId).then((data) => {
      if (!live) return;
      setChapter(data);
      storage.setItem(`lr:last-chapter:${seriesId}`, activeChapterId);
    }).catch(() => { if (live) setError("章节载入失败，请稍后重试。"); });
    return () => { live = false; };
  }, [activeChapterId, seriesId, knowledgeRevision]);

  useEffect(() => {
    if (!chapter || view !== 'reader') return;
    const frame = requestAnimationFrame(positionReadingJump);
    return () => cancelAnimationFrame(frame);
  }, [chapter, view, readingJumpRevision, positionReadingJump]);

  useEffect(() => {
    const viewport = readingRef.current;
    clearReaderSelectionMarks(viewport);
    if (!viewport || !readerSelection) return;
    const frame = requestAnimationFrame(() => {
      window.getSelection()?.removeAllRanges();
      for (const selected of readerSelection.ranges) {
        const paragraph = document.getElementById(selected.paragraphId);
        if (paragraph) wrapTextOffsets(paragraph, selected.startOffset, selected.endOffset, "reader-selection-highlight", "selection");
      }
      markSelectionWikiTerms(readerSelection);
    });
    return () => {
      cancelAnimationFrame(frame);
      clearReaderSelectionMarks(viewport);
    };
  }, [chapter, readerSelection]);

  useEffect(() => {
    const viewport = readingRef.current;
    const selected = readerSelection?.ranges[0];
    const paragraph = selected && document.getElementById(selected.paragraphId);
    if (!viewport || !paragraph || !selected || view !== 'reader') return;
    let frame = 0;
    let disposed = false;
    function position() {
      frame = 0;
      if (disposed || !paragraph || !selected || !viewport) return;
      const popup = selectionPopoverRef.current;
      const segment = textSegmentsFromOffsets(paragraph, selected.startOffset, Math.min(selected.endOffset, selected.startOffset + 1))[0];
      if (!popup || !segment) return;
      const anchor = document.createRange();
      anchor.setStart(segment.node, segment.start);
      anchor.setEnd(segment.node, segment.end);
      const rect = anchor.getClientRects()[0] ?? anchor.getBoundingClientRect();
      const bounds = viewport.getBoundingClientRect();
      const left = Math.max(10, rect.left - bounds.left + viewport.scrollLeft);
      popup.style.left = `${left}px`;
      popup.style.top = `${Math.max(8, rect.top - bounds.top + viewport.scrollTop - 8)}px`;
      popup.style.setProperty('--selection-popup-left', `${left}px`);
    }
    function schedule() { if (!frame && !disposed) frame = requestAnimationFrame(position); }
    const observer = new ResizeObserver(schedule);
    observer.observe(viewport);
    observer.observe(paragraph);
    const paper = viewport.querySelector('.reading-paper');
    if (paper) observer.observe(paper);
    viewport.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    schedule();
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      viewport.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
    };
  }, [chapter, readerSelection, view]);

  useEffect(() => {
    if (!readerSelection) return;
    const copyStoredSelection = (event: ClipboardEvent) => {
      const active = document.activeElement;
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement || active?.getAttribute("contenteditable") === "true") return;
      const nativeSelection = window.getSelection();
      if (nativeSelection && !nativeSelection.isCollapsed && readingRef.current?.contains(nativeSelection.anchorNode)) return;
      event.preventDefault();
      event.clipboardData?.setData("text/plain", readerSelection.copyText);
    };
    document.addEventListener("copy", copyStoredSelection);
    return () => document.removeEventListener("copy", copyStoredSelection);
  }, [readerSelection]);

  function openChapter(chapterId: string, paragraphId = "", paragraphIndex: number | null = null) {
    const summary = flatChapters.find((item) => item.id === chapterId);
    pendingReadingJump.current = { chapterId, paragraphId, paragraphIndex, behavior: view === 'reader' && activeChapterId === chapterId ? 'smooth' : 'auto' };
    setReadingJumpRevision(value => value + 1);
    setView("reader");
    if (chapterId !== activeChapterId) setChapter(null);
    setReaderProgress(0);
    setReaderSelection(null);
    setSelectionPrompt("");
    setActiveChapterId(chapterId);
    setTargetParagraphId(paragraphId);
    setExpandedVolumes(summary ? new Set([summary.volume]) : expandedVolumes);
  }

  function openView(next: View) {
    if (view === 'reader' && next !== 'reader') {
      const viewport = readingRef.current;
      if (viewport) pendingReadingJump.current = { chapterId: activeChapterId, paragraphId: '', paragraphIndex: null, behavior: 'auto', restoreProgress: viewport.scrollTop / Math.max(1, viewport.scrollHeight - viewport.clientHeight) };
    }
    if (next !== view && (next === "wiki" || view === "wiki")) setSpoilerUnlocks(new Set());
    setView(next);
  }

  function selectWiki(slug: string) {
    setSpoilerUnlocks(new Set());
    setLastWikiSlug(slug);
    storage.setItem(`lr:last-wiki-slug:${seriesId}`, slug);
  }

  function openWiki(slug = "home", inSidebar = view === "reader") {
    if (inSidebar) setSideHistory(history => [...history, { mode: sideMode, slug: lastWikiSlug }]);
    selectWiki(slug);
    if (inSidebar) { setSideMode("wiki"); setSideVisible(true); }
    else openView("wiki");
  }

  function openLocator(locator: string) {
    const match = locator.trim().replace(/(\/c\d+)p(\d+)/i, '$1/p$2').match(/\/v(\d+(?:\.\d+)?)(?:\/c(\d+))?(?:\/p(\d+))?/i);
    if (!match) return;
    const volume = Number(match[1]);
    const chapterIndex = match[2] ? Number(match[2]) : null;
    const paragraphIndex = match[3] ? Number(match[3]) : null;
    const target = flatChapters.find((item) => item.volume === volume && (chapterIndex === null || item.index === chapterIndex));
    if (!target) return;
    openChapter(target.id, "", paragraphIndex);
  }

  function toggleVolume(number: number) {
    setExpandedVolumes((current) => {
      const next = new Set(current);
      if (next.has(number)) next.delete(number); else next.add(number);
      return next;
    });
  }

  function goSideBack() {
    const previous = sideHistory[sideHistory.length - 1];
    if (!previous) return;
    setSideHistory(history => history.slice(0, -1));
    setSideMode(previous.mode);
    if (previous.mode === "wiki") selectWiki(previous.slug);
    else setWikiTrackItems([]);
  }

  async function updateReadingMarker(chapterId: string | null, advanceOnly = false) {
    const savedIndex = flatChapters.findIndex(item => item.id === completedChapterId);
    const nextIndex = flatChapters.findIndex(item => item.id === chapterId);
    if (advanceOnly && nextIndex <= savedIndex) return;
    try {
      const marker = await libraryApi.saveReadingMarker(seriesId, chapterId);
      setCompletedChapterId(marker.completedChapterId);
    } catch { setError("阅读标记保存失败，请重试。"); }
  }

  function nextChapter() {
    const next = flatChapters[activeIndex + 1];
    if (!next) return;
    void updateReadingMarker(activeChapterId, true);
    openChapter(next.id);
  }

  function changeSideMode(mode: SideMode) {
    setSideHistory([]);
    if (sideVisible && sideMode === mode) {
      if (mode === "wiki") setSpoilerUnlocks(new Set());
      setSideVisible(false); setWikiTrackItems([]);
    } else {
      if (mode === "wiki" || sideMode === "wiki") setSpoilerUnlocks(new Set());
      setSideMode(mode); setSideVisible(true); if (mode !== "wiki") setWikiTrackItems([]);
    }
  }

  function onReaderScroll() {
    const node = readingRef.current;
    if (!node || pendingReadingJump.current) return;
    const max = Math.max(1, node.scrollHeight - node.clientHeight);
    const next = node.scrollTop / max;
    setReaderProgress(next);
    if (progressSaveTimer.current) clearTimeout(progressSaveTimer.current);
    const paragraph = [...node.querySelectorAll<HTMLElement>('p[data-locator]')].find(item => item.getBoundingClientRect().bottom > node.getBoundingClientRect().top + 80);
    progressSaveTimer.current = setTimeout(() => {
      if (series && activeChapterId) void libraryApi.saveProgress(series.id, activeChapterId, next, paragraph?.id);
    }, 650);
  }

  function captureReaderSelection(event: React.MouseEvent<HTMLElement>) {
    if (event.target instanceof Element && event.target.closest('[data-lr-selection-mark="term"]')) return;
    const selection = window.getSelection(); const rawText = selection?.toString() ?? ""; const text = rawText.trim();
    if (!selection || text.length < 1 || !selection.rangeCount || !readingRef.current?.contains(selection.anchorNode)) { setReaderSelection(null); setSelectionPrompt(""); return; }
    const range = selection.getRangeAt(0);
    const allParagraphs = [...readingRef.current.querySelectorAll<HTMLElement>("p[data-locator]")];
    const paragraphForNode = (node: Node) => (node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement)?.closest("p[data-locator]") as HTMLElement | null;
    const startParagraph = paragraphForNode(range.startContainer); const endParagraph = paragraphForNode(range.endContainer);
    const startIndex = startParagraph ? allParagraphs.indexOf(startParagraph) : -1;
    const endIndex = endParagraph ? allParagraphs.indexOf(endParagraph) : -1;
    const paragraphs = startIndex >= 0 && endIndex >= startIndex
      ? allParagraphs.slice(startIndex, endIndex + 1)
      : allParagraphs.filter((paragraph) => range.intersectsNode(paragraph));
    if (!paragraphs.length) { setReaderSelection(null); return; }
    const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
    const leading = rawText.length - rawText.trimStart().length; const trailing = rawText.length - rawText.trimEnd().length;
    const ranges = paragraphs.map((paragraph, index) => {
      let startOffset = 0; let endOffset = paragraph.textContent?.length ?? 0;
      if (paragraph === startParagraph || paragraph.contains(range.startContainer)) {
        const before = document.createRange(); before.selectNodeContents(paragraph); before.setEnd(range.startContainer, range.startOffset);
        startOffset = before.toString().length;
      }
      if (paragraph === endParagraph || paragraph.contains(range.endContainer)) {
        const beforeEnd = document.createRange(); beforeEnd.selectNodeContents(paragraph); beforeEnd.setEnd(range.endContainer, range.endOffset);
        endOffset = beforeEnd.toString().length;
      }
      if (index === 0) startOffset += leading;
      if (index === paragraphs.length - 1) endOffset -= trailing;
      return { paragraphId: paragraph.id, startOffset: Math.max(0, startOffset), endOffset: Math.max(startOffset, endOffset) };
    });
    const contexts = paragraphs.map((paragraph, index) => mentionContext(paragraph.textContent ?? '', ranges[index].startOffset, ranges[index].endOffset));
    const candidates = wikiMentionIndex.map((entry) => {
      const term = entry.terms.filter(source => (spoilerShieldMode !== "strict" || source.value === entry.title) && (source.match_kind === "literal" || [...normalizeMention(source.value)].length > 1)).map((source) => {
        const matched = contexts.map(context => matchMentionTerm(context, source.value)).find(Boolean);
        return matched ? { matched, source } : null;
      }).filter((value): value is NonNullable<typeof value> => value !== null).sort((a, b) =>
        Number(b.source.match_kind === "literal") - Number(a.source.match_kind === "literal")
        || b.source.score - a.source.score || b.matched.length - a.matched.length
      )[0];
      return term ? {
        slug: entry.slug, title: entry.title, term: term.matched, canonicalName: term.source.canonical_name,
        themeColor: entry.theme_color, literalMatch: term.source.match_kind === "literal", score: term.source.score,
      } : null;
    }).filter((item): item is NonNullable<typeof item> => item !== null);
    const bestLiteral = new Map<string, SelectionWikiMatch>();
    for (const item of candidates) {
      if (!item.literalMatch) continue;
      const key = normalizeMention(item.term); const current = bestLiteral.get(key);
      if (!current || item.score > current.score || (item.score === current.score && item.title.length < current.title.length)) bestLiteral.set(key, item);
    }
    const matches = candidates.filter((item) => !item.literalMatch || bestLiteral.get(normalizeMention(item.term)) === item)
      .sort((a, b) => Number(b.literalMatch) - Number(a.literalMatch) || b.score - a.score || text.indexOf(a.term) - text.indexOf(b.term));
    const viewport = readingRef.current; const viewportRect = viewport.getBoundingClientRect();
    const popupLeft = Math.max(10, rect.left - viewportRect.left + viewport.scrollLeft);
    setReaderSelection({
      text: text.slice(0, 3000), copyText: text, paragraphId: paragraphs[0].id, locator: paragraphs[0].dataset.locator ?? "", ranges,
      x: popupLeft,
      y: Math.max(8, rect.top - viewportRect.top + viewport.scrollTop - 8), matches,
    });
    setSelectionPrompt(text.slice(0, 12000));
  }

  function handleReaderClick(event: React.MouseEvent<HTMLElement>) {
    const mark = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-lr-selection-mark="term"]') : null;
    const slug = mark?.dataset.wikiSlug;
    if (slug) openWiki(slug);
  }

  function askAboutSelection() {
    if (!readerSelection) return;
    setSelectionPrompt(readerSelection.copyText.slice(0, 12000));
    storage.removeItem(`lr:last-chat-thread:${series?.id}:reader:${activeChapterId}`);
    setSelectionChatRequest((value) => value + 1);
    setSideMode("chat"); setSideVisible(true);
  }

  function clearSelectedContext() {
    window.getSelection()?.removeAllRanges();
    setReaderSelection(null);
    setSelectionPrompt("");
  }

  async function submitSearch(nextQuery: string) {
    const needle = nextQuery.trim();
    if (!series || !needle) return;
    const requestId = ++searchRequest.current;
    setSubmittedQuery(needle);
    setSearching(true);
    setSearchResults([]);
    try {
      const response = await libraryApi.search(series.id, needle);
      if (requestId === searchRequest.current) setSearchResults(response.results);
    } catch {
      if (requestId === searchRequest.current) setError("搜索失败，请稍后重试。");
    } finally {
      if (requestId === searchRequest.current) setSearching(false);
    }
  }

  async function markBody(isBody: boolean) {
    if (!chapter || !series) return;
    const updated = await libraryApi.updateChapter(chapter.id, isBody);
    setChapter(updated);
    setSeries(await libraryApi.getSeries(series.id));
  }

  function toggleBookmark() {
    const next = new Set(bookmarks);
    if (next.has(activeChapterId)) next.delete(activeChapterId); else next.add(activeChapterId);
    setBookmarks(next);
    storage.setItem(`lr:bookmarks:${seriesId}`, JSON.stringify([...next]));
  }

  if (error) return <main className="startup-state"><span className="wordmark">UnderStory</span><h1>还差最后一步</h1><p>{error}</p><button onClick={() => location.reload()}>重新连接</button></main>;
  if (!series) return <main className="startup-state" aria-live="polite"><span className="wordmark">UnderStory</span><div className="loading-line" /><p>正在整理书页…</p></main>;

  const overallBefore = series.volumes.filter((v) => v.number < (activeSummary?.volume ?? 1)).reduce((sum, v) => sum + v.character_count, 0)
    + (activeVolume?.chapters.filter((item) => item.index < (activeSummary?.index ?? 1)).reduce((sum, item) => sum + item.character_count, 0) ?? 0);
  const completedIndex = flatChapters.findIndex(item => item.id === completedChapterId);
  const overallProgress = flatChapters.slice(0, completedIndex + 1).reduce((total, item) => total + item.character_count, 0) / Math.max(1, series.total_characters);

  return (
    <main className={`app-shell paper-color-${paperColor} epub-features-${epubFeatures} illustrations-${illustrations}`} data-testid="understory-app">
      <header className="topbar">
        <div className="brand-block"><button className="library-back" title="返回书架" aria-label="返回书架" onClick={onBackToLibrary}><ArrowLeft size={16} /></button><UnderStoryLogo /><span className="wordmark">UnderStory</span><span className="book-identity">{series.title}{series.volumes.length > 1 ? ` · ${series.volumes.length} 卷` : ""}</span></div>
        <nav className="module-nav" aria-label="作品模块" data-active={view === "reader" ? 0 : view === "chat" ? 1 : 2}>
          <button className={view === "reader" ? "active" : ""} onClick={() => openView("reader")}><BookOpenText size={15} />阅读</button>
          <button className={view === "chat" ? "active" : ""} onClick={() => openView("chat")}><MessageCircle size={15} />对话</button>
          <button className={view === "wiki" ? "active" : ""} onClick={() => openView("wiki")}><LibraryBig size={15} />Wiki</button>
        </nav>
        <div className="top-actions"><button className={`analyze-book ${analysisJob?.status === 'running' || analysisJob?.status === 'pending' ? 'is-running' : ''}`} title="查看本书分析" onClick={() => setAnalysisOpen(true)}><Sparkles size={16} /><span>{analysisJob?.status === 'completed' ? '分析完毕' : analysisJob?.status === 'running' || analysisJob?.status === 'pending' ? <>分析中 <span className="analysis-inline-percent">{Math.round(Math.min(1, Math.max(0, analysisJob.progress)) * 100)}%</span></> : analysisJob?.status === 'paused' ? '分析已暂停' : analysisJob?.status === 'failed' ? '分析未完成' : '分析本书'}</span></button>{view === "reader" && <button className="reader-settings-shortcut" title="阅读设置" aria-label="阅读设置" onClick={() => setReadingSettingsOpen(true)}><Settings2 size={18} /></button>}</div>
      </header>

      <ModalLayer open={analysisOpen} className="analysis-backdrop">{analysisOpen && <AnalysisProgress series={series} onClose={() => setAnalysisOpen(false)} onStarted={() => setAnalysisCheck(value => value + 1)} onCompleted={() => setAnalysisCheck(value => value + 1)} />}</ModalLayer>

      <ModalLayer open={readingSettingsOpen} className="reader-settings-backdrop" onClick={event => { if (event.target === event.currentTarget) setReadingSettingsOpen(false); }}>{readingSettingsOpen && <ReadingSettingsDialog onClose={() => setReadingSettingsOpen(false)}><ReadingPreferences paperColor={paperColor} setPaperColor={setPaperColor} showTheme={false} theme={theme} setTheme={setTheme} fontSize={fontSize} setFontSize={setFontSize} lineHeight={lineHeight} setLineHeight={setLineHeight} epubFeatures={epubFeatures} setEpubFeatures={setEpubFeatures} illustrations={illustrations} setIllustrations={setIllustrations} spoilerShieldMode={spoilerShieldMode} setSpoilerShieldMode={setSpoilerShieldMode} semanticRetrieval={semanticRetrieval} setSemanticRetrieval={setSemanticRetrieval} embeddingStatus={embeddingStatus} /></ReadingSettingsDialog>}</ModalLayer>

      <AnimatedRegion className="module-stage" transitionKey={view} onCommit={positionReadingJump}>{view === "wiki" ? <WikiWorkspace key={knowledgeRevision} series={series} selectedSlug={lastWikiSlug} onSelected={selectWiki} onOpenLocator={openLocator} spoiler={spoilerView} /> : view === "chat" ? <ChatWorkspace series={series} currentChapterId={activeChapterId} targetParagraphId={targetParagraphId} readingSettings={readingSettings} onApplyReadingSettings={applyReadingSettings} onOpenLocator={openLocator} onOpenWiki={(slug) => openWiki(slug, false)} /> : (
        <>
          <section className={`reader-workspace ${tocExpanded ? "with-toc" : ""} ${sideExpanded ? "with-side" : ""}`}>
            {!tocVisible && <nav className="side-rail toc-launcher" aria-label="阅读大纲"><button aria-label="展开目录" title="展开目录" onClick={() => setTocVisible(true)}><PanelLeftOpen size={17} /></button></nav>}
            <div className="reader-toc-slot" data-present={tocVisible}>{tocVisible && <aside className="toc-panel" ref={tocPanelRef}>
              <SidebarHeader title={series.volumes.length === 1 ? "章节" : "卷与章节"} actions={<><small>{flatChapters.length} 篇</small><button aria-label="隐藏目录" onClick={() => setTocVisible(false)}><PanelLeftClose size={17} /></button></>} />
              <div className="volume-list sidebar-scroll">{series.volumes.map((volume) => {
                const expanded = expandedVolumes.has(volume.number);
                return <section key={volume.id} className={`volume-group ${expanded ? "expanded" : "collapsed"}`}>
                  <button className="volume-card" onClick={() => toggleVolume(volume.number)}>
                    {/* EPUB covers are local library assets served by the companion API. */}
                    {volume.cover_url && <img src={`${API_ORIGIN}${volume.cover_url}`} alt={series.volumes.length === 1 ? `${series.title}封面` : `第 ${volume.number} 卷封面`} />}
                    <span><strong>{series.volumes.length === 1 ? series.title : `第 ${volume.number} 卷`}</strong><em>{volume.chapters.length} 章</em></span>
                    {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
                  </button>
                  {expanded && <div className="chapter-list">{volume.chapters.map((item) => <button key={item.id} className={item.id === activeChapterId ? "active" : ""} onClick={() => openChapter(item.id)}><span>{item.title}</span><span className="chapter-meta">{!item.is_body && <i>资料</i>}{item.id === completedChapterId && <i title="最新已读标记">已读至</i>}{bookmarks.has(item.id) && <BookMarked size={12} />}</span></button>)}</div>}
                </section>;
              })}</div>
            </aside>}</div>

            <article ref={node => { readingRef.current = node; if (node) positionReadingJump(); }} data-chapter-id={chapter?.id} onLoadCapture={positionReadingJump} onErrorCapture={positionReadingJump} onScroll={onReaderScroll} onMouseUp={captureReaderSelection} onClick={handleReaderClick} className="reading-viewport" style={{ "--reader-font-size": `${fontSize}px`, "--reader-line-height": lineHeight } as React.CSSProperties}>
              <div className="reading-paper chapter-stage" key={activeChapterId}>
                {chapter ? <><header className="chapter-header"><span>{series.volumes.length === 1 ? series.title : `第 ${chapter.volume} 卷`} · {chapter.is_body ? `第 ${chapter.index} 篇` : "资料章节"}</span><h1>{chapter.title}</h1><p>{series.authors?.length ? `${series.authors.join("、")} · ` : ""}{chapter.character_count.toLocaleString()} 字 · {chapter.paragraph_count.toLocaleString()} 段</p></header>
                  <div className="chapter-copy">{chapter.paragraphs.map((paragraph) => <ReaderParagraph key={paragraph.id} paragraph={paragraph} targeted={paragraph.id === targetParagraphId} onOpenImage={setReaderImage} />)}</div>
                  <footer className="chapter-footer"><button disabled={activeIndex <= 0} onClick={() => flatChapters[activeIndex - 1] && openChapter(flatChapters[activeIndex - 1].id)}><ChevronLeft size={15} />上一章</button><span>{activeIndex + 1} / {flatChapters.length}</span><button disabled={activeIndex >= flatChapters.length - 1} onClick={nextChapter}>下一章<ChevronRight size={15} /></button></footer></> : <ChapterSkeleton />}
              </div>
              {readerSelection && <aside ref={selectionPopoverRef} key={`${readerSelection.paragraphId}-${readerSelection.ranges[0]?.startOffset}-${readerSelection.ranges.at(-1)?.endOffset}`} className="reader-selection-popover" style={{ left: readerSelection.x, top: readerSelection.y, "--selection-popup-left": `${readerSelection.x}px` } as React.CSSProperties} onMouseUp={(event) => event.stopPropagation()}>{readerSelection.matches.length > 0 && <SelectionWikiLinks matches={readerSelection.matches} onOpenWiki={(slug) => openWiki(slug)} />}<div className="selection-primary"><button className="ask-selection" onClick={askAboutSelection}><MessageCircle size={15} />询问阅读伙伴<ChevronRight size={14} /></button></div></aside>}
            </article>

            <nav className="side-rail" aria-label="阅读侧栏">
              <button className={sideVisible && sideMode === "peek" ? "active" : ""} onClick={() => changeSideMode("peek")} title="本章轻览"><FileText size={17} /><span>轻览</span></button>
              <button className={sideVisible && sideMode === "wiki" ? "active" : ""} onClick={() => changeSideMode("wiki")} title="Wiki"><LibraryBig size={17} /><span>Wiki</span></button>
              <button className={sideVisible && sideMode === "chat" ? "active" : ""} onClick={() => changeSideMode("chat")} title="对话"><MessageCircle size={17} /><span>对话</span></button>
              <button className={sideVisible && sideMode === "search" ? "active" : ""} onClick={() => changeSideMode("search")} title="搜索"><Search size={17} /><span>搜索</span></button>
            </nav>
            <div className="reader-side-slot" data-present={sideVisible}>{sideVisible && <aside className="right-sidebar" ref={sidePanelRef}><SidebarHeader title={sideMode === "peek" ? "本章轻览" : sideMode === "wiki" ? "Wiki 词条" : sideMode === "chat" ? "阅读伙伴" : "作品搜索"} actions={<button aria-label="关闭侧栏" onClick={() => { setSideVisible(false); setWikiTrackItems([]); }}><PanelRightClose size={17} /></button>} /><AnimatedRegion className={`side-panel-stage mode-${sideMode}`} transitionKey={`${sideMode}-${series.id}`} axis="vertical">{sideMode === "peek" ? <PeekPanel key={knowledgeRevision} seriesId={series.id} chapter={chapter} bookmarked={bookmarks.has(activeChapterId)} onBookmark={toggleBookmark} onMarkBody={markBody} onOpenWiki={(value) => openWiki(value.startsWith("slug:") ? value.slice(5) : `title:${value}`)} /> : sideMode === "search" ? <SearchPanel strict={spoilerShieldMode === "strict"} series={series} query={query} submittedQuery={submittedQuery} setQuery={setQuery} results={searchResults} searching={searching} submit={submitSearch} openChapter={openChapter} openWiki={(slug) => openWiki(slug)} /> : sideMode === "wiki" ? <WikiSideViewer onBack={sideHistory.length ? goSideBack : undefined} key={knowledgeRevision} series={series} selectedSlug={lastWikiSlug} onSelected={selectWiki} onOpenFull={() => openWiki(lastWikiSlug, false)} onOpenLocator={openLocator} onTrackChange={setWikiTrackItems} spoiler={spoilerView} /> : <ChatSidePane key={selectionChatRequest} series={series} currentChapterId={activeChapterId} targetParagraphId={readerSelection?.paragraphId ?? targetParagraphId} selectedText={selectionPrompt} selectedParagraphCount={readerSelection?.ranges.length} readingSettings={readingSettings} onApplyReadingSettings={applyReadingSettings} onClearSelectedText={clearSelectedContext} onOpenLocator={openLocator} onOpenWiki={(slug) => openWiki(slug)} />}</AnimatedRegion></aside>}</div>
          </section>
          <div className={`desktop-progress ${progressOpen || progressPinned ? "expanded" : "collapsed"}`}><button className="progress-trigger" onClick={() => setProgressOpen(!progressOpen)}>{Math.round(overallProgress * 100)}%</button>{(progressOpen || progressPinned) && <><button className="progress-pin" aria-pressed={progressPinned} onClick={() => setProgressPinned(!progressPinned)}>{progressPinned ? "取消固定" : "固定"}</button><ProgressNavigator series={series} activeChapter={activeSummary} activeVolume={activeVolume} overallProgress={overallProgress} readerProgress={readerProgress} highlightedTracks={wikiTrackItems} completedChapterId={completedChapterId} onUpdateMarker={updateReadingMarker} openChapter={openChapter} /></>}</div>
          <ModalLayer open={Boolean(readerImage)} className="reader-image-lightbox" role="dialog" aria-modal="true" aria-label={readerImage?.alt || "查看插图"} onClick={() => setReaderImage(null)}>{readerImage && <><button aria-label="关闭插图" onClick={() => setReaderImage(null)}>×</button>{/* EPUB images are local resources served by the companion API. */}<img src={readerImage.src} alt={readerImage.alt} onClick={(event) => event.stopPropagation()} /></>}</ModalLayer>
        </>
      )}</AnimatedRegion>
    </main>
  );
}

const ReaderParagraph = memo(function ReaderParagraph({ paragraph, targeted, onOpenImage }: { paragraph: Chapter["paragraphs"][number]; targeted: boolean; onOpenImage: (image: { src: string; alt: string }) => void }) {
  const className = [`reader-block`, `role-${paragraph.role}`, targeted ? "target-paragraph" : ""].filter(Boolean).join(" ");
  if (!paragraph.html) return <p id={paragraph.id} data-locator={paragraph.locator} className={className}>{paragraph.text}</p>;
  const html = paragraph.html.replace(/(["'])\/api\//g, `$1${API_ORIGIN}/api/`);
  return <p id={paragraph.id} data-locator={paragraph.locator} className={className} dangerouslySetInnerHTML={{ __html: safeReaderHtml(html, API_ORIGIN) }} onClick={(event) => { const target = event.target; if (target instanceof HTMLImageElement) onOpenImage({ src: target.currentSrc || target.src, alt: target.alt }); }} />;
});

function ReadingSettingsDialog({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    dialog?.querySelector<HTMLButtonElement>('header button')?.focus();
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab' || !dialog) return;
      const controls = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]')];
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener('keydown', handleKey);
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus(); };
  }, []);
  return <section ref={dialogRef} className="reader-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="reader-settings-title"><header><h2 id="reader-settings-title">阅读设置</h2><button aria-label="关闭阅读设置" onClick={onClose}><X size={18} /></button></header><div className="reader-settings-content">{children}</div></section>;
}

function SettingsChoice<T extends string>({ title, value, options, onChange, note }: { title: string; value: T; options: { value: T; label: string; description: string; icon: ReactNode; disabled?: boolean }[]; onChange: (value: T) => void; note?: string }) {
  return <section className="reader-setting-section"><h3>{title}</h3><div className="reader-setting-choices">
    {options.map((option) => <button type="button" key={option.value} className={value === option.value ? "active" : ""} aria-pressed={value === option.value} disabled={option.disabled} onClick={() => onChange(option.value)}><span className="reader-setting-choice-icon" aria-hidden="true">{option.icon}</span><span><strong>{option.label}</strong><small>{option.description}</small></span></button>)}
  </div>{note && <p className="reader-setting-note">{note}</p>}</section>;
}

const EPUB_DEMO_PARAGRAPHS: Chapter["paragraphs"] = [
  { id: "epub-demo-overline", locator: "lr://demo/p0001", role: "paragraph", text: "CHAPTER 01", html: '<span style="color:#9a674d;font-family:sans-serif;font-size:12px;font-weight:800;letter-spacing:.18em">CHAPTER 01</span>' },
  { id: "epub-demo-title", locator: "lr://demo/p0002", role: "heading-2", text: "月光落在书页上", html: '<span style="color:#815b4c;letter-spacing:.04em">月光落在书页上</span>' },
  { id: "epub-demo-epigraph", locator: "lr://demo/p0003", role: "paragraph", text: "—— 一段居中的章节题记 ——", html: '<span class="epub-block-style" style="text-align:center;color:#77736d">—— 一段居中的章节题记 ——</span>' },
  { id: "epub-demo-opening", locator: "lr://demo/p0004", role: "paragraph", text: "夜色渐深，窗边的书页仍泛着微光。这里包含重点文字、斜体语气、下划线、暖金色的着重内容和星的注音示例。风从半开的窗缝里经过，轻轻翻动了桌上的旧书。", html: '<span class="epub-inline-float" style="float:left;font-size:2.5em;font-weight:700;line-height:.82;margin-right:.12em;color:#815b4c">夜</span>色渐深，窗边的书页仍泛着微光。这里包含<strong>重点文字</strong>、<em>斜体语气</em>、<u>下划线</u>、<span style="color:#a25f42">暖金色的着重内容</span>和<ruby>星<rt>ほし</rt></ruby>的注音示例。风从半开的窗缝里经过，轻轻翻动了桌上的旧书。' },
  { id: "epub-demo-quote", locator: "lr://demo/p0005", role: "quote", text: "“有些故事，要在翻到下一页时才真正开始。”", html: "“有些故事，要在翻到下一页时才真正开始。”" },
  { id: "epub-demo-image", locator: "lr://demo/p0006", role: "illustration", text: "一位男子与建筑的黑白矢量插画", html: `<img src="${new URL("./man-and-his-building-1905.svg", location.href).href}" alt="一位男子与建筑的黑白矢量插画">` },
  { id: "epub-demo-subheading", locator: "lr://demo/p0007", role: "heading-4", text: "穿过安静的长廊", html: '<span style="color:#557462">穿过安静的长廊</span>' },
  { id: "epub-demo-copy-2", locator: "lr://demo/p0008", role: "paragraph", text: "他提起灯，沿着长廊缓慢前行。墙上的画像在光影中次第醒来，遥远的钟声越过屋顶，落进沉睡的庭院。书页所保留的不只是文字，也是字号、间距、带有书籍配色的提示文字和段落间细微的呼吸。", html: '<span class="epub-block-style" style="text-indent:2em">他提起灯，沿着长廊缓慢前行。墙上的画像在光影中次第醒来，遥远的钟声越过屋顶，落进沉睡的庭院。书页所保留的不只是文字，也是字号、间距、<span style="color:#557462">带有书籍配色的提示文字</span>和段落间细微的呼吸。</span>' },
  { id: "epub-demo-copy-3", locator: "lr://demo/p0009", role: "paragraph", text: "插图之后，故事重新回到纸面。读者可以继续向下滚动，观察长篇正文中的段落节奏，以及不同设置在连续内容中的实际表现。", html: '<span class="epub-block-style" style="text-indent:2em">插图之后，故事重新回到纸面。读者可以继续向下滚动，观察长篇正文中的段落节奏，以及不同设置在连续内容中的实际表现。</span>' },
];

function EpubSettingsDemo({ paperColor, theme, fontSize, lineHeight, epubFeatures, illustrations }: { paperColor: PaperColor; theme: Theme; fontSize: number; lineHeight: number; epubFeatures: EpubFeatureMode; illustrations: IllustrationMode }) {
  return <div className={`epub-settings-preview paper-color-${paperColor} epub-features-${epubFeatures} illustrations-${illustrations} theme-${theme}`}>
    <div className="epub-settings-preview-label"><span>实时预览</span><small>EPUB 页面示例</small></div>
    <article className="epub-demo-page" style={{ "--reader-font-size": `${fontSize}px`, "--reader-line-height": lineHeight } as React.CSSProperties}>
      <div className="chapter-copy">{EPUB_DEMO_PARAGRAPHS.map((paragraph) => <Fragment key={paragraph.id}><ReaderParagraph paragraph={paragraph} targeted={false} onOpenImage={() => undefined} />{paragraph.role === "illustration" && <footer className="epub-demo-attribution"><strong>Man and his Building</strong><span><a href="https://openclipart.org/detail/356409/manandhisbuilding1905" target="_blank" rel="noreferrer">openclipart.org/356409</a><i aria-hidden="true">·</i>by <a href="https://openclipart.org/artist/j4p4n" target="_blank" rel="noreferrer">j4p4n</a></span></footer>}</Fragment>)}</div>
    </article>
  </div>;
}

function ChapterSkeleton() {
  return <div className="chapter-skeleton" aria-label="正在打开章节"><header><i /><i /><i /></header><div>{[92, 78, 87, 64, 83, 72].map((width, index) => <i key={index} style={{ width: `${width}%` }} />)}</div></div>;
}

function PeekPanel({ seriesId, chapter, bookmarked, onBookmark, onMarkBody, onOpenWiki }: { seriesId: string; chapter: Chapter | null; bookmarked: boolean; onBookmark: () => void; onMarkBody: (value: boolean) => void; onOpenWiki: (title: string) => void }) {
  const [wikiEntries, setWikiEntries] = useState<WikiEntrySummary[]>([]);
  useEffect(() => {
    let live = true;
    void libraryApi.getWikiState(seriesId).then(state => { if (live) setWikiEntries(state.entries); }).catch(() => {});
    return () => { live = false; };
  }, [seriesId]);
  if (!chapter) return <p className="side-empty">正在准备轻览…</p>;
  const overview = chapter.chapter_overview;
  const ready = chapter.is_body && overview.status === "ready";
  return <div className="peek-panel">
    <header className="peek-heading"><div><small>第 {chapter.volume} 卷 · 第 {chapter.index} 篇</small><h2>{chapter.title}</h2></div><button className={bookmarked ? "saved" : ""} onClick={onBookmark} aria-label={bookmarked ? "移除章节书签" : "加入章节书签"} title={bookmarked ? "已加入书签" : "加入书签"}><BookMarked size={17} /></button></header>
    {ready ? <>
      {overview.intro && <p className="peek-introduction">{overview.intro}</p>}
      {overview.overview && overview.overview !== overview.intro && <details className="peek-synopsis" key={chapter.id}><summary>本章提要<ChevronDown size={13} /></summary><p>{overview.overview}</p></details>}
      <div className="peek-groups">
        <PreviewGroup key={`${chapter.id}-people`} title="人物" items={overview.people} wikiEntries={wikiEntries} onOpenWiki={onOpenWiki} />
        <PreviewGroup key={`${chapter.id}-places`} title="地点" items={overview.places} wikiEntries={wikiEntries} onOpenWiki={onOpenWiki} />
        <PreviewGroup key={`${chapter.id}-concepts`} title="设定与事物" items={overview.concepts} wikiEntries={wikiEntries} onOpenWiki={onOpenWiki} />
        <PreviewGroup key={`${chapter.id}-events`} title="本章事件" items={overview.events} wikiEntries={wikiEntries} onOpenWiki={onOpenWiki} wide />
      </div>
    </> : <div className="pending-card muted"><FileText size={18} /><strong>{chapter.is_body ? "尚未生成轻览" : "资料章节"}</strong><p>{chapter.is_body ? "分析本书后查看章节要素。" : "保留原文，跳过 AI 分析。"}</p></div>}
    <section className="peek-chapter-settings"><div className="body-switch"><span><strong>标记为资料</strong><small>{chapter.is_body ? "启用AI分析" : "跳过AI分析"}</small></span><button role="switch" aria-label="标记为资料" aria-checked={!chapter.is_body} className={!chapter.is_body ? "on" : ""} onClick={() => void onMarkBody(!chapter.is_body)}><i /></button></div>
      <dl className="fact-list"><div><dt>篇幅</dt><dd>{chapter.character_count.toLocaleString()} 字 · {chapter.paragraph_count} 段</dd></div><div><dt>章节轻览</dt><dd>{!chapter.is_body ? "不参与分析" : ready ? "已生成" : "待分析"}</dd></div></dl>
    </section>
  </div>;
}

function PreviewGroup({ title, items, wikiEntries, onOpenWiki, wide = false }: { title: string; items: Chapter["chapter_overview"]["people"]; wikiEntries: WikiEntrySummary[]; onOpenWiki: (value: string) => void; wide?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!items.length) return null;
  return <section className={`peek-group ${wide ? "wide-cards" : ""}`}><header><strong>{title}</strong><span>{items.length}</span></header><div className="peek-card-grid">{(expanded ? items : items.slice(0, 4)).map((item, index) => {
    const card = typeof item === "string" ? { title: item } : item;
    const cardTitle = card.title?.trim() ?? "";
    const target = wikiEntries.find(entry => (card.slug && entry.slug === card.slug) || (cardTitle && entry.title === cardTitle));
    const content = <><span className="peek-card-title">{cardTitle || card.summary}</span>{cardTitle && card.summary && <small>{card.summary}</small>}{target && <ChevronRight size={12} />}</>;
    return target ? <button type="button" key={`${cardTitle}-${index}`} className="peek-element linked" onClick={() => onOpenWiki(`slug:${target.slug}`)}>{content}</button> : <div key={`${cardTitle}-${index}`} className="peek-element informational">{content}</div>;
  })}</div>{items.length > 4 && <button type="button" className="peek-expand" onClick={() => setExpanded(value => !value)}>{expanded ? <><ChevronUp size={13} />收起</> : <><ChevronDown size={13} />展开剩余 {items.length - 4} 项</>}</button>}</section>;
}

function SearchPanel({ strict, series, query, submittedQuery, setQuery, results, searching, submit, openChapter, openWiki }: {
  strict: boolean;
  series: NonNullable<Awaited<ReturnType<typeof libraryApi.getSeries>>>;
  query: string;
  submittedQuery: string;
  setQuery: (value: string) => void;
  results: SearchResult[];
  searching: boolean;
  submit: (query: string) => Promise<void>;
  openChapter: (chapterId: string, paragraphId?: string) => void;
  openWiki: (slug: string) => void;
}) {
  const [filter, setFilter] = useState<"all" | SearchResult["result_type"]>("all");
  const [history, setHistory] = useState<string[]>(() => readSearchHistory(series.id));
  const historyKey = `lr:search-history:${series.id}`;
  const isCurrentSearch = Boolean(submittedQuery) && query.trim() === submittedQuery;
  const ordered = useMemo(() => [...results].sort((a, b) => searchResultPriority(a) - searchResultPriority(b)), [results]);
  const visible = filter === "all" ? ordered : ordered.filter((item) => item.result_type === filter);

  function saveHistory(items: string[]) {
    const next = items.slice(0, 10);
    setHistory(next);
    storage.setItem(historyKey, JSON.stringify(next));
  }

  async function runSearch(value: string) {
    const needle = value.trim();
    if (!needle) return;
    setQuery(needle);
    saveHistory([needle, ...history.filter((item) => item !== needle)]);
    await submit(needle);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void runSearch(query);
  }

  return <div className="search-side">
    <form onSubmit={onSubmit}><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索章节、词条与正文" aria-label="搜索作品" /><button aria-label="开始搜索" disabled={searching || !query.trim()}><ChevronRight size={16} /></button></form>
    {!query.trim() ? <section className="search-history" aria-label="搜索历史">
      <header><span><Clock3 size={13} />搜索历史</span>{history.length > 0 && <button type="button" className="clear-search-history" onClick={() => saveHistory([])}>清空</button>}</header>
      {history.length > 0 ? <div>{history.map((item) => <div className="search-history-item" key={item}><button type="button" onClick={() => void runSearch(item)}>{item}</button><button type="button" aria-label={`删除搜索记录：${item}`} onClick={() => saveHistory(history.filter((value) => value !== item))}><X size={13} /></button></div>)}</div> : <p>输入关键词搜索本书。</p>}
    </section> : <>
      <div className="search-filters"><button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button><button className={filter === "chapter" ? "active" : ""} onClick={() => setFilter("chapter")}>章节</button><button className={filter === "wiki" ? "active" : ""} onClick={() => setFilter("wiki")}>Wiki</button><button className={filter === "text" ? "active" : ""} onClick={() => setFilter("text")}>正文</button></div>
      <div className="search-results">{searching ? <p>正在检索…</p> : isCurrentSearch ? visible.map((result) => {
        const featured = result.result_type === "chapter" || (result.result_type === "wiki" && result.title_match);
        const kind = result.result_type === "chapter" ? "章节" : result.result_type === "wiki" ? "Wiki 词条" : series.volumes.length === 1 ? "正文" : `正文 · 第 ${result.volume} 卷`;
        const matchedTitle = result.matched_title || result.chapter_title;
        const aliasMatch = result.result_type === "wiki" && matchedTitle !== result.chapter_title;
        const exactTitleMatch = matchedTitle.trim().localeCompare(submittedQuery.trim(), undefined, { sensitivity: "accent" }) === 0;
        const wikiLabel = `Wiki 词条${result.entry_kind ? ` · ${result.entry_kind}` : ""}`;
        return <button className={`search-result ${featured ? `featured ${result.result_type}` : "standard"}`} key={`${result.locator}-${result.paragraph_id ?? "chapter"}`} onClick={() => result.result_type === "wiki" ? openWiki(result.locator.replace("wiki://", "")) : openChapter(result.chapter_id, result.paragraph_id ?? "")}>
          {featured ? <><span className="search-result-kind">{result.result_type === "wiki" ? <LibraryBig size={13} /> : <BookOpenText size={13} />}{result.result_type === "wiki" ? wikiLabel : kind}</span><strong className="search-result-title">{exactTitleMatch ? matchedTitle : <SearchMatch text={matchedTitle} query={submittedQuery} />}</strong>{aliasMatch && <span className="search-result-canonical">{result.chapter_title}</span>}<small className={strict && result.result_type === "wiki" ? "wiki-strict-teaser" : undefined} aria-hidden={strict && result.result_type === "wiki" || undefined}><SearchMatch text={result.excerpt} query={submittedQuery} /></small></> : <><span>{kind} · <SearchMatch text={result.chapter_title} query={submittedQuery} /></span><strong><SearchMatch text={result.excerpt} query={submittedQuery} /></strong></>}
        </button>;
      }) : <p className="search-ready-hint">按 Enter 开始搜索</p>}{!searching && isCurrentSearch && visible.length === 0 && <p>没有找到匹配内容。</p>}</div>
    </>}
  </div>;
}

function searchResultPriority(result: SearchResult) {
  if (result.result_type === "wiki" && result.title_match_kind === "exact_title") return 0;
  if (result.result_type === "wiki" && result.title_match_kind === "exact_alias") return 1;
  if (result.result_type === "chapter" && result.title_match_kind === "exact_title") return 2;
  if (result.result_type === "wiki" && result.title_match_kind === "partial_title") return 3;
  if (result.result_type === "chapter" && result.title_match_kind === "partial_title") return 4;
  if (result.result_type === "wiki") return 5;
  return 6;
}

function readSearchHistory(seriesId: string) {
  if (typeof window === "undefined") return [];
  try {
    const saved = JSON.parse(storage.getItem(`lr:search-history:${seriesId}`) ?? "[]");
    return Array.isArray(saved) ? saved.filter((item): item is string => typeof item === "string").slice(0, 10) : [];
  } catch { return []; }
}

function SearchMatch({ text, query }: { text: string; query: string }) {
  const terms = [...new Set([query.trim(), ...query.trim().split(/[\s,，、;；|/：:]+/)].filter(Boolean))]
    .sort((a, b) => b.length - a.length);
  if (!terms.length) return <>{text}</>;
  const pattern = new RegExp(`(${terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return <>{text.split(pattern).map((part, index) => terms.some((term) => term.localeCompare(part, undefined, { sensitivity: "accent" }) === 0) ? <mark key={`${part}-${index}`}>{part}</mark> : part)}</>;
}

function ProgressNavigator({ series, activeChapter, activeVolume, overallProgress, readerProgress, highlightedTracks, completedChapterId, onUpdateMarker, openChapter }: { series: NonNullable<Awaited<ReturnType<typeof libraryApi.getSeries>>>; activeChapter?: ChapterSummary; activeVolume?: typeof series.volumes[number]; overallProgress: number; readerProgress: number; highlightedTracks: NarrativeTrackRecord[]; completedChapterId: string | null; onUpdateMarker: (id: string | null) => void; openChapter: (id: string) => void }) {
  const highlighted = trackChapterIds(highlightedTracks, series);
  const chapters = series.volumes.flatMap(volume => volume.chapters);
  const completedIndex = chapters.findIndex(item => item.id === completedChapterId);
  const isCompleted = (id: string) => { const index = chapters.findIndex(item => item.id === id); return index >= 0 && index <= completedIndex; };
  return <section className="progress-dock" aria-label="作品阅读进度"><div className="progress-label"><span>已读进度</span><strong>{Math.round(overallProgress * 100)}%</strong></div><div className="progress-stack"><div className="volume-rail" style={{ "--overall-read": `${overallProgress * 100}%` } as React.CSSProperties}>{series.volumes.map((volume) => { const read = volume.chapters.filter(item => isCompleted(item.id)).reduce((total, item) => total + item.character_count, 0) / Math.max(1, volume.character_count); let offset = 0; const markers = volume.chapters.map((item) => { const start = offset / Math.max(1, volume.character_count) * 100; offset += item.character_count; return highlighted.has(item.id) ? { id: item.id, title: item.title, start, width: item.character_count / Math.max(1, volume.character_count) * 100 } : null; }).filter((item): item is NonNullable<typeof item> => item !== null); const label = series.volumes.length === 1 ? series.title : `第 ${volume.number} 卷`; return <button key={volume.id} style={{ flex: Math.max(1, volume.character_count) }} className={`${volume.number === activeVolume?.number ? "active" : ""} ${read >= 1 ? "read" : ""} ${markers.length ? "track-highlight" : ""}`} title={`${label} · 已读 ${Math.round(read * 100)}%${markers.length ? ` · 轨迹覆盖 ${markers.length} 章` : ""}`} onClick={() => volume.chapters[0] && openChapter(volume.chapters[0].id)}><span>{series.volumes.length === 1 ? series.title : `V${volume.number}`}</span>{markers.length > 0 && <i className="volume-track-markers">{markers.map((marker) => <b key={marker.id} title={marker.title} style={{ left: `${marker.start}%`, width: `${Math.max(.7, marker.width)}%` }} />)}</i>}</button>; })}</div><div className="chapter-rail">{activeVolume?.chapters.map((item) => { const read = isCompleted(item.id) ? 1 : 0; return <button key={item.id} style={{ flex: Math.max(1, item.character_count), "--chapter-read": `${read * 100}%` } as React.CSSProperties} className={`${item.id === activeChapter?.id ? "active" : ""} ${read >= 1 ? "read" : ""} ${highlighted.has(item.id) ? "track-highlight" : ""}`} title={`${item.title}${highlighted.has(item.id) ? " · 当前 Wiki 轨迹覆盖" : ""}`} onClick={() => openChapter(item.id)}><i /></button>; })}</div></div><div className="progress-context"><span>{series.volumes.length === 1 ? series.title : `第 ${activeVolume?.number ?? 1} 卷`}</span><strong>{activeChapter?.title ?? ""}</strong></div><div className="reading-marker-control"><label>最新已读标记<select aria-label="最新已读标记" value={completedChapterId ?? ""} onChange={event => onUpdateMarker(event.target.value || null)}><option value="">尚未开始</option>{series.volumes.flatMap(volume => volume.chapters.map(chapter => <option key={chapter.id} value={chapter.id}>读完第 {volume.number} 卷 · {chapter.title}</option>))}</select></label><small>仅点击正文底部“下一章”会自动推进；也可在此手动修改。</small></div></section>;
}

function trackChapterIds(items: NarrativeTrackRecord[], series: NonNullable<Awaited<ReturnType<typeof libraryApi.getSeries>>>) {
  const result = new Set<string>();
  for (const item of items) {
    const start = locatorOrder(item.start_locator); const end = locatorOrder(item.end_locator ?? item.start_locator);
    for (const volume of series.volumes) for (const chapter of volume.chapters) {
      const order = volume.number * 10_000 + chapter.index;
      if (order >= start && order <= end) result.add(chapter.id);
    }
  }
  return result;
}

function locatorOrder(locator: string) { const match = locator.match(/\/v(\d+)\/c(\d+)/); return match ? Number(match[1]) * 10_000 + Number(match[2]) : -1; }

interface SelectionWikiMatch { slug: string; title: string; term: string; canonicalName: string; themeColor: string; literalMatch: boolean; score: number; }
interface ReaderSelection { text: string; copyText: string; paragraphId: string; locator: string; ranges: { paragraphId: string; startOffset: number; endOffset: number }[]; x: number; y: number; matches: SelectionWikiMatch[]; }

function textSegmentsFromOffsets(root: HTMLElement, startOffset: number, endOffset: number) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const segments: { node: Text; start: number; end: number }[] = [];
  const total = root.textContent?.length ?? 0;
  const start = Math.max(0, Math.min(total, startOffset));
  const end = Math.max(start, Math.min(total, endOffset));
  let cursor = 0;
  let node = walker.nextNode();
  while (node) {
    const textNode = node as Text;
    const next = cursor + textNode.data.length;
    const segmentStart = Math.max(start, cursor);
    const segmentEnd = Math.min(end, next);
    if (segmentStart < segmentEnd) segments.push({ node: textNode, start: segmentStart - cursor, end: segmentEnd - cursor });
    cursor = next;
    if (cursor >= end) break;
    node = walker.nextNode();
  }
  return segments;
}

function wrapTextOffsets(root: HTMLElement, start: number, end: number, className: string, kind: "selection" | "term", slug = "") {
  const segments = textSegmentsFromOffsets(root, start, end);
  for (const segment of segments.reverse()) {
    const after = segment.node.splitText(segment.end);
    const selected = segment.node.splitText(segment.start);
    const mark = document.createElement("span");
    mark.className = className;
    mark.dataset.lrSelectionMark = kind;
    if (slug) mark.dataset.wikiSlug = slug;
    selected.parentNode?.insertBefore(mark, after);
    mark.append(selected);
  }
}

function markSelectionWikiTerms(selection: ReaderSelection) {
  const preferred = selection.matches.filter(match => match.literalMatch).sort((a, b) => Number(b.literalMatch) - Number(a.literalMatch) || b.score - a.score || b.term.length - a.term.length);
  const targets = new Map<string, SelectionWikiMatch>();
  for (const match of preferred) {
    const key = match.term.toLocaleLowerCase();
    if (!targets.has(key)) targets.set(key, match);
  }
  for (const selected of selection.ranges) {
    const paragraph = document.getElementById(selected.paragraphId);
    if (!paragraph) continue;
    const context = mentionContext(paragraph.textContent ?? '', selected.startOffset, selected.endOffset);
    const candidates: { start: number; end: number; match: SelectionWikiMatch }[] = [];
    for (const match of targets.values()) {
      for (const occurrence of wholeWordOccurrences(context, match.term)) candidates.push({ ...occurrence, match });
    }
    candidates.sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
    const accepted: typeof candidates = [];
    for (const candidate of candidates) if (!accepted.some((item) => candidate.start < item.end && candidate.end > item.start)) accepted.push(candidate);
    for (const candidate of accepted.reverse()) wrapTextOffsets(paragraph, candidate.start, candidate.end, "wiki-term-highlight", "term", candidate.match.slug);
  }
}

function clearReaderSelectionMarks(root: HTMLElement | null) {
  if (!root) return;
  for (const kind of ["term", "selection"]) {
    for (const mark of root.querySelectorAll<HTMLElement>(`[data-lr-selection-mark="${kind}"]`)) mark.replaceWith(...mark.childNodes);
  }
  root.normalize();
}

function SelectionWikiLinks({ matches, onOpenWiki }: { matches: SelectionWikiMatch[]; onOpenWiki: (slug: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const visible = selectionLinkMatches(matches, expanded);
  return <div className="selection-wiki">{[true, false].map(literal => {
    const items = visible.filter(item => item.literalMatch === literal);
    if (!items.length) return null;
    return <section key={String(literal)} className={`selection-match-group ${literal ? 'direct-matches' : 'related-matches'}`}>{!literal && <h4>相关词条</h4>}<div className="selection-link-list">{items.map(item => {
      const aliasHit = item.literalMatch && normalizeMention(item.term) !== normalizeMention(item.title);
      return <button key={item.slug} className={literal ? 'literal' : 'related'} title={literal ? aliasHit ? `${item.term} → ${item.title}` : '精准匹配' : '相关性匹配'} onClick={() => onOpenWiki(item.slug)}>{aliasHit ? <span className="alias-pair"><strong>{item.term}</strong><em>{item.title}</em></span> : <span>{item.title}</span>}<ChevronRight size={14} /></button>;
    })}</div></section>;
  })}{matches.length > 6 && <button type="button" className="selection-more-toggle" onClick={() => setExpanded(value => !value)}>{expanded ? '收起词条' : `展开其余 ${matches.length - 6} 项`}</button>}</div>;
}

function selectionLinkMatches<T extends { literalMatch: boolean; score: number }>(matches: T[], expanded: boolean) {
  const ranked = [...matches].sort((a, b) => Number(b.literalMatch) - Number(a.literalMatch) || b.score - a.score);
  return expanded ? ranked : ranked.slice(0, 6);
}

function selectionValueStrings(value: unknown) { return Array.isArray(value) ? value.map(String).filter(Boolean) : []; }

function normalizeMention(value: string) { return value.replace(/[\s·・—_\-:：,，。;；/\\()（）\[\]【】]+/g, "").toLocaleLowerCase(); }

export function ReadingPreferences({ paperColor, setPaperColor, showTheme = true, theme, setTheme, fontSize, setFontSize, lineHeight, setLineHeight, epubFeatures, setEpubFeatures, illustrations, setIllustrations, spoilerShieldMode, setSpoilerShieldMode, semanticRetrieval, setSemanticRetrieval, embeddingStatus }: { paperColor: PaperColor; setPaperColor: (value: PaperColor) => void; showTheme?: boolean; theme: Theme; setTheme: (value: Theme) => void; fontSize: number; setFontSize: (value: number) => void; lineHeight: number; setLineHeight: (value: number) => void; epubFeatures: EpubFeatureMode; setEpubFeatures: (value: EpubFeatureMode) => void; illustrations: IllustrationMode; setIllustrations: (value: IllustrationMode) => void; spoilerShieldMode: "off" | "auto" | "strict"; setSpoilerShieldMode: (value: "off" | "auto" | "strict") => void; semanticRetrieval: boolean; setSemanticRetrieval: (value: boolean) => void; embeddingStatus: EmbeddingIndexStatus | null }) { return (          <div className="reader-settings-body">
            <EpubSettingsDemo paperColor={paperColor} theme={theme} fontSize={fontSize} lineHeight={lineHeight} epubFeatures={epubFeatures} illustrations={illustrations} />
            <div className="reader-settings-controls">
              <section className="reader-setting-section"><h3>排版</h3><div className="reader-slider-grid">
                <label><span>字号</span><output>{fontSize}px</output><input type="range" min="14" max="23" value={fontSize} onChange={(event) => { const value = Number(event.target.value); setFontSize(value); storage.setItem("lr:reader-font-size", String(value)); }} /></label>
                <label><span>行距</span><output>{lineHeight.toFixed(2)}</output><input type="range" min="1.55" max="2.4" step="0.05" value={lineHeight} onChange={(event) => { const value = Number(event.target.value); setLineHeight(value); storage.setItem("lr:reader-line-height", String(value)); }} /></label>
              </div></section>
              <SettingsChoice title="纸张颜色" value={paperColor} options={[{ value: "standard", label: "标准", description: "使用当前主题的标准底色", icon: <FileText /> }, { value: "warm", label: "纸色", description: "略暖的纸张底色与细微纹理", icon: <BookOpen /> }]} onChange={value => { setPaperColor(value); storage.setItem("lr:paper-color", value); }} />
              <SettingsChoice title="EPUB 特性" value={epubFeatures} options={[{ value: "off", label: "关", description: "仅保留纯文本与插图", icon: <EyeOff /> }, { value: "basic", label: "基础", description: "保留强调、对齐等稳定样式", icon: <BookOpen /> }, { value: "advanced", label: "高级", description: "还原字体、颜色与精细排版", icon: <Layers /> }]} onChange={(value) => { setEpubFeatures(value); storage.setItem("lr:epub-features", value); }} />
              <SettingsChoice title="插图" value={illustrations} options={[{ value: "off", label: "关", description: "隐藏正文中的所有插图", icon: <ImageOff /> }, { value: "original", label: "原图", description: "保留图片原貌与轻微阴影", icon: <ImageIcon /> }, { value: "immersive", label: "沉浸", description: "明亮主题下自然叠入纸张", icon: <Images /> }]} onChange={(value) => { setIllustrations(value); storage.setItem("lr:illustrations", value); }} />
              {showTheme && <SettingsChoice title="主题" value={theme} options={[{ value: "paper", label: "明亮", description: "柔和纸张背景，适合日间阅读", icon: <Sun /> }, { value: "dark", label: "深色", description: "降低页面亮度，适合夜间阅读", icon: <Moon /> }, { value: "system", label: "跟随系统", description: "自动匹配设备的外观设置", icon: <Monitor /> }]} onChange={(value) => { setTheme(value); storage.setItem("lr:theme", value); }} />}
              <SettingsChoice title="剧透屏蔽" value={spoilerShieldMode} options={[{ value: "off", label: "关", description: "显示所有 Wiki 内容", icon: <ShieldOff /> }, { value: "auto", label: "自动", description: "根据当前阅读进度自动解锁", icon: <Shield /> }, { value: "strict", label: "严格", description: "始终屏蔽所有剧透，另外提供更严格的屏蔽特性", icon: <ShieldAlert /> }]} onChange={(value) => { setSpoilerShieldMode(value); storage.setItem("lr:spoiler-shield", value); }} />
              <SettingsChoice title="语义化检索" value={semanticRetrieval ? "semantic" : "lexical"} options={[{ value: "lexical", label: "普通匹配", description: "按原文词语精确匹配", icon: <Search /> }, { value: "semantic", label: "语义化", description: embeddingStatus?.available ? "结合向量语义与文本匹配" : "索引不可用时自动回退普通匹配", icon: <Sparkles />, disabled: !embeddingStatus?.configured }]} onChange={(value) => { const enabled = value === "semantic" && Boolean(embeddingStatus?.configured); setSemanticRetrieval(enabled); storage.setItem("lr:semantic-retrieval", enabled ? "1" : "0"); }} note={!embeddingStatus?.configured ? "Embedding 服务尚未配置，当前固定使用普通文本匹配。" : "仅影响阅读与对话中的原文检索；索引或服务不可用时会自动回退普通文本匹配。"} />
            </div>
          </div>
); }
