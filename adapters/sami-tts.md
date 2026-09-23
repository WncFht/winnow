# SAMI TTS 接口使用说明

## 定位

Sami-TTS（剪映逆向通道）的**选型调研笔记**，未接入生产 pipeline。
TTS 选型决策与对比实验见 `state/tmp/tts-bakeoff/`（edge/cv3/omnivoice/
breeze 等多路线 bakeoff，state/ 已 gitignore）。

来源：`luoluoluo22/jianying-editor-skill` 仓库中的 `scripts/universal_tts.py`。这是逆向剪映桌面客户端内部 TTS 通道得到的非官方接口，不是公开稳定 API。

## 一、接口本体

- 协议：WebSocket over TLS
- 地址：`wss://sami.bytedance.com/internal/api/v2/ws`
- 查询参数：`device_id=<dev_id>&iid=<iid>`
- 认证头：`User-Agent: JianyingPro/5.9.0.11632 (Windows 10.0.19045; app_id:3704; device_id:<dev_id>)`
- 命名空间：`TTS`
- 内置应用标识：`app_id=3704`、`appkey=IZjhUeAYwP`（硬编码在源码里）

## 二、调用流程

1. 用 `device_id + iid` 打开 WSS 连接。
2. 发送 `StartTask` 事件，`payload` 里携带 `text`、`speaker`、`audio_config`。
3. 立即发送 `FinishTask` 通知服务端输入结束。
4. 循环 `recv`：
   - 二进制帧 → 追加为音频数据。
   - 文本帧 `event == TaskFailed` → 报错。
   - 文本帧 `event == TaskFinished` → 结束。
5. 把收到的字节流原样写到 `output.ogg`（格式就是 `ogg_opus`）。

请求体关键字段：

```json
{
  "app_id": "3704",
  "appkey": "IZjhUeAYwP",
  "event": "StartTask",
  "namespace": "TTS",
  "task_id": "ai_gen_<随机 8 位 hex>",
  "message_id": "<task_id>_0",
  "payload": "{\"text\":\"...\",\"speaker\":\"zh_male_huoli\",\"audio_config\":{\"format\":\"ogg_opus\",\"sample_rate\":24000,\"bit_rate\":64000}}"
}
```

## 三、device_id / iid 从哪来

代码里写了三级回退：

1. 默认值（任何人都能跑）：
   - `device_id = 1053764930506284`
   - `iid = 2314914062247833`
2. Windows：读 `%LOCALAPPDATA%\JianyingPro\User Data\TTNet\tt_net_config.config`，正则抓 `device_id&#*(\d+)`。
3. Windows：扫 `%LOCALAPPDATA%\JianyingPro\User Data\Log\*.log`（最近 5 个文件），抓 `iid=(\d+)`。
4. macOS：`~/Library/Containers/com.lemon.lvpro/.../User Data` 下同样的两个位置。

也就是说：装了剪映就用本机真实设备号，没装就用公共默认值——默认值挂了才会受影响。

## 四、音色（speaker）

完整列表在 `data/tts_speakers.csv`，约 166 个。常用：

- `zh_male_huoli`（默认男声）
- `zh_female_xiaopengyou`（小孩）
- `zh_male_xionger_stream_gpu`（熊二）
- `zh_female_inspirational`（温柔姐姐）
- `BV025_streaming`、`BV408_streaming`、`BV411_streaming`、`BV701_streaming` 等 BV 系列解说/配音向音色

## 五、Python 直接调用

```python
import asyncio
from scripts.universal_tts import generate_voice

asyncio.run(generate_voice(
    "测试智能配音系统集成成功。",
    "test.ogg",
    speaker="zh_male_huoli",
))
```

带元数据（能拿到实际用的后端）：

```python
path, backend = await generate_voice_with_meta(
    "文案", "out.ogg",
    speaker="zh_male_huoli",
    backend=None,          # None=自动 / "sami"=只走 SAMI / "edge"=只走 edge-tts
    allow_fallback=True,   # SAMI 失败时是否回退 edge-tts
    sami_retries=2,
)
```

## 六、回退策略

- SAMI 失败最多重试 `sami_retries` 次（默认 2，间隔 0.35s）。
- 仍然失败 → 回退到 `edge-tts`：
  - speaker 含 `male` → `zh-CN-YunxiNeural`
  - 否则 → `zh-CN-XiaoxiaoNeural`
  - 输出文件强制改为 `*.mp3`（即便你给了 `.ogg`）
- `backend="sami"` 或 `allow_fallback=False` → SAMI 失败就直接返回 `None`。

## 七、剪映草稿集成

`jianying-editor-skill` 已经把 TTS 包成项目方法，不用直接碰 WSS：

```python
# 只生成配音并放入时间轴
seg = project.add_tts_intelligent(
    "你好，我是全自动剪辑助手。",
    speaker="zh_male_huoli",
    start_time="0s",
    track_name="AudioTrack",
)

# 配音 + 同步字幕（按标点切句 → 逐句 TTS → 按音频时长排字幕）
project.add_narrated_subtitles(
    "欢迎来到 AI 剪辑教程。今天我们演示自动旁白与字幕对齐。",
    speaker="zh_female_xiaopengyou",
    start_time="1s",
    track_name="Subtitles",
)
```

## 八、环境变量

- `JY_TTS_INSECURE_SSL=1` — 关掉 TLS 校验（调试用，默认关）。
- `JY_LOG_LEVEL` / `JY_CLOUD_MAX_MB` / `JY_PROJECTS_ROOT` — 通用配置，跟 SAMI 无关。

## 九、风险与边界

- 这是字节内部接口，无 SLA、随时可能改字段、限流、封 `appkey` 或设备号。
- 音频方向是 **文字 → 语音**。它不能做语音识别（ASR）。要 ASR 自己接 FunASR / faster-whisper / 云端服务。
- 公共默认 `device_id/iid` 被多人共用，做正式产品时应绑定自有剪映客户端凭据并加缓存。
- `ogg_opus` 24 kHz/64 kbps，后端播放器/剪辑工具需要支持 opus。

## 十、参考文件

- 源码：`scripts/universal_tts.py`（本地副本 `state/tmp/sami-test/universal_tts.py`——state/ 已 gitignore，不入库）
- 音色表：`data/tts_speakers.csv`（本地副本 `state/tmp/sami-test/tts_speakers.csv`）
- 上层封装：`rules/audio-voice.md`、`scripts/core/text_ops.py`
- 项目地址：[luoluoluo22/jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill)
