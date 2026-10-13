from typing import Any, Literal

from pydantic import BaseModel, Field


ChapterKind = Literal["chapter", "prologue", "epilogue", "afterword", "appendix"]


class Paragraph(BaseModel):
    id: str
    locator: str
    text: str
    html: str | None = None
    role: Literal[
        "paragraph", "heading", "heading-1", "heading-2", "heading-3",
        "heading-4", "heading-5", "heading-6", "quote", "list-item", "illustration",
    ] = "paragraph"


class ChapterOverview(BaseModel):
    status: Literal["pending", "ready", "skipped"] = "pending"
    intro: str | None = None
    overview: str | None = None
    people: list[str | dict[str, Any]] = Field(default_factory=list)
    events: list[str | dict[str, Any]] = Field(default_factory=list)
    places: list[str | dict[str, Any]] = Field(default_factory=list)
    concepts: list[str | dict[str, Any]] = Field(default_factory=list)


class ReadingMarkerUpdate(BaseModel):
    completedChapterId: str | None


class ChapterSummary(BaseModel):
    id: str
    volume: int | float
    index: int
    title: str
    kind: ChapterKind
    is_body: bool
    character_count: int
    paragraph_count: int
    overview_status: Literal["pending", "ready", "skipped"] = "pending"


class Chapter(ChapterSummary):
    paragraphs: list[Paragraph]
    chapter_overview: ChapterOverview


class VolumeSummary(BaseModel):
    id: str
    number: int | float
    title: str
    authors: list[str] = Field(default_factory=list)
    token_count: int
    character_count: int
    cover_url: str | None = None
    chapters: list[ChapterSummary]


class SeriesSummary(BaseModel):
    id: str
    title: str
    subtitle: str
    authors: list[str] = Field(default_factory=list)
    total_tokens: int
    total_characters: int
    volumes: list[VolumeSummary]


class LibraryResponse(BaseModel):
    series: list[SeriesSummary]


class SearchResult(BaseModel):
    result_type: Literal["chapter", "text", "wiki"] = "text"
    chapter_id: str
    chapter_title: str
    volume: int | float
    locator: str
    paragraph_id: str | None = None
    excerpt: str
    match_start: int = Field(ge=0)
    match_end: int = Field(ge=0)
    title_match: bool = False
    title_match_kind: Literal["exact_title", "exact_alias", "partial_title", "none"] = "none"
    matched_title: str | None = None
    theme_color: str | None = None
    entry_kind: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]


class ChapterUpdate(BaseModel):
    is_body: bool


class ReadingProgress(BaseModel):
    series_id: str
    chapter_id: str
    paragraph_id: str | None = None
    progress: float = Field(default=0, ge=0, le=1)


class ImportResponse(BaseModel):
    series: SeriesSummary
    imported_volumes: list[str]


class EvidenceRef(BaseModel):
    locator: str
    note: str = ""
    marker: str = ""


class NarrativeScene(BaseModel):
    title: str
    summary: str
    start_locator: str
    end_locator: str


class NarrativeEntity(BaseModel):
    name: str
    entity_type: Literal["character", "place", "group", "object", "concept", "other"]
    role: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    state_changes: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class NarrativeEvent(BaseModel):
    title: str
    summary: str
    participants: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    importance: Literal["major", "supporting", "minor"] = "supporting"
    causes: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class NarrativeRelation(BaseModel):
    subject: str
    predicate: str
    object: str
    note: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class SettingFragment(BaseModel):
    name: str
    category: str
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class TrackCandidate(BaseModel):
    title: str
    category: str
    note: str
    locator: str
    importance: float = Field(default=0.5, ge=0, le=1)


class NarrativePacket(BaseModel):
    chapter_id: str
    chapter_intro: str
    summary_short: str
    summary_detailed: str
    narrative_outline: list[str] = Field(default_factory=list)
    scenes: list[NarrativeScene] = Field(default_factory=list)
    entities: list[NarrativeEntity] = Field(default_factory=list)
    events: list[NarrativeEvent] = Field(default_factory=list)
    relations: list[NarrativeRelation] = Field(default_factory=list)
    setting_fragments: list[SettingFragment] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    key_details: list[EvidenceRef] = Field(default_factory=list)
    dialogue_highlights: list[EvidenceRef] = Field(default_factory=list)
    narrative_observations: list[str] = Field(default_factory=list)
    candidate_track_items: list[TrackCandidate] = Field(default_factory=list)


class RagChunk(BaseModel):
    id: str
    chapter_id: str
    chunk_index: int
    start_paragraph_id: str
    end_paragraph_id: str
    start_locator: str
    end_locator: str
    text: str
    character_count: int
    approximate_tokens: int


class HarnessRun(BaseModel):
    id: str
    series_id: str
    chapter_id: str
    chapter_title: str
    model: str
    prompt_version: str
    status: Literal["running", "completed", "failed"]
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    created_at: str


class HarnessArtifacts(BaseModel):
    chapter_id: str
    packet: NarrativePacket | None = None
    chunks: list[RagChunk] = Field(default_factory=list)
    runs: list[HarnessRun] = Field(default_factory=list)


class HarnessCompileRequest(BaseModel):
    chapter_id: str
    model: str = "MiniMax-M3"
    prompt_version: Literal[
        "chapter-packet-v1",
        "chapter-packet-v2-relations",
        "chapter-packet-v3-faithful",
    ] = "chapter-packet-v3-faithful"
    force: bool = False


class HarnessCompileResponse(BaseModel):
    run: HarnessRun
    artifacts: HarnessArtifacts


class HarnessConfig(BaseModel):
    provider: str = "OpenAI-compatible"
    configured: bool
    models: list[str]
    prompt_versions: list[str]


HarnessVariant = Literal["chapter_packet_dag", "full_volume_flash_map", "fast_volume_direct", "long_context_direct", "rag_agent"]


class VolumeChapterSummary(BaseModel):
    model_config = {"extra": "forbid"}

    chapter_id: str
    title: str
    summary: str


class VolumeReportData(BaseModel):
    model_config = {"extra": "forbid"}

    series_id: str
    volume_id: str
    volume_number: int | float
    title: str
    summary: str
    chapter_summaries: list[VolumeChapterSummary]


class VolumeReportRecord(BaseModel):
    id: str
    series_id: str
    volume_id: str
    volume_number: int | float
    harness_variant: HarnessVariant
    model: str
    prompt_version: str
    status: Literal["draft", "completed", "failed"]
    report: VolumeReportData | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    created_at: str


class WikiCategory(BaseModel):
    id: str
    label: str
    parent_kind: str
    description: str
    theme_color: str = "#667c68"
    introduction: str = ""
    body_markdown: str = ""
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    subcategories: list[dict[str, Any]] = Field(default_factory=list)


class WikiEntryPlan(BaseModel):
    slug: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    kind: str
    level: Literal["stub", "standard", "featured"]
    reason: str = ""
    source_refs: list[str] = Field(default_factory=list)
    template: str = "standard_entry"
    needs_narrative_track: bool = False
    action: Literal["create", "keep", "update", "rebuild"] = "create"
    changed_sections: list[str] = Field(default_factory=list)
    subcategory: str = ""
    writer_tier: Literal["core", "supporting", "local"] = "supporting"
    # Historical blueprints may retain their original provider label; all new
    # executions are forced to the current runtime model by KnowledgeService.
    preferred_model: str = ""
    compile_mode: Literal["agentic", "context_only"] = "agentic"
    tool_budget: int = Field(default=2, ge=0, le=8)
    infobox_schema: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_point_ids: list[str] = Field(default_factory=list)


class WikiSpecialPlan(BaseModel):
    slug: str
    title: str
    introduction: str
    angle: str = ""
    section_brief: list[str] = Field(default_factory=list)
    related_entry_slugs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    knowledge_point_ids: list[str] = Field(default_factory=list)
    action: Literal["create", "keep", "update", "rebuild"] = "create"
    changed_sections: list[str] = Field(default_factory=list)
    tool_budget: int = Field(default=4, ge=0, le=8)
    recommended_on_home: bool = True


class WikiNavigationGroup(BaseModel):
    label: str
    slug: str | None = None
    entry_slugs: list[str] = Field(default_factory=list)


class WikiNavigationBox(BaseModel):
    id: str
    title: str
    title_slug: str | None = None
    groups: list[WikiNavigationGroup] = Field(default_factory=list)


class WikiBlueprintData(BaseModel):
    series_id: str
    coverage_volume_ids: list[str]
    complete_work_coverage: bool
    work_profile: dict[str, Any]
    home_title: str
    home_intro: str
    home_body_markdown: str = ""
    home_sections: list[dict[str, Any]] = Field(default_factory=list)
    categories: list[WikiCategory]
    entry_plans: list[WikiEntryPlan]
    specials: list[WikiSpecialPlan] = Field(default_factory=list)
    navigation_boxes: list[WikiNavigationBox] = Field(default_factory=list)
    templates: dict[str, list[str]] = Field(default_factory=dict)
    template_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    home_action: Literal["keep", "update", "rebuild"] = "update"
    deprecated_entry_slugs: list[str] = Field(default_factory=list)
    architecture_notes: list[str] = Field(default_factory=list)
    infobox_schemas: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class WikiBlueprintRecord(BaseModel):
    id: str
    series_id: str
    version: int
    status: Literal["draft", "publishable", "published"]
    model: str
    blueprint: WikiBlueprintData
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: str


class WikiBlueprintSummary(BaseModel):
    id: str
    version: int
    status: Literal["draft", "publishable", "published"]
    home_title: str
    complete_work_coverage: bool = False


class WikiBlock(BaseModel):
    type: Literal[
        "prose", "module_buttons", "info_cards", "fact_table", "quote",
        "timeline", "narrative_track", "entity_list", "chapter_list",
        "event_list", "related_entries", "callout", "markdown",
        "volume_cards", "chapter_cards",
    ]
    title: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class NarrativeTrackRecord(BaseModel):
    id: str
    entry_id: str
    kind: Literal["point", "range"]
    category: str
    title: str
    note: str
    start_locator: str
    end_locator: str | None = None
    importance: float = Field(default=0.5, ge=0, le=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    source_type: Literal["ai", "citation", "appearance", "relation"] = "ai"


class SpoilerAccessRule(BaseModel):
    mode: Literal["open", "progress", "never"] = "open"
    unlock_after_chapter_id: str | None = None
    reason: str = ""


class SpoilerFragmentRule(SpoilerAccessRule):
    id: str = ""
    target: str = ""


class WikiSpoilerPolicy(BaseModel):
    entry: SpoilerAccessRule = Field(default_factory=SpoilerAccessRule)
    fragments: list[SpoilerFragmentRule] = Field(default_factory=list)


class WikiEntryRecord(BaseModel):
    id: str
    series_id: str
    slug: str
    title: str
    kind: str
    level: Literal["stub", "standard", "featured", "special", "home"]
    theme_color: str
    summary: str
    body_markdown: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    infobox: dict[str, Any] = Field(default_factory=dict)
    blocks: list[WikiBlock] = Field(default_factory=list)
    spoiler_policy: WikiSpoilerPolicy = Field(default_factory=WikiSpoilerPolicy)
    track_items: list[NarrativeTrackRecord] = Field(default_factory=list)
    status: Literal["draft", "published"] = "draft"
    revision: int = 1
    user_created: bool = False
    updated_at: str


class WikiEntrySummary(BaseModel):
    """Small navigation/search projection; full prose is loaded per entry."""
    id: str
    series_id: str
    slug: str
    title: str
    kind: str
    level: Literal["stub", "standard", "featured", "special", "home"]
    theme_color: str
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    spoiler_policy: WikiSpoilerPolicy = Field(default_factory=WikiSpoilerPolicy)
    status: Literal["draft", "published"] = "draft"
    revision: int = 1
    user_created: bool = False
    updated_at: str


class WikiMentionTerm(BaseModel):
    value: str
    canonical_name: str
    match_kind: Literal["literal", "related"]
    score: float = 0


class WikiMentionMatch(BaseModel):
    slug: str
    title: str
    kind: str
    theme_color: str
    terms: list[WikiMentionTerm] = Field(default_factory=list)
    direct_match: bool = False


class WikiTrackDraft(BaseModel):
    kind: Literal["point", "range"] = "point"
    category: str = "story"
    title: str
    note: str = ""
    start_locator: str
    end_locator: str | None = None
    importance: float = Field(default=0.5, ge=0, le=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    source_type: Literal["ai", "citation", "appearance", "relation"] = "ai"


class WikiCompiledPayload(BaseModel):
    title: str
    summary: str
    body_markdown: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    infobox: dict[str, Any] = Field(default_factory=dict)
    blocks: list[WikiBlock] = Field(default_factory=list)
    track_items: list[WikiTrackDraft] = Field(default_factory=list)
    spoiler_policy: WikiSpoilerPolicy


class WikiState(BaseModel):
    series_id: str
    blueprint: WikiBlueprintSummary | None = None
    entries: list[WikiEntrySummary] = Field(default_factory=list)
    categories: list[WikiCategory] = Field(default_factory=list)
    navigation_boxes: list[WikiNavigationBox] = Field(default_factory=list)
    compilation_status: dict[str, Any] = Field(default_factory=dict)


class ChatThreadRecord(BaseModel):
    id: str
    series_id: str
    title: str
    created_at: str
    updated_at: str


class ChatMessageRecord(BaseModel):
    id: str
    thread_id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    citations: list[EvidenceRef] = Field(default_factory=list)
    cards: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    thinking: list[dict[str, Any]] = Field(default_factory=list)
    thinking_seconds: float = 0
    created_at: str


class ChatSpoilerConfirmation(BaseModel):
    id: str
    thread_id: str
    message: str = "完整的答案可能包含严重剧透。"
    thinking: list[dict[str, Any]] = Field(default_factory=list)
    thinking_seconds: float = 0
    created_at: str


class ChatThreadDetail(ChatThreadRecord):
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    pending_spoiler_confirmation: ChatSpoilerConfirmation | None = None


class ChatCreateRequest(BaseModel):
    series_id: str
    title: str = "新对话"


class ReadingSettingsSnapshot(BaseModel):
    paper_color: Literal["standard", "warm"] = "standard"
    theme: Literal["paper", "dark", "system"]
    font_size: int = Field(ge=14, le=23)
    line_height: float = Field(ge=1.55, le=2.4)
    epub_features: Literal["off", "basic", "advanced"]
    illustrations: Literal["off", "original", "immersive"]
    spoiler_shield: Literal["off", "auto", "strict"]
    semantic_retrieval: bool = False


class ChatSendRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    current_chapter_id: str | None = None
    current_paragraph_id: str | None = None
    selected_text: str | None = Field(default=None, max_length=12000)
    reading_settings: ReadingSettingsSnapshot | None = None


class ChatSpoilerDecisionRequest(BaseModel):
    decision: Literal["full", "minimize", "stop"]


class WikiSpecialCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    introduction: str = Field(min_length=1, max_length=8000)
    related_entry_slugs: list[str] = Field(default_factory=list)


class AnalysisStartRequest(BaseModel):
    mode: Literal["quality", "fast"] = "quality"
    vectorize: bool = False


class SeriesUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ModelSettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    fast_model: str = Field(min_length=1, max_length=160)
    advanced_model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=1000)
    reasoning_split: bool = True
    composer_concurrency: int = Field(default=3, ge=1, le=8)
    preview_concurrency: int = Field(default=3, ge=1, le=8)


class EmbeddingSettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=1000)


class AnalysisStageProgress(BaseModel):
    key: Literal["vector", "volume", "architect", "compose", "preview"]
    index: int = Field(ge=1, le=5)
    label: str
    completed_points: int = 0
    total_points: int = 0
    progress: float = Field(default=0, ge=0, le=1)
    status: Literal["pending", "running", "completed", "skipped"] = "pending"


class AnalysisJobRecord(BaseModel):
    id: str
    series_id: str
    mode: Literal["quality", "fast"] = "quality"
    vectorize: bool = False
    pause_requested: bool = False
    status: Literal["pending", "running", "paused", "completed", "failed"]
    corpus_signature: str
    current_step: str | None = None
    total_steps: int = 0
    completed_steps: int = 0
    progress: float = 0
    current_stage_index: int = Field(default=1, ge=1, le=5)
    current_stage_key: Literal["vector", "volume", "architect", "compose", "preview"] = "vector"
    current_stage_label: str = "向量化"
    stage_completed_points: int = 0
    stage_total_points: int = 0
    stage_progress: float = 0
    stages: list[AnalysisStageProgress] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    analyzed_volume_count: int = 0
    wiki_entry_count: int = 0
    message: str = ""
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
