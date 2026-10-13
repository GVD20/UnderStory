import { SidebarHeader } from './sidebar';
import { storage } from "./storage";


import { BookOpen, Brain, ChevronDown, ChevronLeft, ChevronRight, FileSearch, LibraryBig, List, LoaderCircle, MessageCircle, MessageSquarePlus, Minus, Plus, RotateCcw, Search, Send, Settings2, ShieldAlert, Sparkles, Trash2, Wrench } from "./icons";
import { Children, FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_ORIGIN, libraryApi } from "./lib/api";
import type { ChatMessageRecord, ChatThreadDetail, ChatThreadRecord, ReadingSettingsChange, ReadingSettingsSnapshot, SeriesSummary, WikiEntryRecord, WikiMentionMatch } from "./types";

type ChatActivity = { event_type?: string; kind?: string; label?: string; tool_name?: string; count?: number };
type ChatSuggestion = { prompt: string; eyebrow: string };
type ChatWelcome = { title: string; description: string };

const CHAT_WELCOMES: ChatWelcome[] = [{ title: "有什么想了解的？", description: "询问作品中的人物、情节或原文。" }];

type ReadingSettingsChatProps = { readingSettings: ReadingSettingsSnapshot; onApplyReadingSettings: (settings: ReadingSettingsSnapshot) => void };

export function ChatWorkspace({ series, readingSettings, onApplyReadingSettings, onOpenLocator, onOpenWiki }: { series: SeriesSummary; currentChapterId: string; targetParagraphId?: string; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void } & ReadingSettingsChatProps) {
  return <ChatPane pageKey="chat" series={series} readingSettings={readingSettings} onApplyReadingSettings={onApplyReadingSettings} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} />;
}

export function ChatSidePane({ series, currentChapterId, targetParagraphId, selectedText, selectedParagraphCount, readingSettings, onApplyReadingSettings, onClearSelectedText, onOpenLocator, onOpenWiki }: { series: SeriesSummary; currentChapterId: string; targetParagraphId?: string; selectedText?: string; selectedParagraphCount?: number; onClearSelectedText: () => void; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void } & ReadingSettingsChatProps) {
  return <ChatPane compact pageKey={`reader:${currentChapterId}`} series={series} currentChapterId={currentChapterId} currentParagraphId={targetParagraphId} selectedText={selectedText} selectedParagraphCount={selectedParagraphCount} readingSettings={readingSettings} onApplyReadingSettings={onApplyReadingSettings} onClearSelectedText={onClearSelectedText} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} />;
}

function ChatPane({ series, pageKey, currentChapterId, currentParagraphId, selectedText, selectedParagraphCount, readingSettings, onApplyReadingSettings, onClearSelectedText, initialPrompt, compact = false, onOpenLocator, onOpenWiki }: { series: SeriesSummary; pageKey: string; currentChapterId?: string; currentParagraphId?: string; selectedText?: string; selectedParagraphCount?: number; onClearSelectedText?: () => void; initialPrompt?: string; compact?: boolean; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void } & ReadingSettingsChatProps) {
  const [threads, setThreads] = useState<ChatThreadRecord[]>([]);
  const [thread, setThread] = useState<ChatThreadDetail | null>(null);
  const [text, setText] = useState(initialPrompt ?? "");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [activity, setActivity] = useState<ChatActivity[]>([]);
  const [streamed, setStreamed] = useState("");
  const [answerStreaming, setAnswerStreaming] = useState(false);
  const [thinkingSeconds, setThinkingSeconds] = useState(0);
  const [spoilerCardLeaving, setSpoilerCardLeaving] = useState(false);
  const [stoppingAnswer, setStoppingAnswer] = useState(false);
  const [heldThinking, setHeldThinking] = useState<{ threadId: string; activities: ChatActivity[]; seconds: number; stopped: boolean } | null>(null);
  const [mentions, setMentions] = useState<WikiMentionMatch[]>([]);
  const [suggestions, setSuggestions] = useState<ChatSuggestion[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(true);
  const [suggestionCycle, setSuggestionCycle] = useState(0);
  const [welcome, setWelcome] = useState(() => randomWelcome());
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [compactHistoryOpen, setCompactHistoryOpen] = useState(false);
  const [queuedUserMessage, setQueuedUserMessage] = useState("");
  const [runningThreadId, setRunningThreadId] = useState<string | null>(null);
  const [excludedContexts, setExcludedContexts] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const followLatest = useRef(true);
  const sendStartedAt = useRef(0);
  const thinkingBaseSeconds = useRef(0);
  const visibleThreadId = useRef<string | null>(null);
  const pageThreadStorageKey = `lr:last-chat-thread:${series.id}:${pageKey}`;
  const refreshSuggestions = useCallback(async () => {
    setSuggestionsLoading(true);
    try {
      const state = await libraryApi.getWikiState(series.id);
      const candidates = shuffle(state.entries.filter((entry) => entry.level !== "home" && entry.status === "published" && entry.spoiler_policy.entry.mode === "open")).slice(0, 7);
      const details = await Promise.all(candidates.map((entry) => libraryApi.getWikiEntry(series.id, entry.slug).catch(() => null)));
      setSuggestions(buildChatSuggestions(details.filter((entry): entry is WikiEntryRecord => Boolean(entry)), series.title));
    } catch { setSuggestions(buildChatSuggestions([], series.title)); }
    finally { setSuggestionsLoading(false); setSuggestionCycle((value) => value + 1); }
  }, [series.id, series.title]);

  useEffect(() => {
    let live = true;
    void Promise.resolve().then(async () => {
      if (!live) return;
      followLatest.current = true; visibleThreadId.current = null; setThread(null); setText(initialPrompt ?? ""); setHeldThinking(null); setCompactHistoryOpen(false); setDeleteConfirmId(null); setError(""); setWelcome(randomWelcome());
      try {
        const items = await libraryApi.getThreads(series.id);
        if (!live) return;
        setThreads(items);
        const rememberedId = storage.getItem(pageThreadStorageKey);
        if (!rememberedId || !items.some((item) => item.id === rememberedId)) {
          if (rememberedId) storage.removeItem(pageThreadStorageKey);
          return;
        }
        const remembered = await libraryApi.getThread(rememberedId);
        if (live) { visibleThreadId.current = remembered.id; setThread(remembered); }
      } catch (cause) { if (live) setError(cause instanceof Error ? cause.message : "对话无法载入"); }
    });
    return () => { live = false; };
  }, [initialPrompt, pageThreadStorageKey, series.id]);
  useEffect(() => { void libraryApi.getWikiMentionIndex(series.id).then(setMentions).catch(() => setMentions([])); void Promise.resolve().then(refreshSuggestions); }, [series.id, refreshSuggestions]);
  useEffect(() => { const node = scrollRef.current; if (node && followLatest.current) node.scrollTop = node.scrollHeight; }, [thread?.messages, activity, streamed]);
  useEffect(() => { const node = textareaRef.current; if (!node) return; node.style.height = "auto"; node.style.height = `${Math.min(node.scrollHeight, 156)}px`; }, [text]);
  useEffect(() => {
    if (!sending || answerStreaming) return;
    const update = () => setThinkingSeconds(thinkingBaseSeconds.current + (performance.now() - sendStartedAt.current) / 1000);
    update(); const timer = window.setInterval(update, 100);
    return () => window.clearInterval(timer);
  }, [sending, answerStreaming]);

  function startNewThread() {
    if (sending) return;
    storage.removeItem(pageThreadStorageKey);
    followLatest.current = true; visibleThreadId.current = null; setHeldThinking(null); setDeleteConfirmId(null); setCompactHistoryOpen(false); setThread(null); setText(""); setError(""); setWelcome(randomWelcome());
    void refreshSuggestions(); window.setTimeout(() => textareaRef.current?.focus(), 0);
  }

  async function openThread(id: string) {
    followLatest.current = true; setHeldThinking(null);
    setDeleteConfirmId(null); setCompactHistoryOpen(false); visibleThreadId.current = id; setThread(await libraryApi.getThread(id)); storage.setItem(pageThreadStorageKey, id);
  }

  async function submit(event: FormEvent) {
    event.preventDefault(); if (!text.trim() || sending || stoppingAnswer || thread?.pending_spoiler_confirmation) return;
    setHeldThinking(null);
    const content = text.trim(); setQueuedUserMessage(content); setText(""); setSending(true); setError(""); setActivity([]); setStreamed(""); setAnswerStreaming(false); setThinkingSeconds(0); followLatest.current = true; thinkingBaseSeconds.current = 0; sendStartedAt.current = performance.now();
    let active: ChatThreadDetail | null = thread;
    try {
      active = active ?? await libraryApi.createThread(series.id);
      const optimistic = { ...active, messages: [...active.messages, { id: `pending-${Date.now()}`, thread_id: active.id, role: "user" as const, content, citations: [], cards: [], actions: [], thinking: [], thinking_seconds: 0, created_at: "" }] };
      visibleThreadId.current = active.id; setThread(optimistic); setQueuedUserMessage(""); setRunningThreadId(active.id);
      storage.setItem(pageThreadStorageKey, active.id);
      setThreads((items) => [active!, ...items.filter((item) => item.id !== active!.id)]);
      const chapterContextKey = `chapter:${currentChapterId ?? ""}`;
      const includeChapter = compact && Boolean(currentChapterId) && !excludedContexts.has(chapterContextKey);
      const includeSelection = compact && Boolean(selectedText);
      const completed = await libraryApi.streamMessage(
        active.id, content,
        includeChapter ? currentChapterId : undefined,
        includeChapter && !selectedText ? currentParagraphId : undefined,
        includeSelection ? selectedText : undefined,
        readingSettings,
        handleStreamEvent,
      );
      applyCompletedReadingSettings(completed, onApplyReadingSettings);
      if (visibleThreadId.current === active.id) setThread(completed);
      storage.setItem(pageThreadStorageKey, completed.id);
      setThreads(await libraryApi.getThreads(series.id));
    } catch (cause) {
      if (!active) setText(content);
      setError(cause instanceof Error ? cause.message : "消息发送失败");
      if (active) void libraryApi.getThreads(series.id).then(setThreads).catch(() => undefined);
    }
    finally { setQueuedUserMessage(""); setRunningThreadId(null); setSending(false); setActivity([]); setStreamed(""); setAnswerStreaming(false); }
  }

  function handleStreamEvent(event: { event: string; data: unknown }) {
    if (event.event === "status") { const detail = event.data as ChatActivity; setActivity((items) => items.at(-1)?.label === detail.label ? items : [...items, detail]); }
    if (event.event === "initial_start") setActivity((items) => [...items, { event_type: "chat_initial", kind: "initial", label: "" }]);
    if (event.event === "initial_delta") setActivity((items) => { const next = [...items]; const index = next.findLastIndex((item) => item.event_type === "chat_initial"); if (index >= 0) next[index] = { ...next[index], label: `${next[index].label ?? ""}${String(event.data)}` }; return next; });
    if (event.event === "answer_start") { setAnswerStreaming(true); const seconds = Number((event.data as { thinking_seconds?: number }).thinking_seconds ?? 0); setThinkingSeconds(seconds || (performance.now() - sendStartedAt.current) / 1000); }
    if (event.event === "delta") setStreamed((value) => value + String(event.data));
  }

  async function resolveSpoiler(decision: "full" | "minimize" | "stop") {
    if (!thread?.pending_spoiler_confirmation || sending) return;
    const previous = thread;
    setSpoilerCardLeaving(true);
    await new Promise((resolve) => window.setTimeout(resolve, 180));
    setSpoilerCardLeaving(false);
    const pending = thread.pending_spoiler_confirmation;
    setThread({ ...thread, pending_spoiler_confirmation: null });
    if (decision === "stop") {
      setHeldThinking({ threadId: thread.id, activities: pending.thinking as ChatActivity[], seconds: pending.thinking_seconds, stopped: true });
      setStoppingAnswer(true); setRunningThreadId(thread.id); setError("");
      try {
        const completed = await libraryApi.resolveSpoilerConfirmation(thread.id, decision, handleStreamEvent);
        setHeldThinking(null);
        if (visibleThreadId.current === thread.id) setThread(completed); setThreads(await libraryApi.getThreads(series.id));
      } catch (cause) { setError(cause instanceof Error ? cause.message : "停止回答失败"); }
      finally { setRunningThreadId(null); setStoppingAnswer(false); }
      return;
    }
    setHeldThinking(null);
    followLatest.current = true; setRunningThreadId(thread.id); setSending(true); setError(""); setActivity(pending.thinking as ChatActivity[]); setStreamed(""); setAnswerStreaming(false); setThinkingSeconds(pending.thinking_seconds); thinkingBaseSeconds.current = pending.thinking_seconds; sendStartedAt.current = performance.now();
    try {
      const completed = await libraryApi.resolveSpoilerConfirmation(thread.id, decision, handleStreamEvent);
      applyCompletedReadingSettings(completed, onApplyReadingSettings);
      if (visibleThreadId.current === thread.id) setThread(completed); setThreads(await libraryApi.getThreads(series.id));
    } catch (cause) { if (visibleThreadId.current === thread.id) setThread({ ...previous, pending_spoiler_confirmation: null }); setHeldThinking({ threadId: thread.id, activities: pending.thinking as ChatActivity[], seconds: pending.thinking_seconds, stopped: false }); setActivity([]); setError(cause instanceof Error ? cause.message : "剧透确认处理失败"); }
    finally { setRunningThreadId(null); setSending(false); setActivity([]); setStreamed(""); setAnswerStreaming(false); }
  }

  async function removeThread(id: string) {
    followLatest.current = true; setHeldThinking(null);
    await libraryApi.deleteThread(id); const next = await libraryApi.getThreads(series.id); setThreads(next); setDeleteConfirmId(null);
    if (thread?.id === id) {
      storage.removeItem(pageThreadStorageKey);
      visibleThreadId.current = null;
      setThread(null);
    }
  }

  const viewingRunningThread = Boolean(runningThreadId && thread?.id === runningThreadId);
  const hasConversation = Boolean(thread?.messages.length || queuedUserMessage || viewingRunningThread || stoppingAnswer);
  const chapterTitle = series.volumes.flatMap((volume) => volume.chapters).find((chapter) => chapter.id === currentChapterId)?.title ?? "当前作品";
  const toggleContext = (key: string) => setExcludedContexts((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const chapterContextKey = `chapter:${currentChapterId ?? ""}`;
  const composer = <form className="agent-composer" onSubmit={submit}>{compact && <div className="composer-context-tags" aria-label="本次对话附加信息"><ContextTag label={chapterTitle} included={!excludedContexts.has(chapterContextKey)} onToggle={() => toggleContext(chapterContextKey)} />{selectedText && <ContextTag label="已选中的文本" detail={selectedText} included onToggle={() => onClearSelectedText?.()} />}</div>}<textarea ref={textareaRef} rows={1} value={text} disabled={stoppingAnswer || Boolean(thread?.pending_spoiler_confirmation)} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={thread?.pending_spoiler_confirmation ? "请先选择剧透回答方式" : compact ? "询问当前章节、人物或选中的原文…" : "向阅读伙伴提问"} aria-label="询问阅读伙伴" /><div className="composer-footer"><span>{thread?.pending_spoiler_confirmation ? "回答已暂停，等待你的选择" : "Enter 发送 · Shift + Enter 换行"}</span><button aria-label="发送消息" disabled={sending || stoppingAnswer || !text.trim() || Boolean(thread?.pending_spoiler_confirmation)}><Send size={15} /><span className="send-label">发送</span></button></div></form>;

  return <section className={`agent-chat ${compact ? "compact" : ""} ${!compact && threads.length === 0 ? "no-history" : ""}`}>
    {!compact && threads.length > 0 && <aside className="chat-thread-list"><SidebarHeader title="阅读对话" actions={<button aria-label="新建对话" title="新建对话" disabled={sending} onClick={startNewThread}><MessageSquarePlus size={16} /></button>} /><div className="sidebar-scroll thread-list-scroll"><ThreadCards threads={threads} activeId={thread?.id} runningId={runningThreadId} deleteConfirmId={deleteConfirmId} onOpen={(id) => void openThread(id)} onRequestDelete={setDeleteConfirmId} onCancelDelete={() => setDeleteConfirmId(null)} onConfirmDelete={(id) => void removeThread(id)} /></div></aside>}
    <main className={`chat-message-pane ${!hasConversation ? "empty" : ""}`}>
      {compact && <nav className="compact-chat-bar sidebar-module-bar" aria-label="对话导航"><button type="button" disabled={!compactHistoryOpen} aria-label="返回当前对话" title="返回当前对话" onClick={() => setCompactHistoryOpen(false)}><ChevronLeft size={16} /></button><button type="button" disabled={threads.length === 0} aria-label="浏览历史对话" title="历史对话" aria-pressed={compactHistoryOpen} onClick={() => setCompactHistoryOpen(value => !value)}><List size={16} /></button><button type="button" aria-label="新建对话" title="新建对话" onClick={startNewThread}><MessageSquarePlus size={16} /></button></nav>}

      {compact && compactHistoryOpen && <div className="compact-history-panel"><ThreadCards threads={threads} activeId={thread?.id} runningId={runningThreadId} deleteConfirmId={deleteConfirmId} onOpen={(id) => void openThread(id)} onRequestDelete={setDeleteConfirmId} onCancelDelete={() => setDeleteConfirmId(null)} onConfirmDelete={(id) => void removeThread(id)} /></div>}
      {!compactHistoryOpen && !hasConversation && <div className={`chat-empty-stage ${selectedText ? "with-selection" : ""}`}>{selectedText ? <SelectionWelcome key={selectedText} text={selectedText} paragraphCount={selectedParagraphCount} /> : <div className="chat-welcome"><Sparkles className="chat-welcome-logo" /><h1>{welcome.title}</h1><p>{welcome.description}</p><div key={suggestionCycle} className={`chat-suggestions ${suggestionsLoading ? "loading" : "ready"}`}>{suggestions.map((suggestion, index) => <button style={{ "--suggestion-index": index } as React.CSSProperties} key={suggestion.prompt} type="button" onClick={() => { setText(suggestion.prompt); window.setTimeout(() => textareaRef.current?.focus(), 0); }}><span>{index === 0 ? <BookOpen size={16} /> : index === 1 ? <Search size={16} /> : <MessageCircle size={16} />}{suggestion.eyebrow}</span><strong>{suggestion.prompt}</strong><ChevronRight size={15} /></button>)}</div></div>}{!compact && composer}</div>}
      {!compactHistoryOpen && <div className="chat-message-scroll" ref={scrollRef} onScroll={(event) => { const node = event.currentTarget; followLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48; }}>
        {thread?.messages.filter((message) => message.role !== "tool").map((message) => message.role === "user" ? <article key={message.id} className="chat-message user"><div className="message-markdown"><ChatMarkdown content={message.content} citations={[]} mentions={[]} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} /></div></article> : <AssistantMessage key={message.id} message={message} mentions={mentions} onApplyReadingSettings={onApplyReadingSettings} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} />)}
        {queuedUserMessage && <article className="chat-message user pending-user-message"><div className="message-markdown"><ChatMarkdown content={queuedUserMessage} citations={[]} mentions={[]} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} /></div></article>}
        {thread?.pending_spoiler_confirmation && <PausedThinking activities={thread.pending_spoiler_confirmation.thinking as ChatActivity[]} seconds={thread.pending_spoiler_confirmation.thinking_seconds} />}
        {thread?.pending_spoiler_confirmation && <SpoilerConfirmationCard disabled={sending || spoilerCardLeaving} leaving={spoilerCardLeaving} onChoose={(decision) => void resolveSpoiler(decision)} />}
        {heldThinking && thread && heldThinking.threadId === thread.id && !thread.pending_spoiler_confirmation && <PausedThinking activities={heldThinking.activities} seconds={heldThinking.seconds} />}
        {heldThinking && thread && heldThinking.threadId === thread.id && heldThinking.stopped && <StoppedAnswerCard />}
        {viewingRunningThread && sending && !answerStreaming && <article className="chat-message assistant live-thinking"><LiveThinking activities={activity} seconds={thinkingSeconds} /></article>}
        {viewingRunningThread && sending && answerStreaming && <article className="chat-message assistant streaming"><ThinkingDisclosure activities={activity} seconds={thinkingSeconds} /><div className="message-markdown"><ChatMarkdown content={streamed} citations={[]} mentions={mentions} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} /></div><span className="stream-caret" /></article>}
      </div>}
      {!compactHistoryOpen && error && <p className="chat-error">{error}</p>}
      {!compactHistoryOpen && (hasConversation || compact) && composer}
    </main>
  </section>;
}

function ContextTag({ label, detail, included, onToggle }: { label: string; detail?: string; included: boolean; onToggle: () => void }) {
  return <span className={`composer-context-tag ${included ? "included" : "excluded"}`} title={detail || label}><span>{label}</span><button type="button" aria-label={`${included ? "不附加" : "附加"}${label}`} aria-pressed={included} onClick={onToggle}>{included ? <Minus size={12} /> : <Plus size={12} />}</button></span>;
}

function SelectionWelcome({ text, paragraphCount }: { text: string; paragraphCount?: number }) {
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const previewRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const node = previewRef.current;
    if (!node || expanded) return;
    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => setClipped(node.scrollHeight > node.clientHeight + 1));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(node); measure();
    return () => { observer.disconnect(); window.cancelAnimationFrame(frame); };
  }, [expanded]);

  return <div className="chat-selection-welcome"><BookOpen className="chat-selection-welcome-icon" /><h1>尽管向我询问。</h1><p>已附加了 {Math.max(1, paragraphCount ?? 1)} 个段落：</p><blockquote className={expanded ? "expanded" : ""}><span ref={previewRef}>{text}</span>{clipped && <button type="button" onClick={() => setExpanded((value) => !value)}>{expanded ? "收起原文" : "展开全部"}<ChevronDown size={13} /></button>}</blockquote></div>;
}

function ThreadCards({ threads, activeId, runningId, deleteConfirmId, onOpen, onRequestDelete, onCancelDelete, onConfirmDelete }: { threads: ChatThreadRecord[]; activeId?: string; runningId: string | null; deleteConfirmId: string | null; onOpen: (id: string) => void; onRequestDelete: (id: string) => void; onCancelDelete: () => void; onConfirmDelete: (id: string) => void }) {
  return <div className="thread-cards">{threads.map((item) => { const longTitle = item.title.length > 15; const running = runningId === item.id; return <div className={`thread-row ${activeId === item.id ? "active" : ""} ${running ? "running" : ""} ${deleteConfirmId === item.id ? "confirming" : ""}`} key={item.id}>{deleteConfirmId === item.id ? <div className="thread-delete-confirm" role="alertdialog" aria-label={`确认删除 ${item.title}`}><span>删除这段对话？</span><div><button className="cancel" onClick={onCancelDelete}>取消</button><button className="confirm" onClick={() => onConfirmDelete(item.id)}>删除</button></div></div> : <><button className="thread-open" onClick={() => onOpen(item.id)}><span className={`thread-title ${longTitle ? "long" : ""}`}><span>{item.title}</span></span><small>{running ? <><LoaderCircle className="thread-running-spinner" size={10} />正在思考</> : formatThreadTime(item.updated_at)}</small></button><button className="delete-thread" aria-label={`删除 ${item.title}`} disabled={running} onClick={() => onRequestDelete(item.id)}><Trash2 size={13} /></button></>}</div>; })}</div>;
}

function SpoilerConfirmationCard({ disabled, leaving, onChoose }: { disabled: boolean; leaving: boolean; onChoose: (decision: "full" | "minimize" | "stop") => void }) {
  return <article className={`chat-message assistant spoiler-confirmation-card ${leaving ? "leaving" : ""}`}><header><span><ShieldAlert size={17} /></span><div><strong>完整的答案可能包含剧透</strong><p>模型认为继续回答可能会泄露影响阅读体验的关键情节。你可以选择回答方式。</p></div></header><div><button disabled={disabled} onClick={() => onChoose("full")}>忽略警告，提供完整回答</button><button disabled={disabled} onClick={() => onChoose("minimize")}>回答，但尽量减少剧透</button><button className="stop" disabled={disabled} onClick={() => onChoose("stop")}>取消并停止回答</button></div></article>;
}

function StoppedAnswerCard() {
  return <article className="chat-message assistant stopped-answer-card"><span><ShieldAlert size={15} /></span><strong>已为你停止回答。</strong></article>;
}

function readingSettingsChange(action: Record<string, unknown>): action is Record<string, unknown> & ReadingSettingsChange {
  return action.type === "reading_settings_change" && Boolean(action.before) && Boolean(action.after) && Array.isArray(action.changes);
}

function applyCompletedReadingSettings(detail: ChatThreadDetail, onApply: (settings: ReadingSettingsSnapshot) => void) {
  const message = [...detail.messages].reverse().find((item) => item.role === "assistant");
  const change = message?.actions.find(readingSettingsChange);
  if (!message || !change || change.changes.length === 0) return;
  onApply(change.after);
  storage.setItem(`lr:chat-reading-settings:${message.id}`, "applied");
}

function ReadingSettingsChangeCard({ messageId, change, onApply }: { messageId: string; change: ReadingSettingsChange; onApply: (settings: ReadingSettingsSnapshot) => void }) {
  const storageKey = `lr:chat-reading-settings:${messageId}`;
  const [undone, setUndone] = useState(() => typeof window !== "undefined" && storage.getItem(storageKey) === "undone");
  function undo() {
    onApply(change.before); storage.setItem(storageKey, "undone"); setUndone(true);
  }
  return <section className={`reading-settings-change-card ${undone ? "undone" : ""}`} aria-label="阅读设置修改">
    <header><span><Settings2 size={16} /></span><div><strong>{undone ? "阅读设置修改已撤回" : "阅读设置已更新"}</strong><small>{undone ? "已恢复修改前的设置" : "回答完成后已自动应用"}</small></div></header>
    <div className="reading-settings-change-list">{change.changes.map((item) => <div key={item.key}><span>{item.label}</span><p><del>{item.before_label}</del><ChevronRight size={12} /><strong>{item.after_label}</strong></p></div>)}</div>
    <button type="button" disabled={undone} onClick={undo}><RotateCcw size={13} />{undone ? "已撤回" : "撤回修改"}</button>
  </section>;
}

function AssistantMessage({ message, mentions, onApplyReadingSettings, onOpenLocator, onOpenWiki }: { message: ChatMessageRecord; mentions: WikiMentionMatch[]; onApplyReadingSettings: (settings: ReadingSettingsSnapshot) => void; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void }) {
  if (message.actions.some((action) => action.type === "answer_stopped")) return <><PausedThinking activities={message.thinking as ChatActivity[]} seconds={message.thinking_seconds} /><StoppedAnswerCard /></>;
  return <article className="chat-message assistant">{message.thinking.length > 0 && <ThinkingDisclosure activities={message.thinking as ChatActivity[]} seconds={message.thinking_seconds} />}<AnswerWithCards message={message} mentions={mentions} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} />{message.actions.map((action, index) => action.locator ? <button className="message-action" key={index} onClick={() => onOpenLocator(String(action.locator))}>{String(action.label ?? "跳转原文")}</button> : readingSettingsChange(action) ? <ReadingSettingsChangeCard key={index} messageId={message.id} change={action} onApply={onApplyReadingSettings} /> : null)}</article>;
}

function AnswerWithCards({ message, mentions, onOpenLocator, onOpenWiki }: { message: ChatMessageRecord; mentions: WikiMentionMatch[]; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void }) {
  const cards = message.cards.map((card, index) => ({ ...card, number: Number(card.number ?? index + 1), type: String(card.type ?? (card.slug ? "wiki" : "source")) }));
  const byNumber = new Map(cards.map((card) => [Number(card.number), card]));
  const embeddedNumbers = new Set<number>();
  const segments: ({ type: "markdown"; content: string } | { type: "cards"; cards: Record<string, unknown>[] })[] = [];
  let markdown: string[] = [];
  const flushMarkdown = () => { const content = markdown.join("\n").trim(); if (content) segments.push({ type: "markdown", content }); markdown = []; };
  for (const line of message.content.split("\n")) {
    const match = line.trim().match(/^\{\{\s*cards\s*:\s*([0-9]+(?:\s*[,，]\s*[0-9]+)*)\s*\}\}$/i);
    if (!match) { markdown.push(line); continue; }
    flushMarkdown();
    const selected = match[1].split(/[,，]/).map(Number).filter((number) => byNumber.has(number) && !embeddedNumbers.has(number)).map((number) => { embeddedNumbers.add(number); return byNumber.get(number)!; });
    if (selected.length) segments.push({ type: "cards", cards: selected });
  }
  flushMarkdown();
  const remaining = cards.filter((card) => !embeddedNumbers.has(Number(card.number))).sort((left, right) => cardTypeOrder(left) - cardTypeOrder(right));
  return <>{segments.map((segment, index) => segment.type === "markdown" ? <div className="message-markdown" key={`text-${index}`}><ChatMarkdown content={segment.content} citations={message.citations} mentions={mentions} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} /></div> : <ChatCardGroup cards={segment.cards} embedded key={`cards-${index}`} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} />)}{remaining.length > 0 && <section className="message-related-links"><span>相关链接</span><ChatCardGroup cards={remaining} onOpenLocator={onOpenLocator} onOpenWiki={onOpenWiki} /></section>}</>;
}

function cardTypeOrder(card: Record<string, unknown>) { return String(card.type) === "wiki" ? 0 : 1; }

function ChatCardGroup({ cards, embedded = false, onOpenLocator, onOpenWiki }: { cards: Record<string, unknown>[]; embedded?: boolean; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void }) {
  const fullWidth = cards.length === 1;
  return <div className={`message-cards ${embedded ? "embedded" : "trailing"} ${fullWidth ? "single" : ""}`}>{cards.map((card, index) => { const source = String(card.type) === "source"; const target = String(source ? card.locator ?? "" : card.slug ?? ""); return source ? <SourceChatCard card={card} fullWidth={fullWidth} key={`${String(card.number ?? index)}-${target}`} onOpen={() => target && onOpenLocator(target)} /> : <button className="wiki-card" key={`${String(card.number ?? index)}-${target}`} onClick={() => target && onOpenWiki(target)}><span><LibraryBig size={12} />{String(card.label ?? card.relation ?? card.kind ?? "Wiki 词条")}</span><strong>{String(card.title ?? card.name ?? "相关词条")}</strong>{Boolean(card.summary) && <small>{String(card.summary)}</small>}<span className="message-card-arrow" aria-hidden="true"><ChevronRight size={15} /></span></button>; })}</div>;
}

function SourceChatCard({ card, fullWidth, onOpen }: { card: Record<string, unknown>; fullWidth: boolean; onOpen: () => void }) {
  const scale = String(card.scale ?? "paragraph");
  const label = String(card.label ?? (scale === "volume" ? "打开整卷" : scale === "chapter" ? "打开章节" : "查看原文片段"));
  const title = String(card.title ?? "原文位置");
  const summary = String(card.summary ?? "");
  const sourceLabel = String(card.source_label ?? shortLocator(String(card.locator ?? "")));
  const Icon = scale === "volume" ? LibraryBig : scale === "chapter" ? BookOpen : FileSearch;
  const coverUrl = String(card.cover_url ?? "");
  const arrow = <span className="message-card-arrow" aria-hidden="true"><ChevronRight size={scale === "volume" || fullWidth ? 16 : 15} /></span>;
  if (scale === "volume") return <button className={`source-card scale-volume ${fullWidth ? "full-width" : "compact-card"} ${coverUrl ? "has-cover" : ""}`} onClick={onOpen}>{coverUrl && <img className="source-card-cover" src={`${API_ORIGIN}${coverUrl}`} alt={`${title}封面`} />}<span className="source-card-copy"><span className="source-card-kicker"><Icon size={12} />{label}</span><strong>{title}</strong><small><span>{sourceLabel}</span>{summary && <span>{summary}</span>}</small></span>{arrow}</button>;
  if (scale === "paragraph" && fullWidth) return <button className="source-card scale-paragraph full-width" onClick={onOpen}><span className="source-card-topline"><small>{sourceLabel}</small><em><Icon size={12} />{label}</em></span><strong className="source-card-excerpt">{summary || title}</strong>{arrow}</button>;
  if (scale === "paragraph") return <button className="source-card scale-paragraph compact-card" onClick={onOpen}><span className="source-card-kicker"><Icon size={12} />直达原文</span><strong>{label}</strong><small>{summary || sourceLabel}</small>{arrow}</button>;
  return <button className={`source-card scale-chapter ${fullWidth ? "full-width" : "compact-card"}`} onClick={onOpen}><span className="source-card-kicker"><Icon size={12} />{label}</span><strong>{title}</strong><small>{sourceLabel}</small>{arrow}</button>;
}

function LiveThinking({ activities, seconds }: { activities: ChatActivity[]; seconds: number }) {
  return <div className="chat-thinking-disclosure live"><div className="chat-thinking-live-header"><LoaderCircle className="thinking-summary-spinner" size={13} /><span>已思考 {formatThinkingSeconds(seconds)}</span></div><ThinkingActivityContent activities={activities} live /></div>;
}

function PausedThinking({ activities, seconds }: { activities: ChatActivity[]; seconds: number }) {
  return <article className="chat-message assistant paused-thinking-message"><div className="chat-thinking-disclosure paused"><div className="chat-thinking-live-header"><Brain className="thinking-mark" size={13} /><span>已思考 {formatThinkingSeconds(seconds)}</span></div><ThinkingActivityContent activities={activities} /></div></article>;
}

function ThinkingActivityContent({ activities, live = false }: { activities: ChatActivity[]; live?: boolean }) {
  const initials = activities.filter((activity) => activity.event_type === "chat_initial");
  const flow = activities.filter((activity) => activity.event_type !== "chat_initial");
  const visible = flow.length ? flow : live ? [{ event_type: "chat_preparing", label: "正在理解问题" }] : [];
  const flowRef = useRef<HTMLDivElement>(null);
  useEffect(() => { const node = flowRef.current; if (node) node.scrollTop = node.scrollHeight; }, [activities]);
  return <div className="chat-thinking-content">{initials.length > 0 && <div className="chat-thinking-initial">{initials.map((activity, index) => <ActivityRow key={`${activity.label}-${index}`} activity={activity} />)}</div>}<div className={`chat-thinking-live ${visible.length > 4 ? "overflowing" : ""}`} ref={flowRef}>{visible.map((activity, index) => <ActivityRow key={`${activity.event_type}-${activity.label}-${index}`} activity={activity} active={live && index === visible.length - 1} />)}</div></div>;
}

function ThinkingDisclosure({ activities, seconds }: { activities: ChatActivity[]; seconds: number }) {
  return <details className="chat-thinking-disclosure"><summary><Brain className="thinking-mark" size={13} /><span>已思考 {formatThinkingSeconds(seconds)}</span><ChevronDown className="thinking-chevron" size={13} /></summary><ThinkingActivityContent activities={activities} /></details>;
}

function formatThinkingSeconds(seconds: number) { const value = Math.max(.1, seconds); return `${value.toFixed(value >= 10 ? 0 : 1)} 秒`; }

function shuffle<T>(items: T[]) {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const target = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[target]] = [copy[target], copy[index]];
  }
  return copy;
}

function randomWelcome() { return CHAT_WELCOMES[Math.floor(Math.random() * CHAT_WELCOMES.length)]; }

function buildChatSuggestions(entries: WikiEntryRecord[], seriesTitle: string): ChatSuggestion[] {
  const suggestions = entries.slice(0, 3).map((entry, index) => {
    const fields = shuffle(Object.keys(entry.infobox ?? {}).filter((key) => key.trim() && !/[⟦⟧]/.test(key))).slice(0, 2);
    const headings = shuffle([...entry.body_markdown.matchAll(/^#{2,4}\s+(.+)$/gm)]
      .map((match) => match[1].replace(/[*_`]/g, "").trim())
      .filter((heading) => heading && !/简介|相关词条|参考|引用|作品导航|⟦SP/i.test(heading)));
    const mode = (index + Math.floor(Math.random() * 3)) % 3;
    const prompt = mode === 0 && fields.length >= 2
      ? `${entry.title}的${fields[0]}与${fields[1]}是什么？`
      : mode === 1 && headings[0]
        ? `${entry.title}在“${headings[0]}”中有哪些值得了解的内容？`
        : `了解一下${entry.title}。`;
    return { prompt, eyebrow: wikiKindLabel(entry.kind) };
  });
  const fallbacks = [
    { eyebrow: "作品导览", prompt: `${seriesTitle}最值得关注的核心人物有哪些？` },
    { eyebrow: "情节梳理", prompt: `梳理一下${seriesTitle}的故事主线。` },
    { eyebrow: "世界观", prompt: `${seriesTitle}有哪些重要设定？` },
  ];
  return [...suggestions, ...fallbacks].slice(0, 3);
}

function formatThreadTime(value: string) {
  const parsed = new Date(`${value.replace(" ", "T")}Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(parsed);
}

function wikiKindLabel(kind: string) {
  return (({ character: "人物", place: "地点", group: "组织", event: "事件", object: "事物", concept: "概念", special: "专题" } as Record<string, string>)[kind] ?? kind) || "Wiki 词条";
}

function ActivityRow({ activity, active = false }: { activity: ChatActivity; active?: boolean }) {
  const Icon = activity.event_type === "chat_initial" ? MessageCircle : activity.tool_name === "search_original_text" ? FileSearch : activity.tool_name === "search_wiki" ? Search : activity.tool_name === "get_wiki_entry" ? LibraryBig : activity.tool_name === "get_reading_settings" || activity.tool_name === "update_reading_settings" ? Settings2 : activity.event_type === "chat_thinking" || activity.event_type === "chat_preparing" ? Brain : Wrench;
  return <div className={`chat-activity-row ${activity.event_type ?? ""} ${active ? "active" : ""}`}><span>{active ? <LoaderCircle size={14} /> : <Icon size={14} />}</span><p>{activity.label ?? "正在思考"}</p></div>;
}

function ChatMarkdown({ content, citations, mentions, onOpenLocator, onOpenWiki }: { content: string; citations: { locator: string; marker?: string }[]; mentions: WikiMentionMatch[]; onOpenLocator: (locator: string) => void; onOpenWiki: (slug: string) => void }) {
  let linked = content; citations.forEach((citation, index) => { const marker = citation.marker || String(index + 1); linked = linked.replaceAll(`[${marker}]`, `[${toSuperscript(index + 1)}](${normalizeCitationHref(citation.locator)})`); });
  const terms = useMemo(() => buildWikiTerms(mentions), [mentions]);
  const auto = (children: ReactNode) => autoWikiChildren(children, terms, onOpenWiki);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={(url) => url.startsWith("lr://") ? url : defaultUrlTransform(url)} components={{ p: ({ children }) => <p>{auto(children)}</p>, li: ({ children }) => <li>{auto(children)}</li>, h2: ({ children }) => <h2>{auto(children)}</h2>, h3: ({ children }) => <h3>{auto(children)}</h3>, table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>, th: ({ children }) => <th>{auto(children)}</th>, td: ({ children }) => <td>{auto(children)}</td>, strong: ({ children }) => <strong>{auto(children)}</strong>, a: ({ href, children }) => href?.startsWith("lr://wiki/") ? <button className="chat-wiki-link" onClick={() => onOpenWiki(href.slice("lr://wiki/".length))}>{children}</button> : href?.startsWith("lr://") ? <button className="chat-citation" title={shortLocator(href)} onClick={() => onOpenLocator(href)}><BookOpen size={11} />{children}</button> : <a href={href}>{children}</a> }}>{linked}</ReactMarkdown>;
}
function toSuperscript(value: number) { return String(value).replace(/[0-9]/g, (digit) => "⁰¹²³⁴⁵⁶⁷⁸⁹"[Number(digit)]); }
function normalizeCitationHref(locator: string) {
  if (locator.startsWith("lr://")) return locator;
  const match = locator.trim().match(/^v\s*(\d+(?:\.\d+)?)\s*(?:c\s*(\d+))?\s*(?:p\s*(\d+))?$/i);
  if (!match) return locator;
  let normalized = `lr://legacy/v${match[1].padStart(2, "0")}`;
  if (match[2]) normalized += `/c${match[2].padStart(2, "0")}`;
  if (match[3]) normalized += `/p${match[3].padStart(4, "0")}`;
  return normalized;
}
function shortLocator(locator: string) { return locator.split("/").slice(-3).join("/"); }

function autoWikiChildren(children: ReactNode, terms: { term: string; slug: string }[], onOpenWiki: (slug: string) => void) {
  return Children.map(children, (child) => {
    if (typeof child !== "string") return child;
    // CommonMark leaves emphasis ending in Chinese punctuation literal when
    // immediately followed by Chinese prose. Recover only that specific case;
    // code spans and already parsed Markdown elements remain untouched.
    return child.split(/(\*\*[^*\n]+[。！？：；，、）】”’]\*\*)(?=\p{Script=Han})/u).map((part, index) =>
      /^\*\*[^*\n]+[。！？：；，、）】”’]\*\*$/u.test(part)
        ? <strong key={index}>{autoWikiText(part.slice(2, -2), terms, onOpenWiki)}</strong>
        : autoWikiText(part, terms, onOpenWiki));
  });
}

function autoWikiText(text: string, terms: { term: string; slug: string }[], onOpenWiki: (slug: string) => void) {
  if (!terms.length || text.length < 2) return text;
  const pattern = new RegExp(`(${terms.map((item) => escapeRegex(item.term)).join("|")})`, "g");
  const byTerm = new Map(terms.map((item) => [item.term, item]));
  return text.split(pattern).map((part, index) => { const match = byTerm.get(part); return match ? <button type="button" className="chat-wiki-link auto" key={`${part}-${index}`} onClick={() => onOpenWiki(match.slug)}>{part}</button> : part; });
}

function buildWikiTerms(mentions: WikiMentionMatch[]) {
  const terms = mentions.flatMap((entry) => [entry.title, ...entry.terms.map((term) => term.value)].filter((value) => value.trim().length >= 2).map((term) => ({ term: term.trim(), slug: entry.slug })));
  return [...new Map(terms.map((item) => [item.term, item])).values()].sort((a, b) => b.term.length - a.term.length).slice(0, 800);
}

function escapeRegex(value: string) { return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
