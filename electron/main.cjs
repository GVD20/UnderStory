const { app, BrowserWindow, ipcMain, dialog, safeStorage, shell, Menu, nativeTheme } = require('electron');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
let backend, window, bootstrap;
const root = path.resolve(__dirname, '..');
app.setName('UnderStory');
if (process.env.UNDERSTORY_DEVICE) { fs.mkdirSync(process.env.UNDERSTORY_DEVICE, { recursive: true }); app.setPath('userData', process.env.UNDERSTORY_DEVICE); }
if (!app.requestSingleInstanceLock()) app.exit(0);
app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
function read(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; } }
function write(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file + '.tmp', JSON.stringify(value, null, 2)); fs.renameSync(file + '.tmp', file); }
async function startBackend(libraryRoot) {
  const token = randomBytes(32).toString('hex');
  const executable = app.isPackaged ? path.join(process.resourcesPath, 'understory-backend', 'understory-backend.exe') : path.join(root, '.venv', 'Scripts', 'python.exe');
  const args = app.isPackaged ? [] : [path.join(root, 'backend', 'run.py')];
  backend = spawn(executable, args, { windowsHide: true, env: { ...process.env, UNDERSTORY_LIBRARY: libraryRoot, UNDERSTORY_DEVICE: app.getPath('userData'), UNDERSTORY_TOKEN: token }, stdio: ['ignore', 'pipe', 'pipe'] });
  const logs = fs.createWriteStream(path.join(app.getPath('logs'), 'backend.log'), { flags: 'a' });
  backend.stderr.pipe(logs);
  backend.once('exit', () => logs.end());
  return new Promise((resolve, reject) => {
    let buffer = '';
    const timeout = setTimeout(() => { backend.kill(); reject(new Error('阅读服务启动超时，请查看日志。')); }, 30000);
    backend.once('error', error => { clearTimeout(timeout); reject(error); });
    backend.once('exit', code => { clearTimeout(timeout); reject(new Error(`阅读服务已退出 (${code})`)); });
    backend.stdout.on('data', chunk => { buffer += chunk; const match = buffer.match(/UNDERSTORY_READY:(\d+)/); if (match) { clearTimeout(timeout); resolve({ origin: `http://127.0.0.1:${match[1]}`, token }); } });
  });
}
function allowedSender(event) { if (event.sender !== window?.webContents) throw new Error('Invalid IPC sender'); }
app.whenReady().then(async () => {
  const prefPath = path.join(app.getPath('userData'), 'preferences.json');
  const preferences = read(prefPath, { schemaVersion: 1, libraryRoot: path.join(app.getPath('documents'), 'UnderStory Library'), storage: {} });
  const themeSource = storage => storage?.['lr:theme'] === 'dark' ? 'dark' : storage?.['lr:theme'] === 'system' ? 'system' : 'light';
  nativeTheme.themeSource = themeSource(preferences.storage);
  if (process.env.UNDERSTORY_LIBRARY) preferences.libraryRoot = process.env.UNDERSTORY_LIBRARY;
  fs.mkdirSync(preferences.libraryRoot, { recursive: true });
  write(prefPath, preferences);
  const config = await startBackend(preferences.libraryRoot);
  bootstrap = { ...config, preferences, platform: process.platform };
  Menu.setApplicationMenu(null);
  const dark = nativeTheme.shouldUseDarkColors;
  const appIcon = app.isPackaged ? path.join(process.resourcesPath, 'app-icon.ico') : path.join(root, 'assets', 'brand', 'app-icon.ico');
  const micaAvailable = process.platform === 'win32' && Number(require('node:os').release().split('.')[2]) >= 22621;
  window = new BrowserWindow({ width: 1280, height: 850, minWidth: 850, minHeight: 560, show: false, title: 'UnderStory', icon: appIcon, backgroundColor: micaAvailable || process.platform === 'darwin' ? '#00000000' : dark ? '#202020' : '#f5f5f5', backgroundMaterial: micaAvailable ? 'mica' : undefined, vibrancy: process.platform === 'darwin' ? 'under-window' : undefined, titleBarStyle: 'hidden', titleBarOverlay: { color: '#00000000', symbolColor: dark ? '#eeeeee' : '#242424', height: 64 }, webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.webContents.session.webRequest.onBeforeSendHeaders({ urls: [config.origin + '/*'] }, (details, callback) => { details.requestHeaders['X-UnderStory-Token'] = config.token; callback({ requestHeaders: details.requestHeaders }); });
  nativeTheme.on('updated', () => {
    if (!window || window.isDestroyed()) return;
    if (process.platform === 'win32') window.setTitleBarOverlay({ color: '#00000000', symbolColor: nativeTheme.shouldUseDarkColors ? '#eeeeee' : '#242424', height: 64 });
    if (!micaAvailable && process.platform !== 'darwin') window.setBackgroundColor(nativeTheme.shouldUseDarkColors ? '#202020' : '#f5f5f5');
  });
  window.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//.test(url)) shell.openExternal(url); return { action: 'deny' }; });
  window.webContents.on('will-navigate', (event, url) => { if (url !== window.webContents.getURL()) { event.preventDefault(); if (/^https?:\/\//.test(url)) shell.openExternal(url); } });
  ipcMain.handle('bootstrap', event => { allowedSender(event); return bootstrap; });
  ipcMain.handle('preferences', (event, storage) => { allowedSender(event); if (!storage || typeof storage !== 'object') throw new Error('Invalid preferences'); preferences.storage = storage; write(prefPath, preferences); nativeTheme.themeSource = themeSource(storage); });
  ipcMain.handle('library-choose', async event => { allowedSender(event); const result = await dialog.showOpenDialog(window, { properties: ['openDirectory', 'createDirectory'] }); if (result.canceled) return null; preferences.libraryRoot = result.filePaths[0]; write(prefPath, preferences); app.relaunch(); app.quit(); return preferences.libraryRoot; });
  ipcMain.handle('secret-write', (event, name, secret) => { allowedSender(event); if (!['model', 'embedding'].includes(name) || typeof secret !== 'string') throw new Error('Invalid credential'); if (!safeStorage.isEncryptionAvailable()) throw new Error('系统凭据加密不可用'); fs.writeFileSync(path.join(app.getPath('userData'), name + '.secret'), safeStorage.encryptString(secret)); });
  ipcMain.handle('secret-read', (event, name) => { allowedSender(event); if (!['model', 'embedding'].includes(name)) throw new Error('Invalid credential'); const file = path.join(app.getPath('userData'), name + '.secret'); return fs.existsSync(file) ? safeStorage.decryptString(fs.readFileSync(file)) : ''; });
  window.once('ready-to-show', () => window.show());
  if (process.env.UNDERSTORY_DEV_URL) await window.loadURL(process.env.UNDERSTORY_DEV_URL); else await window.loadFile(path.join(root, 'dist', 'index.html'));
}).catch(error => { dialog.showErrorBox('UnderStory', error.message); app.quit(); });
app.on('before-quit', () => backend?.kill());
app.on('window-all-closed', () => app.quit());
