/**
 * CDN 自托管下载器（PLAN §7.6）
 *
 * 把卡片 HTML 引用的外部资源抓到 public/vendor/ 并生成 manifest.json，
 * 供 src/utils/vendor-assets.ts 在 SSR 输出时把外链重写为 vendor/… 相对路径。
 *
 * 用法：
 *   npx tsx scripts/fetch-vendor-assets.ts                 # 抓取 claudeStyle 渲染所需的核心集合
 *   npx tsx scripts/fetch-vendor-assets.ts --scan-templates # 追加扫描 src/templates/*.tsx 里的全部 googleapis css
 *   npx tsx scripts/fetch-vendor-assets.ts <url> [name]     # 追加任意 URL（css 会自动下载并重写其中 woff2）
 *
 * 代理：默认走 http://127.0.0.1:7890，可用 VENDOR_PROXY / https_proxy 覆盖。
 * 注意：googleapis css 按 UA 返回不同格式，必须用现代 Chrome UA 才能拿到 woff2。
 */

import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const VENDOR_DIR = path.join(REPO_ROOT, 'public', 'vendor');
const FONTS_DIR = path.join(VENDOR_DIR, 'fonts');
const MANIFEST_PATH = path.join(VENDOR_DIR, 'manifest.json');

const PROXY =
  process.env.VENDOR_PROXY ?? process.env.https_proxy ?? process.env.HTTPS_PROXY ?? 'http://127.0.0.1:7890';
const CHROME_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const COMMON_GOOGLE_FONTS_URL =
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=Poppins:wght@400;500;600;700&family=Outfit:wght@400;500;600;700&family=Exo+2:wght@400;600;700&family=Rajdhani:wght@400;500;600;700&family=Orbitron:wght@500;700;900&family=Press+Start+2P&family=VT323&family=Fira+Code:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&family=Bebas+Neue&family=Space+Grotesk:wght@400;500;700&family=Fredoka+One&family=Nunito:wght@400;600;700;800&family=Quicksand:wght@500;600;700&family=Playfair+Display:wght@400;500;600;700;900&family=Lora:wght@400;500;600&family=Righteous&family=Noto+Sans+SC:wght@400;500;700&family=Google+Sans:wght@400;500;700&display=swap';

type AssetSpec = { url: string; name: string };

/** claudeStyle 一次渲染出现的全部外部引用（head 三条 + 模板内联两条 + tailwind JIT）。 */
const CORE_ASSETS: AssetSpec[] = [
  { url: 'https://cdn.tailwindcss.com', name: 'tailwindcss.js' },
  { url: 'https://fonts.googleapis.com/icon?family=Material+Icons', name: 'fonts/material-icons.css' },
  {
    url: 'https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,0,0&display=swap',
    name: 'fonts/material-symbols-rounded-400.css',
  },
  {
    url: 'https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,300,0,0&display=swap',
    name: 'fonts/material-symbols-rounded-300.css',
  },
  {
    url: 'https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700&display=swap',
    name: 'fonts/nunito-400-600-700.css',
  },
  { url: COMMON_GOOGLE_FONTS_URL, name: 'fonts/common-fonts.css' },
];

function sha8(s: string): string {
  return crypto.createHash('sha256').update(s).digest('hex').slice(0, 8);
}

function curl(url: string, dest: string): void {
  execFileSync(
    'curl',
    ['-fsSL', '--retry', '3', '--connect-timeout', '15', '--proxy', PROXY, '-A', CHROME_UA, '-o', dest, url],
    { stdio: ['ignore', 'inherit', 'inherit'] },
  );
}

function curlString(url: string): string {
  return execFileSync(
    'curl',
    ['-fsSL', '--retry', '3', '--connect-timeout', '15', '--proxy', PROXY, '-A', CHROME_UA, url],
    { maxBuffer: 64 * 1024 * 1024 },
  ).toString('utf8');
}

/** 扫描模板源码里的 googleapis/icon 外链（模板内联 <link>）。 */
function scanTemplateAssetUrls(): string[] {
  const dir = path.join(REPO_ROOT, 'src', 'templates');
  const urls = new Set<string>();
  const re = /https:\/\/fonts\.googleapis\.com\/[^"'\\)\s]+/g;
  for (const f of fs.readdirSync(dir)) {
    if (!f.endsWith('.tsx') && !f.endsWith('.ts')) continue;
    const text = fs.readFileSync(path.join(dir, f), 'utf8');
    for (const m of text.matchAll(re)) urls.add(m[0].replaceAll('&amp;', '&'));
  }
  return [...urls].sort();
}

/** 从 css URL 生成可读文件名：首个 family slug + 短哈希防冲突。 */
function autoNameForCss(url: string): string {
  const fam = /family=([^&"')]+)/.exec(url)?.[1] ?? 'font';
  const slug = decodeURIComponent(fam)
    .replaceAll('+', '-')
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .toLowerCase();
  return `fonts/auto-${slug}-${sha8(url)}.css`;
}

/** 下载 css 并把其中的 url(https://fonts.gstatic.com/…) 本地化为同目录文件。 */
function vendorCss(url: string, destRel: string): string[] {
  const css = curlString(url);
  const urlRe = /url\(\s*(https:\/\/fonts\.gstatic\.com\/[^)\s]+)\s*\)/g;
  const fileMap = new Map<string, string>();
  let rewritten = css;
  for (const m of css.matchAll(urlRe)) {
    const fontUrl = m[1];
    let fname = fileMap.get(fontUrl);
    if (!fname) {
      const base = decodeURIComponent(path.posix.basename(new URL(fontUrl).pathname)) || `${sha8(fontUrl)}.woff2`;
      fname = base;
      // 同名不同源（理论上 gstatic basename 唯一，防御性处理）
      for (const [u, n] of fileMap) {
        if (n === fname && u !== fontUrl) {
          fname = `${sha8(fontUrl)}-${base}`;
          break;
        }
      }
      fileMap.set(fontUrl, fname);
      const dest = path.join(FONTS_DIR, fname);
      if (!fs.existsSync(dest)) {
        curl(fontUrl, dest);
      }
      rewritten = rewritten.split(fontUrl).join(fname);
    }
  }
  const cssDest = path.join(VENDOR_DIR, destRel);
  fs.mkdirSync(path.dirname(cssDest), { recursive: true });
  fs.writeFileSync(cssDest, rewritten);
  return [...fileMap.values()];
}

function main(): void {
  const args = process.argv.slice(2);
  const scan = args.includes('--scan-templates');
  const extra = args.filter(a => !a.startsWith('--'));

  fs.mkdirSync(FONTS_DIR, { recursive: true });

  const manifest: { generatedAt: string; urls: Record<string, string> } = fs.existsSync(MANIFEST_PATH)
    ? JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'))
    : { generatedAt: '', urls: {} };
  manifest.generatedAt = new Date().toISOString();
  manifest.urls = manifest.urls ?? {};

  const specs: AssetSpec[] = [...CORE_ASSETS];
  if (scan) {
    for (const url of scanTemplateAssetUrls()) {
      if (!manifest.urls[url] && !specs.some(s => s.url === url)) {
        specs.push({ url, name: autoNameForCss(url) });
      }
    }
  }
  if (extra.length) {
    const url = extra[0];
    const name = extra[1] ?? autoNameForCss(url);
    if (!specs.some(s => s.url === url)) specs.push({ url, name });
  }

  let cssCount = 0;
  let fontCount = 0;
  for (const spec of specs) {
    const isCss = /\.css$/.test(spec.name) || /fonts\.googleapis\.com/.test(spec.url);
    console.log(`[vendor] ${spec.url} -> ${spec.name}`);
    if (isCss) {
      const fonts = vendorCss(spec.url, spec.name);
      cssCount += 1;
      fontCount += fonts.length;
    } else {
      const dest = path.join(VENDOR_DIR, spec.name);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      curl(spec.url, dest);
    }
    manifest.urls[spec.url] = `vendor/${spec.name}`;
  }

  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2));
  console.log(
    `[vendor] done: ${Object.keys(manifest.urls).length} url(s) mapped, ${cssCount} css written, ${fontCount} font file(s) under ${path.relative(REPO_ROOT, FONTS_DIR)}`,
  );
}

main();
