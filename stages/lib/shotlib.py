#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright==1.63.*",
#   "pyyaml>=6",
#   "pillow>=10",
# ]
# ///
"""stages/lib/shotlib.py — 来源页截图 + 品牌占位卡（PLAN.md §7.6）。

    shot(url, out_path, cfg=None) -> {"path": str|None, "kind": "shot"|"placeholder", ...}
    load_policy(path=None)        -> dict                # state/shot_policy.yaml

策略链（域名策略表 state/shot_policy.yaml 先行）：
  0. news.google.* 中转 URL 先经 googlenewsdecoder 解出出版方真链
     （可选依赖——包缺失/解码失败照原 URL 走老路；命中记 rec.resolved），
     再对真链走下述判定；
  1. host 命中 rules/cloudflare_fronted → 按 action 分派：
     placeholder 直接渲染品牌占位卡，不导航；
     x_embed（x.com/twitter.com）走 cdn.syndication.twimg.com 公开
     tweet-result JSON → 自绘品牌推文卡（不导航 x.com，绕过 403 墙）；
  2. 否则 playwright chromium 截图：ctx locale="en-US" + 启动参数
     --lang=en-US + extra_http_headers Accept-Language=en-US —— 防
     Google-Translate 弹窗烤进图（硬教训，勿回退 zh-CN）；
  3. 墙检测（HTTP≥400 / WALL_PAT 命中 title+body / 空白图 stddev<8 /
     PNG < min_shot_kb / 导航异常）→ 自动降级占位卡，永不阻塞；占位卡
     走 playwright html→png，browser 整体不可用时 PIL 兜底，仍产出
     1920×1080 PNG。

占位卡 = claudeStyle 品牌卡（#fbf9f6/#fdfbf6/#cf4f24），大字 'SOURCE: <domain>'。
截图默认 1400×900@dsf1.5 → 2100×1350 PNG（对齐 repro shotcard ≤1340×716 内嵌）。

常量层移植自 experiments/webshot-hardening/shotlib.py（2026-09 matrix 校准），
运行层蒸馏自同目录 fetch_shots_v2.py（patchright/GFW-wayback 重写路未纳入；
x-embed 以 x_embed action 重新落地 —— syndication 端点直连可达）。

cfg(dict) 键：policy / policy_path / proxy(None=env,'direct' 直连,或 URL)
  / viewport=(w,h) / scale / nav_timeout_ms / retries / headless
  / placeholder_size=(w,h)（占位卡尺寸，缺省 1920×1080）
  / min_shot_kb（真截图 PNG 下限 KB，缺省 8；非 blank 且 <400 仍过小
    按 undersized 判负走占位）。

Smoke:  uv run stages/lib/shotlib.py            # 3 URL 实测（含 x/wechat 占位）
        uv run stages/lib/shotlib.py --offline  # 只跑离线断言
"""
from __future__ import annotations

import html as htmlmod
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

# 直接运行本文件时 sys.path[0]=stages/lib，其中 http.py 会遮蔽 stdlib http
# （playwright/pillow 依赖链会 import 它）—— 摘除自身目录；被 import 时无副作用。
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = REPO_ROOT / "state" / "shot_policy.yaml"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# en-US 语境：--lang + locale + Accept-Language 三处同时钉死，
# 缺任何一处 Google-Translate infobar 都可能复活（matrix 踩过）。
ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,TranslateUI", "--disable-translate",
    "--lang=en-US", "--hide-scrollbars", "--disable-dev-shm-usage",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
    "--force-color-profile=srgb", "--mute-audio",
]

EXTRA_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}

# ------------------------------------------------- ported constants ---------
# 以下整块移植自 experiments/webshot-hardening/shotlib.py（唯二改动：
# STEALTH_JS languages 改 en-US 优先以匹配新 locale 策略；移除未用到的
# patchright/GFW_DEAD/x-embed 重写逻辑）。

WALL_PAT = re.compile(
    r"just a moment|verify you are|verifying you are|performing security verification|"
    r"checking your browser|sign in to confirm|unusual traffic|access denied|"
    r"attention required|are you a robot|enable javascript|please enable js|"
    r"403 forbidden|访问验证|安全验证|环境异常|验证您是真人|完成验证",
    re.I,
)

# 浏览器级错误页（导航"成功"但渲出错误文档：chrome-error://、代理错误页等）
# ——goto 不抛、status 可空可 200，按正文/URL 特征判负走占位兜底。
ERR_PAGE_PAT = re.compile(
    r"this (page|site|webpage) (can'?t|could ?n'?t|isn'?t|cannot) ?\w* ?"
    r"(be )?(reach|load|display|found|open)|"
    r"this page isn'?t working|reload to try again|webpage is not available|"
    r"err_(connection|ssl|http|tunnel|timed_out|name_not|address)[a-z_]*|"
    r"无法访问此网站|无法显示此页|网页无法打开|页面无法加载",
    re.I,
)


def _err_page(page, title: str, body: str) -> bool:
    try:
        if (page.url or "").startswith("chrome-error://"):
            return True
    except Exception:
        pass
    return bool(ERR_PAGE_PAT.search((title or "") + " " + (body or "")))

# Host-suffix blocklist: pure telemetry/ads — nothing needed to render the page.
BLOCK_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "google-analytics.com",
    "googletagmanager.com", "googletagservices.com", "facebook.net",
    "facebook.com/tr", "hotjar.com", "clarity.ms", "segment.io",
    "segment.com", "mixpanel.com", "amplitude.com", "sentry.io",
    "newrelic.com", "nr-data.net", "scorecardresearch.com",
    "chartbeat.com", "quantserve.com", "adsystem.amazon",
    "amazon-adsystem.com", "ads-twitter.com", "analytics.twitter.com",
    "bat.bing.com", "snapchat.com/tr", "pinimg.com/ct", "outbrain.com",
    "taboola.com", "criteo.com", "criteo.net", "adsrvr.org",
    "moatads.com", "doubleverify.com", "ipredictive.com",
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => {
  const arr = [{name:'PDF Viewer'},{name:'Chrome PDF Viewer'},{name:'Chromium PDF Viewer'},
               {name:'Microsoft Edge PDF Viewer'},{name:'WebKit built-in PDF'}];
  arr.refresh = () => {};
  return arr;
}});
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {connect(){},sendMessage(){}};
try {
  const oq = window.navigator.permissions && window.navigator.permissions.query;
  if (oq) window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : oq(p);
} catch(e) {}
"""

# CSS injected via page.screenshot(style=...) — affects only the captured frame.
FREEZE_CSS = """
*,*::before,*::after{
  animation-duration:.001s !important;
  animation-iteration-count:1 !important;
  transition:none !important;
  caret-color:transparent !important;
  scroll-behavior:auto !important;
}
::-webkit-scrollbar{display:none !important}
video{object-fit:cover !important}
"""

# Accept-button selectors for the big CMPs + generic fallbacks.
ACCEPT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#didomi-notice-agree-button",
    ".didomi-continue-without-agreeing",
    ".qc-cmp2-summary-buttons button[mode='primary']",
    "button[title='Accept All']",
    "button[title='I Accept']",
    "#truste-consent-button",
    ".trustarc-acceptall-button",
    "button.sp_choice_type_11",
    "button.sp_choice_type_ACCEPT_ALL",
    "#cmpwelcomebtnyes a",
    "button[aria-label='Accept all' i]",
    "button[aria-label*='accept all' i]",
    "button[aria-label*='同意' i]",
    "a[aria-label*='accept' i]",
]

# CMP containers / chat widgets / translate bars killed unconditionally.
KILL_SEL = """
#onetrust-consent-sdk, .onetrust-pc-dark-filter, .ot-floating-button,
#CybotCookiebotDialog, #CybotCookiebotDialogBodyUnderlay,
#didomi-host, .didomi-popup-backdrop, #didomi-popup,
.qc-cmp2-container, .qc-cmp-ui-content, #qc-cmp2-ui,
#truste-consent-track, .trustarc-banner-container, #truste-consent-content,
div[id^="sp_message_container"], iframe[id^="sp_message_iframe"],
#cmpbox, .cmpbox, #cmpwrapper,
#cc-main, .cm--bar, .cm-overlay,
#usercentrics-root, #usercentrics-cmp-ui, uc-app,
#consent-banner, #cookie-consent, .cookie-consent-banner,
#hs-eu-cookie-confirmation, .hs-cookie-consent,
#intercom-container, #intercom-frame, iframe[src*="intercom"],
#hubspot-messages-iframe-container, .drift-widget-controller-frame,
iframe[src*="drift"], iframe[src*="zendesk"], #launcher,
.goog-te-banner-frame, .skiptranslate, #google_translate_element,
#VIgAd, #onesignal-slidedown-container, .onesignal-slidedown-dialog,
div[id^="popmake-"], .pum-overlay, .fancybox-overlay, .mfp-wrap,
#dlx-popup-container, .modal-backdrop
"""

# Heuristic sweep for leftover fixed overlays + scroll restoration.
SURGERY_JS = """
() => {
  const KILL = document.querySelectorAll(`%s`);
  let removed = 0;
  KILL.forEach(e => { try { e.remove(); removed++; } catch(_){} });
  const vw = innerWidth, vh = innerHeight, hits = [];
  document.querySelectorAll('body *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 40) return;
    const cover = (r.width * r.height) / (vw * vh);
    const zi = parseInt(cs.zIndex) || 0;
    if (cover < 0.12 || zi < 99) return;
    const t = (el.innerText || '').slice(0, 600);
    const src = el.tagName === 'IFRAME' ? (el.src || '') : '';
    if (/cookie|consent|privacy|gdpr|subscribe|newsletter|sign ?up|log ?in|join|accept|agree|donate|subscribe|打开 ?app|下载 ?app|扫码|登录|注册|同意|隐私|订阅|关注|通知|免费|领|验证|真人/i.test(t) ||
        /consent|cmp|privacy|onetrust|didomi|usercentrics/i.test(src) ||
        el.tagName === 'DIALOG') {
      hits.push(el);
    }
  });
  hits.forEach(e => { e.style.setProperty('display', 'none', 'important'); removed++; });
  document.querySelectorAll('dialog[open]').forEach(d => {
    const t = (d.innerText || '');
    if (/cookie|consent|sign|log|subscribe|登录|注册|同意|订阅/i.test(t)) { try { d.close(); d.remove(); removed++; } catch(_){}}
  });
  const HTML = document.documentElement, BODY = document.body;
  [HTML, BODY].forEach(el => {
    if (!el) return;
    const cs = getComputedStyle(el);
    if (cs.overflow === 'hidden' || cs.position === 'fixed')
      el.style.setProperty('overflow', 'auto', 'important');
    el.classList.remove('modal-open','no-scroll','overflow-hidden','fixed',
      'sp-message-open','cmp-dialog-open','fancybox-active','noscroll','disable-scroll');
  });
  document.querySelectorAll('.onetrust-pc-dark-filter, .modal-backdrop, [class*="backdrop"]').forEach(e => {
    const cs = getComputedStyle(e);
    if (cs.position === 'fixed') { e.remove(); removed++; }
  });
  return removed;
}
""" % KILL_SEL


# Consent cookies pre-seeded per CMP so the banner never renders.
def consent_cookies(domain: str):
    return [
        # OneTrust: closed alert + permissive consent record
        {"name": "OptanonAlertBoxClosed", "value": "2025-01-01T00:00:00.000Z",
         "domain": domain, "path": "/"},
        {"name": "OptanonConsent",
         "value": "isGpcEnabled=0&datestamp=Mon+Sep+21+2026+08%3A00%3A00+GMT%2B0800&version=202409.1.0&browserGpcFlag=0&isIABGlobal=false&hosts=&consentId=x&interactionCount=1&landingPath=NotLandingPage&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1%2CC0005%3A1&AwaitingReconsent=false",
         "domain": domain, "path": "/"},
        # Cookiebot
        {"name": "CookieConsent",
         "value": "{stamp:'-1',necessary:true,preferences:true,statistics:true,marketing:true,method:'explicit',ver:1,utc:1758000000000,region:'cn'}",
         "domain": domain, "path": "/"},
        # consentmanager.net
        {"name": "cmpconsentx", "value": "consent-all", "domain": domain, "path": "/"},
        # generic flag cookies seen on news sites
        {"name": "notice_behavior", "value": "expressed,eu", "domain": domain, "path": "/"},
        {"name": "notice_gdpr_prefs", "value": "0,1,2:1a8b", "domain": domain, "path": "/"},
    ]


READY_JS = """
async () => {
  try { await Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 4000))]); } catch(e){}
  const imgs = Array.from(document.images).filter(i => {
    const r = i.getBoundingClientRect();
    return r.top < innerHeight * 1.5 && r.width > 32 && r.height > 32;
  });
  const pending = imgs.filter(i => !i.complete);
  await Promise.race([
    Promise.all(pending.map(i => new Promise(r => { i.onload = i.onerror = r; }))),
    new Promise(r => setTimeout(r, 5000))
  ]);
  return {imgs: imgs.length, pending: pending.length};
}
"""

# ------------------------------------------------------------- policy -------

_DEFAULT_CFG = {
    "viewport": (1400, 900),      # shot png = viewport*scale = 2100x1350
    "scale": 1.5,
    "nav_timeout_ms": 20000,
    "retries": 1,                 # 额外重试次数（墙 flake 用新 ctx 重试）
    "headless": True,
    "proxy": None,                # None=env; 'direct' 直连; 否则代理 URL
    "placeholder_size": (1920, 1080),
    "min_shot_kb": 8,             # PNG 下限；非 blank + <400 下仍过小才算 undersized
    "headful_retry": True,        # 粘性 CF 墙 → Xvfb+headful 升级一次（Turnstile 对
                                # headless 指纹判负率高，headful 常自解；无 Xvfb 自动跳过）
}


def load_policy(path=None) -> dict:
    """state/shot_policy.yaml -> {"default":..., "rules":[...],
    "cloudflare_fronted": {...}}。文件缺失/损坏时返回内置种子策略。"""
    p = Path(path) if path else DEFAULT_POLICY_PATH
    try:
        import yaml
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(doc, dict) and (doc.get("rules") or doc.get("default")):
            return doc
    except Exception:
        pass
    return {
        "default": "screenshot",
        "rules": [
            {"match": "x.com", "action": "x_embed",
             "reason": "403 botwall → syndication 推文卡"},
            {"match": "twitter.com", "action": "x_embed",
             "reason": "→x.com 403 → syndication 推文卡"},
            {"match": "reuters.com", "action": "placeholder",
             "reason": "TLS 全路重置（SNI reset）"},
            {"match": "openai.com", "action": "screenshot", "proxy": "direct",
             "reason": "clash 出口吃 chunk 403/CF 墙，直连干净"},
            {"match": "mp.weixin.qq.com", "action": "placeholder",
             "reason": "风控墙 flake"},
        ],
        "cloudflare_fronted": {"action": "placeholder",
                               "reason": "cf-chl 挑战页",
                               "domains": ["science.org", "www.science.org"]},
    }


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _match(host: str, pat: str) -> bool:
    pat = (pat or "").lower().lstrip(".")
    return bool(pat) and (host == pat or host.endswith("." + pat))


def policy_action(policy: dict, url: str):
    """-> (action, rule_desc)。rules 顺序优先，其次 cloudflare_fronted，
    最后 default。"""
    host = _host(url)
    for r in (policy or {}).get("rules") or []:
        if _match(host, r.get("match")):
            return r.get("action", "screenshot"), f"rule:{r.get('match')}"
    cf = (policy or {}).get("cloudflare_fronted") or {}
    for d in cf.get("domains") or []:
        if _match(host, d):
            return cf.get("action", "placeholder"), f"cf_fronted:{d}"
    return (policy or {}).get("default", "screenshot"), "default"


def _rule_reason(policy: dict, url: str) -> str:
    host = _host(url)
    for r in (policy or {}).get("rules") or []:
        if _match(host, r.get("match")):
            return r.get("reason") or f"policy:{r.get('match')}"
    cf = (policy or {}).get("cloudflare_fronted") or {}
    for d in cf.get("domains") or []:
        if _match(host, d):
            return cf.get("reason") or "cf_fronted"
    return "policy"


def _rule_proxy(policy: dict, url: str):
    """rules 可带 proxy 键："direct" 或显式代理 URL——作为**首次**尝试的
    路由（重试仍回落 env→config 默认链，两条出口各抽几次签）。
    例：openai.com 走 clash 必吃 chunk 403/CF 墙，直连干净。"""
    host = _host(url)
    for r in (policy or {}).get("rules") or []:
        if _match(host, r.get("match")) and r.get("proxy"):
            return r["proxy"]
    return None


# --------------------------------------------------------- placeholder ------
# claudeStyle 品牌卡（对齐 upstream/juya-news-card claudeStyle 调色 +
# repro/render_chrome.py chrome 色系），无外部资源 —— set_content 可用。
def placeholder_html(domain: str, url: str = "") -> str:
    dom = htmlmod.escape(domain or "unknown")
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:1920px; height:1080px; overflow:hidden; }}
body {{
  background:#fbf9f6; display:flex; align-items:center; justify-content:center;
  font-family:'Noto Sans CJK SC','Noto Sans SC','DejaVu Sans',sans-serif;
}}
.card {{
  width:1500px; background:#fdfbf6; border:2px solid #e3ddcd; border-radius:28px;
  box-shadow:0 18px 60px rgba(90,80,60,.14); padding:96px 80px; text-align:center;
}}
.badge {{
  display:inline-block; background:#d14f27; color:#fff; border-radius:10px;
  font-size:26px; font-weight:700; letter-spacing:.30em; padding:10px 26px 10px 34px;
}}
.domain {{
  margin-top:44px; font-size:72px; font-weight:800; color:#4a403a;
  word-break:break-all; line-height:1.15;
}}
.sub {{ margin-top:34px; font-size:27px; color:#8a8175; }}
</style></head><body><div class="card">
  <div class="badge">SOURCE</div>
  <div class="domain">{dom}</div>
  <div class="sub">原文页截图不可用 · source page unavailable</div>
</div></body></html>"""


def _placeholder_pil(domain: str, out_path: Path, size=(1920, 1080)) -> bool:
    """browser 都起不来时的最后兜底：PIL 画同色系占位卡。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        w, h = size
        im = Image.new("RGB", (w, h), "#fbf9f6")
        d = ImageDraw.Draw(im)
        cw, ch = 1500, 560
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        d.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=28,
                            fill="#fdfbf6", outline="#e3ddcd", width=3)
        font = None
        # 发行版路径矩阵：Arch=noto-cjk、Debian/Ubuntu=opentype/noto +
        # truetype/dejavu（ttf-dejavu）、Fedora 系旧名 noto-sans-cjk
        for fp in ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                   "/usr/share/fonts/noto-sans-cjk/NotoSansCJK-Bold.ttc",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(fp, 72)
                small = ImageFont.truetype(fp, 30)
                break
            except Exception:
                continue
        if font is None:
            font = small = ImageFont.load_default()
        label = f"SOURCE: {domain or 'unknown'}"
        d.text((w / 2, h / 2 - 40), label, fill="#4a403a", font=font,
               anchor="mm")
        d.text((w / 2, h / 2 + 90), "source page unavailable",
               fill="#8a8175", font=small, anchor="mm")
        im.save(str(out_path))
        return True
    except Exception:
        return False


# ------------------------------------------------------ x.com syndication ---
# x.com/twitter.com 页面本体是硬 403 botwall，但官方 embed 数据端点
# cdn.syndication.twimg.com/tweet-result 直连可达（2026-09-24 实测；token
# 非严格校验——仍按官方算法生成以防收紧）。拿 JSON 自绘品牌推文卡，
# 全程不导航 x.com 本体。
_X_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")


def _tweet_token(tweet_id: str) -> str:
    """官方 embed 的 token：((id/1e15)*π).toString(36) 去掉 '.' 与所有 '0'。"""
    x = int(tweet_id) / 1e15 * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip = int(x)
    whole = ""
    while ip:
        whole = digits[ip % 36] + whole
        ip //= 36
    frac = x - int(x)
    out = []
    while frac > 0 and len(out) < 15:
        frac *= 36
        d = int(frac)
        out.append(digits[d])
        frac -= d
    return (whole + "".join(out)).replace("0", "")


def _http_json(u: str, timeout: int = 12):
    """urllib 取 JSON：先试直连（syndication/publish 直连可达），再退 env 代理。"""
    import json as _json
    import urllib.request
    req = urllib.request.Request(
        u, headers={"User-Agent": UA, "Accept": "application/json"})
    openers = [urllib.request.build_opener(urllib.request.ProxyHandler({})),
               urllib.request.build_opener()]
    for opn in openers:
        try:
            with opn.open(req, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
    return None


def _fetch_tweet(url: str):
    """x/twitter status URL → tweet-result dict | None。"""
    m = _X_STATUS_RE.search(url or "")
    if not m:
        return None
    tid = m.group(1)
    tw = _http_json(
        "https://cdn.syndication.twimg.com/tweet-result"
        f"?id={tid}&token={_tweet_token(tid)}&lang=en")
    if isinstance(tw, dict) and tw.get("text"):
        tw["_tid"] = tid
        return tw
    return None


_X_LOGO = ("<svg class='xlogo' viewBox='0 0 24 24' fill='#2f2a26'>"
           "<path d='M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406"
           "l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594"
           "l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z'/></svg>")

_X_CHECK = ("<svg class='chk' viewBox='0 0 24 24'>"
            "<circle cx='12' cy='12' r='11' fill='#1d9bf0'/>"
            "<path fill='#fff' d='M10.8 15.9l-3.5-3.5 1.4-1.4 2.1 2.1"
            " 4.5-4.5 1.4 1.4z'/></svg>")


def _x_card_html(tw: dict) -> str:
    """tweet-result JSON → 品牌推文卡（对齐 placeholder_html 色系/版式）。"""
    esc = htmlmod.escape
    user = tw.get("user") or {}
    name = esc(str(user.get("name") or ""))
    handle = esc(str(user.get("screen_name") or ""))
    avatar = esc(str(user.get("profile_image_url_https") or "")
                 .replace("_normal.", "_400x400."))
    verified = user.get("is_blue_verified") or user.get("verified")
    badge_url = esc(str(((user.get("highlighted_label") or {})
                         .get("badge") or {}).get("url") or ""))
    text = str(tw.get("text") or "")
    rng = tw.get("display_text_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2 and rng[1]:
        text = text[int(rng[0]):int(rng[1])]
    for ent in ((tw.get("entities") or {}).get("urls")) or []:
        if ent.get("url") and ent.get("display_url"):
            text = text.replace(ent["url"], ent["display_url"])
    body = esc(text).replace("\n", "<br>")
    try:
        fav = f"{int(tw.get('favorite_count')):,}"
    except (TypeError, ValueError):
        fav = ""
    date = str(tw.get("created_at") or "")[:10]
    img = ""
    media = (tw.get("photos") or tw.get("mediaDetails")
             or (tw.get("entities") or {}).get("media") or [])
    for mm in media:
        src = isinstance(mm, dict) and (mm.get("url") or
                                        mm.get("media_url_https"))
        if src:
            img = (f"<img class='media' src='{esc(str(src))}'"
                   " onerror=\"this.style.display='none'\">")
            break
    avtag = (f"<img class='av' src='{avatar}'"
             " onerror=\"this.style.display='none'\">") if avatar \
        else "<div class='av'></div>"
    biz = (f"<img class='biz' src='{badge_url}'"
           " onerror=\"this.style.display='none'\">") if badge_url else ""
    chk = _X_CHECK if verified else ""
    meta = " · ".join(x for x in (esc(date), f"♥ {esc(fav)}" if fav else "")
                     if x)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:1920px; height:1080px; overflow:hidden; }}
body {{
  background:#fbf9f6; display:flex; align-items:center; justify-content:center;
  font-family:'Noto Sans CJK SC','Noto Sans SC','DejaVu Sans',sans-serif;
}}
.card {{
  width:1400px; background:#fdfbf6; border:2px solid #e3ddcd;
  border-radius:28px; box-shadow:0 18px 60px rgba(90,80,60,.14);
  padding:64px 72px;
}}
.head {{ display:flex; align-items:flex-start; }}
.av {{ width:96px; height:96px; border-radius:50%; background:#e3ddcd;
       flex:none; }}
.who {{ margin-left:28px; flex:1; min-width:0; }}
.name {{ font-size:44px; font-weight:800; color:#2f2a26; display:flex;
         align-items:center; gap:12px; }}
.chk {{ width:36px; height:36px; flex:none; }}
.biz {{ width:38px; height:38px; border-radius:6px; flex:none; }}
.handle {{ margin-top:8px; font-size:30px; color:#8a8175; }}
.xlogo {{ width:46px; height:46px; flex:none; margin-top:6px; }}
.text {{ margin-top:42px; font-size:44px; line-height:1.5; color:#2f2a26;
         word-break:break-word; }}
.media {{ margin-top:36px; max-width:100%; max-height:400px;
          border-radius:18px; border:1px solid #e3ddcd; }}
.foot {{ margin-top:46px; display:flex; align-items:center;
         justify-content:space-between; }}
.meta {{ font-size:30px; color:#8a8175; }}
.badge {{ background:#d14f27; color:#fff; border-radius:10px;
          font-size:24px; font-weight:700; letter-spacing:.2em;
          padding:8px 20px 8px 26px; }}
</style></head><body><div class="card">
  <div class="head">{avtag}
    <div class="who">
      <div class="name">{name}{chk}{biz}</div>
      <div class="handle">@{handle}</div>
    </div>{_X_LOGO}
  </div>
  <div class="text">{body}</div>{img}
  <div class="foot"><div class="meta">{meta}</div>
    <div class="badge">POST</div></div>
</div></body></html>"""


def _x_oembed_card(url: str):
    """tweet-result 失败的次选：publish.twitter.com/oembed 的 blockquote
    套品牌卡壳（仍不导航 x.com；widgets.js script 剥掉——我们只要静态 HTML）。"""
    import urllib.parse
    data = _http_json("https://publish.twitter.com/oembed?url=" +
                      urllib.parse.quote(url or "", safe=""))
    bq = (data or {}).get("html") if isinstance(data, dict) else None
    if not bq:
        return None
    bq = re.sub(r"<script[^>]*>.*?</script>", "", bq, flags=re.S)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:1920px; height:1080px; overflow:hidden; }}
body {{
  background:#fbf9f6; display:flex; align-items:center; justify-content:center;
  font-family:'Noto Sans CJK SC','Noto Sans SC','DejaVu Sans',sans-serif;
}}
.card {{
  width:1400px; background:#fdfbf6; border:2px solid #e3ddcd;
  border-radius:28px; box-shadow:0 18px 60px rgba(90,80,60,.14);
  padding:72px 80px;
}}
blockquote {{ font-size:42px; line-height:1.5; color:#2f2a26;
              word-break:break-word; }}
blockquote a {{ color:#d14f27; text-decoration:none; }}
blockquote p {{ margin-bottom:36px; }}
</style></head><body><div class="card">{bq}</div></body></html>"""


# ------------------------------------------------------------ mechanics -----

def _blankish(path: Path):
    """空白/近空白图检测：(is_blank, stddev)。PIL 不可用时按文件大小粗判。"""
    try:
        from PIL import Image
        im = Image.open(path).convert("L").resize((96, 64))
        px = list(im.tobytes())
        mean = sum(px) / len(px)
        sd = (sum((v - mean) ** 2 for v in px) / len(px)) ** 0.5
        return sd < 8, round(sd, 1)
    except Exception:
        try:
            return path.stat().st_size < 9000, -1
        except Exception:
            return True, -1


def _env_proxy(url: str):
    scheme = (url.split(":", 1)[0] or "https").lower()
    return (os.environ.get(f"{scheme.upper()}_PROXY")
            or os.environ.get(f"{scheme.lower()}_proxy")
            or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))


def _cfg_proxy():
    """config.yaml / config.example.yaml 的 proxy.http——env 缺省时的兜底。"""
    try:
        import yaml
        for name in ("config.yaml", "config.example.yaml"):
            p = REPO_ROOT / name
            if p.is_file():
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                px = doc.get("proxy")
                if isinstance(px, dict) and px.get("http"):
                    return str(px["http"])
    except Exception:
        pass
    return None


def _resolve_proxy(url: str, proxy, force_cfg: bool = False):
    """-> playwright proxy dict | None。'direct'/'none' 强制直连。
    force_cfg=True 时 env 缺省再退到 config proxy.http（重试兜底路由）。
    语义依赖：browser 以 proxy=None + 干净 env 启动，ctx 的 None 才等于
    真直连（ctx proxy=None 本是"继承 browser 级代理"——playwright 1.63
    也不认 "direct://" 哨兵，会 ERR_PROXY_CONNECTION_FAILED）。"""
    if isinstance(proxy, str) and proxy.lower() in ("direct", "none", "off"):
        return None
    cand = proxy if isinstance(proxy, str) and proxy else _env_proxy(url)
    if not cand and force_cfg:
        cand = _cfg_proxy()
    return {"server": cand} if cand else None


def _clean_env(**extra) -> dict:
    """剥掉 *_proxy 的进程环境：Chromium 会读 env 代理当默认出口，
    导致 ctx proxy=None 的"直连"其实仍走 clash。browser 一律用干净
    环境启动，代理改由 ctx 级显式指定——规则路由/direct 才真正生效。"""
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env.update(extra)
    return env


def _gn_resolve(url: str, timeout: int = 10):
    """news.google.com/rss/articles/<id> 中转页 → 真实出版方 URL。

    batchexecute 签名参数会随 Google 轮换，手写 RPC 易腐——直接用
    googlenewsdecoder（cards.py PEP723 已声明）。包装缺失/解码失败
    返回 None——照原 URL 走老路（中转页兜底）。"""
    if "news.google." not in _host(url):
        return None
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(url)
        real = (r or {}).get("decoded_url") if r.get("status") else None
        return real if isinstance(real, str) and "google." not in _host(real) \
            else None
    except Exception:
        return None


# Playwright 路由拦截会让 Chromium 走非优化网络栈（HTTP/2 指纹变化），
# Vercel/CF 边缘据此对同源 JS chunk 回 403 → openai.com 这类站整页
# "couldn't load"。所以绝不注册 "**/*"：只给 blocklist 域与媒体扩展名
# 挂 URL-glob 路由——不匹配的请求完全不进拦截路径，指纹零变化。
_MEDIA_EXT_GLOB = (
    "**/*.{mp4,webm,mov,m4v,mkv,mp3,m4a,ogg,wav,flac,flv,m3u8,mpd}"
)


def _install_block(ctx):
    def _abort(route):
        try:
            route.abort()
        except Exception:
            pass
    for h in BLOCK_HOSTS:
        try:
            ctx.route(f"**/*{h}*", _abort)
        except Exception:
            pass
    try:
        ctx.route(_MEDIA_EXT_GLOB, _abort)
    except Exception:
        pass


def _clean(page):
    """CMP accept 点击 + 通用文本按钮 + 滚动触发懒加载 + fonts/img 等待 +
    overlay surgery。全部 try 包裹 —— 任何一步失败不致命。"""
    for sel in ACCEPT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=600)
                page.wait_for_timeout(250)
        except Exception:
            pass
    try:
        page.evaluate("""() => {
          let c=0; const pat=/accept all|accept cookies|agree|allow all|同意|接受|我知道了|允许所有|确定/i;
          document.querySelectorAll('button,[role=button],a').forEach(b=>{
            const t=(b.innerText||'').trim();
            if(t&&t.length<28&&pat.test(t)){const r=b.getBoundingClientRect();
              if(r.width>30&&r.height>15&&r.top<innerHeight&&c<2){try{b.click();c++;}catch(e){}}}});
          return c;}""")
    except Exception:
        pass
    try:
        page.evaluate("window.scrollTo(0,Math.min(600,document.body.scrollHeight));")
        page.wait_for_timeout(400)
        page.evaluate("window.scrollTo(0,0);")
        page.wait_for_timeout(300)
    except Exception:
        pass
    try:
        page.evaluate(READY_JS)
    except Exception:
        pass
    try:
        page.evaluate(SURGERY_JS)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _settle(page, nav_timeout: int) -> tuple[str, str]:
    """goto/reload 后的就绪等待：load 态 + news.google 中转跳转 +
    CF/Turnstile 挑战自解（~18s 上限 + checkbox 点击）。返回 (title, body)。"""
    try:
        page.wait_for_load_state("load", timeout=9000)
    except Exception:
        pass
    # news.google/rss/articles 是 JS 中转页（"Loading <real url>"）——
    # 不等它跳完就会截到中转页（undersized）。轮询最多 12s 等真站跳转。
    for _ in range(12):
        if "news.google." not in (_host(page.url) or ""):
            break
        page.wait_for_timeout(1000)
    if "news.google." not in (_host(page.url) or ""):
        try:
            page.wait_for_load_state("load", timeout=9000)
        except Exception:
            pass
    page.wait_for_timeout(700)
    # CF/Turnstile：最多 ~18s 等挑战页自解，顺手点 checkbox
    for _ in range(6):
        try:
            sniff = (page.title() or "") + " " + page.evaluate(
                "document.body?document.body.innerText.slice(0,400):''")
        except Exception:
            sniff = ""
        if not WALL_PAT.search(sniff):
            break
        try:
            for f in page.frames:
                if "challenges.cloudflare" in (f.url or ""):
                    el = f.query_selector("input[type=checkbox],label")
                    if el:
                        el.click(timeout=800)
        except Exception:
            pass
        page.wait_for_timeout(3000)
    try:
        title = page.title() or ""
        body = page.evaluate(
            "document.body?document.body.innerText.slice(0,800):''") or ""
    except Exception:
        title, body = "", ""
    return title, body


# 站点级"couldn't load"是边缘节点概率性 403 子资源所致（openai.com
# 实测命中率 ~25%/次，与 stealth/代理无关）——同 ctx 内 reload 换一批
# chunk 请求即可抽新签；可能落到 CF 挑战页，故每轮重跑 _settle。
# wall 也抽新签，但连续 2 次 wall 视为粘性墙（非 flake）早停省时间。
_ERR_RELOADS = 5


def _attempt(ctx, url, shot_path: Path, nav_timeout: int) -> dict:
    page = ctx.new_page()
    page.on("dialog", lambda d: d.dismiss())     # js dialog 会挂死 headless
    try:
        resp = page.goto(url, wait_until="domcontentloaded",
                         timeout=nav_timeout)
        status = resp.status if resp else None
        title, body = "", ""
        walls = 0
        # 站点级"couldn't load"是异步 error boundary：SSR 标题先对，
        # chunk 403 后 React 才把 DOM 换成错误页——所以检查必须放在
        # _clean 之后（给异步崩溃留浮现窗口），错了就整轮 reload 重抽。
        for r in range(1 + _ERR_RELOADS):
            if r:
                try:
                    page.reload(wait_until="domcontentloaded",
                                timeout=nav_timeout)
                except Exception:
                    break
            title, body = _settle(page, nav_timeout)
            bad = _err_page(page, title, body) or \
                bool(WALL_PAT.search(title + " " + body))
            if not bad:
                _clean(page)
                page.wait_for_timeout(900)  # error boundary 异步浮现窗口
                try:
                    title = page.title() or ""
                    body = page.evaluate(
                        "document.body?document.body.innerText.slice(0,800):''"
                        ) or ""
                except Exception:
                    title, body = "", ""
                bad = _err_page(page, title, body) or \
                    bool(WALL_PAT.search(title + " " + body))
            walls = walls + 1 if WALL_PAT.search(title + " " + body) else 0
            if not bad:
                break
            if walls >= 2:
                break                        # 粘性 CF 墙，不再浪费 reload
        page.screenshot(path=str(shot_path), style=FREEZE_CSS,
                        animations="disabled", caret="hide", timeout=15000)
        return {"status": status, "title": title[:110],
                "wall": bool(WALL_PAT.search(title + " " + body)),
                "err_page": _err_page(page, title, body),
                "body_head": body[:100].replace("\n", " ")}
    finally:
        page.close()


# ------------------------------------------------------------------ API -----

class ShotSession:
    """复用同一 chromium 的批量截图会话。用法::

        with ShotSession(cfg) as s:
            rec = s.shot(url, out_path)

    browser 起不来时 session 仍可用：每次 shot 走 PIL 占位兜底。
    """

    def __init__(self, cfg=None):
        self.cfg = {**_DEFAULT_CFG, **(cfg or {})}
        self.policy = self.cfg.get("policy") or load_policy(
            self.cfg.get("policy_path"))
        self._pw = None
        self._browser = None
        self._browser_hf = None    # Xvfb headful 升级路（懒启动）
        self._xvfb = None
        self.driver_error = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            # browser 恒直连启动：env 剥代理（Chromium 会读 *_proxy 环境
            # 变量当默认出口）+ proxy=None；每次截图在 ctx 显式解析路由。
            self._browser = self._pw.chromium.launch(
                headless=self.cfg["headless"], args=ARGS,
                proxy=None, timeout=30000, env=_clean_env())
        except Exception as e:
            self.driver_error = f"{type(e).__name__}: {e}"
            self._browser = None
        return self

    def __exit__(self, *exc):
        for br in (self._browser, self._browser_hf):
            try:
                if br:
                    br.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        try:
            if self._xvfb:
                self._xvfb.terminate()
                self._xvfb.wait(timeout=5)
        except Exception:
            pass
        self._browser = self._browser_hf = self._pw = self._xvfb = None

    def _headful_browser(self):
        """粘性 CF 墙升级路：Xvfb + headful chromium。Turnstile 对 headless
        指纹判负率高，headful 常自解——openai.com 实测 headless 全墙时
        headful 一把过。懒启动；无 Xvfb/启动失败 → None 跳过。"""
        if self._browser_hf or not self._pw:
            return self._browser_hf
        import shutil
        import subprocess
        if not shutil.which("Xvfb"):
            return None
        disp = None
        for n in range(200, 220):
            if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
                disp = f":{n}"
                break
        if not disp:
            return None
        try:
            self._xvfb = subprocess.Popen(
                ["Xvfb", disp, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            # 同主 browser：剥 *_proxy env，代理由 ctx 级显式给。
            self._browser_hf = self._pw.chromium.launch(
                headless=False, args=ARGS, timeout=30000,
                env=_clean_env(DISPLAY=disp))
        except Exception:
            self._browser_hf = None
        return self._browser_hf

    # ---- per-url -----------------------------------------------------------

    def shot(self, url: str, out_path) -> dict:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"url": url, "host": _host(url)}
        t0 = time.time()
        if "news.google." in rec["host"]:
            real = _gn_resolve(url)          # 中转页解出真文，直接截出版方
            if real:
                rec["resolved"] = real
                url = real
                rec["host"] = _host(url)
        action, rule = policy_action(self.policy, url)
        rec["action"] = action
        rec["rule"] = rule
        if action == "placeholder":
            rec["reason"] = _rule_reason(self.policy, url)
            return self._finish_placeholder(rec, out_path)
        if action == "x_embed":
            r = self._x_embed(rec, out_path, url)
            rec["ms"] = int((time.time() - t0) * 1000)
            return r
        self._screenshot(url, out_path, rec)
        rec["ms"] = int((time.time() - t0) * 1000)
        return rec

    def _finish_placeholder(self, rec, out_path: Path) -> dict:
        if self._placeholder_browser(rec["host"], out_path, rec["url"]) or \
           _placeholder_pil(rec["host"], out_path,
                            self.cfg["placeholder_size"]):
            rec.update(path=str(out_path), kind="placeholder", ok=True)
        else:
            rec.update(path=None, kind="placeholder", ok=False)
        return rec

    def _placeholder_browser(self, domain, out_path: Path, url) -> bool:
        return self._render_html(placeholder_html(domain, url), out_path)

    def _render_html(self, html_doc: str, out_path: Path) -> bool:
        """set_content → screenshot，走 placeholder_size（1920×1080）。
        READY_JS 等头像/媒体图加载（x_embed 卡片有外链图）。"""
        if not self._browser:
            return False
        w, h = self.cfg["placeholder_size"]
        try:
            ctx = self._browser.new_context(
                viewport={"width": w, "height": h}, device_scale_factor=1,
                locale="en-US", bypass_csp=True)
            pg = ctx.new_page()
            try:
                pg.set_content(html_doc, wait_until="load", timeout=12000)
                try:
                    pg.evaluate(READY_JS)
                except Exception:
                    pass
                pg.wait_for_timeout(400)
                pg.screenshot(path=str(out_path), timeout=10000)
            finally:
                ctx.close()
            return out_path.exists() and out_path.stat().st_size > 5000
        except Exception:
            return False

    def _x_embed(self, rec: dict, out_path: Path, url: str) -> dict:
        """x_embed action：syndication tweet-result → 品牌推文卡；
        数据拿不到退 oembed blockquote 卡，再不行退占位卡。"""
        tw = _fetch_tweet(url)
        html_doc = _x_card_html(tw) if tw else _x_oembed_card(url)
        if html_doc is None:
            rec["reason"] = "x_embed_fetch_failed"
            return self._finish_placeholder(rec, out_path)
        if self._render_html(html_doc, out_path):
            rec.update(path=str(out_path), kind="shot", ok=True,
                       via="x_embed" if tw else "x_oembed",
                       tweet_id=(tw or {}).get("_tid"))
            return rec
        rec["reason"] = "x_embed_render_failed"
        return self._finish_placeholder(rec, out_path)

    def _try_ctx(self, browser, url, out_path: Path, rec: dict,
                 route, force_cfg: bool) -> bool:
        """单次 ctx 尝试：建 ctx → _attempt → 判成败 + 失败归因。返回是否成功。"""
        vw, vh = self.cfg["viewport"]
        ctx = None
        try:
            ctx = browser.new_context(
                viewport={"width": vw, "height": vh},
                device_scale_factor=self.cfg["scale"],
                user_agent=UA, locale="en-US",
                extra_http_headers=dict(EXTRA_HEADERS),
                reduced_motion="reduce", color_scheme="light",
                ignore_https_errors=True, service_workers="block",
                proxy=_resolve_proxy(url, route, force_cfg=force_cfg))
            ctx.add_init_script(STEALTH_JS)
            try:
                bare = ".".join((_host(url) or "").split(".")[-2:])
                if bare:
                    ctx.add_cookies(consent_cookies("." + bare))
            except Exception:
                pass
            _install_block(ctx)
            r = _attempt(ctx, url, out_path, int(self.cfg["nav_timeout_ms"]))
            rec.update(r)
            blank, sd = _blankish(out_path)
            rec["stddev"] = sd
            rec["shot_kb"] = out_path.stat().st_size // 1024 \
                if out_path.exists() else 0
            if (not rec["wall"]) and (not blank) \
                    and not rec.get("err_page") \
                    and (rec.get("status") or 0) < 400 \
                    and rec["shot_kb"] > int(self.cfg["min_shot_kb"]):
                rec.update(path=str(out_path), kind="shot", ok=True)
                rec.pop("reason", None)   # 清掉前次尝试留下的失败归因
                return True
            # 失败归因（供 missing[] 记录）
            if rec.get("err_page"):
                rec["reason"] = "error_page"
            elif rec.get("wall"):
                rec["reason"] = "wall_detected"
            elif (rec.get("status") or 0) >= 400:
                rec["reason"] = f"http_{rec['status']}"
            elif blank:
                rec["reason"] = "blank_capture"
            else:
                rec["reason"] = "undersized_capture"
        except Exception as e:
            rec["err"] = f"{type(e).__name__}: {str(e)[:140]}"
            rec["reason"] = rec.get("reason") or rec["err"]
        finally:
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass
        return False

    def _screenshot(self, url, out_path: Path, rec: dict):
        if not self._browser:
            rec["reason"] = f"browser_unavailable: {self.driver_error}"
            return self._finish_placeholder(rec, out_path)
        retries = int(self.cfg["retries"])
        rprox = _rule_proxy(self.policy, url)   # 规则级首选路由（如 openai→direct）
        tries = 0
        for attempt in range(1 + retries):
            # attempt0 走规则路由；重试回落默认链（env→config）换出口抽签
            route = rprox if (attempt == 0 and rprox) else self.cfg["proxy"]
            tries += 1
            if self._try_ctx(self._browser, url, out_path, rec,
                             route, force_cfg=attempt > 0):
                rec["attempts"] = tries
                return
            if attempt < retries:
                time.sleep(2)          # wechat 式 flake：新 ctx 间隔重试
        # 粘性 CF 墙最后一搏：Xvfb headful（Turnstile 对 headful 判负率低）
        if rec.get("reason") == "wall_detected" \
                and self.cfg.get("headful_retry"):
            br = self._headful_browser()
            if br:
                tries += 1
                # headful 升级路走默认代理链（env→config），刻意忽略规则
                # 路由：clash 出口实测常过 Turnstile，直连反而吃墙
                if self._try_ctx(br, url, out_path, rec,
                                 self.cfg["proxy"], force_cfg=True):
                    rec["attempts"] = tries
                    rec["via"] = "headful"
                    return
        rec["reason"] = rec.get("reason") or "shot_failed"
        return self._finish_placeholder(rec, out_path)


def shot(url: str, out_path, cfg=None) -> dict:
    """单发便捷封装：自开自关一个 ShotSession。
    -> {"path": str|None, "kind": "shot"|"placeholder", "ok": bool, ...}"""
    with ShotSession(cfg) as s:
        return s.shot(url, out_path)


# ------------------------------------------------------------- self test ----

def _selftest():
    import json
    offline = "--offline" in sys.argv

    # --- offline asserts ---------------------------------------------------
    pol = load_policy()
    assert policy_action(pol, "https://x.com/a/status/1") == \
        ("x_embed", "rule:x.com")
    assert policy_action(pol, "https://mobile.twitter.com/a/status/1") == \
        ("x_embed", "rule:twitter.com")
    assert policy_action(pol, "https://www.reuters.com/x") == \
        ("placeholder", "rule:reuters.com")
    assert policy_action(pol, "https://mp.weixin.qq.com/s/abc") == \
        ("placeholder", "rule:mp.weixin.qq.com")
    assert policy_action(pol, "https://www.science.org/x") == \
        ("placeholder", "cf_fronted:science.org")
    assert policy_action(pol, "https://example.com/a")[0] == "screenshot"
    doc = placeholder_html("x.com")
    assert "SOURCE" in doc and "x.com" in doc and "1920px" in doc
    assert _tweet_token("2102464672519815512").startswith("53h35hnc")
    print("offline policy/placeholder asserts OK")

    if offline:
        print("live shots skipped (--offline)")
        return

    out = Path.home() / ".cache" / "winnow_shotlib_selftest"
    out.mkdir(parents=True, exist_ok=True)
    fails = []

    # 1) x.com —— x_embed：syndication 推文卡；网络异常时退占位也算过
    r = shot("https://x.com/sama/status/2102464672519815512",
             out / "x.png")
    print("[x]", json.dumps(r, ensure_ascii=False))
    assert r["ok"] and r["path"]

    # 2) wechat —— 策略命中占位
    r = shot("https://mp.weixin.qq.com/s/selftest-placeholder",
             out / "wechat.png")
    print("[wechat]", json.dumps(r, ensure_ascii=False))
    assert r["kind"] == "placeholder" and r["ok"] and r["path"]

    # 3) 正常新闻页 —— 直连先试，失败换代理再试（代理走 env→config→空
    #    默认链；本机 clash 7890 是示例不是默认，无代理环境只跑直连）
    got_shot = False
    cands = ["https://www.the-decoder.com/",
             "https://www.scmp.com/",
             "https://github.com/anthropics/claude-code"]
    for cand in cands:
        alt = _env_proxy(cand) or _cfg_proxy()
        for prox in (["direct"] + ([alt] if alt else [])):
            r = shot(cand, out / "news.png", {"proxy": prox})
            print(f"[news {prox or 'direct'}]",
                  json.dumps({k: r.get(k) for k in
                              ("kind", "ok", "status", "wall", "reason",
                               "shot_kb", "ms")}, ensure_ascii=False))
            if r["kind"] == "shot" and r["ok"]:
                got_shot = True
                break
        if got_shot:
            break
    if not got_shot:
        fails.append("no news candidate produced a real shot")

    # 尺寸校验：占位卡必须 1920x1080；真截图 ≈ 1400x900@1.5
    try:
        from PIL import Image
        for name in ("x.png", "wechat.png"):
            im = Image.open(out / name)
            assert im.size == (1920, 1080), (name, im.size)
        if (out / "news.png").exists():
            im = Image.open(out / "news.png")
            assert im.size == (2100, 1350), im.size
        print("image size asserts OK")
    except ImportError:
        print("WARN: PIL missing — skipped size asserts")

    if fails:
        print("FAIL:", fails)
        sys.exit(1)
    print("shotlib self-test OK ->", out)


if __name__ == "__main__":
    _selftest()
