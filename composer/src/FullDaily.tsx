import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence} from 'remotion';
import {assetSrc, FullDailyProps, OSeg} from './plan';

// 消费 70_render_plan.json（render_plan/1，composer 唯一输入）。
// 与 remotion-feas 验证过的 compose.py 逐点移植语义一致：
//  - video_track 已平铺满铺 [0,total]：逐段 <Sequence from><Img>（shot 窗口
//    三段嵌套由 render_plan.py 预先编译成独立 VSeg，此处不做推断）
//  - audio_track：无界 <Sequence from=at><Audio>，自然播完 = adelay+amix
//  - overlay_track 字幕 pill：有 <src>.txt sidecar → live-text pill
//    （bottom:60 向上生长防溢出 + maxWidth wrap）；否则 <Img> PNG pill 按 xy 定位

export const SubtitlePill: React.FC<{text: string}> = ({text}) => (
  <div
    style={{
      position: 'absolute',
      bottom: 60,
      left: '50%',
      transform: 'translateX(-50%)',
      background: 'rgba(0,0,0,0.75)',
      color: 'white',
      fontSize: 44,
      lineHeight: 1.35,
      padding: '16px 42px',
      borderRadius: 44,
      fontFamily: '"Alibaba PuHuiTi 3.0","Noto Sans CJK SC",sans-serif',
      maxWidth: 1600,
      textAlign: 'center',
    }}
  >
    {text}
  </div>
);

/** ffmpeg overlay 表达式 → CSS 定位。契约形态 "(main_w-overlay_w)/2:930"。 */
const xyStyle = (
  xy: string,
  W: number,
  H: number,
): React.CSSProperties => {
  const [xe = '', ye = ''] = xy.split(':');
  const ev = (e: string): number => {
    try {
      return new Function(
        'main_w',
        'main_h',
        'overlay_w',
        'overlay_h',
        `return (${e});`,
      )(W, H, 0, 0) as number;
    } catch {
      return NaN;
    }
  };
  const style: React.CSSProperties = {position: 'absolute'};
  // 含 overlay_w 的横向表达式基本都是居中 → 用 CSS 精确居中（不依赖 PNG 宽度）
  if (/overlay_w/.test(xe)) {
    style.left = '50%';
    style.transform = 'translateX(-50%)';
  } else {
    const v = ev(xe);
    if (Number.isFinite(v)) {
      style.left = v;
    } else {
      style.left = '50%';
      style.transform = 'translateX(-50%)';
    }
  }
  // 纵向：纯数值/可求值（overlay_h=0 近似）→ top；否则回退字幕默认 bottom:60
  const vy = ev(ye);
  if (Number.isFinite(vy) && vy >= 0 && vy < H) style.top = vy;
  else style.bottom = 60;
  return style;
};

const OverlaySeg: React.FC<{
  o: OSeg;
  i: number;
  W: number;
  H: number;
  base?: string;
  text?: string;
}> = ({o, i, W, H, base, text}) =>
  text ? (
    <SubtitlePill text={text} />
  ) : (
    <Img
      src={assetSrc(o.src, base)}
      style={xyStyle(o.xy, W, H)}
      data-overlay={i}
    />
  );

export const FullDaily: React.FC<FullDailyProps> = (props) => {
  const {plan, assetsBase, subTexts} = props;
  if (!plan) {
    // calculateMetadata 必然先跑；到不了这里，除非 comp 被绕过元数据直挂
    throw new Error('FullDaily: plan 未解析（calculateMetadata 未运行？）');
  }
  const fps = plan.fps ?? 30;
  const [W, H] = plan.size ?? [1920, 1080];
  const f = (sec: number) => Math.round(sec * fps);

  return (
    <AbsoluteFill style={{background: '#000'}}>
      {/* video track：满铺无洞，逐段 Img */}
      {plan.video_track.map((v, i) => (
        <Sequence
          key={`v${i}`}
          from={f(v.start)}
          durationInFrames={Math.max(1, f(v.end - v.start))}
          name={v.src}
        >
          <Img
            src={assetSrc(v.src, assetsBase)}
            style={{width: '100%', height: '100%', objectFit: 'cover'}}
          />
        </Sequence>
      ))}
      {/* overlay track：字幕 pill（live-text 优先，PNG 兜底） */}
      {(plan.overlay_track ?? []).map((o, i) => (
        <Sequence
          key={`o${i}`}
          from={f(o.start)}
          durationInFrames={Math.max(1, f(o.end - o.start))}
        >
          <OverlaySeg
            o={o}
            i={i}
            W={W}
            H={H}
            base={assetsBase}
            text={subTexts?.[o.src]}
          />
        </Sequence>
      ))}
      {/* audio track：每条 mp3 延迟到 at，自然播完（amix 语义） */}
      {plan.audio_track.map((a, i) => (
        <Sequence key={`a${i}`} from={f(a.at)}>
          <Audio src={assetSrc(a.src, assetsBase)} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
