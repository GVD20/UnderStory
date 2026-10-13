import { UnderStoryLogo } from "./brand-logo";
import { AnimatedRegion } from "./animated-region";
import { GlobalSettings } from "./global-settings";


import { ArrowLeft, BookOpenText, ChevronRight, Cpu, Import, LibraryBig, Pencil, Home, Plus, Settings2, Sparkles, Trash2, X } from "./icons";
import { ChangeEvent, FormEvent, useCallback, useEffect, useState } from "react";
import { UnderStoryApp } from "./reader-app";
import { API_ORIGIN, libraryApi } from "./lib/api";
import { ModalLayer } from "./modal-layer";
import type { EmbeddingIndexHistoryItem, EmbeddingSettings, ModelSettings, SeriesSummary } from "./types";

function seriesFromHash() {
  const prefix = "#series=";
  if (!location.hash.startsWith(prefix)) return "";
  try { return decodeURIComponent(location.hash.slice(prefix.length)); }
  catch { return ""; }
}

export function LibraryApp() {
  const [library, setLibrary] = useState<SeriesSummary[]>([]);
  const [selectedSeriesId, setSelectedSeriesId] = useState("");
  const [routeReady, setRouteReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [importOpen, setImportOpen] = useState(false);
  const [importMode, setImportMode] = useState<"new" | "existing">("new");
  const [importTargetId, setImportTargetId] = useState("");
  const [importing, setImporting] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [volumeNumbers, setVolumeNumbers] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [titleEdited, setTitleEdited] = useState(false);
  const [error, setError] = useState("");
  const [fileInputKey, setFileInputKey] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<SeriesSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [renameTarget, setRenameTarget] = useState<SeriesSummary | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [modelKey, setModelKey] = useState("");
  const [changingModelKey, setChangingModelKey] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [modelAction, setModelAction] = useState<"models" | "test" | "">("");
  const [modelNotice, setModelNotice] = useState("");
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [settingsPane, setSettingsPane] = useState<"menu" | "llm" | "embedding">("menu");
  const [embeddingSettings, setEmbeddingSettings] = useState<EmbeddingSettings | null>(null);
  const [embeddingKey, setEmbeddingKey] = useState("");
  const [changingEmbeddingKey, setChangingEmbeddingKey] = useState(false);
  const [embeddingAction, setEmbeddingAction] = useState<"models" | "test" | "save" | "">("");
  const [embeddingNotice, setEmbeddingNotice] = useState("");
  const [availableEmbeddingModels, setAvailableEmbeddingModels] = useState<string[]>([]);
  const [embeddingIndexes, setEmbeddingIndexes] = useState<EmbeddingIndexHistoryItem[]>([]);

  const refresh = useCallback(async () => {
    const result = await libraryApi.getLibrary();
    setLibrary(result.series);
    return result.series;
  }, []);

  useEffect(() => {
    queueMicrotask(() => { setSelectedSeriesId(seriesFromHash()); setSettingsOpen(location.hash === "#settings"); setRouteReady(true); });
    void libraryApi.getLibrary().then((result) => setLibrary(result.series)).catch(() => setError("本地阅读服务尚未就绪。" )).finally(() => setLoading(false));
    const onLocationChange = () => { setSelectedSeriesId(seriesFromHash()); setSettingsOpen(location.hash === "#settings"); };
    window.addEventListener("popstate", onLocationChange);
    return () => window.removeEventListener("popstate", onLocationChange);
  }, [refresh]);


  useEffect(() => {
    const open = () => void openModelSettings();
    window.addEventListener("understory-model-settings", open);
    return () => window.removeEventListener("understory-model-settings", open);
  }, []);

  function openSeries(seriesId: string) {
    history.pushState(null, "", `${location.pathname}${location.search}#series=${encodeURIComponent(seriesId)}`);
    setSettingsOpen(false);
    setSelectedSeriesId(seriesId);
  }

  function returnToLibrary() {
    history.pushState(null, "", location.pathname + location.search);
    setSelectedSeriesId(""); setSettingsOpen(false); setModelOpen(false);
  }

  function openSettings(models = false) {
    if (location.hash !== "#settings") history.pushState(null, "", location.pathname + location.search + "#settings");
    setSelectedSeriesId(""); setSettingsOpen(true); setModelOpen(models);
  }

  const importTarget = library.find(series => series.id === importTargetId);
  function chooseImportTarget(id: string) {
    setImportTargetId(id); setError("");
    const next = Math.max(0, ...(library.find(series => series.id === id)?.volumes.map(volume => volume.number) ?? [])) + 1;
    setVolumeNumbers(files.map((_, index) => String(next + index)));
  }

  function pickFiles(event: ChangeEvent<HTMLInputElement>) {
    const existing = new Set(files.map(fileKey));
    const additions = [...event.target.files ?? []].filter((file) => !existing.has(fileKey(file)));
    const candidates = [
      ...files.map((file, index) => ({ file, number: volumeNumbers[index] ?? String(index + 1) })),
      ...additions.map((file, index) => ({ file, number: String((importMode === "existing" ? Math.max(0, ...(importTarget?.volumes.map(volume => volume.number) ?? [])) + files.length + index + 1 : guessVolumeNumber(file.name, files.length + index + 1))) })),
    ].sort((left, right) => Number(left.number) - Number(right.number));
    const nextFiles = candidates.map((item) => item.file);
    setFiles(nextFiles);
    setVolumeNumbers(candidates.map((item) => item.number));
    if (nextFiles.length > 1 && !titleEdited) setTitle(guessSeriesTitle(nextFiles.map((file) => file.name)));
    setFileInputKey((value) => value + 1);
    setError("");
  }

  function removeImportFile(index: number) {
    setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setVolumeNumbers((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setError("");
  }

  function updateImportVolumeNumber(index: number, value: string) {
    const candidates = files.map((file, itemIndex) => ({
      file, number: itemIndex === index ? value : volumeNumbers[itemIndex] ?? "",
    }));
    if (candidates.every((item) => item.number !== "" && Number.isFinite(Number(item.number)))) {
      candidates.sort((left, right) => Number(left.number) - Number(right.number));
    }
    setFiles(candidates.map((item) => item.file));
    setVolumeNumbers(candidates.map((item) => item.number));
  }

  async function importWork(event: FormEvent) {
    event.preventDefault();
    if (!files.length || importing) return;
    const numbers = volumeNumbers.map(Number);
    if (numbers.some((number) => !Number.isFinite(number) || number <= 0)) {
      setError("每个卷号都必须是大于 0 的有效数字。"); return;
    }
    if (new Set(numbers).size !== numbers.length) {
      setError("待导入列表中仍有重复卷号，请调整后再确认。"); return;
    }
    if (importMode === "existing" && !importTarget) { setError("请选择要合并的图书。"); return; }
    if (importMode === "existing" && importTarget?.volumes.some(volume => numbers.includes(volume.number))) { setError("卷号与已有卷重复，请调整后导入。"); return; }
    setImporting(true); setError("");
    try {
      const result = await libraryApi.importEpubs(files, numbers, importMode === "existing" ? importTargetId : undefined, importMode === "existing" ? importTarget?.title : title);
      await refresh();
      setImportOpen(false); setFiles([]); setVolumeNumbers([]); setTitle(""); setTitleEdited(false); setFileInputKey((value) => value + 1);
      openSeries(result.series.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "EPUB 导入失败");
    } finally { setImporting(false); }
  }

  async function deleteWork() {
    if (!deleteTarget || deleting) return;
    setDeleting(true); setError("");
    try { await libraryApi.deleteSeries(deleteTarget.id); await refresh(); setDeleteTarget(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "删除作品失败"); }
    finally { setDeleting(false); }
  }

  async function renameWork(event: FormEvent) {
    event.preventDefault();
    if (!renameTarget || !renameTitle.trim()) return;
    setError("");
    try { await libraryApi.renameSeries(renameTarget.id, renameTitle.trim()); await refresh(); setRenameTarget(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "作品名称修改失败"); }
  }

  async function openModelSettings() {
    setError(""); setModelNotice(""); setEmbeddingNotice(""); setAvailableModels([]); setAvailableEmbeddingModels([]); setSettingsPane("llm"); openSettings(true);
    try {
      const [settings, embedding, history] = await Promise.all([libraryApi.getModelSettings(), libraryApi.getEmbeddingSettings(), libraryApi.getEmbeddingIndexHistory().catch(() => ({ indexes: [], count: 0 }))]);
      const concurrency = normalizedConcurrency(settings.composer_concurrency ?? settings.preview_concurrency);
      setModelSettings({ ...settings, composer_concurrency: concurrency, preview_concurrency: concurrency });
      setEmbeddingSettings(embedding);
      setEmbeddingIndexes(history.indexes);
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : "模型配置无法载入"); }
  }

  async function saveModelSettings(event: FormEvent) {
    event.preventDefault();
    if (!modelSettings) return;
    setSavingSettings(true); setError("");
    try {
      setModelSettings(await libraryApi.saveModelSettings({ ...modelSettings, api_key: modelKey }));
      setModelKey(""); setChangingModelKey(false); setModelNotice("配置已保存，后续模型请求将使用这组设置。");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "模型配置保存失败"); }
    finally { setSavingSettings(false); }
  }

  async function inspectModels() {
    if (!modelSettings || modelAction) return;
    setModelAction("models"); setError(""); setModelNotice("");
    try {
      const result = await libraryApi.listConfiguredModels({ ...modelSettings, api_key: modelKey });
      setAvailableModels(result.models);
      setModelNotice(`接口返回 ${result.count} 个可用模型。`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法获取模型列表"); }
    finally { setModelAction(""); }
  }

  async function testModelConnection() {
    if (!modelSettings || modelAction) return;
    setModelAction("test"); setError(""); setModelNotice("");
    try {
      const result = await libraryApi.testModelConnection({ ...modelSettings, api_key: modelKey });
      setModelNotice(result.message);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "模型连接测试失败"); }
    finally { setModelAction(""); }
  }

  async function saveEmbeddingSettings() {
    if (!embeddingSettings || embeddingAction) return;
    setEmbeddingAction("save"); setError(""); setEmbeddingNotice("");
    try {
      setEmbeddingSettings(await libraryApi.saveEmbeddingSettings({ ...embeddingSettings, api_key: embeddingKey }));
      const history = await libraryApi.getEmbeddingIndexHistory();
      setEmbeddingIndexes(history.indexes);
      const restored = history.indexes.some((item) => item.current_configuration && item.active);
      setEmbeddingKey(""); setChangingEmbeddingKey(false); setEmbeddingNotice(restored ? "Embedding 配置已保存，已重新加载此前建立的向量数据库。" : "Embedding 配置已保存，旧数据库已保留；可在作品分析中建立当前模型的新数据库。");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Embedding 配置保存失败"); }
    finally { setEmbeddingAction(""); }
  }

  async function inspectEmbeddingModels() {
    if (!embeddingSettings || embeddingAction) return;
    setEmbeddingAction("models"); setError(""); setEmbeddingNotice("");
    try {
      const result = await libraryApi.listEmbeddingModels({ ...embeddingSettings, api_key: embeddingKey });
      setAvailableEmbeddingModels(result.models); setEmbeddingNotice(`接口返回 ${result.count} 个可用模型。`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法获取向量化模型列表"); }
    finally { setEmbeddingAction(""); }
  }

  async function testEmbeddingConnection() {
    if (!embeddingSettings || embeddingAction) return;
    setEmbeddingAction("test"); setError(""); setEmbeddingNotice("");
    try {
      const result = await libraryApi.testEmbeddingConnection({ ...embeddingSettings, api_key: embeddingKey });
      setEmbeddingNotice(result.message);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Embedding 连接测试失败"); }
    finally { setEmbeddingAction(""); }
  }

  const modelPanel = (<form className="model-settings-dialog" onSubmit={(event) => settingsPane === "llm" ? void saveModelSettings(event) : event.preventDefault()} aria-label="模型配置">
        <div className="model-tabs" role="group" aria-label="模型类型"><button type="button" className={settingsPane === "llm" ? "active" : ""} onClick={() => { setSettingsPane("llm"); setError(""); }}><Sparkles size={17} />LLM</button><button type="button" className={settingsPane === "embedding" ? "active" : ""} onClick={() => { setSettingsPane("embedding"); setError(""); }}><Cpu size={17} />Embedding</button></div>
        <div className="model-settings-content" key={settingsPane}>{settingsPane === "llm" && modelSettings ? <>
          <label>服务名称<input value={modelSettings.provider} onChange={(event) => setModelSettings({ ...modelSettings, provider: event.target.value })} /></label>
          <div className="model-tier-grid">
            <label><span><strong>快速模型</strong><small>章节整理、轻览与常规词条</small></span><input list="available-models" value={modelSettings.fast_model} onChange={(event) => setModelSettings({ ...modelSettings, fast_model: event.target.value })} placeholder="例如 MiniMax-M3" /></label>
            <label><span><strong>高级模型</strong><small>卷级分析、Wiki 规划与对话</small></span><input list="available-models" value={modelSettings.advanced_model} onChange={(event) => setModelSettings({ ...modelSettings, advanced_model: event.target.value })} placeholder="例如 MiniMax-M3" /></label>
          </div>
          <datalist id="available-models">{availableModels.map((model) => <option value={model} key={model} />)}</datalist>
          <label>API 地址<input value={modelSettings.base_url} onChange={(event) => setModelSettings({ ...modelSettings, base_url: event.target.value })} /></label>
          <label className="model-concurrency-slider"><span><strong>分析并发</strong><small>Wiki 词条与章节轻览同时请求数</small></span><output>{normalizedConcurrency(modelSettings.composer_concurrency)}</output><input type="range" min={1} max={8} step={1} value={normalizedConcurrency(modelSettings.composer_concurrency)} onChange={(event) => { const concurrency = normalizedConcurrency(event.target.value); setModelSettings({ ...modelSettings, composer_concurrency: concurrency, preview_concurrency: concurrency }); }} /><div><span>1</span><span>8</span></div></label>
          {modelSettings.api_key_configured && !changingModelKey ? <div className="model-key-status"><span><strong>API Key</strong><small>{modelSettings.api_key_hint} · 已保存</small></span><button type="button" onClick={() => setChangingModelKey(true)}>更换密钥</button></div> : <label>API Key<input autoFocus={!modelSettings.api_key_configured} type="password" value={modelKey} onChange={(event) => setModelKey(event.target.value)} placeholder="输入 API Key" /></label>}
          <div className="model-connection-actions"><button type="button" disabled={Boolean(modelAction)} onClick={() => void inspectModels()}>{modelAction === "models" ? "正在获取…" : "获取模型列表"}</button><button type="button" disabled={Boolean(modelAction)} onClick={() => void testModelConnection()}>{modelAction === "test" ? "正在测试…" : "测试连接"}</button></div>
          {availableModels.length > 0 && <p className="model-list-result">{availableModels.join(" · ")}</p>}
          {modelNotice && <p className="model-test-success">{modelNotice}</p>}
          <label className="settings-check"><input type="checkbox" checked={modelSettings.reasoning_split} onChange={(event) => setModelSettings({ ...modelSettings, reasoning_split: event.target.checked })} />接收模型的独立思考过程</label>
        </> : settingsPane === "embedding" && embeddingSettings ? <>
          <p className="embedding-intro">Embedding 作为独立外挂服务运行。作品原文和查询必须使用同一向量化模型；更换模型或 API 地址后会建立新的索引版本。</p>
          <label>服务名称<input value={embeddingSettings.provider} onChange={(event) => setEmbeddingSettings({ ...embeddingSettings, provider: event.target.value })} placeholder="例如 本地 Embedding 服务" /></label>
          <label><span><strong>向量化模型</strong><small>服务端可识别的模型 ID</small></span><input list="available-embedding-models" value={embeddingSettings.model} onChange={(event) => setEmbeddingSettings({ ...embeddingSettings, model: event.target.value })} placeholder="例如 BAAI/bge-m3" /></label>
          <datalist id="available-embedding-models">{availableEmbeddingModels.map((model) => <option value={model} key={model} />)}</datalist>
          <label>API 地址<input value={embeddingSettings.base_url} onChange={(event) => setEmbeddingSettings({ ...embeddingSettings, base_url: event.target.value })} placeholder="http://127.0.0.1:8000/v1" /></label>
          {embeddingSettings.api_key_configured && !changingEmbeddingKey ? <div className="model-key-status"><span><strong>API Key</strong><small>{embeddingSettings.api_key_hint} · 已保存</small></span><button type="button" onClick={() => setChangingEmbeddingKey(true)}>更换密钥</button></div> : <label>API Key（本地服务可留空）<input type="password" value={embeddingKey} onChange={(event) => setEmbeddingKey(event.target.value)} placeholder="可选" /></label>}
          <div className="model-connection-actions"><button type="button" disabled={Boolean(embeddingAction)} onClick={() => void inspectEmbeddingModels()}>{embeddingAction === "models" ? "正在获取…" : "获取模型列表"}</button><button type="button" disabled={Boolean(embeddingAction)} onClick={() => void testEmbeddingConnection()}>{embeddingAction === "test" ? "正在测试…" : "测试向量化"}</button></div>
          {availableEmbeddingModels.length > 0 && <p className="model-list-result">{availableEmbeddingModels.join(" · ")}</p>}
          {embeddingNotice && <p className="model-test-success">{embeddingNotice}</p>}
          <section className="embedding-index-history"><header><strong>历史向量数据库</strong><small>{embeddingIndexes.length ? `${embeddingIndexes.length} 个作品索引` : "尚未建立"}</small></header>
            {embeddingIndexes.length ? <div>{embeddingIndexes.map((item) => <article className={item.active ? "active" : ""} key={item.id}><span><strong>{item.model}</strong><small>{item.series_title} · {item.dimensions ? `${item.dimensions} 维` : "维度待定"} · {item.embedded_chunks}/{item.total_chunks} 切片</small></span><em>{item.active ? "当前加载" : item.current_configuration ? "等待补建" : "已保留"}</em></article>)}</div> : <p>完成作品向量化后，数据库会保留在这里。切换模型不会删除历史索引。</p>}
          </section>
        </> : <ModelSettingsSkeleton />}
        {error && <p className="library-import-error">{error}</p>}
        </div>
        {settingsPane !== "menu" && <footer>{settingsPane === "llm" ? <button className="primary" disabled={!modelSettings || savingSettings}>{savingSettings ? "正在保存…" : "保存 LLM 配置"}</button> : <button type="button" className="primary" disabled={!embeddingSettings || Boolean(embeddingAction)} onClick={() => void saveEmbeddingSettings()}>{embeddingAction === "save" ? "正在保存…" : "保存 Embedding 配置"}</button>}</footer>}
      </form>);

  if (!routeReady) return <main className="library-shell route-loading" aria-live="polite"><span className="wordmark">UnderStory</span><div className="loading-line" /><p>正在打开书架…</p></main>;


  return <main className={`library-shell library-enter ${selectedSeriesId && !settingsOpen ? "work-open" : ""}`} data-testid="library-home">
    <AnimatedRegion className="application-region" transitionKey={selectedSeriesId && !settingsOpen ? `work:${selectedSeriesId}` : "home"}>
    {selectedSeriesId && !settingsOpen ? <div className="route-stage"><UnderStoryApp seriesId={selectedSeriesId} onBackToLibrary={returnToLibrary} /></div> : <>
    <nav className="library-nav" aria-label="主导航" data-active={settingsOpen ? 1 : 0}><button className={!settingsOpen ? "active" : ""} title="书架" aria-label="书架" onClick={returnToLibrary}><Home size={24} filled={!settingsOpen} /></button><button className={settingsOpen ? "active" : ""} title="设置" aria-label="设置" onClick={() => openSettings()}><Settings2 size={24} filled={settingsOpen} /></button></nav>
    <header className="library-header"><div><UnderStoryLogo /><span className="wordmark">UnderStory</span></div><div className="library-actions">{!settingsOpen && <button className="library-import" onClick={() => { setImportOpen(true); setError(""); }}><Import size={16} />导入 EPUB</button>}</div></header>
    <AnimatedRegion className="home-content-region" transitionKey={settingsOpen ? "settings" : "shelf"} axis="vertical" reverse={!settingsOpen}>
    {settingsOpen ? <GlobalSettings initialPane={modelOpen ? "models" : "general"} modelPanel={modelPanel} onModels={() => void openModelSettings()} /> : <section className="library-content"><h1 className="shelf-title">书架</h1>
      {loading ? <div className="library-empty"><Sparkles size={21} /><span>正在整理书架…</span></div> : library.length ? <div className="series-grid">{library.map((series) => {
        const cover = series.volumes.find((volume) => volume.cover_url)?.cover_url;
        const chapters = series.volumes.reduce((sum, volume) => sum + volume.chapters.length, 0);
        return <article className="series-card-shell" key={series.id}><button className="series-card" onClick={() => openSeries(series.id)}>
          <span className="series-cover">{cover ? <img src={`${API_ORIGIN}${cover}`} alt={`${series.title}封面`} /> : <LibraryBig size={28} />}</span>
          <span className="series-card-copy"><small>{series.volumes.length} 卷 · {chapters} 篇</small><strong>{series.title}</strong><em>{series.authors?.join("、") || (series.subtitle === "本地 EPUB 书库" ? "" : series.subtitle)}</em><span>{series.total_characters.toLocaleString()} 字</span></span><ChevronRight size={18} />
        </button><div className="series-card-actions"><button aria-label={`编辑 ${series.title}`} title="编辑作品名" onClick={() => { setRenameTarget(series); setRenameTitle(series.title); }}><Pencil size={14} /></button><button aria-label={`删除 ${series.title}`} title="删除作品" onClick={() => setDeleteTarget(series)}><Trash2 size={14} /></button></div></article>;
      })}<button className="series-import-tile" aria-label="导入作品" onClick={() => setImportOpen(true)}><Plus /></button></div> : <div className="library-empty"><BookOpenText size={25} /><strong>书架还是空的</strong><span>一次选择一部作品的多卷 EPUB，即可建立新的作品书库。</span><button onClick={() => setImportOpen(true)}>导入第一部作品</button></div>}
    </section>}
    </AnimatedRegion>
    </>}
    </AnimatedRegion>
    <ModalLayer open={importOpen} className="import-backdrop" role="presentation">
      <form className="library-import-dialog" onSubmit={importWork} aria-label="导入 EPUB">
        <header><div><span className="eyebrow">New work</span><h2>导入 EPUB</h2></div><button type="button" aria-label="关闭导入" disabled={importing} onClick={() => setImportOpen(false)}><X size={17} /></button></header>
        <div className="import-mode" role="group" aria-label="导入方式"><button type="button" disabled={importing} aria-pressed={importMode === "new"} onClick={() => { setImportMode("new"); setVolumeNumbers(files.map((file, index) => String(guessVolumeNumber(file.name, index + 1)))); setError(""); }}>新建图书</button><button type="button" disabled={importing || !library.length} aria-pressed={importMode === "existing"} onClick={() => { setImportMode("existing"); chooseImportTarget(importTargetId || library[0]?.id || ""); }}>合并到已有图书</button></div>
        {importMode === "existing" ? <label>目标图书<select disabled={importing} value={importTargetId} onChange={event => chooseImportTarget(event.target.value)}>{library.map(series => <option key={series.id} value={series.id}>{series.title} · {series.volumes.length} 卷</option>)}</select><small className="setting-note">追加新卷，已有正文、分析和对话保留。</small></label> :
        <label>作品名称 <input value={title} onChange={(event) => { setTitle(event.target.value); setTitleEdited(true); }} placeholder={files.length > 1 ? "已根据文件名自动识别，可修改" : "选择多卷后自动识别，也可手动填写"} disabled={importing} /></label>}
        <label className={`epub-drop ${files.length ? "compact" : ""}`}><Import size={20} /><strong>{files.length ? "添加新卷" : "选择 EPUB 文件"}</strong><span>{files.length ? `当前共 ${files.length} 卷` : "支持一次选择多卷"}</span><input key={fileInputKey} type="file" disabled={importing} multiple accept=".epub,application/epub+zip" onChange={pickFiles} /></label>
        {files.length > 0 && <div className="import-volume-list">
          <div className="import-volume-head"><span>文件</span><span>卷号</span><span /></div>
          {files.map((file, index) => <div className="import-volume-row" key={fileKey(file)}>
            <span title={file.name}>{file.name}</span>
            <input aria-label={`${file.name} 卷号`} type="number" disabled={importing} min="0.1" step="0.1" value={volumeNumbers[index] ?? ""} onChange={(event) => updateImportVolumeNumber(index, event.target.value)} />
            <button type="button" aria-label={`移除 ${file.name}`} disabled={importing} onClick={() => removeImportFile(index)}><Trash2 size={14} /></button>
          </div>)}
        </div>}
        {error && <p className="library-import-error">{error}</p>}
        <footer><button type="button" disabled={importing} onClick={() => setImportOpen(false)}>取消</button><button className="primary" disabled={!files.length || importing}>{importing ? "正在导入…" : `确认导入${files.length ? ` ${files.length} 卷` : ""}`}</button></footer>
      </form>
    </ModalLayer>
    <ModalLayer open={Boolean(deleteTarget)} className="import-backdrop" role="presentation">{deleteTarget && <section className="delete-work-dialog" role="dialog" aria-modal="true" aria-label="删除作品"><Trash2 size={22} /><h2>删除《{deleteTarget.title}》？</h2><p>作品将从书架移除，完整数据保留在书库的 .trash 目录中。</p>{error && <p className="library-import-error">{error}</p>}<footer><button onClick={() => setDeleteTarget(null)}>取消</button><button className="danger" disabled={deleting} onClick={() => void deleteWork()}>{deleting ? "正在删除…" : "删除作品"}</button></footer></section>}</ModalLayer>
    <ModalLayer open={Boolean(renameTarget)} className="import-backdrop">{renameTarget && <form className="compact-dialog" onSubmit={renameWork} aria-label="编辑作品名称"><header><h2>编辑作品名称</h2><button type="button" aria-label="关闭编辑" onClick={() => setRenameTarget(null)}><X size={17} /></button></header><label>作品名称<input autoFocus value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} /></label>{error && <p className="library-import-error">{error}</p>}<footer><button type="button" onClick={() => setRenameTarget(null)}>取消</button><button className="primary">保存</button></footer></form>}</ModalLayer>

  </main>;
}

function ModelSettingsSkeleton() { return <div className="settings-skeleton" aria-label="正在读取模型配置"><i /><i /><i /><i /></div>; }

function normalizedConcurrency(value: unknown) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.max(1, Math.min(8, Math.round(numeric))) : 3;
}

function fileKey(file: File) { return `${file.name}:${file.size}:${file.lastModified}`; }

function guessVolumeNumber(filename: string, fallback: number) {
  const matches = [...filename.replace(/\.epub$/i, "").matchAll(/(?<!\d)(\d{1,3}(?:\.\d+)?)(?!\d)/g)];
  const value = Number(matches.at(-1)?.[1]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function guessSeriesTitle(filenames: string[]) {
  const candidates = filenames.map((filename) => filename
    .replace(/\.epub$/i, "")
    .replace(/^\s*[\[【][^\]】]+[\]】][.．\s]*/, "")
    .replace(/\s+(?:v(?:ol(?:ume)?)?\.?\s*|第\s*)?\d{1,3}(?:\.\d+)?(?:\s|卷|$).*$/i, "")
    .replace(/\s*(?:[-_—–]\s*)?(?:v(?:ol(?:ume)?)?\.?\s*|第\s*)?\d{1,3}(?:\.\d+)?\s*(?:卷)?\s*$/i, "")
    .replace(/[\s\-_—–·]+$/g, "").trim());
  if (candidates.every((candidate) => candidate === candidates[0])) return candidates[0];
  let common = candidates[0] ?? "";
  for (const candidate of candidates.slice(1)) {
    while (common && !candidate.startsWith(common)) common = common.slice(0, -1);
  }
  return common.replace(/\s+\d*$/, "").replace(/[\s\-_—–·]+$/g, "").trim();
}
