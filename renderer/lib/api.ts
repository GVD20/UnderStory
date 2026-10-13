import { desktopConfig, selectWork } from "../storage";
import type { AnalysisJobRecord, AnalysisLogRecord, Chapter, ChatThreadDetail, ChatThreadRecord, EmbeddingIndexHistoryItem, EmbeddingIndexStatus, EmbeddingSettings, ModelSettings, ReadingSettingsSnapshot, SearchResult, SeriesSummary, WikiEntryRecord, WikiMentionMatch, WikiState } from "../types";

export let API_ORIGIN = "";
export function configureApi() { API_ORIGIN = desktopConfig.origin; }
const apiBase = () => `${API_ORIGIN}/api`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, { ...init, headers: { ...init?.headers, "X-UnderStory-Token": desktopConfig.token } });
  if (!response.ok) {
    const raw = await response.text();
    let detail = raw;
    try { detail = (JSON.parse(raw) as { detail?: string }).detail ?? raw; } catch { /* plain text */ }
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export type ChatStreamEvent = { event: "status" | "initial_start" | "initial_delta" | "initial_end" | "confirmation_required" | "answer_start" | "delta" | "complete" | "error"; data: unknown };

async function streamChat(
  path: string, body: Record<string, unknown>, onEvent: (event: ChatStreamEvent) => void,
): Promise<ChatThreadDetail> {
  const response = await fetch(`${apiBase()}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-UnderStory-Token": desktopConfig.token }, body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) throw new Error(await response.text() || "流式对话连接失败");
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let completed: ChatThreadDetail | null = null;
  while (true) {
    const { value, done } = await reader.read(); buffer += decoder.decode(value, { stream: !done });
    const packets = buffer.split("\n\n"); buffer = packets.pop() ?? "";
    for (const packet of packets) {
      const event = packet.match(/^event: (.+)$/m)?.[1] as ChatStreamEvent["event"] | undefined;
      const raw = packet.match(/^data: (.+)$/m)?.[1]; if (!event || !raw) continue;
      const data = JSON.parse(raw) as unknown; onEvent({ event, data });
      if (event === "complete") completed = data as ChatThreadDetail;
      if (event === "error") throw new Error(String((data as { message?: string }).message ?? "对话失败"));
    }
    if (done) break;
  }
  if (!completed) throw new Error("对话流结束但没有最终结果");
  return completed;
}

export const libraryApi = {
  getLibrary: () => request<{ series: SeriesSummary[] }>("/library"),
  getSeries: async (id: string) => { await selectWork(id); return request<SeriesSummary>(`/series/${id}`); },
  deleteSeries: (id: string) => request<{ deleted: boolean }>(`/series/${encodeURIComponent(id)}`, { method: "DELETE" }),
  renameSeries: (id: string, title: string) => request<SeriesSummary>(`/series/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) }),
  getModelSettings: () => request<ModelSettings>("/settings/model"),
  saveModelSettings: (settings: Pick<ModelSettings, "provider" | "fast_model" | "advanced_model" | "base_url" | "reasoning_split" | "composer_concurrency" | "preview_concurrency"> & { api_key?: string }) => secureSettings<ModelSettings>("model", "/settings/model", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  listConfiguredModels: (settings: Pick<ModelSettings, "provider" | "fast_model" | "advanced_model" | "base_url" | "reasoning_split" | "composer_concurrency" | "preview_concurrency"> & { api_key?: string }) => request<{ models: string[]; count: number }>("/settings/model/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  testModelConnection: (settings: Pick<ModelSettings, "provider" | "fast_model" | "advanced_model" | "base_url" | "reasoning_split" | "composer_concurrency" | "preview_concurrency"> & { api_key?: string }) => request<{ ok: boolean; model: string; message: string }>("/settings/model/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  getEmbeddingSettings: () => request<EmbeddingSettings>("/settings/embedding"),
  getEmbeddingIndexHistory: () => request<{ indexes: EmbeddingIndexHistoryItem[]; count: number }>("/settings/embedding/indexes"),
  saveEmbeddingSettings: (settings: Pick<EmbeddingSettings, "provider" | "model" | "base_url"> & { api_key?: string }) => secureSettings<EmbeddingSettings>("embedding", "/settings/embedding", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  listEmbeddingModels: (settings: Pick<EmbeddingSettings, "provider" | "model" | "base_url"> & { api_key?: string }) => request<{ models: string[]; count: number }>("/settings/embedding/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  testEmbeddingConnection: (settings: Pick<EmbeddingSettings, "provider" | "model" | "base_url"> & { api_key?: string }) => request<{ ok: boolean; model: string; dimensions: number; message: string }>("/settings/embedding/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) }),
  getEmbeddingStatus: (seriesId: string) => request<EmbeddingIndexStatus>(`/embedding/${encodeURIComponent(seriesId)}/status`),
  getChapter: (id: string) => request<Chapter>(`/chapters/${id}`),
  updateChapter: (id: string, isBody: boolean) =>
    request<Chapter>(`/chapters/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_body: isBody }),
    }),
  search: (seriesId: string, query: string) =>
    request<{ query: string; results: SearchResult[] }>(
      `/search?series_id=${encodeURIComponent(seriesId)}&q=${encodeURIComponent(query)}`,
    ),
  getProgress: (seriesId: string) => request<{ series_id: string; chapter_id: string; paragraph_id: string | null; progress: number } | null>(`/progress/${seriesId}`),
  getReadingMarker: (seriesId: string) => request<{ completedChapterId: string | null }>(`/reading-marker/${seriesId}`),
  saveReadingMarker: (seriesId: string, completedChapterId: string | null) => request<{ completedChapterId: string | null }>(`/reading-marker/${seriesId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ completedChapterId }) }),
  saveProgress: (seriesId: string, chapterId: string, progress: number, paragraphId?: string) =>
    request(`/progress/${seriesId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        series_id: seriesId,
        chapter_id: chapterId,
        paragraph_id: paragraphId ?? null,
        progress: Math.max(0, Math.min(1, progress)),
      }),
    }),
  importEpubs: async (files: File[], volumeNumbers: number[], seriesId?: string, title?: string) => {
    const body = new FormData();
    files.forEach((file) => body.append("files", file));
    if (seriesId) body.append("series_id", seriesId);
    if (title?.trim()) body.append("series_title", title.trim());
    body.append("volume_numbers", JSON.stringify(volumeNumbers));
    return request<{ series: SeriesSummary; imported_volumes: string[] }>("/library/import", {
      method: "POST",
      body,
    });
  },
  startAnalysis: (seriesId: string, mode: "quality" | "fast", vectorize = false) => request<AnalysisJobRecord>(`/analysis/${seriesId}/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode, vectorize }) }),
  getLatestAnalysis: (seriesId: string) => request<AnalysisJobRecord | null>(`/analysis/${seriesId}/latest`),
  getAnalysisJob: (jobId: string) => request<AnalysisJobRecord>(`/analysis/jobs/${jobId}`),
  pauseAnalysis: (jobId: string) => request<AnalysisJobRecord>(`/analysis/jobs/${jobId}/pause`, { method: "POST" }),
  getAnalysisLogs: (jobId: string) => request<{ logs: AnalysisLogRecord[] }>(`/analysis/jobs/${jobId}/logs`).then((result) => result.logs),
  getWikiState: (seriesId: string) => request<WikiState>(`/wiki/${seriesId}`),
  getWikiEntry: (seriesId: string, slug: string) => request<WikiEntryRecord>(`/wiki/${seriesId}/entries/${slug}`),
  getWikiMentionIndex: (seriesId: string) => request<{ entries: WikiMentionMatch[] }>(`/wiki/${seriesId}/mention-index`).then((result) => result.entries),
  createSpecial: (seriesId: string, title: string, introduction: string, relatedEntrySlugs: string[]) => request<WikiEntryRecord>(`/wiki/${seriesId}/specials`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, introduction, related_entry_slugs: relatedEntrySlugs }) }),
  deleteSpecial: (seriesId: string, slug: string) => request(`/wiki/${seriesId}/specials/${slug}`, { method: "DELETE" }),
  getThreads: (seriesId: string) => request<ChatThreadRecord[]>(`/chat/threads?series_id=${encodeURIComponent(seriesId)}`),
  createThread: (seriesId: string) => request<ChatThreadDetail>("/chat/threads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ series_id: seriesId, title: "新对话" }) }),
  getThread: (threadId: string) => request<ChatThreadDetail>(`/chat/threads/${threadId}`),
  deleteThread: (threadId: string) => request<{ deleted: boolean }>(`/chat/threads/${threadId}`, { method: "DELETE" }),
  sendMessage: (threadId: string, content: string, currentChapterId?: string, currentParagraphId?: string, selectedText?: string, readingSettings?: ReadingSettingsSnapshot) => request<ChatThreadDetail>(`/chat/threads/${threadId}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, current_chapter_id: currentChapterId ?? null, current_paragraph_id: currentParagraphId ?? null, selected_text: selectedText ?? null, reading_settings: readingSettings ?? null }) }),
  streamMessage: (threadId: string, content: string, currentChapterId: string | undefined, currentParagraphId: string | undefined, selectedText: string | undefined, readingSettings: ReadingSettingsSnapshot, onEvent: (event: ChatStreamEvent) => void) => streamChat(`/chat/threads/${threadId}/messages/stream`, { content, current_chapter_id: currentChapterId ?? null, current_paragraph_id: currentParagraphId ?? null, selected_text: selectedText ?? null, reading_settings: readingSettings }, onEvent),
  resolveSpoilerConfirmation: (threadId: string, decision: "full" | "minimize" | "stop", onEvent: (event: ChatStreamEvent) => void) => streamChat(`/chat/threads/${threadId}/spoiler-confirmation/stream`, { decision }, onEvent),
};

async function secureSettings<T>(name: string, path: string, init: RequestInit): Promise<T> {
  const payload = JSON.parse(String(init.body)) as { api_key?: string };
  const result = await request<T>(path, init);
  if (payload.api_key?.trim()) await window.desktop.writeSecret(name, payload.api_key.trim());
  return result;
}
