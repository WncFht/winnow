import React, { useLayoutEffect, useRef } from 'react';
import { TemplateConfig } from './types';
import { GeneratedContent } from '../types';
import {
    ThemeColor,
    getCardThemeColor,
    generateTitleFitScript,
    generateFitTextScript,
    generateViewportFitScript,
    getStandardTitleConfig,
} from '../utils/layout-calculator';
import { generateDownloadableHtml } from '../utils/template';
import { autoAddSpaceToHtml } from '../utils/text-spacing';

/**
 * claudeStyle palette — identical to the per-item cards so the intro
 * frame reads as the same product.
 */
const THEME_COLORS: ThemeColor[] = [
    { bg: '#f0eee6', onBg: '#4a403a', icon: '#c96442' },
    { bg: '#f0eee6', onBg: '#4a403a', icon: '#e09f3e' },
    { bg: '#f0eee6', onBg: '#4a403a', icon: '#335c67' },
    { bg: '#f0eee6', onBg: '#4a403a', icon: '#9e2a2b' }
];

interface OverviewMasonryProps {
    data: GeneratedContent;
    scale: number;
}

/**
 * OverviewMasonry — section list for the "today's overview" intro frame.
 * Same GeneratedContent contract: cards[] = sections, desc = HTML bullet
 * list (`<br>` separated or <ul>). Unlike claudeStyle's fixed-width grid,
 * cards pack into 2 balanced columns so uneven bullet counts stay tidy.
 */
const OverviewMasonry: React.FC<OverviewMasonryProps> = ({ data, scale }) => {
    const wrapperRef = useRef<HTMLDivElement>(null);
    const titleRef = useRef<HTMLHeadingElement>(null);
    const CARD_TITLE_MIN_FONT_SIZE = 24;
    const titleConfig = getStandardTitleConfig(data.cards.length);

    useLayoutEffect(() => {
        if (typeof window === 'undefined') return;
        if (!wrapperRef.current || !titleRef.current) return;
        const wrapper = wrapperRef.current;
        const title = titleRef.current;

        const fitTitle = () => {
            let size = titleConfig.initialFontSize;
            title.style.fontSize = size + 'px';
            let guard = 0;
            while (title.scrollWidth > 1700 && size > titleConfig.minFontSize && guard < 100) {
                size -= 1;
                title.style.fontSize = size + 'px';
                guard++;
            }
        };
        fitTitle();

        const fitCardTitles = () => {
            const titles = wrapper.querySelectorAll('.card-title') as NodeListOf<HTMLElement>;
            titles.forEach(el => {
                el.style.fontSize = '';
                const style = window.getComputedStyle(el);
                const baseFontSize = parseFloat(style.fontSize);
                if (!baseFontSize) return;
                let fontSize = baseFontSize;
                const minFontSize = Math.max(CARD_TITLE_MIN_FONT_SIZE, Math.floor(baseFontSize * 0.7));
                let guard = 0;
                while (el.scrollWidth > el.clientWidth && fontSize > minFontSize && guard < 50) {
                    fontSize--;
                    el.style.fontSize = fontSize + 'px';
                    guard++;
                }
            });
        };
        fitCardTitles();

        const fitViewport = () => {
            const maxH = 1040;
            const contentH = wrapper.scrollHeight;
            if (contentH > maxH) {
                const nextScale = Math.max(0.6, maxH / contentH);
                wrapper.style.transform = `scale(${nextScale})`;
                return;
            }
            wrapper.style.transform = '';
        };
        const timer = window.setTimeout(fitViewport, 50);
        const settleTimer = window.setTimeout(() => {
            fitCardTitles();
            fitViewport();
        }, 220);
        return () => {
            window.clearTimeout(timer);
            window.clearTimeout(settleTimer);
        };
    }, [data, titleConfig]);

    const ssrScript = `
      ${generateTitleFitScript(titleConfig)}
      ${generateFitTextScript('.card-title', CARD_TITLE_MIN_FONT_SIZE)}
      ${generateViewportFitScript()}
    `;

    return (
        <div style={{ width: 1920, height: 1080, transform: `scale(${scale})`, transformOrigin: 'top left', overflow: 'hidden' }}>
            <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,300,0,0&display=swap" rel="stylesheet" />
            <style>{`
            @font-face {
                font-family: 'CustomPreviewFont';
                src: url('/assets/htmlFont.ttf') format('truetype');
            }
            .main-container {
                font-family: system-ui, -apple-system, sans-serif;
                background-color: #fbf9f6;
                color: #4a403a;
            }
            .warm-title {
                font-weight: 700;
                color: #c96442;
                line-height: 1.2;
                white-space: nowrap;
                text-shadow: 2px 2px 0px rgba(201, 100, 66, 0.1);
            }
            .material-symbols-rounded {
                font-family: 'Material Symbols Rounded' !important;
                font-weight: 300 !important;
                font-style: normal;
                display: inline-block;
                line-height: 1;
                text-transform: none;
                letter-spacing: normal;
                white-space: nowrap;
                direction: ltr;
                font-variation-settings: 'FILL' 0, 'wght' 300, 'GRAD' 0, 'opsz' 24 !important;
            }
            .masonry-board { column-count: 2; column-gap: 26px; }
            .masonry-card {
                break-inside: avoid;
                margin-bottom: 26px;
                padding: 26px 34px 28px;
                background-color: #ffffff;
                border-radius: 30px;
                box-shadow: 0 10px 30px -10px rgba(74, 64, 58, 0.1);
                border: 1px solid rgb(218, 216, 212);
            }
            .js-desc strong { font-weight: 700; }
            .js-desc code {
                background-color: rgb(240, 239, 235) !important;
                color: rgb(92, 22, 22) !important;
                border: 0.5px solid #d1cfcc !important;
                border-radius: 8px !important;
                padding: 0.1em 0.3em;
                font-family: system-ui, -apple-system, sans-serif;
                font-size: 0.9em;
            }
            .content-scale { transform-origin: center center; }
        `}</style>

            <div className="main-container relative box-border w-full h-full overflow-hidden flex flex-col items-center justify-center">
                <div
                    ref={wrapperRef}
                    className="content-wrapper w-full flex flex-col items-center px-24 box-border content-scale z-10"
                    style={{ gap: '40px' }}
                >
                    <div className="title-zone flex-none flex items-center justify-center w-full">
                        <h1
                            ref={titleRef}
                            className="text-center warm-title main-title"
                            style={{ fontSize: `${titleConfig.initialFontSize}px` }}
                        >
                            {data.mainTitle}
                        </h1>
                    </div>

                    <div className="card-zone flex-none w-full">
                        <div className="masonry-board">
                            {data.cards.map((card, idx) => {
                                const theme = getCardThemeColor(THEME_COLORS, idx);
                                return (
                                    <div key={idx} className="masonry-card">
                                        <div className="flex items-center gap-3 mb-3">
                                            <span
                                                className="material-symbols-rounded"
                                                style={{ fontSize: '40px', color: theme.icon }}
                                            >
                                                {card.icon}
                                            </span>
                                            <h3
                                                className="font-bold leading-tight text-4xl card-title"
                                                style={{
                                                    color: theme.icon,
                                                    whiteSpace: 'nowrap',
                                                    overflow: 'hidden',
                                                    textOverflow: 'ellipsis'
                                                }}
                                            >
                                                {card.title}
                                            </h3>
                                        </div>
                                        <p
                                            className="font-medium text-2-5xl js-desc"
                                            style={{ color: '#141413', lineHeight: 1.6 }}
                                            dangerouslySetInnerHTML={{ __html: autoAddSpaceToHtml(card.desc) }}
                                        />
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                </div>
            </div>
            <script dangerouslySetInnerHTML={{ __html: ssrScript }} />
        </div>
    );
};

export const overviewMasonryTemplate: TemplateConfig = {
    id: 'overviewMasonry',
    name: '概览双栏瀑布流',
    description: '今日概览列表卡：分区卡片按双栏瀑布流排布，沿用暖调卡片配色',
    icon: 'view_agenda',
    downloadable: true,
    ssrReady: true,
    render: (data, scale) => <OverviewMasonry data={data} scale={scale} />,
    generateHtml: (data) => generateDownloadableHtml(data, 'overviewMasonry'),
};

export { OverviewMasonry };
