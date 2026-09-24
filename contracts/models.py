#!/usr/bin/env python3
"""artifact-contracts — 全链路阶段产物契约（pydantic 定义 → JSON Schema 2020-12 发射）。

设计原则（贯穿所有阶段）：
1. 每个规范产物首字段 `"schema": "<name>/<major>"`；JSONL 每行自描述（拼接/追加不丢语义）。
2. 时间一律 float 秒（毫秒精度，对齐 ffprobe/ffmpeg filter 原生单位）；
   日期 RFC 3339；期号 `YYYY-MM-DD`。SRT/ffconcat/adelay 的方言只在投影层格式化。
3. 身份两级：`item_key` = sha256(url_canon)[:16] 机械身份（采集→去重全程可用）；
   `id` = 编辑期 slug `^[a-z0-9-]{2,24}$`（triage 时人工/LLM 指定，下游一切文件名以此 join）。
4. LLM 阶段产物带 `prov`（model/prompt/input_sha/decided_at）——换模型重跑可回放。
5. 文件类产物清单（manifest）带 sha256 + 尺寸/时长——下游不再 ffprobe 重测。
6. 单一事实源：MD/SRT/VTT/ffconcat/render_plan 全部是投影（generated-header 注释，不手改）。

阶段链（文件名建议带阶段号前缀，ls 即 DAG）：
  10_raw_items.jsonl   采集层：JSON Feed 1.1 item 形状 + `_` 前缀管道扩展字段
  11_raw_manifest.json 批次清单：窗口/源统计/抓取错误（JSONL 无法放 envelope，故单配）
  20_filtered.jsonl    过滤判定（item_key 引用 raw）
  30_summaries.jsonl   逐条概要 + entities + facts
  35_dedup.jsonl       去重判定（history.db 的落档投影；cluster_id 引用 dedup-history schema）
  40_selected.json     人工勾选：有序 kept[]（顺序=正片顺序）+ slug/section 指定
  50_issue.json        issue/v1 规范文档（见 experiments/issue-contract，本实验直接复用）
  60_voice_script.jsonl 拍平口播句序列 = TTS 输入契约
  61_audio/NNN_item_si.* + 61_audio_manifest.json
  62_timeline.json     timeline/1：items+segs+overlays（shot 窗口已编译为绝对时间）
  62_episode.srt/.vtt, 70_cards.ffconcat   投影
  63_cards.json        GeneratedContent+id（上游契约）+ envelope
  63_cards_manifest.json / 64_frames_manifest.json
  70_render_plan.json  完全解析后的合成计划（composer 唯一输入）
  80_build_manifest.json  out.mp4 的构建清单（输入哈希 + 编码参数 + 输出度量）
  00_meta.json         run 级 BOM：各阶段产物登记 + 状态
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

OUT = Path(__file__).resolve().parent
SCHEMAS = OUT / "schemas"

Slug = Field(pattern=r"^[a-z0-9-]{2,24}$")
ItemKey = Field(pattern=r"^[0-9a-f]{16}$", description="sha256(url_canon)[:16]")
RFC3339 = Field(description="RFC 3339 timestamp, e.g. 2026-09-21T08:30:00+08:00")
Episode = Field(pattern=r"^(\d{4}-\d{2}-\d{2}|_.+)$",
                description="正片 YYYY-MM-DD；_ 前缀 = 沙盒 run dir（episode 常回退 run_dir.name）")


# ---------- cross-cutting ----------

class Provenance(BaseModel):
    """LLM 阶段溯源：换 prompt/模型后可定点重放。"""
    model_config = ConfigDict(extra="forbid")
    model: str = Field(description="e.g. swe-2-max / gpt-6-astra / gemini-3.8-flash")
    prompt: str = Field(description="prompt 版本标识 e.g. filter-v3 (git path 或 hash)")
    input_sha: str = Field(description="本行输入(item_key 或拼接文本)的 sha256[:16]，重放核对")
    decided_at: str = RFC3339


# ---------- stage 1: raw_items.jsonl ----------

class RawSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="源显示名，如 '机器之心'/'OpenAI Blog'")
    feed_url: str = Field(description="RSS/API endpoint 或抓取页 URL")
    kind: Literal["rss", "atom", "api", "scrape", "manual"]
    item_guid: Optional[str] = Field(default=None, description="源站原生 guid/id")


class RawFetch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: int = Field(description="HTTP status；manual=0")
    via: Literal["direct", "mirror", "cache", "manual"] = "direct"
    reachable: bool = True
    etag: Optional[str] = None
    content_sha256: Optional[str] = Field(default=None, description="原始响应体 hash[:16]")


class RawItem(BaseModel):
    """采集层 item = JSON Feed 1.1 字段 + `_` 前缀管道扩展。

    对 JSON Feed 1.1 的借用是有意的：id/url/title/content_text/date_published/
    authors/tags/image/attachments 均为标准字段，任何 JSON Feed reader 可直读；
    管道私有字段全部 `_` 前缀（spec 要求 reader 忽略未知键）。
    """
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["raw_item/1"] = Field(default="raw_item/1", alias="schema")
    item_key: str = ItemKey
    id: str = Field(description="= item_key（JSON Feed 要求唯一 id）")
    url: str = Field(description="原始链接（未规范化）")
    url_canon: str = Field(description="规范化 URL：去 utm_*/fbclid/www./尾斜杠，https 归一")
    title: str
    content_text: Optional[str] = Field(default=None, description="feed summary/正文纯文本")
    content_html: Optional[str] = Field(
        default=None,
        description="DEPRECATED — not emitted since the pool refactor; "
                    "full HTML lives via _raw_ref -> data/raw_cache")
    date_published: Optional[str] = Field(default=None, description="源站发布时间，可为空")
    date_fetched: str = RFC3339
    language: Optional[str] = Field(default=None, description="BCP-47-ish: zh/en/…")
    tags: list[str] = Field(default_factory=list)
    image: Optional[str] = None
    source_: RawSource = Field(alias="_source")
    fetch: RawFetch = Field(alias="_fetch")
    raw_ref: Optional[str] = Field(default=None, alias="_raw_ref",
                                   description="落盘原文相对路径（html/json dump），审计回放用")


class RawManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["raw_manifest/1"] = Field(default="raw_manifest/1", alias="schema")
    episode: str = Episode
    window: dict = Field(description="{from,to} RFC3339 抓取窗口")
    file: str = Field(description="本批 JSONL 文件名")
    n_items: int
    sources: list[dict] = Field(
        description="[{name,method,tier,status,items_new,items_fresh,items_total,"
                    "last_error,latency_ms,via,endpoint}] 每源统计")
    produced_at: str = RFC3339
    stats: Optional[dict] = Field(
        default=None,
        description="产物体积簿记：jsonl_bytes/max_line_bytes + content_text "
                    "{cap_chars,n_present,n_truncated,max_chars}（collect 填，可选）")


# ---------- stage 2: filtered.jsonl ----------

class FilterVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["filter_verdict/1"] = Field(default="filter_verdict/1", alias="schema")
    item_key: str = ItemKey
    verdict: Literal["keep", "drop", "review"] = Field(
        description="keep=直进概要; review=灰区留给人工/二级模型")
    ai_relevance: float = Field(ge=0, le=1, description="AI 相关度")
    news_value: Optional[float] = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    prov: Provenance


# ---------- stage 3: summaries.jsonl ----------

class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["summary/1"] = Field(default="summary/1", alias="schema")
    item_key: str = ItemKey
    title_zh: str = Field(description="中文工作标题（可不同于原题）")
    summary: str = Field(description="1-2 句概要，供人工勾选/去重判读")
    entities: list[str] = Field(default_factory=list, description="模型/公司/人名专名")
    facts: list[str] = Field(default_factory=list,
                             description="精确数字/版本/日期 claim 片段，下游数字白名单种子")
    section_guess: Optional[str] = None
    prov: Provenance


# ---------- stage 3.5: dedup.jsonl ----------

class DedupVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["dedup_verdict/1"] = Field(default="dedup_verdict/1", alias="schema")
    item_key: str = ItemKey
    verdict: Literal["fresh", "suppressed", "reissue", "gray"]
    cluster_id: Optional[int] = Field(default=None, description="history.db clusters.cluster_id")
    match_cos: Optional[float] = Field(default=None, ge=-1, le=1)
    judge: Optional[dict] = Field(default=None, description="LLM judge 判词原文")


# ---------- stage 4: selected.json ----------

class KeptItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_key: str = ItemKey
    id: str = Slug
    section: str = Field(description="编辑指定的分区 slug（进入 issue.sections）")
    note: Optional[str] = Field(default=None, description="人工批注（可空）")


class Selected(BaseModel):
    """人工勾选结果。kept[] 数组顺序 = 正片顺序；id 是此后一切 join 键。
    kept 可为空——§11 零条目停刊（no_items degraded）路径写 kept=[]。"""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["selected/1"] = Field(default="selected/1", alias="schema")
    episode: str = Episode
    decided_at: str = RFC3339
    decided_by: Literal["human", "auto"] = "human"
    kept: list[KeptItem] = Field(min_length=0)
    dropped: list[dict] = Field(default_factory=list,
                                description="[{item_key,reason?}] 被人工否掉的，留痕")


# ---------- stage 6: voice_script.jsonl ----------

class VoiceSeg(BaseModel):
    """拍平的一句口播 = 一次 TTS 请求。数组序即合成序（n 渲染时派生）。"""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["voice_seg/1"] = Field(default="voice_seg/1", alias="schema")
    seg_id: str = Field(pattern=r"^\d{3,}_[a-z0-9-]+_\d+$",
                        description="NNN_item_si：与音频文件名同构（去扩展名）")
    item: str = Slug
    si: int = Field(ge=0, description="item 内句序")
    text: str = Field(description="纯文本，数字已转可读形式，无任何标记")
    text_display: Optional[str] = Field(
        default=None,
        description="ttsnorm 前的书面原文（'46分' 而非 '四十六分'）——"
                    "字幕 pill/srt/vtt 用；空 = 与 text 相同")
    role: Literal["intro", "body", "outro"] = "body"


# ---------- stage 6.5: audio_manifest.json ----------

class AudioFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seg_id: str
    file: str = Field(description="相对 run 根的路径 e.g. 61_audio/012_qwen_0.wav")
    dur: float = Field(gt=0, description="秒，ffprobe format=duration 实测")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha: str = Field(pattern=r"^[0-9a-f]{16}$",
                          description="sha256(text)[:16]——voice_script 改动→需要重合成的行一眼识别")


class AudioManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["audio_manifest/1"] = Field(default="audio_manifest/1", alias="schema")
    episode: str = Episode
    engine: str = Field(description="e.g. edge-tts 7.2.8 / F5-TTS local / Azure")
    voice: str
    rate: Optional[str] = None
    codec: str = Field(description="mp3|pcm_s16le …")
    sample_rate: int
    files: list[AudioFile]


# ---------- stage 7: timeline.json ----------

class TimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Slug
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    visual: Optional[str] = Field(
        default=None,
        description="显示绑定：此 span 用哪个 item 的帧（默认=自身）。"
                    "intro/outro 这类非内容 span 复用邻近卡片时用，"
                    "e.g. outro.visual='kimi' 尾句挂在最后一张卡上。")


class TimelineWord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class TimelineSeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=0)
    seg_id: str
    item: str = Slug
    si: int = Field(ge=0)
    file: str
    text: str
    text_display: Optional[str] = Field(
        default=None, description="书面原文（voice_seg.text_display 透传）——字幕投影用")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    dur: float = Field(gt=0, description="= end-start，冗余落档防算分歧")
    words: Optional[list[TimelineWord]] = Field(
        default=None,
        description="可选词级时间戳（edge-tts WordBoundary 事件可直接落档，"
                    "offset 换算为绝对秒）；逐词卡拉 OK 字幕预留，不填也行")


class TimelineOverlay(BaseModel):
    """shot/截图弹卡的编译结果：视频计划期的句区间 → 绝对时间窗。"""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["shot", "sticker", "lower_third"] = "shot"
    item: str = Slug
    src: str = Field(description="frames/<item>.shot.png 等")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    at_sentences: list[int] = Field(description="来源句区间（编译前的语义输入，审计用）")


class Timeline(BaseModel):
    """时间轴规范契约（timeline/1）。一切按秒排布；SRT/VTT/ffconcat/render_plan 全是投影。"""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["timeline/1"] = Field(default="timeline/1", alias="schema")
    episode: str = Episode
    time_unit: Literal["seconds"] = "seconds"
    total: float = Field(gt=0)
    lead_in: float = Field(default=0.6, ge=0)
    tail: float = Field(default=0.8, ge=0)
    gap: dict = Field(default_factory=lambda: {"sentence": 0.22, "item": 0.55},
                      description="句间/条间静默模型——重排句序后时间轴可整体重算")
    items: list[TimelineItem]
    segs: list[TimelineSeg]
    overlays: list[TimelineOverlay] = Field(default_factory=list)


# ---------- stage 8: cards.json / *_manifest.json ----------

class CardData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="2-6 字")
    desc: str = Field(description="HTML 行内：<strong>/<code>（juya-news-card 契约）")
    icon: str = Field(description="Material Symbols 名")


class CardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Slug
    mainTitle: str
    cards: list[CardData] = Field(max_length=8)


class Cards(BaseModel):
    """GeneratedContent+id 的 envelope 版（上游渲染器消费 items[] 数组本身）。"""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["cards/1"] = Field(default="cards/1", alias="schema")
    episode: str = Episode
    renderer: str = Field(description="juya-news-card@<git sha>")
    template: str = "claudeStyle"
    items: list[CardItem]


class FrameFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Slug
    kind: Literal["card", "shot", "chrome", "sub", "cover"]
    path: str
    w: Optional[int] = None
    h: Optional[int] = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    t: Optional[list[float]] = Field(default=None, min_length=2, max_length=2,
                                     description="kind=shot 时的绝对时间窗（与 timeline.overlays 一致）")


class FramesManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["frames_manifest/1"] = Field(default="frames_manifest/1", alias="schema")
    episode: str = Episode
    dir: str
    files: list[FrameFile]
    missing: list[str] = Field(default_factory=list,
                               description="声明了但没渲出来的 item.kind（合成时跳过，不炸）")


# ---------- stage 9: render_plan.json ----------

class VSeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class ASeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str
    at: float = Field(ge=0, description="绝对开始秒（adelay 值）")


class OSeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    xy: str = Field(description="ffmpeg overlay 表达式 e.g. (main_w-overlay_w)/2:930")


class RenderPlan(BaseModel):
    """完全解析的合成计划——composer 唯一输入，不含任何待推断语义。"""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["render_plan/1"] = Field(default="render_plan/1", alias="schema")
    episode: str = Episode
    fps: int = 30
    size: list[int] = Field(default=[1920, 1080], min_length=2, max_length=2)
    aspect: str = Field(default="16:9",
                        description="PLAN D4 画幅参数（当前只做 16:9），记录 config.render.aspect")
    total: float = Field(gt=0)
    video_track: list[VSeg]
    audio_track: list[ASeg]
    overlay_track: list[OSeg] = Field(default_factory=list)


# ---------- stage 10: build_manifest.json ----------

class BuildTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ffmpeg: str
    vcodec: str = "libx264"
    preset: str = "medium"
    crf: int = 19
    fps: int = 30
    acodec: str = "aac"
    abitrate: str = "192k"


class BuildManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["build/1"] = Field(default="build/1", alias="schema")
    episode: str = Episode
    built_at: str = RFC3339
    inputs: dict = Field(description="{render_plan,timeline,audio_manifest,frames_manifest}: sha256:…")
    tool: BuildTool
    output: dict = Field(description="{path,dur,bytes,sha256,width,height}")


# ---------- stage 0: meta.json (run BOM) ----------

class StageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: str
    sha256: Optional[str] = None
    status: Literal["done", "pending", "failed", "skipped"] = "done"
    produced_at: Optional[str] = RFC3339
    producer: Optional[str] = Field(default=None, description="脚本/人/模型标识")


class RunMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Literal["run_manifest/1"] = Field(default="run_manifest/1", alias="schema")
    episode: str = Episode
    created_at: str = RFC3339
    stages: dict[str, StageEntry] = Field(description="stage 名 → 产物登记；DAG 断点续跑的依据")


# ---------- emit JSON Schema ----------

MODELS = {
    "raw_item": RawItem,
    "raw_manifest": RawManifest,
    "filter_verdict": FilterVerdict,
    "summary": Summary,
    "dedup_verdict": DedupVerdict,
    "selected": Selected,
    "voice_seg": VoiceSeg,
    "audio_manifest": AudioManifest,
    "timeline": Timeline,
    "cards": Cards,
    "frames_manifest": FramesManifest,
    "render_plan": RenderPlan,
    "build_manifest": BuildManifest,
    "run_manifest": RunMeta,
}


def main():
    SCHEMAS.mkdir(exist_ok=True)
    for name, m in MODELS.items():
        s = m.model_json_schema()
        s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        s["$id"] = f"https://schemas.local/{name}/v1"
        (SCHEMAS / f"{name}.schema.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=2))
        print("emit", name)


if __name__ == "__main__":
    main()
