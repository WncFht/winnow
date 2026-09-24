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
    shot_many(items, cfg=None)    -> list[dict]          # 批量复用同一 browser
    load_policy(path=None)        -> dict                # state/shot_policy.yaml

策略链（域名策略表 state/shot_policy.yaml 先行）：
  0. news.google.* 中转 URL 先经 googlenewsdecoder 解出出版方真链
     （可选依赖——包缺失/解码失败照原 URL 走老路；命中记 rec.resolved），
     再对真链走下述判定；
  1. host 命中 rules/cloudflare_fronted → 直接渲染品牌占位卡，不导航；
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
运行层蒸馏自同目录 fetch_shots_v2.py（patchright/GFW-wayback/x-embed 等
重写路未纳入 —— 策略表只保留 screenshot|placeholder 两种 action）。

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
            {"match": "x.com", "action": "placeholder", "reason": "403 botwall"},
            {"match": "twitter.com", "action": "placeholder", "reason": "→x.com 403"},
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
        for fp in ("/usr/share/fonts/noto-sans-cjk/NotoSansCJK-Bold.ttc",
                   "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
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
    force_cfg=True 时 env 缺省再退到 config proxy.http（重试兜底路由）。"""
    if isinstance(proxy, str) and proxy.lower() in ("direct", "none", "off"):
        return None
    cand = proxy if isinstance(proxy, str) and proxy else _env_proxy(url)
    if not cand and force_cfg:
        cand = _cfg_proxy()
    return {"server": cand} if cand else None


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


def _block(route):
    req = route.request
    if req.resource_type == "media" or any(h in req.url for h in BLOCK_HOSTS):
        route.abort()
    else:
        route.continue_()


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


def _attempt(ctx, url, shot_path: Path, nav_timeout: int) -> dict:
    page = ctx.new_page()
    page.on("dialog", lambda d: d.dismiss())     # js dialog 会挂死 headless
    try:
        resp = page.goto(url, wait_until="domcontentloaded",
                         timeout=nav_timeout)
        status = resp.status if resp else None
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
        _clean(page)
        page.screenshot(path=str(shot_path), style=FREEZE_CSS,
                        animations="disabled", caret="hide", timeout=15000)
        title = page.title() or ""
        body = page.evaluate(
            "document.body?document.body.innerText.slice(0,800):''") or ""
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
        self.driver_error = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            proxy = _resolve_proxy("https://example.com", self.cfg["proxy"])
            self._browser = self._pw.chromium.launch(
                headless=self.cfg["headless"], args=ARGS,
                proxy=proxy, timeout=30000)
        except Exception as e:
            self.driver_error = f"{type(e).__name__}: {e}"
            self._browser = None
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = None

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
        if not self._browser:
            return False
        w, h = self.cfg["placeholder_size"]
        try:
            ctx = self._browser.new_context(
                viewport={"width": w, "height": h}, device_scale_factor=1,
                locale="en-US", bypass_csp=True)
            pg = ctx.new_page()
            try:
                pg.set_content(placeholder_html(domain, url),
                               wait_until="load", timeout=8000)
                pg.screenshot(path=str(out_path), timeout=8000)
            finally:
                ctx.close()
            return out_path.exists() and out_path.stat().st_size > 5000
        except Exception:
            return False

    def _screenshot(self, url, out_path: Path, rec: dict):
        if not self._browser:
            rec["reason"] = f"browser_unavailable: {self.driver_error}"
            return self._finish_placeholder(rec, out_path)
        vw, vh = self.cfg["viewport"]
        retries = int(self.cfg["retries"])
        last_err = None
        for attempt in range(1 + retries):
            ctx = None
            try:
                ctx = self._browser.new_context(
                    viewport={"width": vw, "height": vh},
                    device_scale_factor=self.cfg["scale"],
                    user_agent=UA, locale="en-US",
                    extra_http_headers=dict(EXTRA_HEADERS),
                    reduced_motion="reduce", color_scheme="light",
                    ignore_https_errors=True, service_workers="block",
                    proxy=_resolve_proxy(url, self.cfg["proxy"],
                                         force_cfg=attempt > 0))
                ctx.add_init_script(STEALTH_JS)
                try:
                    bare = ".".join((_host(url) or "").split(".")[-2:])
                    if bare:
                        ctx.add_cookies(consent_cookies("." + bare))
                except Exception:
                    pass
                ctx.route("**/*", _block)
                r = _attempt(ctx, url, out_path,
                             int(self.cfg["nav_timeout_ms"]))
                rec.update(r)
                blank, sd = _blankish(out_path)
                rec["stddev"] = sd
                rec["shot_kb"] = out_path.stat().st_size // 1024 \
                    if out_path.exists() else 0
                if (not rec["wall"]) and (not blank) \
                        and not rec.get("err_page") \
                        and (rec.get("status") or 0) < 400 \
                        and rec["shot_kb"] > int(self.cfg["min_shot_kb"]):
                    rec.update(path=str(out_path), kind="shot", ok=True,
                               attempts=attempt + 1)
                    return
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
                last_err = rec["reason"]
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:140]}"
                rec["err"] = last_err
            finally:
                try:
                    if ctx:
                        ctx.close()
                except Exception:
                    pass
            if attempt < retries:
                time.sleep(2)          # wechat 式 flake：新 ctx 间隔重试
        rec["reason"] = rec.get("reason") or last_err or "shot_failed"
        return self._finish_placeholder(rec, out_path)


def shot(url: str, out_path, cfg=None) -> dict:
    """单发便捷封装：自开自关一个 ShotSession。
    -> {"path": str|None, "kind": "shot"|"placeholder", "ok": bool, ...}"""
    with ShotSession(cfg) as s:
        return s.shot(url, out_path)


def shot_many(items, cfg=None):
    """items: [(url, out_path), ...] 或 [{'url':..,'out_path':..}, ...]
    -> list[dict]（与 shot() 同形）。"""
    out = []
    with ShotSession(cfg) as s:
        for it in items:
            if isinstance(it, dict):
                u, p = it["url"], it["out_path"]
            else:
                u, p = it[0], it[1]
            out.append(s.shot(u, p))
    return out


# ------------------------------------------------------------- self test ----

def _selftest():
    import json
    offline = "--offline" in sys.argv

    # --- offline asserts ---------------------------------------------------
    pol = load_policy()
    assert policy_action(pol, "https://x.com/a/status/1") == \
        ("placeholder", "rule:x.com")
    assert policy_action(pol, "https://mobile.twitter.com/a") == \
        ("placeholder", "rule:twitter.com")
    assert policy_action(pol, "https://mp.weixin.qq.com/s/abc") == \
        ("placeholder", "rule:mp.weixin.qq.com")
    assert policy_action(pol, "https://www.science.org/x") == \
        ("placeholder", "cf_fronted:science.org")
    assert policy_action(pol, "https://example.com/a")[0] == "screenshot"
    doc = placeholder_html("x.com")
    assert "SOURCE" in doc and "x.com" in doc and "1920px" in doc
    print("offline policy/placeholder asserts OK")

    if offline:
        print("live shots skipped (--offline)")
        return

    out = Path.home() / ".cache" / "ainews_shotlib_selftest"
    out.mkdir(parents=True, exist_ok=True)
    fails = []

    # 1) x.com —— 策略命中，应直接占位（不导航，快）
    r = shot("https://x.com/thsottiaux/status/2101352781219258527",
             out / "x.png")
    print("[x]", json.dumps(r, ensure_ascii=False))
    assert r["kind"] == "placeholder" and r["ok"] and r["path"]

    # 2) wechat —— 策略命中占位
    r = shot("https://mp.weixin.qq.com/s/selftest-placeholder",
             out / "wechat.png")
    print("[wechat]", json.dumps(r, ensure_ascii=False))
    assert r["kind"] == "placeholder" and r["ok"] and r["path"]

    # 3) 正常新闻页 —— 直连先试，失败换代理再试
    got_shot = False
    cands = ["https://www.the-decoder.com/",
             "https://www.scmp.com/",
             "https://github.com/anthropics/claude-code"]
    for cand in cands:
        for prox in (None, "http://127.0.0.1:7890"):
            cfg = {"proxy": prox} if prox else {"proxy": "direct"}
            r = shot(cand, out / "news.png", cfg)
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
