import { usePanelVisibility } from "./panel-visibility";
import { AnimatedRegion } from "./animated-region";
import { SidebarHeader } from './sidebar';


import { ArrowLeft, BookOpen, ChevronDown, ChevronRight, Compass, FileText, Home, ListTree, Lock, PanelLeftClose, PanelLeftOpen, Sparkles } from "./icons";
import { Children, createContext, type ComponentPropsWithoutRef, ReactNode, useContext, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_ORIGIN, libraryApi } from "./lib/api";
import type { NarrativeTrackRecord, SeriesSummary, SpoilerAccessRule, WikiBlock, WikiEntryRecord, WikiEntrySummary, WikiState } from "./types";

export interface SpoilerViewState { mode: "off" | "auto" | "strict"; activeChapterId: string; readerProgress: number; unlocked: Set<string>; onUnlock: (key: string) => void; }

export function WikiWorkspace({ series, selectedSlug = "home", onSelected, onOpenLocator, spoiler }: { series: SeriesSummary; selectedSlug?: string; onSelected?: (slug: string) => void; onOpenLocator?: (locator: string) => void; spoiler: SpoilerViewState }) {
  return <WikiViewer series={series} selectedSlug={selectedSlug} onSelected={onSelected} onOpenLocator={onOpenLocator} spoiler={spoiler} />;
}

export function WikiSideViewer({ onBack, series, selectedSlug, onSelected, onOpenFull, onOpenLocator, onTrackChange, spoiler }: { series: SeriesSummary; selectedSlug: string; onBack?: () => void; onSelected: (slug: string) => void; onOpenFull: () => void; onOpenLocator: (locator: string) => void; onTrackChange?: (items: NarrativeTrackRecord[]) => void; spoiler: SpoilerViewState }) {
  return <WikiViewer onBack={onBack} compact series={series} selectedSlug={selectedSlug} onSelected={onSelected} onOpenFull={onOpenFull} onOpenLocator={onOpenLocator} onTrackChange={onTrackChange} spoiler={spoiler} />;
}

function WikiViewer({ onBack, series, selectedSlug, compact = false, onSelected, onOpenFull, onOpenLocator, onTrackChange, spoiler }: { series: SeriesSummary; selectedSlug: string; onBack?: () => void; compact?: boolean; onSelected?: (slug: string) => void; onOpenFull?: () => void; onOpenLocator?: (locator: string) => void; onTrackChange?: (items: NarrativeTrackRecord[]) => void; spoiler: SpoilerViewState }) {
  const [state, setState] = useState<WikiState | null>(null);
  const [entry, setEntry] = useState<WikiEntryRecord | null>(null);
  const [error, setError] = useState("");
  const [navVisible, setNavVisible, wikiNavRef, navExpanded] = usePanelVisibility(true, "left");
  const navCollapsed = !navExpanded;
  const [compactDirectory, setCompactDirectory] = useState(false);
  const entryCache = useRef(new Map<string, WikiEntryRecord>());
  const entryScrollRef = useRef<HTMLElement>(null);

  useEffect(() => { void libraryApi.getWikiState(series.id).then(setState).catch((cause) => setError(cause instanceof Error ? cause.message : "Wiki 无法载入")); }, [series.id]);
  const category = useMemo(() => state?.categories.find((item) => `category:${item.id}` === selectedSlug) ?? null, [selectedSlug, state]);
  const resolvedSlug = useMemo(() => {
    if (!state || selectedSlug.startsWith("category:")) return null;
    const title = selectedSlug.startsWith("title:") ? selectedSlug.slice(6) : "";
    return (state.entries.find((item) => item.slug === selectedSlug)
      ?? (title ? state.entries.find((item) => item.title === title) : null)
      ?? state.entries.find((item) => item.slug === "home") ?? null)?.slug ?? null;
  }, [selectedSlug, state]);
  useEffect(() => {
    if (!resolvedSlug || category || compactDirectory) { if (compact) onTrackChange?.([]); return; }
    const cached = entryCache.current.get(resolvedSlug);
    let live = true;
    const request = cached ? Promise.resolve(cached) : libraryApi.getWikiEntry(series.id, resolvedSlug);
    void request.then((record) => {
      entryCache.current.set(resolvedSlug, record); if (live) setEntry(record);
    }).catch((cause) => { if (live) setError(cause instanceof Error ? cause.message : "词条无法载入"); });
    return () => { live = false; };
  }, [category, compact, compactDirectory, onTrackChange, resolvedSlug, series.id]);
  useEffect(() => { if (compact) onTrackChange?.((entry?.slug === resolvedSlug ? entry.track_items : []).map((item) => ({ ...item, start_locator: normalizeWikiLocator(item.start_locator, series), end_locator: item.end_locator ? normalizeWikiLocator(item.end_locator, series) : null }))); }, [compact, entry, onTrackChange, resolvedSlug, series]);

  const entriesByKind = useMemo(() => {
    const map = new Map<string, WikiEntrySummary[]>();
    for (const item of state?.entries ?? []) {
      if (item.level === "home") continue;
      const group = map.get(item.kind) ?? [];
      group.push(item); map.set(item.kind, group);
    }
    return map;
  }, [state]);
  const displayedEntry = entry?.slug === resolvedSlug ? entry : null;
  const entryLocked = displayedEntry ? spoilerRuleLocked(displayedEntry.spoiler_policy.entry, series, spoiler, `${displayedEntry.slug}:entry`) : false;
  const waitingForEntry = Boolean(!category && !compactDirectory && resolvedSlug && !displayedEntry);

  function choose(slug: string) {
    setCompactDirectory(false);
    onSelected?.(slug);
  }

  function openLocator(locator: string) {
    onOpenLocator?.(normalizeWikiLocator(locator, series));
  }

  if (error) return <div className="wiki-status"><strong>Wiki 暂时无法载入</strong><span>{error}</span></div>;
  if (!state) return <WikiSkeleton compact={compact} />;
  if (!state.blueprint) return <WikiCompilationGate series={series} state={state} compact={compact} onOpenFull={onOpenFull} />;

  return <section className={`wiki-system ${compact ? "compact" : ""} ${navCollapsed ? "nav-collapsed" : ""}`}>
    {!compact && navCollapsed && <button className="wiki-nav-toggle" onClick={() => setNavVisible((value) => !value)} aria-label={navCollapsed ? "展开 Wiki 导航" : "收起 Wiki 导航"}>{navCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}</button>}
    {!compact && <div className="wiki-directory-slot" data-present={navVisible}>{navVisible && <aside className="wiki-navigation" ref={wikiNavRef}>
      <SidebarHeader title="作品 Wiki" actions={<button aria-label="收起 Wiki 导航" onClick={() => setNavVisible(false)}><PanelLeftClose size={16} /></button>} />
      <div className="sidebar-scroll wiki-directory-scroll"><div className="wiki-directory-name">{state.blueprint.home_title}</div>
      <button className={resolvedSlug === "home" ? "active" : ""} onClick={() => choose("home")}><Home size={15} />作品首页</button>
      {(entriesByKind.get("special") ?? []).length > 0 && <section className="wiki-nav-group wiki-special-group"><span>专题</span>{entriesByKind.get("special")!.map((item) => <button key={item.slug} className={resolvedSlug === item.slug ? "active" : ""} onClick={() => choose(item.slug)}><SpoilerAwareSidebarTitle entry={item} series={series} spoiler={spoiler} strong /><WikiEntryTeaser entry={item} series={series} spoiler={spoiler} /></button>)}</section>}
      {state.categories.map((category) => {
        const matches = [...entriesByKind.entries()].filter(([kind]) => entryKindMatchesCategory(kind, category)).flatMap(([, values]) => values);
        return <section className="wiki-nav-group" key={category.id}><button className={`wiki-category-link ${selectedSlug === `category:${category.id}` ? "active" : ""}`} onClick={() => choose(`category:${category.id}`)}>{category.label}<ChevronRight size={13} /></button>{matches.map((item) => <button key={item.slug} className={resolvedSlug === item.slug ? "active" : ""} onClick={() => choose(item.slug)}><SpoilerAwareSidebarTitle entry={item} series={series} spoiler={spoiler} /><small>{levelLabel(item.level)}</small></button>)}</section>;
      })}
      </div>
    </aside>}</div>}
    <article ref={entryScrollRef} className={`wiki-entry-scroll ${entryLocked ? "spoiler-scroll-locked" : ""}`}>
      {compact && <nav className="compact-wiki-bar sidebar-module-bar" aria-label="Wiki 词条导航"><button aria-label="返回上一个侧栏" title="返回" disabled={!onBack && resolvedSlug === "home" && !compactDirectory} onClick={() => { if (onBack) { onBack(); return; } if (compactDirectory) setCompactDirectory(false); else choose("home"); }}><ArrowLeft size={16} /></button><button aria-label="作品首页" title="首页" onClick={() => choose("home")}><Home size={16} /></button><button aria-label="词条目录" title="目录" aria-pressed={compactDirectory} onClick={() => setCompactDirectory(value => !value)}><ListTree size={16} /></button><button className="open-full" aria-label="打开完整 Wiki" title="完整 Wiki" onClick={onOpenFull}><BookOpen size={16} /></button></nav>}

      <AnimatedRegion className="wiki-entry-stage" transitionKey={compactDirectory ? "directory" : selectedSlug} ready={!waitingForEntry} onCommit={() => entryScrollRef.current?.scrollTo({ top: 0, behavior: "auto" })}>{compactDirectory ? <CompactWikiDirectory state={state} entriesByKind={entriesByKind} onSelect={choose} series={series} spoiler={spoiler} /> : category ? <CategoryLanding category={category} state={state} series={series} spoiler={spoiler} onSelect={choose} onOpenLocator={openLocator} /> : waitingForEntry ? <WikiEntryLoading /> : displayedEntry ? <div className={entryLocked ? "spoiler-entry-locked" : ""}><EntryHeader entry={displayedEntry} state={state} spoiler={spoiler} series={series} /><div className={`wiki-entry-body ${Object.keys(displayedEntry.infobox ?? {}).length ? "has-infobox" : ""} ${displayedEntry.track_items.length ? "has-track" : ""}`}>
        {Object.keys(displayedEntry.infobox ?? {}).length > 0 && <div className="wiki-infobox-rail"><WikiInfobox key={displayedEntry.slug} compact={compact} entry={displayedEntry} onSelect={choose} spoiler={spoiler} series={series} /></div>}
        <div className="wiki-editorial-column">{displayedEntry.body_markdown && <section className="wiki-markdown"><SpoilerMarkdown markdown={displayedEntry.body_markdown} entry={displayedEntry} prefix="body" state={state} series={series} spoiler={spoiler} onSelect={choose} onOpenLocator={openLocator} /></section>}
        {displayedEntry.blocks.map((block, index) => <CompactBlockFold key={`${block.type}-${index}`} compact={compact} block={block} seriesItemCount={block.type === "volume_cards" ? series.volumes.length : 0}><SpoilerFragment target={`block:${index}:title`} entry={displayedEntry} series={series} spoiler={spoiler}><SpoilerFragment target={`block:${index}:data`} entry={displayedEntry} series={series} spoiler={spoiler}><WikiBlockRenderer block={block} blockIndex={index} entry={displayedEntry} state={state} series={series} spoiler={spoiler} onSelect={choose} onOpenLocator={openLocator} /></SpoilerFragment></SpoilerFragment></CompactBlockFold>)}</div>
        {displayedEntry.track_items.length > 0 && <NarrativeTrack key={displayedEntry.slug} entry={displayedEntry} series={series} spoiler={spoiler} onOpenLocator={openLocator} />}
        <WikiNavigationBoxes state={state} currentSlug={displayedEntry.slug} onSelect={choose} />
      </div>{entryLocked && <EntryLockPanel entry={displayedEntry} series={series} spoiler={spoiler} />}</div> : <div className="wiki-status"><strong>词条暂时不可用</strong><span>请从目录选择其他页面。</span></div>}</AnimatedRegion>
    </article>
  </section>;
}

function WikiSkeleton({ compact }: { compact: boolean }) {
  return <section className={`wiki-skeleton ${compact ? "compact" : ""}`} aria-label="正在打开作品 Wiki"><aside>{[70, 88, 62, 79, 68].map((width, index) => <i key={index} style={{ width: `${width}%` }} />)}</aside><article><header><i /><i /><i /></header><div>{[94, 86, 91, 72, 88, 64].map((width, index) => <i key={index} style={{ width: `${width}%` }} />)}</div></article></section>;
}

function WikiEntryLoading() {
  return <div className="wiki-entry-loading" aria-label="正在载入词条"><header><i /><i /><i /></header><div>{[92, 74, 86, 68, 81].map((width) => <i key={width} style={{ width: `${width}%` }} />)}</div></div>;
}

function EntryLockPanel({ entry, series, spoiler }: { entry: WikiEntryRecord; series: SeriesSummary; spoiler: SpoilerViewState }) {
  const rule = entry.spoiler_policy.entry;
  const condition = rule.mode === "progress" && rule.unlock_after_chapter_id ? chapterUnlockLabel(rule.unlock_after_chapter_id, series) : "不会随阅读进度自动解锁";
  return <div className="spoiler-entry-overlay"><div><Lock size={24} /><span>剧透屏蔽</span><strong>词条已锁定</strong><p>{condition}</p>{rule.reason && <small>{rule.reason}</small>}<button onClick={() => spoiler.onUnlock(`${entry.slug}:entry`)}>仅在本次浏览中解锁</button></div></div>;
}

function WikiEntryTeaser({ entry, spoiler }: { entry: WikiEntrySummary; series: SeriesSummary; spoiler: SpoilerViewState }) {
  return <small className={spoiler.mode === "strict" ? "wiki-strict-teaser" : undefined} aria-hidden={spoiler.mode === "strict" || undefined}>{entry.summary}</small>;
}

function SpoilerAwareSidebarTitle({ entry, series, spoiler, strong = false }: { entry: WikiEntrySummary; series: SeriesSummary; spoiler: SpoilerViewState; strong?: boolean }) {
  const rule = entry.spoiler_policy?.entry;
  const marked = spoiler.mode !== "off" && Boolean(rule && rule.mode !== "open");
  const locked = marked && spoilerRuleLocked(rule, series, spoiler, `${entry.slug}:entry`);
  const content = <><span className="wiki-sidebar-title-lock" aria-hidden="true">{locked && <Lock />}</span><span className="wiki-sidebar-title-text">{entry.title}</span></>;
  const className = `wiki-sidebar-entry-title ${locked ? "locked" : marked ? "unlocked" : "open"}`;
  return strong ? <strong className={className}>{content}</strong> : <span className={className}>{content}</span>;
}

function CompactWikiDirectory({ state, entriesByKind, onSelect, series, spoiler }: { state: WikiState; entriesByKind: Map<string, WikiEntrySummary[]>; onSelect: (slug: string) => void; series: SeriesSummary; spoiler: SpoilerViewState }) {
  return <div className="compact-wiki-directory"><header><span className="eyebrow">Wiki directory</span><h1>作品图鉴</h1><p>按专题与类目浏览全部词条。</p></header>{(entriesByKind.get("special") ?? []).length > 0 && <section><h2>专题</h2><div className="compact-directory-grid specials">{entriesByKind.get("special")!.map((entry) => <button key={entry.slug} onClick={() => onSelect(entry.slug)}><span>专题阅读</span><SpoilerAwareSidebarTitle entry={entry} series={series} spoiler={spoiler} strong /><WikiEntryTeaser entry={entry} series={series} spoiler={spoiler} /></button>)}</div></section>}{state.categories.map((category) => { const entries = [...entriesByKind.entries()].filter(([kind]) => entryKindMatchesCategory(kind, category)).flatMap(([, values]) => values); if (!entries.length) return null; return <section key={category.id}><h2>{category.label}</h2><p>{category.description}</p><div className="compact-directory-grid">{entries.map((entry) => <button key={entry.slug} onClick={() => onSelect(entry.slug)}><SpoilerAwareSidebarTitle entry={entry} series={series} spoiler={spoiler} strong /><WikiEntryTeaser entry={entry} series={series} spoiler={spoiler} /></button>)}</div></section>; })}</div>;
}

function CompactBlockFold({ compact, block, children, seriesItemCount = 0 }: { compact: boolean; block: WikiBlock; children: ReactNode; seriesItemCount?: number }) {
  const [expanded, setExpanded] = useState(false);
  const count = blockItems(block.data, "items", "cards", "entries", "entities", "buttons", "modules", "chapters", "volumes").length
    || ((block.data.entry_slugs as unknown[] | undefined)?.length ?? 0) || seriesItemCount;
  const foldable = compact && count > 2 && ["module_buttons", "info_cards", "related_entries", "entity_list", "chapter_cards", "volume_cards"].includes(block.type);
  return <div className={`compact-module-fold ${foldable ? "foldable" : ""} ${expanded ? "expanded" : ""}`}><div className="compact-module-clip">{children}</div>{foldable && <button className="compact-module-toggle" onClick={() => setExpanded((value) => !value)}>{expanded ? <><ChevronDown size={13} />收起</> : <><ChevronDown size={13} />展开剩余 {count - 2} 项</>}</button>}</div>;
}

function WikiCompilationGate({ series, state, compact, onOpenFull }: { series: SeriesSummary; state: WikiState; compact: boolean; onOpenFull?: () => void }) {
  return <div className={`wiki-compilation-gate ${compact ? "compact" : ""}`}><BookOpen size={28} /><h2>尚未建立 Wiki</h2><p>分析本书后，即可浏览人物、情节与词条。</p><button onClick={() => window.dispatchEvent(new Event('understory-analysis'))}><Sparkles size={16} />分析本书</button></div>;
}

function EntryHeader({ entry, state, spoiler, series }: { entry: WikiEntryRecord; state: WikiState; spoiler: SpoilerViewState; series: SeriesSummary }) {
  return <header className={`wiki-entry-header ${entry.level === "home" ? "home" : ""}`}><div><span className="wiki-entry-kind">{kindLabel(entry.kind)} · {levelLabel(entry.level)}</span><SpoilerFragment target="title" entry={entry} series={series} spoiler={spoiler}><h1>{entry.title}</h1></SpoilerFragment>{entry.level !== "home" && <SpoilerFragment target="summary" entry={entry} series={series} spoiler={spoiler}><p>{entry.summary}</p></SpoilerFragment>}</div><dl><div><dt>修订</dt><dd>r{entry.revision}</dd></div><div><dt>覆盖</dt><dd>{state.compilation_status.complete_work_coverage ? "全作品" : "部分卷"}</dd></div></dl></header>;
}

function CategoryLanding({ category, state, series, spoiler, onSelect, onOpenLocator }: { category: WikiState["categories"][number]; state: WikiState; series: SeriesSummary; spoiler: SpoilerViewState; onSelect: (slug: string) => void; onOpenLocator?: (locator: string) => void }) {
  return <div className="wiki-category-page"><header><span className="eyebrow">词条类目</span><h1>{category.label}</h1><p>{category.introduction || category.description}</p></header><div className="wiki-category-editorial">{category.body_markdown && <section className="wiki-markdown"><WikiMarkdown markdown={category.body_markdown} entries={state.entries} onSelect={onSelect} onOpenLocator={onOpenLocator} /></section>}{(category.blocks ?? []).map((block, index) => <WikiBlockRenderer key={`${block.type}-${index}`} block={block} state={state} series={series} spoiler={spoiler} onSelect={onSelect} onOpenLocator={onOpenLocator} />)}</div></div>;
}

function WikiInfobox({ entry, onSelect, compact = false, spoiler, series }: { entry: WikiEntryRecord; onSelect: (slug: string) => void; compact?: boolean; spoiler: SpoilerViewState; series: SeriesSummary }) {
  const fieldLabels = entry.metadata.infobox_labels as Record<string, string> | undefined;
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const rows = Object.entries(entry.infobox).filter(([, value]) => value !== "" && value !== null && value !== undefined && (!Array.isArray(value) || value.length));
  const visibleRows = compact && !showAll ? rows.slice(0, 10) : rows;
  return <aside className={`wiki-infobox ${compact && rows.length > 10 && !showAll ? "is-clipped" : ""}`}><div><span>{kindLabel(entry.kind)}</span><strong>{entry.title}</strong></div><dl>{visibleRows.map(([key, value]) => <SpoilerFragment key={key} target={`infobox:${key}`} entry={entry} series={series} spoiler={spoiler}><WikiInfoboxRow label={fieldLabels?.[key] || infoboxLabel(key)} value={value} expanded={expandedKey === key} onToggle={() => setExpandedKey((current) => current === key ? null : key)} onSelect={onSelect} entry={entry} series={series} spoiler={spoiler} /></SpoilerFragment>)}</dl>{compact && rows.length > 10 && <button className="wiki-infobox-more" aria-expanded={showAll} onClick={() => setShowAll((value) => !value)}>{showAll ? "收起信息表" : `展开剩余 ${rows.length - 10} 项`}<ChevronDown size={13} /></button>}</aside>;
}

function WikiInfoboxRow({ label, value, expanded, onToggle, onSelect, entry, series, spoiler }: { label: string; value: unknown; expanded: boolean; onToggle: () => void; onSelect: (slug: string) => void; entry: WikiEntryRecord; series: SeriesSummary; spoiler: SpoilerViewState }) {
  const valueRef = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  useEffect(() => {
    const element = valueRef.current;
    if (!element) return;
    const measure = () => {
      const collapsedHeight = parseFloat(getComputedStyle(element).fontSize) * 3;
      setOverflowing(element.scrollHeight > collapsedHeight + 1);
    };
    const frame = requestAnimationFrame(measure);
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [expanded, value]);
  const toggleFromRow = (target: EventTarget | null) => {
    if (!overflowing || (target instanceof Element && target.closest("button, a"))) return;
    onToggle();
  };
  return <div className={`wiki-infobox-row ${expanded ? "expanded" : ""} ${overflowing ? "can-expand" : ""}`} onClick={(event) => toggleFromRow(event.target)}><dt>{label}</dt><dd><div ref={valueRef} className="infobox-value">{renderInfoboxValue(value, onSelect, entry, series, spoiler)}</div></dd>{overflowing && <button type="button" className="infobox-expand-toggle" aria-expanded={expanded} aria-label={expanded ? `收起${label}` : `展开${label}`} onClick={onToggle}><ChevronDown size={14} /></button>}</div>;
}

function renderInfoboxValue(value: unknown, onSelect: (slug: string) => void, entry: WikiEntryRecord, series: SeriesSummary, spoiler: SpoilerViewState): React.ReactNode {
  if (Array.isArray(value)) return <>{value.map((item, index) => <span className="infobox-chip" key={index}>{renderInfoboxValue(item, onSelect, entry, series, spoiler)}</span>)}</>;
  if (value && typeof value === "object") { const item = value as Record<string, unknown>; if (item.slug) return <button onClick={() => onSelect(String(item.slug))}>{renderMarkedWikiText(String(item.label ?? item.title ?? item.slug), [], onSelect, entry, series, spoiler)}</button>; return renderMarkedWikiText(String(item.value ?? item.label ?? ""), [], onSelect, entry, series, spoiler); }
  return renderMarkedWikiText(String(value), [], onSelect, entry, series, spoiler);
}

type WikiMarkdownRenderState = {
  entries: WikiEntrySummary[];
  onSelect: (slug: string) => void;
  onOpenLocator?: (locator: string) => void;
  linked: (children: ReactNode) => ReactNode;
};

const WikiMarkdownRenderContext = createContext<WikiMarkdownRenderState | null>(null);

function useWikiMarkdownRenderState() {
  const value = useContext(WikiMarkdownRenderContext);
  if (!value) throw new Error("Wiki Markdown renderer is missing its context");
  return value;
}

function WikiMarkdownLink({ href, children }: ComponentPropsWithoutRef<"a">) {
  const current = useWikiMarkdownRenderState();
  if (href?.startsWith("lr://wiki/")) {
    const rawSlug = href.slice("lr://wiki/".length).split(/[?#]/, 1)[0];
    let slug = rawSlug;
    try { slug = decodeURIComponent(rawSlug); } catch { /* keep the literal slug */ }
    return current.entries.some((item) => item.slug === slug)
      ? <button type="button" className="wiki-term-link" onClick={() => current.onSelect(slug)}>{children}</button>
      : <span>{children}</span>;
  }
  return href?.startsWith("lr://") ? <button type="button" className="wiki-citation" title={`跳转到 ${shortLocator(href)}`} onClick={() => current.onOpenLocator?.(href)}><BookOpen size={12} />{children}</button> : <a href={href}>{children}</a>;
}

function WikiMarkdownParagraph({ children }: ComponentPropsWithoutRef<"p">) { return <p>{useWikiMarkdownRenderState().linked(children)}</p>; }
function WikiMarkdownListItem({ children }: ComponentPropsWithoutRef<"li">) { return <li>{useWikiMarkdownRenderState().linked(children)}</li>; }
function WikiMarkdownTableCell({ children }: ComponentPropsWithoutRef<"td">) { return <td>{useWikiMarkdownRenderState().linked(children)}</td>; }
function WikiMarkdownTableHeadCell({ children }: ComponentPropsWithoutRef<"th">) { return <th>{useWikiMarkdownRenderState().linked(children)}</th>; }
function WikiMarkdownTable({ children }: ComponentPropsWithoutRef<"table">) { return <div className="markdown-table-scroll"><table>{children}</table></div>; }
function WikiMarkdownStrong({ children }: ComponentPropsWithoutRef<"strong">) { return <strong>{useWikiMarkdownRenderState().linked(children)}</strong>; }
function WikiMarkdownHeading2({ children }: ComponentPropsWithoutRef<"h2">) { return <h2>{useWikiMarkdownRenderState().linked(children)}</h2>; }
function WikiMarkdownHeading3({ children }: ComponentPropsWithoutRef<"h3">) { return <h3>{useWikiMarkdownRenderState().linked(children)}</h3>; }

const WIKI_MARKDOWN_COMPONENTS: Components = {
  a: WikiMarkdownLink,
  p: WikiMarkdownParagraph,
  li: WikiMarkdownListItem,
  table: WikiMarkdownTable,
  th: WikiMarkdownTableHeadCell,
  td: WikiMarkdownTableCell,
  strong: WikiMarkdownStrong,
  h2: WikiMarkdownHeading2,
  h3: WikiMarkdownHeading3,
};

function WikiMarkdown({ markdown, entries, currentSlug, onSelect, onOpenLocator, entry, series, spoiler, spoilerScope }: { markdown: string; entries: WikiEntrySummary[]; currentSlug?: string; onSelect: (slug: string) => void; onOpenLocator?: (locator: string) => void; entry?: WikiEntryRecord; series?: SeriesSummary; spoiler?: SpoilerViewState; spoilerScope?: string }) {
  const terms = useMemo(() => buildWikiTerms(entries, currentSlug), [currentSlug, entries]);
  const prepared = useMemo(() => prepareSpoilerMarkdown(markdown, spoilerScope), [markdown, spoilerScope]);
  const linked = (children: ReactNode) => renderWikiRichText(children, terms, onSelect, entry, series, spoiler, prepared.fragments, onOpenLocator);
  return <WikiMarkdownRenderContext.Provider value={{ entries, onSelect, onOpenLocator, linked }}><ReactMarkdown urlTransform={(url) => url.startsWith("lr://") ? url : defaultUrlTransform(url)} remarkPlugins={[remarkGfm]} components={WIKI_MARKDOWN_COMPONENTS}>{normalizeWikiMarkdown(prepared.markdown)}</ReactMarkdown></WikiMarkdownRenderContext.Provider>;
}

function SpoilerMarkdown({ markdown, entry, prefix, state, series, spoiler, onSelect, onOpenLocator }: { markdown: string; entry: WikiEntryRecord; prefix: string; state: WikiState; series: SeriesSummary; spoiler: SpoilerViewState; onSelect: (slug: string) => void; onOpenLocator?: (locator: string) => void }) {
  if (markdown.includes("⟦SP:")) return <WikiMarkdown markdown={markdown} entries={state.entries} currentSlug={entry.slug} onSelect={onSelect} onOpenLocator={onOpenLocator} entry={entry} series={series} spoiler={spoiler} spoilerScope={prefix} />;
  return <>{splitWikiParagraphs(markdown).map((paragraph, index) => <SpoilerFragment key={index} target={`${prefix}:${index}`} entry={entry} series={series} spoiler={spoiler}><WikiMarkdown markdown={paragraph} entries={state.entries} currentSlug={entry.slug} onSelect={onSelect} onOpenLocator={onOpenLocator} entry={entry} series={series} spoiler={spoiler} /></SpoilerFragment>)}</>;
}

function SpoilerFragment({ target, entry, series, spoiler, children }: { target: string; entry: WikiEntryRecord; series: SeriesSummary; spoiler: SpoilerViewState; children: ReactNode }) {
  const rule = entry.spoiler_policy.fragments.find((item) => item.target === target);
  const unlockKey = `${entry.slug}:${target}`;
  if (!rule) return <>{children}</>;
  const locked = spoilerRuleLocked(rule, series, spoiler, unlockKey);
  const manuallyUnlocked = spoiler.unlocked.has(unlockKey);
  const toggleable = locked || manuallyUnlocked;
  return <div className={`spoiler-fragment ${locked ? "locked" : "unlocked"}`} role={toggleable ? "button" : undefined} tabIndex={toggleable ? 0 : undefined} aria-label={locked ? "剧透内容，点击临时解锁" : manuallyUnlocked ? "已临时解锁，点击重新屏蔽" : "已按阅读进度解锁的剧透内容"} onClick={() => toggleable && spoiler.onUnlock(unlockKey)} onKeyDown={(event) => { if (toggleable && (event.key === "Enter" || event.key === " ")) spoiler.onUnlock(unlockKey); }}><div className="spoiler-fragment-content">{children}</div><span className="spoiler-fragment-cover">{locked ? spoilerUnlockHint(rule, series) : "点击重新屏蔽"}</span></div>;
}

function spoilerRuleLocked(rule: SpoilerAccessRule | undefined, series: SeriesSummary, spoiler: SpoilerViewState, unlockKey: string) {
  if (spoiler.mode === "off" || !rule || rule.mode === "open" || spoiler.unlocked.has(unlockKey)) return false;
  if (spoiler.mode === "strict") return true;
  if (rule.mode === "never") return true;
  const chapters = series.volumes.flatMap((volume) => volume.chapters);
  const currentIndex = chapters.findIndex((chapter) => chapter.id === spoiler.activeChapterId);
  const unlockIndex = chapters.findIndex((chapter) => chapter.id === rule.unlock_after_chapter_id);
  if (unlockIndex < 0) return true;
  return currentIndex < unlockIndex || (currentIndex === unlockIndex && spoiler.readerProgress < 0.98);
}

function splitWikiParagraphs(markdown: string) {
  return markdown.trim().split(/\n\s*\n/).filter((item) => item.trim());
}

function WikiBlockRenderer({ block, blockIndex = 0, entry, state, series, spoiler, onSelect, onOpenLocator }: { block: WikiBlock; blockIndex?: number; entry?: WikiEntryRecord; state: WikiState; series: SeriesSummary; spoiler?: SpoilerViewState; onSelect: (slug: string) => void; onOpenLocator?: (locator: string) => void }) {
  const data = block.data;
  const title = String(block.title || data.title || "");
  const blockTitle = <BlockTitle title={entry && spoiler ? renderMarkedWikiText(title, [], onSelect, entry, series, spoiler, onOpenLocator) : title} />;
  if (block.type === "prose" || block.type === "markdown") {
    const markdown = String(data.markdown ?? data.text ?? "").trim();
    return markdown ? <section className="wiki-block-render prose">{blockTitle}{entry && spoiler ? <SpoilerMarkdown markdown={markdown} entry={entry} prefix={`block:${blockIndex}:body`} state={state} series={series} spoiler={spoiler} onSelect={onSelect} onOpenLocator={onOpenLocator} /> : <WikiMarkdown markdown={markdown} entries={state.entries} onSelect={onSelect} onOpenLocator={onOpenLocator} />}</section> : null;
  }
  if (block.type === "module_buttons") {
    const items = blockItems(data, "items", "buttons", "modules");
    return items.length ? <section className="wiki-block-render">{blockTitle}<div className={`wiki-module-buttons count-${Math.min(8, items.length)}`}>{items.slice(0, 8).map((item, index) => { const slug = resolveBlockEntrySlug(item, state.entries); const target = state.entries.find((candidate) => candidate.slug === slug); const category = slug.startsWith("category:") ? state.categories.find((candidate) => candidate.id === slug.slice("category:".length)) : undefined; const itemTitle = String(item.title ?? item.label ?? item.name ?? target?.title ?? category?.label ?? "未命名入口"); const description = String(item.description ?? item.summary ?? item.note ?? item.reason ?? target?.summary ?? category?.description ?? "查看相关内容"); return <button key={String(item.id ?? slug ?? index)} onClick={() => slug && onSelect(slug)} disabled={!slug}><strong>{entry && spoiler ? renderMarkedWikiText(itemTitle, [], onSelect, entry, series, spoiler, onOpenLocator) : itemTitle}</strong><span className={spoiler?.mode === "strict" && target ? "wiki-strict-teaser" : undefined} aria-hidden={spoiler?.mode === "strict" && target ? true : undefined}>{entry && spoiler ? renderMarkedWikiText(description, [], onSelect, entry, series, spoiler, onOpenLocator) : description}</span></button>; })}</div></section> : null;
  }
  if (block.type === "info_cards" || block.type === "related_entries" || block.type === "entity_list") {
    const slugs = (data.entry_slugs as string[] | undefined) ?? [];
    const embedded = blockItems(data, "items", "cards", "entries", "entities");
    const items: Record<string, unknown>[] = embedded.length ? embedded : state.entries.filter((entry) => slugs.includes(entry.slug)).map(entry => ({ ...entry }));
    const resolved = items.map((item, index) => { const slug = resolveBlockEntrySlug(item, state.entries); const target = state.entries.find((candidate) => candidate.slug === slug); const itemTitle = String(item.title ?? item.name ?? item.label ?? target?.title ?? "").trim(); const isSpecial = target?.kind === "special" || target?.level === "special" || String(item.kind ?? item.type ?? "").toLocaleLowerCase() === "special" || String(item.kind ?? item.type ?? "").includes("专题"); return { item, index, slug, target, itemTitle, isSpecial }; }).filter((item) => item.itemTitle);
    const ordered = block.type === "related_entries" ? [...resolved.filter((item) => !item.isSpecial), ...resolved.filter((item) => item.isSpecial)] : resolved;
    const isSpecialGroup = title.includes("专题") || ordered.some((item) => item.isSpecial);
    return ordered.length ? <section className={`wiki-block-render ${isSpecialGroup ? "wiki-special-recommendations" : ""}`}>{blockTitle}<div className={`wiki-info-cards ${block.type === "related_entries" ? "wiki-related-entry-grid" : ""}`}>{ordered.map(({ item, index, slug, target, itemTitle, isSpecial }) => { const locked = Boolean(spoiler?.mode === "strict" && target); const relation = String(item.relation ?? item.summary ?? item.description ?? item.note ?? item.reason ?? target?.summary ?? "相关词条"); const visibleTitle = entry && spoiler ? renderMarkedWikiText(itemTitle, [], onSelect, entry, series, spoiler, onOpenLocator) : itemTitle; const visibleRelation = entry && spoiler ? renderMarkedWikiText(relation, [], onSelect, entry, series, spoiler, onOpenLocator) : relation; const relationNode = locked ? <span className="wiki-strict-teaser" aria-hidden="true">{visibleRelation}</span> : visibleRelation; return <button className={`${isSpecial ? "special-card" : ""} ${locked ? "locked-special-card" : ""}`} key={String(slug ?? itemTitle ?? index)} onClick={() => slug && onSelect(slug)} disabled={!slug}><span>{isSpecial ? "专题阅读" : kindLabel(String(item.kind ?? item.type ?? target?.kind ?? "词条"))}</span><strong>{visibleTitle}</strong>{block.type === "related_entries" ? <small className="wiki-related-relation">{relationNode}</small> : <p>{relationNode}</p>}</button>; })}</div></section> : null;
  }
  if (block.type === "fact_table") {
    const columns = primitiveArray(data.columns);
    const matrixRows = nestedRows(data.rows);
    if (columns.length && matrixRows.length) return <section className="wiki-block-render"><BlockTitle title={title} /><table className="wiki-fact-table"><thead><tr>{columns.map((column, index) => <th key={index}>{column}</th>)}</tr></thead><tbody>{matrixRows.map((row, index) => <tr key={index}>{row.map((value, cellIndex) => <td key={cellIndex}>{value}</td>)}</tr>)}</tbody></table></section>;
    const rows = blockItems(data, "rows", "items").map((row) => ({ label: String(row.label ?? row.key ?? row.attribute ?? row.name ?? "").trim(), value: String(row.value ?? row.content ?? row.description ?? "").trim() })).filter((row) => row.label || row.value);
    return rows.length ? <section className="wiki-block-render"><BlockTitle title={title} /><table className="wiki-fact-table"><tbody>{rows.map((row, index) => <tr key={index}>{row.label && <th>{row.label}</th>}<td colSpan={row.label ? undefined : 2}>{row.value}</td></tr>)}</tbody></table></section> : null;
  }
  if (block.type === "quote") {
    const quotes = blockItems(data, "quotes", "items");
    if (!quotes.length && String(data.text ?? "").trim()) quotes.push(data);
    return quotes.length ? <section className="wiki-block-render"><BlockTitle title={title} />{quotes.map((quote, index) => <blockquote key={index}>{String(quote.text ?? quote.quote ?? "")}{Boolean(quote.source || quote.speaker) && <cite>{String(quote.source ?? quote.speaker)}</cite>}{Boolean(quote.locator) && <button onClick={() => onOpenLocator?.(String(quote.locator))}>{shortLocator(String(quote.locator))}</button>}</blockquote>)}</section> : null;
  }
  if (["timeline", "event_list", "chapter_list"].includes(block.type)) {
    const aliases = block.type === "timeline" ? ["items", "events"] : block.type === "chapter_list" ? ["items", "chapters"] : ["items", "events"];
    const items = blockItems(data, ...aliases);
    return items.length ? <section className="wiki-block-render"><BlockTitle title={title} /><div className="wiki-linear-list">{items.map((item, index) => { const locator = blockItemLocator(item, series); return <button key={index} onClick={() => locator && onOpenLocator?.(locator)} disabled={!locator}><i /><span><strong>{String(item.title ?? item.label ?? "节点")}</strong><p>{String(item.summary ?? item.note ?? item.description ?? "")}</p></span>{(item.time || item.date || locator) && <code>{String(item.time ?? item.date ?? shortLocator(locator!))}</code>}</button>; })}</div></section> : null;
  }
  if (block.type === "callout") { const text = String(data.text ?? "").trim(); return text ? <aside className="wiki-callout"><FileText size={17} /><div>{title && <strong>{title}</strong>}<p>{text}</p></div></aside> : null; }
  if (block.type === "volume_cards") { const authored = blockItems(data, "items", "volumes", "cards"); const selected = authored.length ? authored.map((item) => { const number = Number(item.volume ?? item.number ?? item.volume_number); const volume = series.volumes.find((candidate) => candidate.id === String(item.volume_id ?? "") || candidate.number === number); return volume ? { volume, title: String(item.title ?? volume.title), description: String(item.description ?? item.summary ?? item.note ?? item.synopsis ?? `${volume.chapters.filter((chapter) => chapter.is_body).length} 个正文章节`) } : null; }).filter((item): item is { volume: SeriesSummary["volumes"][number]; title: string; description: string } => Boolean(item)) : series.volumes.map((volume) => ({ volume, title: volume.title, description: `${volume.chapters.filter((chapter) => chapter.is_body).length} 个正文章节` })); const singleVolume = series.volumes.length === 1; return selected.length ? <section className="wiki-block-render"><BlockTitle title={title} /><div className="wiki-volume-cards">{selected.map(({ volume, title: volumeTitle, description }) => <article key={volume.id}>{volume.cover_url && <img src={`${API_ORIGIN}${volume.cover_url}`} alt={singleVolume ? `${series.title}封面` : `第 ${volume.number} 卷封面`} />}<div><span>{singleVolume ? series.title : `第 ${String(volume.number).padStart(2, "0")} 卷`}</span><strong>{volumeTitle}</strong><p>{description}</p></div></article>)}</div></section> : null; }
  if (block.type === "chapter_cards") { const items = blockItems(data, "items", "chapters", "cards"); return items.length ? <section className="wiki-block-render"><BlockTitle title={title} /><div className="wiki-chapter-cards">{items.map((item, index) => { const locator = blockItemLocator(item, series); return <button key={index} onClick={() => locator && onOpenLocator?.(locator)} disabled={!locator}><span>{String(item.volume_label ?? item.chapter_label ?? "章节")}</span><strong>{String(item.title ?? "未命名章节")}</strong><p>{String(item.summary ?? item.note ?? "")}</p></button>; })}</div></section> : null; }
  return null;
}

function WikiNavigationBoxes({ state, currentSlug, onSelect }: { state: WikiState; currentSlug: string; onSelect: (slug: string) => void }) {
  const entries = new Map(state.entries.map((item) => [item.slug, item]));
  const categories = new Set(state.categories.map((item) => `category:${item.id}`));
  const canOpen = (slug: string | null) => Boolean(slug && (slug === "home" || entries.has(slug) || categories.has(slug)));
  const boxes = (state.navigation_boxes ?? []).filter((box) => currentSlug === "home" || box.groups.some((group) => group.entry_slugs.includes(currentSlug)));
  if (!boxes.length) return null;
  return <section className="wiki-navigation-boxes" aria-label="作品导航">{boxes.map((box) => {
    const groups = box.groups.map((group) => ({ ...group, members: group.entry_slugs.map((slug) => entries.get(slug)).filter((item): item is WikiEntrySummary => Boolean(item)) })).filter((group) => group.members.length);
    if (!groups.length) return null;
    return <section className="wiki-navigation-box" key={box.id}>
      <header><span>作品导航</span>{canOpen(box.title_slug) ? <button onClick={() => onSelect(box.title_slug!)}>{box.title}</button> : <strong>{box.title}</strong>}</header>
      <div>{groups.map((group) => <div className="wiki-navigation-row" key={`${box.id}-${group.label}`}>
        {canOpen(group.slug) ? <button className="wiki-navigation-label" onClick={() => onSelect(group.slug!)}>{group.label}</button> : <span className="wiki-navigation-label">{group.label}</span>}
        <div className="wiki-navigation-links">{group.members.map((item) => <button className={item.slug === currentSlug ? "active" : ""} key={item.slug} onClick={() => onSelect(item.slug)}>{item.title}</button>)}</div>
      </div>)}</div>
    </section>;
  })}</section>;
}

function NarrativeTrack({ entry, series, spoiler, onOpenLocator }: { entry: WikiEntryRecord; series: SeriesSummary; spoiler: SpoilerViewState; onOpenLocator?: (locator: string) => void }) {
  const items = useMemo(() => mergeTrackItems(entry.track_items, series), [entry.track_items, series]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = items.find(item => item.id === selectedId);
  const [expanded, setExpanded] = useState(false);
  const visible = spoiler.mode !== "strict" || expanded;
  const sources = [['ai', '关键节点'], ['citation', '正文引用'], ['appearance', '出场'], ['relation', '关系']] as const;
  const groups = sources.map(([source, label]) => {
    const rows: { item: NarrativeTrackRecord; start: number; end: number }[][] = [];
    const ordered = items.filter(item => (item.source_type ?? 'ai') === source).map(item => {
      const start = Math.min(98, Math.max(0, locatorPercent(item.start_locator, series)));
      const end = item.kind === 'range' ? Math.min(100, Math.max(start + 2, locatorPercent(item.end_locator ?? item.start_locator, series))) : start + 2;
      return { item, start, end };
    }).sort((a, b) => a.start - b.start);
    for (const node of ordered) {
      const row = rows.find(row => row[row.length - 1].end + 1 <= node.start);
      if (row) row.push(node); else rows.push([node]);
    }
    return { source, label, rows };
  }).filter(group => group.rows.length);
  return <section className="wiki-block-render narrative-track-browser"><div className="track-title"><h2>叙事轨迹</h2>{spoiler.mode === "strict" ? <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? "收起轨迹" : "展开轨迹"}<ChevronDown size={14} /></button> : <span>{items.length} 个标记</span>}</div>{visible && <>
    <div className="track-scale"><span>作品开篇</span><span>作品结尾</span></div>
    <div className="track-map">{groups.map(group => <div className={`track-source-group source-${group.source}`} key={group.source}><span className="track-source-label">{group.label}</span><div className="track-lanes">{group.rows.map((row, index) => <div className="track-lane" key={index}>{row.map(({ item, start, end }) => <button type="button" key={item.id} className={`${item.kind} ${selectedId === item.id ? 'selected' : ''}`} style={{ left: `${start}%`, width: `${end - start}%` }} title={ `${item.title} · ${chapterForLocator(item.start_locator, series)?.title ?? shortLocator(item.start_locator)}${item.note ? `\n${item.note}` : ''}`} aria-label={item.title} aria-pressed={selectedId === item.id} onClick={() => setSelectedId(selectedId === item.id ? null : item.id)} />)}</div>)}</div></div>)}</div>
    {selected && <div className="track-selection"><strong>{selected.title}</strong>{selected.note && selected.note !== selected.title && <p>{selected.note}</p>}<button onClick={() => onOpenLocator?.(selected.start_locator)}>阅读原文<ChevronRight size={13} /></button></div>}
  </>} </section>;
}

function mergeTrackItems(items: WikiEntryRecord["track_items"], series: SeriesSummary): WikiEntryRecord["track_items"] {
  const merged = new Map<string, WikiEntryRecord["track_items"][number]>();
  for (const original of items) {
    const chapter = chapterForLocator(original.start_locator, series);
    if (!chapter) continue;
    const chapterKey = original.start_locator.match(/\/v\d+(?:\.\d+)?\/c\d+/)?.[0] ?? original.start_locator;
    const normalized = original.source_type === "citation" ? {
      ...original, kind: "range" as const,
      title: chapter?.title ?? original.title,
      note: `${chapter?.title ?? "引用章节"} · ${shortLocator(original.start_locator)}`,
    } : original;
    const sourceType = original.source_type ?? "ai";
    const key = sourceType === "citation"
      ? `citation|${chapterKey}`
      : `${normalized.kind}|${sourceType}|${normalized.start_locator}|${normalized.end_locator ?? ""}`;
    const existing = merged.get(key);
    if (!existing) merged.set(key, normalized);
    else merged.set(key, {
      ...existing,
      title: uniqueText([existing.title, normalized.title]).join(" / "),
      note: uniqueText([existing.note, normalized.note]).join("；"),
      evidence: [...existing.evidence, ...normalized.evidence].filter((item, index, all) => all.findIndex((candidate) => candidate.locator === item.locator) === index),
    });
  }
  return [...merged.values()];
}

function buildWikiTerms(entries: WikiEntrySummary[], currentSlug?: string) {
  const terms = new Map<string, string>();
  for (const entry of entries) {
    if (entry.level === "home" || entry.slug === currentSlug) continue;
    const aliases = [entry.title, ...valueStrings(entry.metadata.aliases)];
    for (const term of aliases) if (term.trim().length >= 2 && !terms.has(term.trim())) terms.set(term.trim(), entry.slug);
  }
  return [...terms.entries()].sort((a, b) => b[0].length - a[0].length);
}

function renderWikiLinks(children: ReactNode, terms: [string, string][], onSelect: (slug: string) => void): ReactNode {
  if (!terms.length) return children;
  const matcher = new RegExp(`(${terms.map(([term]) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  const slugs = new Map(terms);
  return Children.map(children, (child) => {
    if (typeof child !== "string") return child;
    return child.split(matcher).map((part, index) => slugs.has(part) ? <button type="button" className="wiki-term-link" key={`${part}-${index}`} onClick={() => onSelect(slugs.get(part)!)}>{part}</button> : part);
  });
}

type PreparedSpoilerFragment = { id: string; text: string; instanceKey: string };

function prepareSpoilerMarkdown(markdown: string, scope = `text-${stableTextKey(markdown)}`) {
  const fragments = new Map<string, PreparedSpoilerFragment>();
  let index = 0;
  const prepared = markdown.replace(/⟦SP:([A-Za-z][A-Za-z0-9_-]{0,31})⟧([\s\S]*?)⟦\/SP:\1⟧/g, (_match, id: string, text: string) => {
    const occurrence = index++;
    const token = `LRSPTOKEN${occurrence}X`;
    fragments.set(token, { id, text, instanceKey: `${scope}:${id}:${occurrence}` });
    return token;
  }).replace(/⟦\/?SP:[^⟧]+⟧/g, "");
  return { markdown: prepared, fragments };
}

function renderMarkedWikiText(value: string, terms: [string, string][], onSelect: (slug: string) => void, entry?: WikiEntryRecord, series?: SeriesSummary, spoiler?: SpoilerViewState, onOpenLocator?: (locator: string) => void) {
  const prepared = prepareSpoilerMarkdown(value);
  return renderWikiRichText(prepared.markdown, terms, onSelect, entry, series, spoiler, prepared.fragments, onOpenLocator);
}

function renderWikiRichText(children: ReactNode, terms: [string, string][], onSelect: (slug: string) => void, entry?: WikiEntryRecord, series?: SeriesSummary, spoiler?: SpoilerViewState, preparedFragments: Map<string, PreparedSpoilerFragment> = new Map(), onOpenLocator?: (locator: string) => void): ReactNode {
  return Children.map(children, (child) => {
    if (typeof child !== "string" || !entry || !series || !spoiler) return renderWikiLinks(child, terms, onSelect);
    const parts: ReactNode[] = [];
    const matcher = /LRSPTOKEN\d+X/g;
    let cursor = 0; let match: RegExpExecArray | null;
    while ((match = matcher.exec(child))) {
      if (match.index > cursor) parts.push(<span key={`plain-${cursor}`}>{renderWikiLinks(child.slice(cursor, match.index), terms, onSelect)}</span>);
      const fragment = preparedFragments.get(match[0]);
      if (fragment) parts.push(<InlineSpoiler key={fragment.instanceKey} instanceKey={fragment.instanceKey} id={fragment.id} text={fragment.text} entry={entry} series={series} spoiler={spoiler} onOpenLocator={onOpenLocator} />);
      cursor = matcher.lastIndex;
    }
    if (cursor < child.length) parts.push(<span key={`plain-${cursor}`}>{renderWikiLinks(child.slice(cursor), terms, onSelect)}</span>);
    return parts.length ? parts : renderWikiLinks(child, terms, onSelect);
  });
}

function InlineSpoiler({ id, instanceKey, text, entry, series, spoiler, onOpenLocator }: { id: string; instanceKey: string; text: string; entry: WikiEntryRecord; series: SeriesSummary; spoiler: SpoilerViewState; onOpenLocator?: (locator: string) => void }) {
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pointerActiveRef = useRef(false);
  const [tooltipVisible, setTooltipVisible] = useState(false);
  const [tooltipPosition, setTooltipPosition] = useState({ x: 0, y: 0 });
  const rule = entry.spoiler_policy.fragments.find((item) => item.id === id) ?? { mode: "never" as const, unlock_after_chapter_id: null, reason: "" };
  const unlockKey = `${entry.slug}:spoiler:${instanceKey}`;
  const locked = spoilerRuleLocked(rule, series, spoiler, unlockKey);
  const manuallyUnlocked = spoiler.unlocked.has(unlockKey);
  const toggleable = locked || manuallyUnlocked;
  const hint = manuallyUnlocked ? "已手动解锁" : locked ? spoilerUnlockHint(rule, series) : spoilerUnlockedHint(rule, series);
  const showTooltip = spoiler.mode !== "off" && rule.mode !== "open";

  function beginTooltip(clientX: number, clientY: number) {
    if (!showTooltip) return;
    setTooltipPosition({ x: clientX, y: clientY });
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = setTimeout(() => setTooltipVisible(true), 500);
  }

  function hideTooltip() {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = null;
    setTooltipVisible(false);
  }

  useEffect(() => () => { if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current); }, []);

  return <><span className={`spoiler-inline ${locked ? "locked" : "unlocked"}`} role={toggleable ? "button" : undefined} tabIndex={toggleable ? 0 : undefined} aria-label={locked ? `剧透内容，${hint}` : manuallyUnlocked ? "已手动解锁，点击重新屏蔽" : hint} onPointerEnter={(event) => { pointerActiveRef.current = true; beginTooltip(event.clientX, event.clientY); }} onPointerMove={(event) => { if (!tooltipVisible) setTooltipPosition({ x: event.clientX, y: event.clientY }); }} onPointerLeave={() => { pointerActiveRef.current = false; hideTooltip(); }} onFocus={(event) => { if (pointerActiveRef.current) return; const rect = event.currentTarget.getBoundingClientRect(); setTooltipPosition({ x: rect.left + rect.width / 2, y: rect.top }); setTooltipVisible(true); }} onBlur={() => { if (!pointerActiveRef.current) hideTooltip(); }} onClick={(event) => { if (toggleable) { event.stopPropagation(); spoiler.onUnlock(unlockKey); } }} onKeyDown={(event) => { if (toggleable && (event.key === "Enter" || event.key === " ")) spoiler.onUnlock(unlockKey); }}><span><ReactMarkdown urlTransform={(url) => url.startsWith("lr://") ? url : defaultUrlTransform(url)} components={{ p: ({ children }) => <>{children}</>, h2: ({ children }) => <strong>{children}</strong>, h3: ({ children }) => <strong>{children}</strong>, a: ({ href, children }) => href?.startsWith("lr://") ? <button type="button" className="wiki-citation" onClick={(event) => { event.stopPropagation(); onOpenLocator?.(href); }}>{children}</button> : <a href={href}>{children}</a> }}>{normalizeWikiMarkdown(text)}</ReactMarkdown></span></span>{showTooltip && typeof document !== "undefined" && createPortal(<i className={`spoiler-hover-tooltip ${tooltipVisible ? "visible" : ""}`} style={{ left: tooltipPosition.x, top: tooltipPosition.y }} aria-hidden="true"><Lock size={11} />{hint}</i>, document.body, unlockKey)}</>;
}

function spoilerUnlockHint(rule: SpoilerAccessRule, series: SeriesSummary) {
  if (rule.mode !== "progress" || !rule.unlock_after_chapter_id) return "始终屏蔽 · 或点击解锁";
  const chapter = series.volumes.flatMap((volume) => volume.chapters.map((item) => ({ ...item, volume: volume.number }))).find((item) => item.id === rule.unlock_after_chapter_id);
  return chapter ? `直到V${chapter.volume}C${chapter.index}后 · 或点击解锁` : "直到指定章节后 · 或点击解锁";
}

function spoilerUnlockedHint(rule: SpoilerAccessRule, series: SeriesSummary) {
  if (rule.mode !== "progress" || !rule.unlock_after_chapter_id) return "已解锁";
  const chapter = series.volumes.flatMap((volume) => volume.chapters.map((item) => ({ ...item, volume: volume.number }))).find((item) => item.id === rule.unlock_after_chapter_id);
  return chapter ? `V${chapter.volume}C${chapter.index}后已解锁` : "已按阅读进度解锁";
}

function stableTextKey(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function chapterUnlockLabel(chapterId: string, series: SeriesSummary) {
  const chapter = series.volumes.flatMap((volume) => volume.chapters.map((item) => ({ ...item, volume: volume.number }))).find((item) => item.id === chapterId);
  return chapter ? (series.volumes.length === 1 ? `读完「${chapter.title}」后自动解锁` : `读完第 ${chapter.volume} 卷「${chapter.title}」后自动解锁`) : "到达指定章节后自动解锁";
}

function valueStrings(value: unknown): string[] { return Array.isArray(value) ? value.map(String) : typeof value === "string" ? value.split(/[、,，/；;]/) : []; }

function entryKindMatchesCategory(kind: string, category: WikiState["categories"][number]) {
  const normalizedKind = kind.trim().toLocaleLowerCase();
  return [category.id, category.label, category.parent_kind]
    .map((value) => String(value ?? "").trim().toLocaleLowerCase())
    .filter((value) => value && value !== "category" && value !== "other")
    .some((value) => value === normalizedKind || value.includes(normalizedKind) || normalizedKind.includes(value));
}

function BlockTitle({ title }: { title: ReactNode }) { return title ? <h2>{title}</h2> : null; }
function arrayData(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item));
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["item", "items", "$values"]) if (key in record) return arrayData(record[key]);
  }
  return [];
}
function blockItems(data: Record<string, unknown>, ...keys: string[]) { for (const key of keys) { const items = arrayData(data[key]); if (items.length) return items; } return []; }
function primitiveArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (value && typeof value === "object") { const record = value as Record<string, unknown>; for (const key of ["item", "items", "$values"]) if (key in record) return primitiveArray(record[key]); }
  return [];
}
function nestedRows(value: unknown): string[][] {
  const raw = Array.isArray(value) ? value : value && typeof value === "object" ? ((value as Record<string, unknown>).item ?? (value as Record<string, unknown>).items ?? []) : [];
  return Array.isArray(raw) ? raw.map((row) => Array.isArray(row) ? row.map(String) : row && typeof row === "object" && Array.isArray((row as Record<string, unknown>).item) ? ((row as Record<string, unknown>).item as unknown[]).map(String) : []).filter((row) => row.length) : [];
}
function resolveBlockEntrySlug(item: Record<string, unknown>, entries: WikiEntrySummary[]) {
  const requested = String(item.slug ?? item.link_slug ?? item.to ?? "").trim();
  if (requested.startsWith("category:")) return requested;
  if (requested && entries.some((entry) => entry.slug === requested)) return requested;
  const title = String(item.title ?? item.name ?? item.label ?? "").trim();
  return entries.find((entry) => entry.title === title)?.slug ?? "";
}
function blockItemLocator(item: Record<string, unknown>, series: SeriesSummary) {
  if (item.locator) return normalizeWikiLocator(String(item.locator), series);
  const chapterId = String(item.chapter_id ?? "");
  for (const volume of series.volumes) { const chapter = volume.chapters.find((candidate) => candidate.id === chapterId); if (chapter) return `lr://series/${series.id}/v${String(volume.number).padStart(2, "0")}/c${String(chapter.index).padStart(2, "0")}/p0001`; }
  return "";
}
function normalizeWikiLocator(locator: string, series: SeriesSummary) {
  let normalized = locator.trim().replace(/(\/c\d{1,3})p(\d{1,5})(?=$|[?#])/i, "$1/p$2");
  const match = normalized.match(/\/v(\d+)\/c(\d+)(?:\/p(\d+))?/i);
  if (!match) return normalized;
  const volume = series.volumes.find((item) => item.number === Number(match[1]));
  if (!volume) return normalized;
  const requested = Number(match[2]);
  const chapter = volume.chapters.find((item) => item.index === requested) ?? (requested === 0 ? [...volume.chapters].sort((a, b) => a.index - b.index)[0] : undefined);
  if (!chapter) return normalized;
  normalized = normalized.replace(/\/v\d+\/c\d+(?:\/p\d+)?/i, `/v${String(volume.number).padStart(2, "0")}/c${String(chapter.index).padStart(2, "0")}/p${String(Number(match[3] ?? 1)).padStart(4, "0")}`);
  return normalized;
}
function normalizeWikiMarkdown(markdown: string) {
  return markdown
    .replace(/`(\[[^\]]+\]\(lr:\/\/[^)]+\))`/g, "$1")
    .replace(/\[([^\]]+)\]\s+\((lr:\/\/[^)]+)\)/g, "[$1]($2)")
    .replace(/［([^］]+)］[（(](lr:\/\/[^）)]+)[）)]/g, "[$1]($2)")
    .replace(/\[([^\]]+)\][（](lr:\/\/[^）]+)[）]/g, "[$1]($2)");
}
function kindLabel(value: string) { return ({ special: "专题", character: "人物", nation: "国家", organization: "组织", group: "组织", mech: "机甲与武器", weapon: "武器", concept: "概念与设定", event: "事件", location: "地点", place: "地点", item: "物品", other: "其他词条", home: "作品首页" } as Record<string, string>)[value.toLocaleLowerCase()] ?? value; }
function levelLabel(value: string) { return ({ home: "首页", special: "专题", featured: "重点词条", standard: "标准词条", stub: "简要词条" } as Record<string, string>)[value] ?? value; }
function infoboxLabel(value: string) {
  const key = value.trim().toLocaleLowerCase();
  return ({
    name: "名称", aliases: "别名", title: "称号", type: "类型", entry_type: "词条类型",
    status: "状态", gender: "性别", age: "年龄", race: "种族", origin: "出身",
    appearance: "外貌", personality: "性格", callsign: "代号", rank: "军衔／身份",
    affiliation: "所属", squad: "所属部队", commander: "指挥官", subordinate: "下属",
    key_relations: "主要关系", relations: "相关人物", related_entries: "相关词条",
    first_appearance: "首次登场", last_appearance: "最后登场", appearance_volume: "登场卷",
    abilities: "能力", equipment: "装备", combat_style: "战斗方式", pilot: "驾驶员",
    manufacturer: "制造方", armament: "武装", deployment: "部署", weight: "重量", height: "高度",
    material: "材质", "material/quantity": "材质／数量", principle: "原理", creator: "创造者",
    user: "使用者", function: "作用", scope: "作用范围", location: "所在地", terrain: "地形",
    strategic_value: "战略价值", territory: "领土", capital: "首都", political_system: "政体",
    military: "军事力量", leader: "领导者", main_races: "主要种族", parent: "上级／归属",
    key_members: "主要成员", founded: "成立时间", time: "时间", participants: "参与者",
    cause: "起因", result: "结果", history: "沿革", death: "生死／结局",
    core_themes: "核心主题", key_figures: "关键人物", timeline_span: "时间跨度",
    time_span: "时间跨度", primary_characters: "主要人物", core_symbols: "核心象征",
    core_variations: "主要变化", thematic_axes: "主题维度", key: "项目", label: "标签",
    value: "内容", slug: "词条标识", kind: "类目", level: "等级",
  } as Record<string, string>)[key] ?? value;
}
function uniqueText(values: string[]) { return values.map((value) => value.trim()).filter((value, index, all) => value && all.indexOf(value) === index); }
function shortLocator(locator: string) { return locator.split("/").slice(-3).join("/"); }
function locatorPercent(locator: string, series: SeriesSummary) { const match = locator.match(/\/v(\d+(?:\.\d+)?)\/c(\d+)/); if (!match) return 0; const volumeNumber = Number(match[1]); const chapterNumber = Number(match[2]); const before = series.volumes.filter((volume) => volume.number < volumeNumber).reduce((sum, volume) => sum + volume.character_count, 0); const volume = series.volumes.find((item) => item.number === volumeNumber); const chapterBefore = volume?.chapters.filter((chapter) => chapter.index < chapterNumber).reduce((sum, chapter) => sum + chapter.character_count, 0) ?? 0; return (before + chapterBefore) / Math.max(1, series.total_characters) * 100; }
function chapterForLocator(locator: string, series: SeriesSummary) { const match = locator.match(/\/v(\d+(?:\.\d+)?)\/c(\d+)/); if (!match) return undefined; const volume = series.volumes.find((item) => item.number === Number(match[1])); return volume?.chapters.find((chapter) => chapter.index === Number(match[2])); }
