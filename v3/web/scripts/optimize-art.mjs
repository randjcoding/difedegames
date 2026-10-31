import sharp from 'sharp';
import { readdir, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
const directory = resolve('public/art');
let files;
try { files = (await readdir(directory)).filter(file => file.endsWith('.png')); } catch { console.log('Artwork not present; CSS fallbacks remain available.'); process.exit(0); }
for (const file of files) {
  for (const width of [768, 1536]) {
    const output = resolve(directory, file.replace('.png', `-${width}.webp`));
    const input = resolve(directory, file);
    try { if ((await stat(output)).mtimeMs >= (await stat(input)).mtimeMs) continue; } catch { /* New derivatives have no cache yet. */ }
    await sharp(input).resize({ width, withoutEnlargement: true }).webp({ quality: 82, effort: 5 }).toFile(output);
  }
}
console.log(`Prepared responsive WebP derivatives for ${files.length} original images.`);
