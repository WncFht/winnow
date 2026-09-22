import type { GeneratedContent } from '../types';
import { ensureTemplateResolverRegistered } from './runtime-resolver';
import { generateDownloadableHtml } from '../utils/template';
import { localizeExternalAssetUrls } from '../utils/vendor-assets';

type HtmlRenderOptions = {
  bottomReservedPx?: number;
};

export function generateTemplateHtml(
  data: GeneratedContent,
  templateId?: string,
  options?: HtmlRenderOptions,
): string {
  ensureTemplateResolverRegistered();
  // CDN 自托管（PLAN §7.6）：把 head/模板内联的外链资源按
  // public/vendor/manifest.json 重写为 vendor/… 相对路径；
  // manifest 缺失时 no-op，保持上游 CDN 行为。
  return localizeExternalAssetUrls(generateDownloadableHtml(data, templateId, options));
}
