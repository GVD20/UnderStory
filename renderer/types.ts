export type View = "reader" | "chat" | "wiki";
export type Theme = "paper" | "dark" | "system";
export type SideMode = "peek" | "wiki" | "chat" | "search";

export interface ChapterSummary {
  id: string;
  volume: number;
  index: number;
  title: string;
  kind: "chapter" | "prologue" | "epilogue" | "afterword" | "appendix";
  is_body: boolean;
  character_count: number;
  paragraph_count: number;
  overview_status: "pending" | "ready" | "skipped";
}

export interface VolumeSummary {
  id: string;
  number: number;
  title: string;
  authors: string[];
  token_count: number;
  character_count: number;
  cover_url: string | null;
  chapters: ChapterSummary[];
}

export interface SeriesSummary {
  id: string;
  title: string;
  subtitle: string;
  authors: string[];
  total_tokens: number;
  total_characters: number;
  volumes: VolumeSummary[];
}

export interface ModelSettings {
  provider: string;
  fast_model: string;
  advanced_model: string;
  base_url: string;
  api_key_configured: boolean;
  api_key_hint: string;
  reasoning_split: boolean;
  composer_concurrency: number;
  preview_concurrency: number;
}

export interface EmbeddingSettings {
  provider: string;
  model: string;
  base_url: string;
  configured: boolean;
  api_key_configured: boolean;
  api_key_hint: string;
}

export interface EmbeddingIndexStatus {
  configured: boolean;
  available: boolean;
  complete: boolean;
  index_id: string | null;
  model: string;
  dimensions: number;
  completed_volumes: number;
  total_volumes: number;
  embedded_chunks: number;
  total_chunks: number;
}

export interface EmbeddingIndexHistoryItem {
  id: string;
  series_id: string;
  series_title: string;
  model: string;
  base_url: string;
  dimensions: number;
  status: string;
  active: boolean;
  current_configuration: boolean;
  completed_volumes: number;
  total_volumes: number;
  embedded_chunks: number;
  total_chunks: number;
  created_at: string;
  updated_at: string;
}

export interface Paragraph {
  id: string;
  locator: string;
  text: string;
  html: string | null;
  role: "paragraph" | "heading" | "heading-1" | "heading-2" | "heading-3" | "heading-4" | "heading-5" | "heading-6" | "quote" | "list-item" | "illustration";
}

export interface Chapter extends ChapterSummary {
  paragraphs: Paragraph[];
  chapter_overview: {
    status: "pending" | "ready" | "skipped";
    intro: string | null;
    overview: string | null;
    people: (string | ChapterPreviewCard)[];
    places: (string | ChapterPreviewCard)[];
    concepts: (string | ChapterPreviewCard)[];
    events: (string | ChapterPreviewCard)[];
  };
}

export interface SearchResult {
  result_type: "chapter" | "text" | "wiki";
  chapter_id: string;
  chapter_title: string;
  volume: number;
  locator: string;
  paragraph_id: string | null;
  excerpt: string;
  match_start: number;
  match_end: number;
  title_match: boolean;
  title_match_kind: "exact_title" | "exact_alias" | "partial_title" | "none";
  matched_title: string | null;
  theme_color: string | null;
  entry_kind: string | null;
}

export interface EvidenceRef { locator: string; note: string; marker?: string; }
export interface ChapterPreviewCard { title: string; slug?: string; summary?: string; importance?: "primary" | "secondary"; }
export interface WikiCategory { id: string; label: string; parent_kind: string; description: string; theme_color: string; introduction: string; body_markdown: string; blocks: WikiBlock[]; subcategories: { id: string; label: string; description?: string }[]; }
export interface WikiBlock { type: string; title: string | null; data: Record<string, unknown>; }
export interface WikiNavigationGroup { label: string; slug: string | null; entry_slugs: string[]; }
export interface WikiNavigationBox { id: string; title: string; title_slug: string | null; groups: WikiNavigationGroup[]; }
export interface SpoilerAccessRule { mode: "open" | "progress" | "never"; unlock_after_chapter_id: string | null; reason: string; }
export interface SpoilerFragmentRule extends SpoilerAccessRule { id?: string; target?: string; }
export interface WikiSpoilerPolicy { entry: SpoilerAccessRule; fragments: SpoilerFragmentRule[]; }
export interface NarrativeTrackRecord {
  id: string; entry_id: string; kind: "point" | "range"; category: string;
  title: string; note: string; start_locator: string; end_locator: string | null;
  importance: number; evidence: EvidenceRef[];
  source_type: "ai" | "citation" | "appearance" | "relation";
}
export interface WikiEntryRecord {
  id: string; series_id: string; slug: string; title: string; kind: string;
  level: "stub" | "standard" | "featured" | "special" | "home";
  theme_color: string; summary: string; body_markdown: string;
  metadata: Record<string, unknown>; infobox: Record<string, unknown>; blocks: WikiBlock[]; track_items: NarrativeTrackRecord[];
  spoiler_policy: WikiSpoilerPolicy;
  status: "draft" | "published"; revision: number; user_created: boolean; updated_at: string;
}
export type WikiEntrySummary = Pick<WikiEntryRecord, "id" | "series_id" | "slug" | "title" | "kind" | "level" | "theme_color" | "summary" | "metadata" | "spoiler_policy" | "status" | "revision" | "user_created" | "updated_at">;
export interface WikiMentionTerm { value: string; canonical_name: string; match_kind: "literal" | "related"; score: number; }
export interface WikiMentionMatch { slug: string; title: string; kind: string; theme_color: string; terms: WikiMentionTerm[]; direct_match: boolean; }
export interface WikiState {
  series_id: string;
  blueprint: null | { id: string; version: number; status: string; home_title: string; complete_work_coverage: boolean };
  entries: WikiEntrySummary[]; categories: WikiCategory[];
  navigation_boxes: WikiNavigationBox[];
  compilation_status: { planned?: number; compiled?: number; remaining?: number; complete_work_coverage?: boolean };
}
export interface ChatMessageRecord {
  id: string; thread_id: string; role: "user" | "assistant" | "tool"; content: string;
  citations: EvidenceRef[]; cards: Record<string, unknown>[]; actions: Record<string, unknown>[];
  thinking: Record<string, unknown>[]; thinking_seconds: number; created_at: string;
}
export interface ReadingSettingsSnapshot {
  paper_color: "standard" | "warm";
  theme: "paper" | "dark" | "system";
  font_size: number;
  line_height: number;
  epub_features: "off" | "basic" | "advanced";
  illustrations: "off" | "original" | "immersive";
  spoiler_shield: "off" | "auto" | "strict";
  semantic_retrieval: boolean;
}
export interface ReadingSettingsChange {
  type: "reading_settings_change";
  before: ReadingSettingsSnapshot;
  after: ReadingSettingsSnapshot;
  changes: { key: keyof ReadingSettingsSnapshot; label: string; before: unknown; after: unknown; before_label: string; after_label: string }[];
}
export interface ChatThreadRecord { id: string; series_id: string; title: string; created_at: string; updated_at: string; }
export interface ChatSpoilerConfirmation { id: string; thread_id: string; message: string; thinking: Record<string, unknown>[]; thinking_seconds: number; created_at: string; }
export interface ChatThreadDetail extends ChatThreadRecord { messages: ChatMessageRecord[]; pending_spoiler_confirmation: ChatSpoilerConfirmation | null; }
export interface AnalysisStageProgress {
  key: "vector" | "volume" | "architect" | "compose" | "preview"; index: number; label: string;
  completed_points: number; total_points: number; progress: number;
  status: "pending" | "running" | "completed" | "skipped";
}
export interface AnalysisJobRecord {
  id: string; series_id: string; status: "pending" | "running" | "paused" | "completed" | "failed";
  mode: "quality" | "fast";
  vectorize: boolean;
  pause_requested: boolean;
  corpus_signature: string; current_step: string | null;
  total_steps: number; completed_steps: number; progress: number;
  current_stage_index: number; current_stage_key: AnalysisStageProgress["key"]; current_stage_label: string;
  stage_completed_points: number; stage_total_points: number; stage_progress: number;
  stages: AnalysisStageProgress[];
  input_tokens: number; output_tokens: number; analyzed_volume_count: number; wiki_entry_count: number;
  message: string; error: string | null;
  created_at: string; updated_at: string; completed_at: string | null;
}
export interface AnalysisLogRecord {
  id: number; step_key: string | null; level: "info" | "success" | "warning" | "error";
  message: string; input_tokens: number; output_tokens: number; created_at: string;
}
