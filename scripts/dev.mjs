import { createServer } from 'vite';
import { spawn } from 'node:child_process';
const server = await createServer();
await server.listen();
const child = spawn('node_modules/electron/dist/electron.exe', ['.'], { stdio: 'inherit', env: { ...process.env, UNDERSTORY_DEV_URL: 'http://127.0.0.1:5173' } });
child.on('exit', async code => { await server.close(); process.exit(code ?? 0); });
process.on('SIGINT', () => child.kill());
