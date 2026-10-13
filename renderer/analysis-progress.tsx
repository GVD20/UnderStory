import { useEffect, useState } from 'react';
import { libraryApi } from './lib/api';
import type { AnalysisJobRecord, SeriesSummary } from './types';
import { BookOpen, X } from './icons';

export function AnalysisProgress({ series, onClose, onCompleted, onStarted }: { series: SeriesSummary; onClose: () => void; onCompleted: () => void; onStarted?: () => void }) {
  const [loaded, setLoaded] = useState(false);
  const [job, setJob] = useState<AnalysisJobRecord | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [vectorize, setVectorize] = useState(false);
  const [mode, setMode] = useState<'quality' | 'fast'>('quality');
  useEffect(() => {
    let live = true;
    const refresh = () => libraryApi.getLatestAnalysis(series.id).then(value => { if (live) setJob(value); }).catch(reason => { if (live) setError(String(reason)); }).finally(() => { if (live) setLoaded(true); });
    void refresh();
    const timer = setInterval(() => void refresh(), 2500);
    return () => { live = false; clearInterval(timer); };
  }, [series.id]);
  useEffect(() => {
    if (!job) return;
    setMode(job.mode);
    setVectorize(job.vectorize);
  }, [job?.id]);
  useEffect(() => { if (job?.status === 'completed') onCompleted(); }, [job?.status]);
  async function run() {
    setBusy(true); setError('');
    try { setJob(await libraryApi.startAnalysis(series.id, mode, vectorize)); onStarted?.(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  async function pause() {
    if (!job) return;
    setBusy(true);
    try { setJob(await libraryApi.pauseAnalysis(job.id)); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  const running = job?.status === 'running' || job?.status === 'pending';
  const pausing = running && job?.pause_requested;
  const optionsLocked = busy || !loaded || Boolean(job && job.status !== 'completed');
  const progress = Math.max(0, Math.min(100, (job?.progress ?? 0) * 100));
  const welcome = loaded && !running && progress === 0;
  return <section className="analysis-simple" role="dialog" aria-modal="true" aria-label="分析本书">
    <header><h2>分析本书</h2><button aria-label="关闭" onClick={onClose}><X /></button></header>
    <h3>{series.title}</h3>
    {!loaded ? <p role="status">正在读取分析状态…</p> : welcome ? <div className="analysis-welcome"><BookOpen size={30} /><h3>开始分析本书</h3><p>生成章节轻览与 Wiki，让对话能够检索作品内容。</p></div> : <>
    <div className="analysis-percent" aria-live="polite">{Math.round(progress)}<small>%</small></div>
    <progress value={progress} max={100} />
    <p>{pausing ? '正在等待当前模型请求完成并保存，随后暂停…' : job?.status === 'completed' ? '分析完成' : job?.status === 'paused' ? '已暂停，可继续分析' : job?.status === 'failed' ? '分析中断，可重试' : stageLabel(job?.current_stage_label || '准备整理章节、Wiki 与轻览')}</p>
    <ol className="analysis-stages">{job?.stages.map(stage => <li key={stage.key} data-status={stage.status}><span>{stageLabel(stage.label)}</span><small>{stage.status === 'completed' ? '完成' : stage.status === 'skipped' ? '跳过' : stage.status === 'running' ? `${stage.completed_points} / ${stage.total_points}` : '等待'}</small></li>)}</ol>
    </>}
    <fieldset className="analysis-mode-picker" disabled={optionsLocked}>
      <legend>分析模式</legend>
      <div>{(['quality', 'fast'] as const).map(value => <label key={value} data-selected={mode === value}>
        <input type="radio" name="analysis-mode" value={value} checked={mode === value} onChange={() => setMode(value)} />
        <span><strong>{value === 'quality' ? '质量分析' : '快速分析'}</strong><small>{value === 'quality' ? '完整 Agent 轮次与词条上下文' : '压缩轮次，精简本地词条上下文'}</small></span>
      </label>)}</div>
    </fieldset>
    {job && job.status !== 'completed' && !running && <p>本轮选项已固定，完成后可切换模式。</p>}
    {job?.status === 'paused' && job.message && <p className="error-message" role="status">{stageLabel(job.message)}</p>}
    {(error || job?.error) && <p className="error-message" role="alert">{error || job?.error}</p>}
    <footer>{!running && <label className="semantic-index-option"><input type="checkbox" disabled={optionsLocked} checked={vectorize} onChange={event => setVectorize(event.target.checked)} />同时建立语义索引</label>}<button className="primary" disabled={busy || !loaded || pausing} onClick={() => void (running ? pause() : run())}>{busy ? '处理中…' : pausing ? '正在暂停…' : running ? '暂停分析' : job && job.status !== 'completed' ? '继续分析' : job ? '重新分析' : '开始分析'}</button></footer>
  </section>;
}

function stageLabel(label: string) { return label.replace(/Wiki Blueprint/gi, "Wiki 规划").replace(/Wiki Compose/gi, "Wiki 词条"); }
