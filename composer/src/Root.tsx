import React from 'react';
import {Composition} from 'remotion';
import {FullDaily} from './FullDaily';
import {calcPlanMetadata, FullDailyProps} from './plan';

// duration/fps/size 在 calculateMetadata 里由 70_render_plan.json 决定；
// 这里的值只是占位（会被元数据覆盖）。
export const Root: React.FC = () => {
  return (
    <Composition
      id="FullDaily"
      component={FullDaily}
      durationInFrames={300}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={{} as FullDailyProps}
      calculateMetadata={calcPlanMetadata}
    />
  );
};
