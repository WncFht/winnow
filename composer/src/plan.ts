import {CalculateMetadataFunction, staticFile} from 'remotion';

// render_plan/1 —— contracts/schemas/render_plan.schema.json
// 70_render_plan.json 是 composer 唯一输入：完全解析的绝对秒轴计划，
// shot 窗口/卡片持有区间已由 render_plan.py 编译平铺进 video_track（满铺无洞）。
export type VSeg = {src: string; start: number; end: number};
export type ASeg = {src: string; at: number};
export type OSeg = {src: string; start: number; end: number; xy: string};
export type RenderPlan = {
  schema?: string; // "render_plan/1"
  episode?: string;
  fps?: number; // default 30
  size?: number[]; // [w,h] default [1920,1080]
  total: number; // 秒
  video_track: VSeg[];
  audio_track: ASeg[];
  overlay_track?: OSeg[];
};

export type FullDailyProps = {
  /** 内嵌整份 plan：`--props='{"plan":{...}}'` */
  plan?: RenderPlan;
  /** public-dir 相对路径或可 fetch URL：`--props='{"planUrl":"70_render_plan.json"}'` */
  planUrl?: string;
  /** 相对 src 的前缀：public-dir 相对子路径（'run/...'）或 URL 前缀 */
  assetsBase?: string;
  /** calculateMetadata 填充：overlay src → 字幕文本（<src 去扩展名>.txt sidecar） */
  subTexts?: Record<string, string>;
};

const URLISH = /^(https?:|data:|blob:|file:)/i;

/**
 * 解析 plan 内的 src：
 *  - http(s)/data/blob/file URL          → 原样
 *  - 绝对路径                            → file://（Img 可用；Audio 建议走 --public-dir）
 *  - assetsBase 为 URL / 绝对路径        → 拼接（URL 原样、绝对路径转 file://）
 *  - 其余相对路径                        → staticFile(assetsBase + src)
 * render.sh 总是把 --public-dir 指向 plan 所在 run dir，
 * 故契约相对路径（64_frames/… 61_audio/… 65_subs/…）直接 staticFile 命中。
 */
export const assetSrc = (src: string, base?: string): string => {
  if (URLISH.test(src)) return src;
  if (src.startsWith('/')) return `file://${src}`;
  const b = (base ?? '').replace(/\/+$/, '');
  if (b) {
    if (URLISH.test(b)) return `${b}/${src}`;
    if (b.startsWith('/')) return `file://${b}/${src}`;
    return staticFile(`${b}/${src}`);
  }
  return staticFile(src);
};

/** REMOTION_PLAN 注入（remotion.config.ts DefinePlugin 读文件内容进 bundle） */
const injectedPlanJson = (): string | undefined =>
  typeof process !== 'undefined' ? process.env.REMOTION_PLAN_JSON : undefined;

const looksLikePlan = (p: unknown): p is RenderPlan =>
  !!p && typeof p === 'object' && Array.isArray((p as RenderPlan).video_track) &&
  Array.isArray((p as RenderPlan).audio_track) &&
  typeof (p as RenderPlan).total === 'number';

/**
 * plan 解析优先级：
 *   1. --props '{"plan":{…}}'        （内嵌对象）
 *   2. REMOTION_PLAN=<path> env      （config 读取注入 REMOTION_PLAN_JSON）
 *   3. --props '{"planUrl":"…"}'     （fetch，经 --public-dir/staticFile 或 URL）
 *   4. 默认候选 render_plan.json / 70_render_plan.json（public dir 根）
 */
const resolvePlan = async (
  props: FullDailyProps,
  signal?: AbortSignal,
): Promise<RenderPlan> => {
  if (looksLikePlan(props.plan)) return props.plan;

  const inj = injectedPlanJson();
  if (inj) {
    const p = JSON.parse(inj) as unknown;
    if (looksLikePlan(p)) return p;
  }

  const candidates = [props.planUrl, 'render_plan.json', '70_render_plan.json']
    .filter((u): u is string => !!u);
  const errs: string[] = [];
  for (const u of candidates) {
    try {
      const r = await fetch(assetSrc(u, props.assetsBase), {signal});
      if (r.ok) {
        const p = (await r.json()) as unknown;
        if (looksLikePlan(p)) return p;
        errs.push(`${u}: 不是 render_plan 形状`);
      } else {
        errs.push(`${u}: HTTP ${r.status}`);
      }
    } catch (e) {
      errs.push(`${u}: ${String(e)}`);
    }
  }
  throw new Error(
    'FullDaily: 找不到 70_render_plan.json (' +
      errs.join('; ') +
      ')。用法: --public-dir=<run_dir> --props=\'{"planUrl":"70_render_plan.json"}\' ' +
      '或 REMOTION_PLAN=<abs path to 70_render_plan.json>。',
  );
};

/**
 * 字幕 live-text：PLAN §7.7「Remotion 用 live-text，ffmpeg 兜底用 PNG pill」。
 * overlay_track[].src 指向 PNG pill（契约要求文件存在）；约定同 basename 的
 * .txt sidecar 存口播文本（如 65_subs/000.txt）。fetch 到 → live-text pill；
 * 没有 → 退回 <Img> PNG pill 按 xy 定位。text 放不进 OSeg schema（extra=forbid），
 * sidecar 是给 Remotion 的投影伴生文件。
 */
const loadSubTexts = async (
  plan: RenderPlan,
  props: FullDailyProps,
  signal?: AbortSignal,
): Promise<Record<string, string>> => {
  const out: Record<string, string> = {};
  await Promise.all(
    (plan.overlay_track ?? []).map(async (o) => {
      const t = o.src.replace(/\.[a-z0-9]+$/i, '.txt');
      if (t === o.src) return;
      try {
        const r = await fetch(assetSrc(t, props.assetsBase), {signal});
        if (r.ok) {
          const txt = (await r.text()).trim();
          if (txt) out[o.src] = txt;
        }
      } catch {
        /* 无 sidecar → PNG pill 兜底 */
      }
    }),
  );
  return out;
};

/**
 * duration/fps/size 全部由 plan 决定（PLAN §7.7/D4：aspect 只做 16:9，
 * size 是参数，Composition 宽高随 plan.size）。
 */
export const calcPlanMetadata: CalculateMetadataFunction<
  FullDailyProps
> = async ({props, abortSignal}) => {
  const plan = await resolvePlan(props, abortSignal);
  const fps = plan.fps ?? 30;
  const [w, h] = plan.size ?? [1920, 1080];
  const subTexts = await loadSubTexts(plan, props, abortSignal);
  return {
    durationInFrames: Math.max(1, Math.ceil(plan.total * fps)),
    fps,
    width: w,
    height: h,
    props: {...props, plan, subTexts},
  };
};
