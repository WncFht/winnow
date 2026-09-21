import React from 'react';
import {AbsoluteFill, Audio, Img, Sequence, staticFile} from 'remotion';
import timeline from './timeline.json';
import itemsJson from './items.json';

// Faithful port of repro/compose.py semantics to a Remotion composition:
//  - each item's card PNG holds from its span start to the NEXT item's start
//  - items with a shot window swap in <id>_shot.png between those sentences
//  - every TTS seg contributes: <Audio> delayed to seg.start + subtitle pill
//    shown for [seg.start, seg.end)

export const FPS = 30;
export const TOTAL_FRAMES = Math.ceil(timeline.total * FPS);

const f = (sec: number) => Math.round(sec * FPS);

type Seg = {
  n: number;
  item: string;
  si: number;
  file: string;
  text: string;
  start: number;
  end: number;
};
type ItemSpan = {id: string; start: number; end: number};
type Item = {
  id: string;
  shot?: boolean;
  shot_sentences?: [number, number] | number[];
};

const spans: Record<string, ItemSpan> = {};
for (const s of timeline.items as ItemSpan[]) spans[s.id] = s;
const segs = timeline.segs as Seg[];
const items = itemsJson.items as Item[];

// ---- visual bounds, identical to compose.py ----
const bounds: {item: Item; S: number; E: number}[] = items.map((it, i) => ({
  item: it,
  S: spans[it.id].start,
  E: i + 1 < items.length ? spans[items[i + 1].id].start : timeline.total,
}));
bounds[0].S = 0;

const SubtitlePill: React.FC<{text: string}> = ({text}) => (
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

const CardImg: React.FC<{id: string; suffix?: string}> = ({id, suffix = ''}) => (
  <Img
    src={staticFile(`frames_v2/${id}${suffix}.png`)}
    style={{width: '100%', height: '100%', objectFit: 'cover'}}
  />
);

const ItemVisual: React.FC<{item: Item; S: number; E: number}> = ({
  item,
  S,
  E,
}) => {
  // shot window in absolute seconds (same computation as compose.py)
  let win: [number, number] | null = null;
  if (item.shot && item.shot_sentences) {
    const [a, b] = item.shot_sentences;
    const isegs = segs.filter((s) => s.item === item.id);
    if (a > 0 && a <= isegs.length && b > 0 && b <= isegs.length) {
      win = [isegs[a - 1].start, isegs[b - 1].end];
    }
  }
  if (!win) return <CardImg id={item.id} />;
  const [sS, sE] = win;
  const parts: React.ReactNode[] = [];
  if (sS - S > 0.15)
    parts.push(
      <Sequence key="a" from={0} durationInFrames={f(sS - S)}>
        <CardImg id={item.id} />
      </Sequence>,
    );
  parts.push(
    <Sequence
      key="shot"
      from={f(sS - S)}
      durationInFrames={f(Math.min(sE, E) - sS)}
    >
      <CardImg id={item.id} suffix="_shot" />
    </Sequence>,
  );
  if (E - sE > 0.15)
    parts.push(
      <Sequence
        key="b"
        from={f(sE - S)}
        durationInFrames={f(E - Math.max(sE, S))}
      >
        <CardImg id={item.id} />
      </Sequence>,
    );
  return <>{parts}</>;
};

export const FullDaily: React.FC = () => {
  return (
    <AbsoluteFill style={{background: '#000'}}>
      {/* video track: item cards (+ shot windows) */}
      {bounds.map(({item, S, E}) => (
        <Sequence
          key={item.id}
          from={f(S)}
          durationInFrames={f(E - S)}
          name={item.id}
        >
          <ItemVisual item={item} S={S} E={E} />
        </Sequence>
      ))}
      {/* subtitle track: one pill per TTS seg */}
      {segs.map((s) => (
        <Sequence
          key={`sub-${s.n}`}
          from={f(s.start)}
          durationInFrames={Math.max(1, f(s.end - s.start))}
        >
          <SubtitlePill text={s.text} />
        </Sequence>
      ))}
      {/* audio track: each mp3 delayed to seg.start, plays to natural end
          (amix semantics — unbounded Sequence, Audio stops at file end) */}
      {segs.map((s) => (
        <Sequence key={`au-${s.n}`} from={f(s.start)}>
          <Audio src={staticFile(`audio/${s.file}`)} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
