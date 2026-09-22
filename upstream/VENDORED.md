# Vendored: juya-news-card

- 上游: https://github.com/Mappedinfo/juya-news-card.git
- 定格 SHA: e7442e4a29c35fce0dea1538c892b7f04b1c119d
- License: MIT（LICENSE 已保留）
- 原内部 gitdir 移至 `upstream/juya-news-card.gitdir/`（gitignored，可改名回 `.git` 恢复 diff 能力）

## 本地 patch（相对定格 SHA 的全部改动）

运行时依赖 `stages/cards.py` → `npx tsx scripts/render-batch.ts`：

- `scripts/render-batch.ts`（新）——批量渲染入口，cards.py 直接调用
- `scripts/fetch-vendor-assets.ts`（新）——CDN 资产自托管抓取
- `src/utils/vendor-assets.ts`（新）——vendor/ 路径解析 + file:// 渲染写盘
- `public/vendor/`（新）——自托管 tailwind JIT / 字体 woff2，断 CDN 不降级
- `src/templates/overviewMasonry.tsx`（新）——备用模板
- `src/templates/ssr-runtime.ts` ——HTML 生成走相对 vendor/ 引用
- `src/templates/claudeStyle.tsx` `index.ts` `catalog.ts` `meta.json` ——模板修正
- `server/next-runtime.ts` `scripts/{generate,offline-render,batch-generate,generate-review-pack}.ts` ——vendor 适配小改
- `package.json` / `package-lock.json` ——+tsx/playwright 等渲染依赖

## 重新同步上游

```
mv upstream/juya-news-card.gitdir upstream/juya-news-card/.git
git -C upstream/juya-news-card fetch origin
git -C upstream/juya-news-card diff HEAD origin/main   # 看上游新增
# 同步完后把 .git 再挪回 .gitdir（或保持 vendor 不提交流程）
```
