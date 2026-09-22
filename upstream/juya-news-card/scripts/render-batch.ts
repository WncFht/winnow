import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { chromium } from 'playwright';
import { generateTemplateHtml } from '../src/templates/ssr-runtime.js';
import { writeHtmlForFileRender } from '../src/utils/vendor-assets.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function main() {
  const argv = process.argv.slice(2);
  const dumpDom = argv.includes('--dump-dom');
  const [inputPath, outputDir] = argv.filter(a => !a.startsWith('--'));
  if (!inputPath || !outputDir) {
    console.error('Usage: tsx scripts/render-batch.ts <items.json> <output-dir> [--dump-dom]');
    process.exit(1);
  }

  const raw = JSON.parse(fs.readFileSync(inputPath, 'utf-8'));
  // 兼容裸 items[] 数组与 63_cards.json envelope（{schema, episode, items[]）
  const items = Array.isArray(raw) ? raw : raw.items;
  if (!Array.isArray(items)) {
    console.error('Input must be an items array or an envelope with .items[]');
    process.exit(1);
  }
  const domDir = path.join(outputDir, 'dom');
  fs.mkdirSync(domDir, { recursive: true });

  const browser = await chromium.launch();
  const fontPathCandidates = [
    path.join(process.cwd(), 'assets', 'htmlFont.ttf'),
    path.join(process.cwd(), 'public', 'assets', 'htmlFont.ttf'),
  ];
  const fontPath = fontPathCandidates.find(p => fs.existsSync(p));
  let fontFace = '';
  if (fontPath) {
    const fontBase64 = fs.readFileSync(fontPath).toString('base64');
    fontFace = `
      <style>
        @font-face {
          font-family: 'CustomPreviewFont';
          src: url(data:font/ttf;base64,${fontBase64}) format('truetype');
        }
        .main-container {
          font-family: 'CustomPreviewFont', system-ui, -apple-system, sans-serif !important;
        }
      </style>
    `;
  }

  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
  });

  for (const item of items) {
    const { id, template, bottomReservedPx, ...content } = item;
    const html = generateTemplateHtml(
      content,
      template || 'claudeStyle',
      Number.isFinite(bottomReservedPx) ? { bottomReservedPx } : undefined,
    );
    const finalHtml = fontFace ? html.replace('</head>', `${fontFace}</head>`) : html;

    // CDN 自托管后的 HTML 使用相对 vendor/… 引用；setContent(about:blank)
    // 无法解析也不允许加载 file:// 子资源，故写盘注入 <base> 后走 file:// 导航。
    const { htmlPath, fileUrl } = writeHtmlForFileRender(finalHtml, domDir, `${id}.html`);

    const page = await context.newPage();
    await page.goto(fileUrl, { waitUntil: 'networkidle' });
    await page.evaluate(() => (document as Document & { fonts?: FontFaceSet }).fonts?.ready);
    await page.waitForTimeout(1200);

    if (dumpDom) {
      fs.writeFileSync(path.join(domDir, `${id}.dom.html`), await page.content());
    }

    const screenshotPath = path.join(outputDir, `${id}.png`);
    await page.screenshot({ path: screenshotPath });
    console.log(`Rendered ${id} (${content.cards.length} cards) -> ${path.relative(process.cwd(), htmlPath)}`);
    await page.close();
  }

  await context.close();
  await browser.close();
}

main();
