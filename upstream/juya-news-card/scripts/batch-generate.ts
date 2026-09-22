/**
 * 批量测试脚本
 * 
 * 使用 mock 数据测试所有主题的渲染效果，无需调用 LLM API。
 * 
 * 用法：
 *   npm run batch-generate                    # 使用默认主题 (googleMaterial)
 *   npm run batch-generate -- --theme xxx     # 使用指定主题
 *   npm run batch-generate -- --list-themes   # 列出所有主题
 *   npm run batch-generate -- --all-themes    # 测试所有主题
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { chromium } from 'playwright';
import minimist from 'minimist';
import { generateHtmlFromReactComponent } from '../server/ssr-helper.js';
import { TEMPLATES } from '../src/templates/index.js';
import { writeHtmlForFileRender } from '../src/utils/vendor-assets.js';

// 导入 mock 数据
import mockData from '../tests/mock-data.json';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const args = minimist(process.argv.slice(2));

// 列出所有主题
if (args['list-themes']) {
  console.log('Available themes:');
  Object.keys(TEMPLATES).forEach(id => console.log(`- ${id}`));
  console.log(`\nTotal: ${Object.keys(TEMPLATES).length} themes`);
  process.exit(0);
}

// 默认主题为 googleMaterial
const DEFAULT_THEME = 'googleMaterial';

// 获取要测试的主题列表
function getThemesToTest(): string[] {
  if (args['all-themes']) {
    return Object.keys(TEMPLATES);
  }
  const themeId = args.theme || DEFAULT_THEME;
  if (!TEMPLATES[themeId]) {
    console.error(`Error: Theme '${themeId}' not found. Use --list-themes to see available themes.`);
    process.exit(1);
  }
  return [themeId];
}

/**
 * 为单个 mock 数据项生成截图和 HTML
 */
async function generateForMockData(
  mockItem: typeof mockData[0],
  cardCount: number,
  themeId: string,
  outputDir: string,
  browser: any
) {
  console.log(`  [${cardCount} Cards] ${mockItem.mainTitle}`);

  try {
    const html = generateHtmlFromReactComponent(mockItem, themeId);

    // 使用 Playwright 截图
    const context = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();

    // 注入自定义字体
    const fontPathCandidates = [
      path.join(process.cwd(), 'assets', 'htmlFont.ttf'),
      path.join(process.cwd(), 'public', 'assets', 'htmlFont.ttf'),
    ];
    const fontPath = fontPathCandidates.find(p => fs.existsSync(p));
    let finalHtml = html;
    if (fontPath) {
      const fontBase64 = fs.readFileSync(fontPath).toString('base64');
      const fontFace = `
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
      finalHtml = html.replace('</head>', `${fontFace}</head>`);
    }

    // vendor/… 相对引用需 file:// 导航（注入 <base> 指向 public/）
    const { fileUrl } = writeHtmlForFileRender(finalHtml, outputDir, `.render-${cardCount}.html`);
    await page.goto(fileUrl, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);

    // 保存截图
    const screenshotPath = path.join(outputDir, `cards-${cardCount}.png`);
    await page.screenshot({ path: screenshotPath });

    // 保存 HTML 和数据
    const subDir = path.join(outputDir, `data-${cardCount}`);
    fs.mkdirSync(subDir, { recursive: true });
    fs.writeFileSync(path.join(subDir, 'content.json'), JSON.stringify(mockItem, null, 2));
    fs.writeFileSync(path.join(subDir, 'page.html'), html);

    await context.close();
    console.log(`    ✓ Saved to ${screenshotPath}`);

  } catch (error) {
    console.error(`    ✗ Error for ${cardCount} cards:`, error);
  }
}

/**
 * 测试单个主题
 */
async function testTheme(themeId: string, baseOutputDir: string, browser: any) {
  console.log(`\n📦 Testing theme: ${themeId}`);

  const themeOutputDir = path.join(baseOutputDir, themeId);
  fs.mkdirSync(themeOutputDir, { recursive: true });

  // mock 数据按卡片数量排列（1-8张卡片）
  for (let i = 0; i < mockData.length; i++) {
    const mockItem = mockData[i];
    const cardCount = i + 1;
    await generateForMockData(mockItem, cardCount, themeId, themeOutputDir, browser);
  }
}

async function main() {
  const themesToTest = getThemesToTest();
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const baseOutputDir = path.join(process.cwd(), `output/batch-test-${timestamp}`);
  fs.mkdirSync(baseOutputDir, { recursive: true });

  console.log('='.repeat(60));
  console.log('🚀 Batch Generation (Using Mock Data)');
  console.log('='.repeat(60));
  console.log(`Output directory: ${baseOutputDir}`);
  console.log(`Themes to test: ${themesToTest.length}`);
  console.log(`Mock data items: ${mockData.length} (1-8 cards)`);

  const browser = await chromium.launch();

  for (const themeId of themesToTest) {
    await testTheme(themeId, baseOutputDir, browser);
  }

  await browser.close();

  console.log('\n' + '='.repeat(60));
  console.log('✅ Batch Generation Complete');
  console.log('='.repeat(60));
  console.log(`All results are in: ${baseOutputDir}`);
}

main();
