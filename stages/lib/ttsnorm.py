"""stages/lib/ttsnorm.py — 口播文本规范化（PLAN §7.5）。

确定性处理（无 LLM、无网络），顺序固定：

  1. 剥标记：`<strong>/<code>/<em>…` → markdown 壳 → 删 `**`/`` ` ``；剥
     URL/域名/email/@handle/emoji——口播文本必须纯文本（voice_seg.text 契约）。
  2. 数字符号 → 中文读法：百分数 30%→百分之三十、区间 5-7%→百分之五到七、
     小数/版本号 4.2→四点二、年份 2026年→二零二六年（4 位+年/财年逐位读）、
     量级后缀 29B→二百九十亿 / 1M→一百万 / 128K→十二万八千、整数走数值读法
     （1500万→一千五百万）；量词前的 "2"→两（2个→两个）；负号→负。
     ——NOTE.md 实测 edge-tts 服务端也能读对阿拉伯数字，但落档契约要求
     "数字已转可读形式"，且本地引擎（IndexTTS 后备）数字规范化差，统一展开。
  3. "字母-数字"连字符拆掉（回归锁死：GPT-6→GPT6→GPT六——voice-script-gen
     实测 edge-tts 把这种 "-" 读成"杠"）；token 内含数字的复合连字符拆空格
     （Xing4.0-29B-A4B→Xing4.0 29B A4B、MiniMax-M3.1→MiniMax M3.1、
     cua-s1-forms→cua s1 forms）；纯数字区间 N-M→N到M；纯字母连字符保留
     （Thinker-Talker）。
  4. 发音词典替换：tts_dict.yaml 的 {写法: 读法}，长键优先、大小写
     不敏感、ASCII 键按字母数字边界匹配（API 命中"调API"不吃 GraphAPI）；
     数字结尾的键额外挡 "."（"Qwen3"不吃 "Qwen3.5"）。
  5. 标点 → 停顿提示：;:/引号/省略号/破折号→逗号、.→句号、括号剥壳、
     其余不可读符号剥掉；结尾无标点补 "。"。

非幂等（"GPT六"再过一遍会命中 GPT→"G P T"）——一次转换用。

API：
    load_dict(path) -> dict          # YAML 缺/坏 → {}
    normalize(text, pron=None) -> str
    LETTER_DIGIT_HYPHEN              # 导出校验用：规范输出中不得再出现

Smoke: uv run stages/lib/ttsnorm.py
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

import yaml

# digest._tts_text_fix 的窄规则（仅字母-数字相邻）：保留导出供校验用。
LETTER_DIGIT_HYPHEN = re.compile(r"([A-Za-z])-(\d)|(\d)-([A-Za-z])")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DICT = REPO_ROOT / "tts_dict.yaml"

_DIGITS = "零一二三四五六七八九"
_SEC_UNIT = ["", "十", "百", "千"]
_GRP_UNIT = ["", "万", "亿", "万亿", "亿亿"]

# ---------------------------------------------------------------- markup ----

_TAG_OPEN = re.compile(r"</?(?:strong|em|b|i|s|u)>")
_TAG_CODE = re.compile(r"</?code>")
_TAG_ANY = re.compile(r"<[a-zA-Z/!][^>]*>")
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­￼]")
_URL = re.compile(r"https?://\S+|www\.[\w.-]+\S*|[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"
                  r"|@[A-Za-z0-9_]+")
_DOMAIN = re.compile(r"[\w-]+\.(?:com|net|org|io|ai|cn|dev|app|co|me|tv|info)"
                     r"(?:/[^\s，。]*)?", re.I)
_EMOJI = re.compile(r"[🀄-🫿\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")

# --------------------------------------------------------------- numbers ----


def _sec_zh(s: int, top: bool) -> str:
    """0<s<10000 → 中文小节；top=整数最高节（10-19 读'十X'，否则'一十X'）。"""
    if 10 <= s < 20:
        return ("十" if top else "一十") + (_DIGITS[s % 10] if s % 10 else "")
    out, zero = "", False
    digs = [int(c) for c in str(s)]
    L = len(digs)
    for i, x in enumerate(digs):
        pos = L - 1 - i
        if x == 0:
            zero = True
        else:
            if zero:
                out += "零"
                zero = False
            out += _DIGITS[x] + _SEC_UNIT[pos]
    return out


def int_zh(n: int) -> str:
    """非负整数 → 中文数值读法（1500→一千五百、10020→一万零二十）。"""
    if n == 0:
        return "零"
    secs = []
    while n:
        secs.append(n % 10000)
        n //= 10000
    out, need_zero = "", False
    for i in range(len(secs) - 1, -1, -1):
        s = secs[i]
        if s == 0:
            if out:
                need_zero = True
            continue
        if out and (need_zero or s < 1000):
            out += "零"
        out += _sec_zh(s, top=(i == len(secs) - 1)) + _GRP_UNIT[i]
        need_zero = False
    return out


def _digit_str(s: str) -> str:
    return "".join(_DIGITS[int(c)] for c in s)


_YEAR = re.compile(r"(?<![\d.])(\d{4})\s*(?=(?:财)?年)")
_DEC = re.compile(r"\d+(?:\.\d+)+")
_KMB = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)([KMB])(?![A-Za-z])")
_TWO_MEASURE = re.compile(r"(?<![\d.])2\s*(?=[个只条款种家位名项类轮倍句层片颗"
                          r"篇段次所间周年点万亿])")
_INT = re.compile(r"\d+")
_KMB_UNIT = {"K": 10**3, "M": 10**6, "B": 10**9}


def _dec_zh(m: re.Match) -> str:
    parts = m.group(0).split(".")
    return int_zh(int(parts[0])) + "".join("点" + _digit_str(p)
                                           for p in parts[1:])


def _kmb_zh(m: re.Match) -> str:
    n = int(float(m.group(1)) * _KMB_UNIT[m.group(2)])
    return int_zh(n)


# --------------------------------------------------------------- hyphens ----

_ALNUM_RUN = re.compile(r"[0-9A-Za-z.]")
_HYPHEN = re.compile(r"(?<=[0-9A-Za-z])-(?=[0-9A-Za-z])")
_PURE_NUM = re.compile(r"[0-9.]+")
_PURE_INT = re.compile(r"[0-9]+")
_KEEP_HYPHEN = "\x01"          # 保留下来的字母-字母连字符占位，躲过标点清洗


def _hyphen_sub(s: str) -> str:
    """逐个判定 alnum 间连字符：纯数字区间→到、纯字母复合→保留、
    字母|纯数字（GPT-6）→直接删、其余含数字的混合 token→空格。"""
    matches = list(_HYPHEN.finditer(s))
    if not matches:
        return s
    reps = []
    for m in matches:
        l = m.start()
        while l > 0 and _ALNUM_RUN.match(s[l - 1]):
            l -= 1
        r = m.end()
        while r < len(s) and _ALNUM_RUN.match(s[r]):
            r += 1
        lrun, rrun = s[l:m.start()], s[m.end():r]
        lnum = bool(_PURE_NUM.fullmatch(lrun))
        rnum = bool(_PURE_NUM.fullmatch(rrun))
        lalpha = lrun.isalpha()
        ralpha = rrun.isalpha()
        if lnum and rnum:
            reps.append("到")                    # 5-7、2024-2025
        elif lalpha and ralpha:
            reps.append(_KEEP_HYPHEN)            # Thinker-Talker
        elif lalpha and _PURE_INT.fullmatch(rrun):
            reps.append("")                      # GPT-6 → GPT6（回归锁死）
        else:
            reps.append(" ")                     # 4.0-29B、M3.1 类版本串
    out, last = [], 0
    for m, rep in zip(matches, reps):
        out.append(s[last:m.start()])
        out.append(rep)
        last = m.end()
    out.append(s[last:])
    return "".join(out)


# ------------------------------------------------------------------ dict ----


def load_dict(path=None) -> dict:
    """读 YAML 发音词典 {写法: 读法}；文件缺/顶层非映射 → {}（不阻塞合成）。"""
    p = Path(path) if path else DEFAULT_DICT
    try:
        if not p.is_file():
            return {}
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v and k != v:
            out[k] = v
    return out


def _edge(term: str) -> str:
    """词边界 guards：ASCII 字母数字边缘才加；数字结尾的键连 '.' 也挡
    （'Qwen3' 不吃 'Qwen3.5'、'FP16' 不吃 'FP16.5'）。"""
    esc = re.escape(term)
    left = r"(?<![0-9A-Za-z])" if term[0].isascii() and term[0].isalnum() else ""
    if term[-1].isascii() and term[-1].isdigit():
        right = r"(?![0-9A-Za-z.])"
    elif term[-1].isascii() and term[-1].isalnum():
        right = r"(?![0-9A-Za-z])"
    else:
        right = ""
    return left + esc + right


def _apply_dict(s: str, pron: dict) -> str:
    """长键优先、大小写不敏感；ASCII 键带边界，其余键纯子串替换。"""
    for k in sorted(pron, key=len, reverse=True):
        v = pron[k]
        if k[0].isascii() and k[-1].isascii():
            s = re.sub(_edge(k), lambda _m, vv=v: vv, s, flags=re.I)
        else:
            s = s.replace(k, v)
    return s


# --------------------------------------------------------- punct → pause ----

_PUNCT = str.maketrans({
    ",": "，", ";": "，", ":": "，",
    '"': "，", "“": "，", "”": "，", "„": "，", "‟": "，",
    "「": "，", "」": "，", "『": "，", "』": "，",
    "!": "！", "¡": "！", "?": "？", "¿": "？",
    "—": "，", "–": "，", "―": "，", "‐": "，", "-": "，",
    "→": "，", "←": "，", "↑": "，", "↓": "，", "↔": "，",
    "•": "、", "/": "、", "／": "、",
    "(": "", ")": "", "[": "", "]": "", "{": "", "}": "",
    "（": "", "）": "", "【": "", "】": "", "《": "", "》": "",
    "〈": "", "〉": "", "〔": "", "〕": "", "［": "", "］": "",
    "·": "", "・": "", "‥": "",
    "*": " ", "_": " ", "`": " ", "#": " ", "~": " ", "^": " ",
    "|": " ", "\\": " ", "<": " ", ">": " ", "=": " ", "+": " ",
    "@": " ", "$": " ", "€": " ", "£": " ", "&": " ",
    "℃": "摄氏度", "℉": "华氏度", "°": "度",
    "　": " ", "\t": " ", "\n": " ", "\r": " ",
})
_QUOTE1 = re.compile(r"(?<![A-Za-z])'|'(?![A-Za-z])")   # 引号→逗号，单词内 ' 保留
_CJK = r"㐀-鿿豈-﫿"


# ------------------------------------------------------------------ main ----


def normalize(text: str, pron: dict | None = None) -> str:
    """一句口播原文 → 喂给 TTS 的规范化文本。对任意输入不抛异常。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _ZERO_WIDTH.sub("", s)
    # 1) 剥标记 / URL / emoji
    s = _TAG_OPEN.sub("**", s)
    s = _TAG_CODE.sub("`", s)
    s = _TAG_ANY.sub("", s)
    s = s.replace("**", "").replace("`", "")
    s = _URL.sub(" ", s)
    s = _DOMAIN.sub(" ", s)
    s = _EMOJI.sub(" ", s)
    # 2) 连字符预处理：空白化齐 → 一元负号 → 百分数/区间 → 复合连字符
    s = re.sub(r"(?<=[0-9A-Za-z])\s*-\s*(?=[0-9A-Za-z])", "-", s)
    s = re.sub(r"(?<![0-9A-Za-z])-(?=\d)", "负", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*[-~—–]\s*(\d+(?:\.\d+)?)\s*%",
               r"百分之\1到\2", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"百分之\1", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*~\s*(\d+(?:\.\d+)?)", r"\1到\2", s)
    s = re.sub(r"~(?=\d)", "约", s)
    # 含连字符的整词键（SWE-bench/RISC-V/用户显式写法）优先于连字符规则
    if pron:
        s = _apply_dict(s, {k: v for k, v in pron.items() if "-" in k})
    s = _hyphen_sub(s)
    # 3) 数字前的符号/单位
    s = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", s)      # 千分位逗号
    s = s.replace("≈", "约").replace("&", "和")
    s = re.sub(r"(?<=\d)\s*[×x]\s*(?=\d)", "乘", s)
    s = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", "加", s)
    s = re.sub(r"[$](?=\s*\d)", "美元", s)
    s = re.sub(r"€(?=\s*\d)", "欧元", s)
    s = re.sub(r"[¥￥]\s*(\d)", r"\1元", s)
    s = s.replace("°C", "摄氏度").replace("°F", "华氏度")
    # 4) 发音词典（先于此后的数字展开——词典值本身不含阿拉伯数字）
    if pron:
        s = _apply_dict(s, pron)
    # 5) 数字 → 中文读法：年份逐位 → 小数 → 量级后缀 → 两 → 整数
    s = _YEAR.sub(lambda m: _digit_str(m.group(1)), s)
    s = _DEC.sub(_dec_zh, s)
    s = _KMB.sub(_kmb_zh, s)
    s = _TWO_MEASURE.sub("两", s)
    s = _INT.sub(lambda m: int_zh(int(m.group(0))), s)
    # 6) 标点 → 停顿提示
    s = re.sub(r"\.{2,}", "，", s)                      # 省略号/长点
    s = _QUOTE1.sub("，", s)
    s = s.translate(_PUNCT)
    s = s.replace(".", "。")                            # 残余句点
    s = s.replace(_KEEP_HYPHEN, "-")                    # 还原字母-字母连字符
    # 7) 折叠：逗号去重、句读不留逗号、标点前不留空格、CJK 间空格去掉
    s = re.sub(r"，{2,}", "，", s)
    s = re.sub(r"([。！？])，", r"\1", s)
    s = re.sub(r"，(?=[。！？])", "", s)
    s = re.sub(r" (?=[，。！？、])", "", s)
    s = re.sub(rf"(?<=[{_CJK}]) (?=[{_CJK}])", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ，、-")
    if s and s[-1] not in "。！？":
        s += "。"
    return s


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":  # uv run stages/lib/ttsnorm.py
    pron = load_dict()
    print(f"tts_dict: {len(pron)} entries <- {DEFAULT_DICT}")
    assert len(pron) >= 50, "tts_dict.yaml 种子词条不足"

    cases = [
        # (输入, 期望输出) —— 任务回归：连字符拆 + 数字读法 + 词典
        ("GPT-6", "GPT六。"),
        ("API 2.5 版", "A P I 二点五版。"),
        ("份额30%", "份额百分之三十。"),
        ("Claude Code 2.0", "克劳德 Code 二点零。"),
        ("OpenAI 今天发布了 GPT-5，API 定价下降了百分之二十。",
         "Open A I 今天发布了 GPT五，A P I 定价下降了百分之二十。"),
        ("阿里发布Qwen3-Max，对标MiniMax-M3.1。",
         "阿里发布千问三 Max，对标Mini Max M三点一。"),
        ("Xing4.0-29B-A4B 开源", "Xing四点零二百九十亿 A四B 开源。"),
        ("份额从5-7%升到9%", "份额从百分之五到七升到百分之九。"),
        ("2026年9月22日发布，支持1500万用户。",
         "二零二六年九月二十二日发布，支持一千五百万用户。"),
        ("上下文1M tokens，可训练参数仅70.6万。",
         "上下文一百万 tokens，可训练参数仅七十点六万。"),
        ("在 SWE-bench 上达到 SOTA", "在 S W E bench 上达到 S O T A。"),
        ("英伟达 CUDA 生态与华为 CANN 竞争加剧",
         "英伟达库达生态与华为 C A N N 竞争加剧。"),
        ("Thinker-Talker 双模块架构", "Thinker-Talker 双模块架构。"),
        ("下降了-3%，回收价 $5", "下降了负百分之三，回收价美元五。"),
        ("下一代模型 2 个版本、2 款芯片", "下一代模型两个版本、两款芯片。"),
        ("阶跃星辰 **Step 5** Preview 现身 `leaderboard`",
         "阶跃星辰 Step 五 Preview 现身 leaderboard。"),
        ("2024-2025 财年营收 424,911 美元",
         "二千零二十四到二零二五财年营收四十二万四千九百一十一美元。"),
        ("", ""),
        ("DeepSeek V3.2 同日开源", "Deep Seek V三点二同日开源。"),
        ("GitHub Copilot 免费", "Git Hub Co Pilot 免费。"),
    ]
    bad = 0
    for src, want in cases:
        got = normalize(src, pron)
        mark = "PASS" if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"[{mark}] {src!r}\n       -> {got!r}" +
              ("" if got == want else f"  (want {want!r})"))
    joined = " ".join(normalize(c, pron) for c, _ in cases)
    assert not LETTER_DIGIT_HYPHEN.search(joined), "残留字母-数字连字符"
    # 幂等性软检查：二次规范化不得引入连字符回退
    twice = normalize(normalize("GPT-6 份额30%", pron), pron)
    assert "-" not in twice and "杠" not in twice
    sys.exit(1 if bad else 0)
