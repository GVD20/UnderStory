import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

// Render the maintained vector master; optional argument selects an installed sharp module.
const require = createRequire(import.meta.url);
const sharp = require(process.argv[2] || 'sharp');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assets = path.join(root, 'assets', 'brand');
const svg = await fs.readFile(path.join(assets, 'app-icon.svg'));
await sharp(svg).resize(1024, 1024).png().toFile(path.join(assets, 'app-icon.png'));
const sizes = [16, 24, 32, 48, 64, 128, 256];
const images = await Promise.all(sizes.map(size => sharp(svg).resize(size, size).png().toBuffer()));
const header = Buffer.alloc(6 + sizes.length * 16);
header.writeUInt16LE(1, 2);
header.writeUInt16LE(sizes.length, 4);
let offset = header.length;
images.forEach((png, i) => {
  const entry = 6 + i * 16;
  header[entry] = header[entry + 1] = sizes[i] === 256 ? 0 : sizes[i];
  header.writeUInt16LE(1, entry + 4);
  header.writeUInt16LE(32, entry + 6);
  header.writeUInt32LE(png.length, entry + 8);
  header.writeUInt32LE(offset, entry + 12);
  offset += png.length;
});
await fs.writeFile(path.join(assets, 'app-icon.ico'), Buffer.concat([header, ...images]));

const logo = await fs.readFile(path.join(assets, 'logo.svg'), 'utf8');
const tiles = [];
for (const [size, left] of [[256, 28], [64, 316], [32, 412], [16, 478]]) {
  tiles.push({ input: await sharp(svg).resize(size, size).png().toBuffer(), left, top: 32 });
}
for (const [color, left] of [['#292929', 328], ['#eeeeee', 420]]) {
  tiles.push({ input: await sharp(Buffer.from(logo.replaceAll('currentColor', color))).resize(24, 24).png().toBuffer(), left, top: 144 });
}
const canvas = Buffer.from('<svg width="540" height="324" xmlns="http://www.w3.org/2000/svg"><path fill="#f3f3f3" d="M0 0h396v324H0z"/><path fill="#202020" d="M396 0h144v324H396z"/></svg>');
await sharp(canvas).composite(tiles).png().toFile(path.join(assets, 'preview.png'));
console.log('Exported app-icon.png, multi-size app-icon.ico and preview.png');
