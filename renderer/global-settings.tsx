import { AnimatedRegion } from "./animated-region";
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { libraryApi } from './lib/api';
import { ReadingPreferences } from './reader-app';
import { desktopConfig, storage } from './storage';
import { BookOpen, Cpu, Settings2 } from './icons';
import type { Theme } from './types';

type Pane = 'general' | 'reading' | 'models';
export function GlobalSettings({ onModels, modelPanel, initialPane = 'general' }: { onModels: () => void; modelPanel: ReactNode; initialPane?: Pane }) {
  const settingsRef = useRef<HTMLElement>(null);
  const [pane, setPane] = useState<Pane>(initialPane);
  const [theme, setTheme] = useState((storage.getItem('lr:theme') ?? 'paper') as Theme);
  const [fontSize, setFontSize] = useState(Number(storage.getItem('lr:reader-font-size') ?? 18));
  const [lineHeight, setLineHeight] = useState(Number(storage.getItem('lr:reader-line-height') ?? 2.05));
  const [epubFeatures, setEpubFeatures] = useState((storage.getItem('lr:epub-features') ?? 'basic') as 'off' | 'basic' | 'advanced');
  const [paperColor, setPaperColor] = useState((storage.getItem('lr:paper-color') ?? 'standard') as 'standard' | 'warm');
  const [illustrations, setIllustrations] = useState((storage.getItem('lr:illustrations') ?? 'original') as 'off' | 'original' | 'immersive');
  const [spoilerShieldMode, setSpoilerShieldMode] = useState((storage.getItem('lr:spoiler-shield') ?? 'auto') as 'off' | 'auto' | 'strict');
  const [semanticRetrieval, setSemanticRetrieval] = useState(storage.getItem('lr:semantic-retrieval') === '1');
  const [error, setError] = useState('');
  const [embeddingConfigured, setEmbeddingConfigured] = useState(false);
  const [accent, setAccent] = useState(storage.getItem('lr:accent') ?? 'green');
  useEffect(() => { setPane(initialPane); }, [initialPane]);
  useEffect(() => () => { window.dispatchEvent(new Event('understory-preferences')); }, []);
  useEffect(() => { void libraryApi.getEmbeddingSettings().then(settings => setEmbeddingConfigured(settings.configured)).catch(reason => setError(String(reason))); }, []);
  const titles = { general: '通用', reading: '阅读设置', models: '模型配置' };
  return <>
    <section ref={settingsRef} className="global-settings settings-page" aria-label="设置">
      <nav className="settings-navigation" aria-label="设置分类">
        <div className="settings-brand"><strong>设置</strong></div>
        {([['general', Settings2], ['reading', BookOpen], ['models', Cpu]] as const).map(([key, Icon]) => <button key={key} aria-current={pane === key ? 'page' : undefined} className={pane === key ? 'active' : ''} onClick={() => { setPane(key); if (key === 'models') onModels(); }}><Icon size={19} />{titles[key]}</button>)}
      </nav>
      <AnimatedRegion className="settings-main" transitionKey={pane} onCommit={() => settingsRef.current?.querySelector('.settings-content')?.scrollTo({ top: 0 })}>
        <header><h1>{titles[pane]}</h1></header>
        <div className="settings-content">{pane === 'general' ? <><section className="settings-section"><h2>外观</h2><h3 className="setting-label">主题</h3><div className="appearance-themes" role="group" aria-label="主题">{([['paper', '浅色'], ['dark', '深色'], ['system', '跟随系统']] as const).map(([key, label]) => <button key={key} aria-pressed={theme === key} onClick={() => { setTheme(key); storage.setItem('lr:theme', key); }}>{label}</button>)}</div><h3 className="setting-label">强调色</h3><div className="accent-choices" role="group" aria-label="强调色">{([['green', '青芽', '#74c184'], ['teal', '潮汐', '#207e78'], ['cyan', '湖蓝', '#247d9d'], ['blue', '蓝色', '#2869bb'], ['indigo', '靛青', '#5867b2'], ['purple', '紫色', '#8053b5'], ['rose', '玫瑰', '#ad4d72'], ['coral', '珊瑚', '#b45844'], ['orange', '琥珀', '#a86620'], ['graphite', '石墨', '#626262']] as const).map(([key, label, color]) => <button key={key} aria-pressed={accent === key} className={accent === key ? 'selected' : ''} onClick={() => { setAccent(key); storage.setItem('lr:accent', key); }}><i style={{ background: color }} /><span>{label}</span></button>)}</div></section><section className="settings-section"><h2>书库</h2><div className="setting-directory"><div><strong>作品数据目录</strong><code>{desktopConfig.preferences.libraryRoot}</code></div><button onClick={() => void window.desktop.chooseLibrary().catch(reason => setError(String(reason)))}>更改目录</button></div><p className="setting-note">更换目录后应用会重启。已有作品保留在原目录。</p></section></> : pane === 'reading' ? <div className="reading-preferences"><ReadingPreferences paperColor={paperColor} setPaperColor={setPaperColor} showTheme={false} theme={theme} setTheme={setTheme} fontSize={fontSize} setFontSize={setFontSize} lineHeight={lineHeight} setLineHeight={setLineHeight} epubFeatures={epubFeatures} setEpubFeatures={setEpubFeatures} illustrations={illustrations} setIllustrations={setIllustrations} spoilerShieldMode={spoilerShieldMode} setSpoilerShieldMode={setSpoilerShieldMode} semanticRetrieval={semanticRetrieval} setSemanticRetrieval={setSemanticRetrieval} embeddingStatus={{ configured: embeddingConfigured, available: embeddingConfigured } as import('./types').EmbeddingIndexStatus} /></div> : modelPanel}{error && <p className="error-message" role="alert">{error}</p>}</div>
      </AnimatedRegion>
    </section>
  </>;
}
