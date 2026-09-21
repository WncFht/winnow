import {Config} from '@remotion/cli/config';
import {readFileSync} from 'node:fs';
import {isAbsolute, resolve} from 'node:path';
import {DefinePlugin} from 'webpack';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
Config.setChromiumDisableWebSecurity(true);

// REMOTION_PLAN=<path to 70_render_plan.json>：
// config 在 Node 侧读文件，内容经 DefinePlugin 注入 bundle
// （process.env.REMOTION_PLAN_JSON，见 src/plan.ts resolvePlan 优先级 #2）。
// 相对 src 仍需 --public-dir=<run dir> 或 props.assetsBase 解析（render.sh 已处理）。
const planPath = process.env.REMOTION_PLAN;
if (planPath) {
  const abs = isAbsolute(planPath)
    ? planPath
    : resolve(process.cwd(), planPath);
  const json = readFileSync(abs, 'utf-8');
  process.env.REMOTION_PLAN_JSON = json; // Remotion 亦内联 REMOTION_* env，双保险
  Config.overrideWebpackConfig((conf) => ({
    ...conf,
    plugins: [
      ...(conf.plugins ?? []),
      new DefinePlugin({
        'process.env.REMOTION_PLAN_JSON': JSON.stringify(json),
      }),
    ],
  }));
}
