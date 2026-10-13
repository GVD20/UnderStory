declare global {
  interface Window { desktop: { bootstrap: () => Promise<{ origin: string; token: string; preferences: { libraryRoot: string; storage: Record<string, string> }; platform: string }>; savePreferences: (storage: Record<string, string>) => Promise<void>; chooseLibrary: () => Promise<string | null>; writeSecret: (name: string, value: string) => Promise<void>; readSecret: (name: string) => Promise<string> }; }
}
export let desktopConfig: Awaited<ReturnType<Window['desktop']['bootstrap']>>;
let values: Record<string, string> = {};
let saveTimer: ReturnType<typeof setTimeout>;
let activeWork = '';
const workValues: Record<string, Record<string, string>> = {};
const workTimers: Record<string, ReturnType<typeof setTimeout>> = {};
const isDeviceKey = (key: string) => /^lr:(theme|accent|reader-font-size|reader-line-height|paper-color|epub-features|illustrations|spoiler-shield|semantic-retrieval)$/.test(key);
export async function initializeDesktop() {
  desktopConfig = await window.desktop.bootstrap(); values = { ...desktopConfig.preferences.storage };
  document.documentElement.dataset.platform = desktopConfig.platform;
  applyTheme();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
  const [model, embedding] = await Promise.all(['model', 'embedding'].map(name => window.desktop.readSecret(name)));
  const response = await fetch(desktopConfig.origin + '/api/credentials', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-UnderStory-Token': desktopConfig.token }, body: JSON.stringify({ model, embedding }) });
  if (!response.ok) throw new Error('无法初始化模型凭据');
}
export async function selectWork(id: string) {
  if (activeWork === id) return;
  if (!workValues[id]) {
    const response = await fetch(`${desktopConfig.origin}/api/state/${encodeURIComponent(id)}`, { headers: { 'X-UnderStory-Token': desktopConfig.token } });
    if (!response.ok) throw new Error('无法读取作品状态');
    workValues[id] = await response.json() as Record<string, string>;
  }
  activeWork = id;
}
export const storage = {
  getItem: (key: string) => (isDeviceKey(key) ? values : workValues[activeWork] ?? {})[key] ?? null,
  setItem(key: string, value: string) { (isDeviceKey(key) ? values : workValues[activeWork] ??= {})[key] = value; if (key === 'lr:theme' || key === 'lr:accent') applyTheme(); persist(key); },
  removeItem(key: string) { delete (isDeviceKey(key) ? values : workValues[activeWork] ?? {})[key]; persist(key); },
};
function applyTheme() {
  document.documentElement.dataset.accent = values['lr:accent'] ?? 'green';
  const theme = values['lr:theme'] ?? 'paper';
  document.documentElement.dataset.theme = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'paper';
}
function persist(key: string) {
  if (!isDeviceKey(key)) {
    const id = activeWork;
    if (!id) return;
    clearTimeout(workTimers[id]);
    workTimers[id] = setTimeout(() => { void fetch(`${desktopConfig.origin}/api/state/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-UnderStory-Token': desktopConfig.token }, body: JSON.stringify(workValues[id]) }); }, 150);
    return;
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void window.desktop.savePreferences(values); }, 150);
}
