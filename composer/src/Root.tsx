import React from 'react';
import {Composition} from 'remotion';
import {FullDaily, TOTAL_FRAMES, FPS} from './FullDaily';

export const Root: React.FC = () => {
  return (
    <Composition
      id="FullDaily"
      component={FullDaily}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={1920}
      height={1080}
    />
  );
};
