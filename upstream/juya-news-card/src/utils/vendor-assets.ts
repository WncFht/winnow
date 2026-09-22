/**
 * CDN 自托管支持（PLAN §7.6）
 *
 * SSR 输出的 HTML 通过 manifest（public/vendor/manifest.json）把外部
 * CDN 引用（cdn.tailwindcss.com / fonts.googleapis.com / fonts.gstatic.com …）
 * 重写为相对路径 `vendor/…`，使卡片渲染在被墙/离线环境下不退化。
 *
 * - `localizeExternalAssetUrls(html)`：在 ssr-runtime 中对最终 HTML 做
 *   字符串级替换，覆盖 head 链接与各模板内联的外联 <link>/<script>。
 * - 相对 `vendor/…` 引用在 http 根路径服务与 file:// 渲染（配合
 *   `injectFileBaseHref` 注入的 <base>）下均可解析。
 * - manifest 缺失时全部 no-op，回退上游原行为（外链 CDN）。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export type VendorManifest = {
  generatedAt?: string;
  urls: Record<string, string>;
};

let manifestCache: Record<string, string> | null = null;

function candidateManifestPaths(): string[] {
  const paths: string[] = [];
  try {
    paths.push(fileURLToPath(new URL('../../public/vendor/manifest.json', import.meta.url)));
  } catch {
    /* import.meta.url unavailable (bundled) */
  }
  paths.push(path.resolve(process.cwd(), 'public', 'vendor', 'manifest.json'));
  return paths;
}

/** 读取 vendor manifest；不存在/不可读时返回 {}（no-op 回退）。 */
export function loadVendorManifest(): Record<string, string> {
  if (manifestCache) return manifestCache;
  manifestCache = {};
  for (const p of candidateManifestPaths()) {
    try {
      const parsed = JSON.parse(fs.readFileSync(p, 'utf8')) as VendorManifest;
      if (parsed && typeof parsed.urls === 'object' && parsed.urls) {
        manifestCache = parsed.urls;
      }
    } catch {
      /* keep looking */
    }
    if (Object.keys(manifestCache).length) break;
  }
  return manifestCache;
}

/** 测试/长驻进程下强制重读 manifest。 */
export function resetVendorManifestCache(): void {
  manifestCache = null;
}

const EXTERNAL_ASSET_HOSTS = /https?:\/\/(?:cdn\.tailwindcss\.com|fonts\.googleapis\.com|fonts\.gstatic\.com)[^"'\\\s<>)]*/g;

/**
 * 把 HTML 中出现的 manifest 外部 URL 替换为本地 `vendor/…` 相对路径。
 * 同时处理 `&` → `&amp;` 的转义形式（SSR/escapeHtml 输出）。
 * 未被 manifest 覆盖的外部资源 URL 保留原样并 warn。
 */
export function localizeExternalAssetUrls(html: string): string {
  const urls = loadVendorManifest();
  const entries = Object.entries(urls)
    .filter(([orig, local]) => Boolean(orig) && Boolean(local))
    .sort((a, b) => b[0].length - a[0].length); // 长 URL 先替换，避免前缀误伤

  let out = html;
  for (const [orig, local] of entries) {
    if (out.includes(orig)) out = out.split(orig).join(local);
    const amp = orig.replaceAll('&', '&amp;');
    if (amp !== orig && out.includes(amp)) out = out.split(amp).join(local);
  }

  const leftovers = out.match(EXTERNAL_ASSET_HOSTS);
  if (leftovers && leftovers.length > 0) {
    const unique = [...new Set(leftovers)];
    console.warn(
      `[vendor-assets] ${unique.length} external asset ref(s) not vendored (run scripts/fetch-vendor-assets.ts): ${unique.join(', ')}`,
    );
  }
  return out;
}

/** `<pkg>/public/` 目录的 file:// URL（带尾斜杠），供 <base> 注入用。 */
export function publicDirFileHref(): string {
  let publicDir: string;
  try {
    publicDir = fileURLToPath(new URL('../../public/', import.meta.url));
  } catch {
    publicDir = path.resolve(process.cwd(), 'public') + path.sep;
  }
  if (!publicDir.endsWith(path.sep)) publicDir += path.sep;
  return pathToFileURL(publicDir).href;
}

/**
 * 在 <head> 首位注入 <base href="file://…/public/">，
 * 使写到任意位置的 HTML 都能解析相对 `vendor/…`、`assets/…` 引用。
 * （page.setContent 的 about:blank 文档禁止加载 file:// 子资源，
 *  必须 page.goto(file://…) 渲染，故配套此函数写盘后导航。）
 */
export function injectFileBaseHref(html: string): string {
  const base = `<base href="${publicDirFileHref()}" />`;
  if (html.includes('<head>')) return html.replace('<head>', `<head>\n  ${base}`);
  return `${base}\n${html}`;
}

/** 写 HTML 到 outDir 并返回可用于 page.goto 的 file:// URL。 */
export function writeHtmlForFileRender(
  html: string,
  outDir: string,
  name: string,
): { htmlPath: string; fileUrl: string } {
  fs.mkdirSync(outDir, { recursive: true });
  const htmlPath = path.join(outDir, name);
  fs.writeFileSync(htmlPath, injectFileBaseHref(html));
  return { htmlPath, fileUrl: pathToFileURL(htmlPath).href };
}
