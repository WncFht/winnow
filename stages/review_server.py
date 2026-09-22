# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""review_server — 人工闸 1 勾选 UI（PLAN §7.3；种子 experiments/manual-filter-ui/serve_review.py）。

纯 http.server 零依赖。读 `40_candidates.json`（gate_select --prepare 产物），
渲染移动端友好勾选页；POST /decide {kept:[{item_key,id?,section?,note?}] 按列出顺序}
→ 校验 + slug 化 + max_items 截断 → 写 `40_selected.json`（selected/1, decided_by:human），
随后自动关闭（just pick 配方前台运行，提交后即释放终端）。

用法：
  review_server.py --run-dir runs/<date> [--port 8923]
  REVIEW_PORT=8923 review_server.py --run-dir R
绑定 0.0.0.0 —— 验收要求局域网手机可开。启动时生成一次性 token 打进 URL
（http://<ip>:<port>/?t=…）：GET / 与 POST /decide 无 token 一律 403，
防止同 WiFi 设备一条 curl 改写当日决策。/healthz 公开，其余路径 404。
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import re
import secrets
import socketserver
import sys
import threading
from collections import Counter
from os import environ
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> contracts/adapters
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))     # stages/ -> gate_select

from lib import meta
import gate_select as gs  # slugify_id/unique_slug/section_slug/lint_selected/now_iso

MAX_BODY = 256 * 1024
# 一行缺任一字段 -> 整个 env 视为不合格（宁可 500 提示页，不要半残渲染）
REQUIRED_CAND_KEYS = ("item_key", "title_zh", "section", "summary",
                      "source", "url", "id")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def valid_env(env) -> bool:
    """candidates envelope 结构校验：dict + candidates 为 list[dict] 且行含必备字段。"""
    if not isinstance(env, dict):
        return False
    cands = env.get("candidates")
    if not isinstance(cands, list):
        return False
    return all(isinstance(c, dict) and all(k in c for k in REQUIRED_CAND_KEYS)
               for c in cands)


def _mmdd(c: dict) -> str:
    """date_published 优先、fallback date_fetched -> 'MM-DD'；都没有 -> ''。"""
    for f in ("date_published", "date_fetched"):
        m = re.match(r"\d{4}-(\d{2})-(\d{2})", str(c.get(f) or ""))
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return ""


_CSS = r"""
*{box-sizing:border-box}
:root{--barh:64px}
body{font:15px/1.55 system-ui,'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif;
 max-width:920px;margin:10px auto;padding:0 10px 90px;background:#fafaf7;color:#222}
h2{position:sticky;top:var(--barh);z-index:4;background:#fafaf7;font-size:15px;
 margin:16px 0 4px;color:#8a5a00;border-bottom:1px solid #e5ddc8;padding:4px 2px}
h2 .sl{color:#b89;font-size:11px;font-weight:400}
.scnt{color:#999;font-size:12px;font-weight:400}
.scnt b{color:#2c6bed}
.row{display:flex;gap:9px;padding:11px 6px;border-radius:8px;align-items:flex-start;
 border-bottom:1px solid #f0ead8}
.row.cur{background:#fff3d6;outline:1px solid #e8c96a}
.row:not(:has(.cb:checked)){opacity:.55}
.row.over{outline:2px solid #e06060;background:#fdf2f2}
.row.over .t::after{content:' ［将丢弃］';color:#c33;font-size:12px;font-weight:400}
.cb{width:22px;height:22px;accent-color:#2c6bed;margin:6px 6px 0 2px;flex:none}
.bd{min-width:0;flex:1;cursor:pointer}
.t{font-weight:650;font-size:15px}
.s{color:#555;font-size:13px;display:-webkit-box;-webkit-line-clamp:2;
 -webkit-box-orient:vertical;overflow:hidden;cursor:pointer}
.s.x{display:block;overflow:visible}
.meta{font-size:12px;color:#888;margin-top:2px}
.b{display:inline-block;background:#eee7d2;border-radius:4px;padding:0 5px;margin-right:4px}
.b.day{background:#dde8f8;color:#1a4a8a}
.b.gray{background:#f2dfb8;color:#8a5a00}
.b.re{background:#d8e8f6;color:#1a4a8a}
a{color:#1a5fd0}
.rs{color:#555;font-size:12px;margin-top:2px}
.ed{margin-top:4px;font-size:12px;color:#888}
.ed summary{cursor:pointer;color:#1a5fd0;display:inline-block;list-style:none}
.ed summary::before{content:'⋯ '}
.ed[open] summary{margin-bottom:3px}
.ed input{font-size:16px;padding:4px 6px;border:1px solid #ddd;border-radius:4px;background:#fff}
.ed .id{width:9em}.ed .sec{width:9em}.ed .nt{width:10em}
#bar{position:sticky;top:0;z-index:5;background:#fafaf7;padding:8px 0;
 border-bottom:2px solid #333;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#count{font-weight:700}
#count.over{color:#c33}
#chips{display:inline-flex;gap:4px}
#chips button{font-size:12px;padding:3px 10px;border-radius:14px;min-height:0}
#chips button.on{background:#2c6bed;color:#fff;border-color:#2c6bed}
button{font-size:15px;padding:7px 16px;border-radius:8px;border:1px solid #999;
 background:#fff;min-height:38px}
#go,#dgo{background:#2c6bed;color:#fff;border-color:#2c6bed}
button:disabled{opacity:.5}
#banner{background:#fff0f0;border:1px solid #e0a0a0;border-radius:8px;
 padding:8px 10px;margin:8px 0;font-size:13px}
#errbox{display:none;background:#fde8e8;border:1px solid #e0a0a0;color:#a02020;
 border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px;white-space:pre-wrap}
.kbd{color:#999;font-size:12px}
#dock{display:none}
@media (max-width:640px),(pointer:coarse){
 .kbd{display:none}
 body{padding-bottom:84px}
 #bar{padding:6px 0;gap:6px}
 #bar>button,#chips button{padding:6px 10px;font-size:13px;min-height:36px}
 #dock{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:9;
  background:#fffdf8;border-top:1px solid #ddd;padding:8px 12px;
  padding-bottom:calc(8px + env(safe-area-inset-bottom));gap:10px;align-items:center}
 #dcount{font-weight:700;font-size:13px}
 #dgo{flex:1;min-height:44px;font-size:16px}
}
"""

_JS = r"""
"use strict";
let cur=0,submitting=false,dirty=false,submitted=false;
const DEF={};
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
const escH=s=>String(s).replace(/[&<>"']/g,
 c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function rows(){return $$('.row')}
function paint(){rows().forEach((r,i)=>r.classList.toggle('cur',i===cur));
 const r=rows()[cur];if(r)r.scrollIntoView({block:'nearest'});}
function upd(){
 let k=0;const sel=[];
 rows().forEach(r=>{const c=r.querySelector('.cb');r.classList.remove('over');
  if(c.checked){k++;sel.push(r);}});
 sel.slice(MAX).forEach(r=>r.classList.add('over'));
 const over=Math.max(0,k-MAX);
 const txt=over?`已选 ${k} · 上限 ${MAX} · 末尾 ${over} 条将被丢弃`
               :`已选 ${k} / ${N} 保留`;
 const ce=$('#count');ce.textContent=txt;ce.classList.toggle('over',over>0);
 const dc=$('#dcount');if(dc)dc.textContent=txt;
 $$('h2').forEach(h=>{const s=h.dataset.sec;
  const rs=rows().filter(r=>r.dataset.sec===s);
  h.querySelector('.scnt b').textContent=
   rs.reduce((a,r)=>a+(r.querySelector('.cb').checked?1:0),0);
  h.querySelector('.scnt i').textContent=rs.length;});
}
function saveDraft(){try{
 const o={checked:rows().filter(r=>r.querySelector('.cb').checked)
   .map(r=>r.dataset.key),edits:{}};
 rows().forEach(r=>{const k=r.dataset.key,d=DEF[k];
  const e={id:r.querySelector('.id').value,
   section:r.querySelector('.sec').value,note:r.querySelector('.nt').value};
  if(d&&(e.id!==d.id||e.section!==d.sec||e.note!==d.nt))o.edits[k]=e;});
 localStorage.setItem(DK,JSON.stringify(o));}catch(e){}}
function mark(){dirty=true;upd();saveDraft();}
function applyAll(v){rows().forEach(r=>r.querySelector('.cb').checked=v);mark();}
function askClear(){if(confirm('清空全部勾选？'))applyAll(false);}
function err(m){const e=$('#errbox');e.style.display='block';e.textContent=m;
 e.scrollIntoView({block:'center'});}
function resultPage(d){
 let h='<div style="max-width:680px;margin:40px auto;padding:0 14px">';
 h+='<h1>✓ 已提交</h1>';
 h+=`<p>保留 <b>${d.kept}</b> 条 · 丢弃 ${d.dropped} 条</p>`;
 const bs=Object.entries(d.by_section||{}).map(([s,n])=>`${escH(s)} ${n}`).join(' · ');
 if(bs)h+=`<p>分区：${bs}</p>`;
 if(d.no_items)h+='<p style="color:#a33"><b>kept=0 —— 本期按 §11 走停刊（no_items）路径</b></p>';
 if((d.truncated_keys||[]).length){
  h+=`<p style="color:#a33">超过上限，末尾 ${d.truncated_keys.length} 条被截断丢弃：</p><ul>`;
  (d.truncated_titles||[]).forEach(t=>{h+=`<li>${escH(t)}</li>`;});h+='</ul>';}
 if((d.unknown_keys||[]).length)
  h+=`<p>未知 key（已忽略）：${d.unknown_keys.map(escH).join(', ')}</p>`;
 if((d.warnings||[]).length)
  h+=`<p>警告：${d.warnings.map(escH).join('；')}</p>`;
 h+='<p style="color:#888">服务器已关闭，改主意请重跑 <code>just pick</code>。</p></div>';
 document.body.innerHTML=h;}
async function submit(){
 if(submitting||submitted)return;
 const sel=rows().filter(r=>r.querySelector('.cb').checked);
 const over=sel.slice(MAX);
 const body={t:TOKEN,kept:sel.map(r=>({item_key:r.dataset.key,
  id:r.querySelector('.id').value.trim(),
  section:r.querySelector('.sec').value.trim(),
  note:r.querySelector('.nt').value||null}))};
 if(sel.length===0){
  if(!confirm('一条都没选——将写入空 kept，本期停刊（no_items），确认？'))return;
  body.confirm_empty=true;
 }else{
  let msg=`保留 ${sel.length} 条 · 丢弃 ${N-sel.length} 条。`;
  if(over.length)msg+=`\n\n超过上限 ${MAX} 条：末尾 ${over.length} 条将被截断丢弃——\n`
   +over.map(r=>'· '+r.querySelector('.t').textContent.trim()).join('\n');
  if(!confirm(msg))return;
 }
 submitting=true;$('#go').disabled=true;const dg=$('#dgo');if(dg)dg.disabled=true;
 try{
  const r=await fetch('/decide',{method:'POST',
   headers:{'Content-Type':'application/json','X-Review-Token':TOKEN},
   body:JSON.stringify(body)});
  const d=await r.json().catch(()=>({ok:false,error:'HTTP '+r.status+'（响应非 JSON）'}));
  if(!r.ok||!d.ok){
   let m='提交失败：'+(d.error||('HTTP '+r.status));
   if(d.lint)m+='\n'+d.lint.join('\n');
   err(m);return;
  }
  submitted=true;dirty=false;try{localStorage.removeItem(DK)}catch(e){}
  resultPage(d);
 }catch(e){err('提交失败（网络）：'+e);}
 finally{submitting=false;const g=$('#go');if(g)g.disabled=false;
  const g2=$('#dgo');if(g2)g2.disabled=false;}
}
document.addEventListener('change',e=>{
 if(e.target.closest('.row'))mark();});
document.addEventListener('input',e=>{
 if(e.target.closest('.row'))mark();});
$$('.bd').forEach(b=>b.addEventListener('click',e=>{
 if(e.target.closest('input,a,button,summary,.ed,.s'))return;
 const c=b.parentElement.querySelector('.cb');c.checked=!c.checked;mark();}));
$$('.s').forEach(s=>s.addEventListener('click',()=>s.classList.toggle('x')));
$$('#chips button').forEach(b=>b.addEventListener('click',()=>{
 $$('#chips button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const f=b.dataset.f;
 rows().forEach(r=>{const c=r.querySelector('.cb');
  r.style.display=(f==='all'||(f==='maybe'&&r.classList.contains('maybe'))
   ||(f==='sel'&&c.checked)||(f==='unsel'&&!c.checked))?'':'none';});
 $$('h2').forEach(h=>{const s=h.dataset.sec;
  h.style.display=rows().some(r=>r.dataset.sec===s
   &&r.style.display!=='none')?'':'none';});}));
document.addEventListener('keydown',e=>{
 if(/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)){
  if(e.key==='Enter')e.target.blur();return;}
 if(e.key==='j'||e.key==='ArrowDown'){cur=Math.min(cur+1,N-1);paint();}
 else if(e.key==='k'||e.key==='ArrowUp'){cur=Math.max(cur-1,0);paint();}
 else if(e.key==='x'||e.key===' '){const c=$('#c'+cur);
  if(c){c.checked=!c.checked;mark();}e.preventDefault();}
 else if(e.key==='a'){const v=!rows().every(r=>r.querySelector('.cb').checked);
  if(confirm(v?('全选全部 '+N+' 条？'):'清空全部勾选？'))applyAll(v);}
 else if(e.key==='Enter'){submit();}
});
window.addEventListener('beforeunload',e=>{
 if(dirty&&!submitted){e.preventDefault();e.returnValue='';}});
// ---- init：DEF 基线（含 PREV 预填）→ draft 覆盖（草稿 > already.kept > recommend）
rows().forEach(r=>{const k=r.dataset.key;
 DEF[k]={id:r.querySelector('.id').value,sec:r.querySelector('.sec').value,
  nt:r.querySelector('.nt').value};});
let draft=null;try{draft=JSON.parse(localStorage.getItem(DK)||'null');}catch(e){}
if(draft&&Array.isArray(draft.checked)){
 const set=new Set(draft.checked);
 rows().forEach(r=>{const k=r.dataset.key;
  r.querySelector('.cb').checked=set.has(k);
  const ed=(draft.edits||{})[k];
  if(ed){r.querySelector('.id').value=ed.id;
   r.querySelector('.sec').value=ed.section;
   r.querySelector('.nt').value=ed.note||'';
   r.querySelector('.ed').open=true;}});
 dirty=true;}
function fixBar(){document.documentElement.style.setProperty('--barh',
 $('#bar').offsetHeight+'px');}
window.addEventListener('resize',fixBar);fixBar();
upd();paint();
"""


def _vocab() -> list:
    try:
        from lib.prompts import SECTION_VOCAB
        return SECTION_VOCAB
    except Exception:
        return []


def page(env: dict, already: dict | None, token: str) -> bytes:
    """渲染勾选页；坏行跳过、字段全 .get。异常由 do_GET 兜底成 500 提示页。"""
    cands = [c for c in env.get("candidates", []) if isinstance(c, dict)]
    cfg = env.get("config") or {}
    ep = env.get("episode", "?")
    max_items = cfg.get("max_items", 20)
    deadline = cfg.get("gate1_deadline", "08:30")

    vocab = _vocab()
    sec_names = {slug: name for slug, name in vocab}
    sec_options = "".join(f'<option value="{esc(sl)}">{esc(nm)}</option>'
                          for sl, nm in vocab)

    # 已提交过的 kept -> 预填（勾选/id/分区/批注），优先级低于 localStorage 草稿
    prev: dict[str, dict] = {}
    if isinstance(already, dict):
        for k in already.get("kept") or []:
            if isinstance(k, dict) and k.get("item_key"):
                prev[str(k["item_key"])] = k

    # 同 cluster 有 >1 候选时才显示 dup 徽标（fresh 单例是纯噪音）
    cluster_n = Counter((c.get("dedup") or {}).get("cluster_id")
                        for c in cands if isinstance(c.get("dedup"), dict))
    cluster_n.pop(None, None)

    rows, last_sec, n_rows = [], None, 0
    for c in cands:
        key = str(c.get("item_key") or "")
        if not key or "title_zh" not in c:
            continue
        sec = str(c.get("section") or "misc")
        if sec != last_sec:
            label = sec_names.get(sec, sec)
            sub = f' <small class=sl>{esc(sec)}</small>' if sec in sec_names else ""
            rows.append(f'<h2 data-sec="{esc(sec)}">{esc(label)}{sub}'
                        f' <span class=scnt>已选 <b>0</b>/<i>0</i></span></h2>')
            last_sec = sec
        p = prev.get(key) or {}
        badges = []
        day = _mmdd(c)
        if day:
            badges.append(f"<span class='b day'>{day}</span>")
        if isinstance(c.get("news_value"), (int, float)):
            badges.append(f"<span class=b>价值 {c['news_value']:.1f}</span>")
        if isinstance(c.get("ai_relevance"), (int, float)):
            badges.append(f"<span class=b>相关 {c['ai_relevance']:.1f}</span>")
        gray = bool(c.get("gray")) or c.get("filter_verdict") == "review"
        if gray:
            badges.append("<span class='b gray'>待定</span>")
        d = c.get("dedup") or {}
        dv = d.get("verdict")
        if d.get("cluster_id") is not None and (
                dv in ("reissue", "gray", "gray_pending")
                or cluster_n.get(d["cluster_id"], 0) > 1):
            mc = (f" ρ{d['match_cos']:.2f}"
                  if isinstance(d.get("match_cos"), (int, float)) else "")
            badges.append(f"<span class=b>dup:{esc(dv)}#{d['cluster_id']}{mc}</span>")
        if dv == "reissue":
            badges.append("<span class='b re'>更新</span>")
        reasons = " · ".join(esc(r) for r in (c.get("reasons") or [])[:4])
        url = str(c.get("url") or "")
        src_html = (f'<a href="{esc(url)}" target=_blank rel=noopener>'
                    f'{esc(c.get("source"))}</a>' if re.match(r"https?://", url)
                    else f"<span class=src>{esc(c.get('source'))}</span>")
        checked = "checked" if (key in prev if prev else c.get("recommend")) else ""
        edited = bool(p) and (p.get("id") != c.get("id")
                              or p.get("section") != c.get("section")
                              or p.get("note"))
        rs_html = f'<div class=rs>▸ {reasons}</div>' if reasons else ""
        rows.append(f"""
<div class="row{' maybe' if gray else ''}" data-key="{esc(key)}" data-sec="{esc(sec)}" id=r{n_rows}>
 <input type=checkbox class=cb id=c{n_rows} aria-label="{esc(c.get('title_zh'))}" {checked}>
 <div class=bd>
  <div class=t>{esc(c.get('title_zh'))}</div>
  <div class=s>{esc(c.get('summary'))}</div>
  <div class=meta>{' '.join(badges)} {src_html}</div>
  {rs_html}
  <details class=ed{' open' if edited else ''}><summary>编辑</summary><div>
   id <input class=id value="{esc(p.get('id') or c.get('id'))}" maxlength=24>
   分区 <input class=sec value="{esc(p.get('section') or c.get('section'))}" maxlength=24 list=seclist>
   <input class=nt placeholder="批注" maxlength=60 value="{esc(p.get('note') or '')}"></div></details>
 </div>
</div>""")
        n_rows += 1

    banner = ""
    if already:
        banner = (f"<div id=banner>已提交过（decided_by={esc(already.get('decided_by'))} "
                  f"{esc(already.get('decided_at'))}，kept={len(already.get('kept') or [])}）"
                  f"— 勾选已按上次决定预填；再次提交将覆盖</div>")

    consts = (f"const TOKEN={json.dumps(token)},MAX={int(max_items)},"
              f"N={n_rows},EP={json.dumps(str(ep))},DK='review:'+EP;")
    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>AI早报 {esc(ep)} · 人工筛选</title>
<style>{_CSS}</style>
<div id=bar><b>人工筛选 · {esc(ep)}</b><span id=count></span>
<span id=chips><button data-f=all class=on>全部</button><button data-f=maybe>待定</button><button data-f=sel>已选</button><button data-f=unsel>未选</button></span>
<button id=go onclick="submit()">提交 (Enter)</button>
<button onclick="applyAll(true)">全选</button><button onclick="askClear()">全不选</button>
<span class=kbd>≤{max_items} 条 · 死线 {esc(deadline)} · j/k 移动 x/空格 勾 a 全切</span></div>
{banner}
<div id=errbox></div>
<div id=list>{''.join(rows)}</div>
<datalist id=seclist>{sec_options}</datalist>
<div id=dock><span id=dcount></span><button id=dgo onclick="submit()">提交</button></div>
<script>{consts}</script>
<script>{_JS}</script>""".encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(30)  # 半开/慢客户端不再吊死线程
        except OSError:
            pass

    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _tok(self) -> str | None:
        """query ?t= / X-Review-Token header / body.t（POST 已解析时由调用方传入）。"""
        q = parse_qs(urlparse(self.path).query)
        return q.get("t", [None])[0] or self.headers.get("X-Review-Token")

    def _tok_ok(self, body_tok=None) -> bool:
        tok = self.server.token
        if not tok:
            return True
        cand = self._tok() or body_tok
        return isinstance(cand, str) and cand == tok

    def _refresh(self):
        """40_selected 每次 GET/POST 重读（banner 实时）；candidates mtime 变了就重载 env。"""
        srv = self.server
        sel = srv.run_dir / gs.SEL_NAME
        if sel.exists():
            try:
                srv.already = json.loads(sel.read_text(encoding="utf-8"))
            except Exception:
                pass
        cp = srv.run_dir / gs.CAND_NAME
        try:
            mt = cp.stat().st_mtime
        except OSError:
            return
        if mt != srv.cand_mtime:
            try:
                env = json.loads(cp.read_text(encoding="utf-8"))
            except Exception:
                return
            if valid_env(env):  # 坏文件不顶掉手上这份
                srv.env = env
                srv.cand_mtime = mt

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._refresh()
        if path == "/api/candidates":
            if not self._tok_ok():
                self._send_json(403, {"ok": False, "error": "bad token"})
                return
            if self.server.env is None:
                self._send_json(409, {"ok": False, "error": "no candidates"})
                return
            self._send_json(200, self.server.env)
            return
        if path != "/":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if self.server.env is None:
            self._send(500, ("<h1>40_candidates.json 不存在或结构不合格</h1><p>先跑 "
                             "<code>just pick</code> / <code>gate_select.py --prepare</code></p>"
                             ).encode())
            return
        if not self._tok_ok():
            self._send(403, ("<h1>403</h1><p>缺 token —— 用终端打印的完整 URL"
                             "（含 ?t=…）打开</p>").encode())
            return
        try:
            self._send(200, page(self.server.env, self.server.already,
                                 self.server.token))
        except Exception as e:
            self._send(500, f"<h1>渲染失败</h1><pre>{esc(e)}</pre>".encode())

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/decide":
            self._send_json(404, {"ok": False, "error": "unknown route"})
            return
        # ---- body 读取守卫：缺/非数/≤0 → 400；>256KB → 413；短读 → 400 ----
        cl = self.headers.get("Content-Length")
        if cl is None:
            self._send_json(400, {"ok": False, "error": "missing Content-Length"})
            return
        try:
            n = int(cl)
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "bad Content-Length"})
            return
        if n <= 0:
            self._send_json(400, {"ok": False, "error": "empty body"})
            return
        if n > MAX_BODY:
            self._send_json(413, {"ok": False, "error": "body too large"})
            return
        try:
            raw = self.rfile.read(n)
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"read failed: {e}"})
            return
        if len(raw) < n:
            self._send_json(400, {"ok": False, "error": "short body"})
            return
        try:
            data = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad json: {e}"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "body must be an object"})
            return
        # ---- token：LAN 上无 token 的 POST 一律 403 ----
        body_tok = data.get("t") if isinstance(data.get("t"), str) else None
        if not self._tok_ok(body_tok):
            self._send_json(403, {"ok": False, "error": "bad token"})
            return
        self._refresh()
        env = self.server.env
        if env is None:
            self._send_json(409, {"ok": False, "error": "no candidates"})
            return
        # ---- decide-once：0.8s 关服窗口内的并发/重试不再静默覆盖 ----
        force = parse_qs(u.query).get("force", [""])[0] == "1"
        if self.server.decided and not force:
            self._send_json(409, {"ok": False, "already": self.server.decided,
                                  "error": "already decided; resubmit with ?force=1"})
            return
        kept_in = data.get("kept", [])
        if not isinstance(kept_in, list):
            self._send_json(400, {"ok": False, "error": "kept must be a list"})
            return
        if not kept_in and data.get("confirm_empty") is not True:
            self._send_json(409, {"ok": False,
                                  "error": "empty kept: pass confirm_empty:true"})
            return

        bykey = {str(c["item_key"]): c for c in env.get("candidates", [])
                 if isinstance(c, dict) and c.get("item_key")}
        max_items = int((env.get("config") or {}).get("max_items") or 20)
        kept, unknown = [], []
        taken: set[str] = set()
        seen: set[str] = set()
        for e in kept_in:
            if isinstance(e, str):
                e = {"item_key": e}
            if not isinstance(e, dict):
                continue
            key = str(e.get("item_key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            cand = bykey.get(key)
            if cand is None:
                unknown.append(key)
                continue
            k = dict(cand)
            slug = gs.slugify_id(str(e.get("id") or cand.get("id") or ""), key)
            if not gs.SLUG_RE.match(slug):
                slug = gs.slugify_id(str(cand.get("id") or ""), key)
            k["id"] = gs.unique_slug(slug, key, taken)
            # 分区全程 slug：vocab 中文名映射 -> slugify -> 'misc'
            k["section"] = gs.section_slug(e.get("section") or cand.get("section"),
                                           key)
            note = e.get("note")
            k["note"] = (str(note).strip()[:60] or None) if note is not None else None
            kept.append(k)
        if not kept and data.get("confirm_empty") is not True:
            self._send_json(409, {"ok": False,
                                  "error": "kept empty after dropping unknown keys; "
                                           "pass confirm_empty:true to force 停刊"})
            return
        truncated_keys, truncated_titles = [], []
        dropped, dropped_keys = [], set()
        overflow = kept[max_items:]
        if overflow:
            kept = kept[:max_items]
            for c in overflow:
                c["_drop_reason"] = "over_max_items"
                truncated_keys.append(c["item_key"])
                truncated_titles.append(str(c.get("title_zh") or c["item_key"]))
                dropped_keys.add(c["item_key"])
            dropped.extend(overflow)
        kept_keys = {k["item_key"] for k in kept}
        for c in env.get("candidates", []):
            if not isinstance(c, dict):
                continue
            ck = c.get("item_key")
            if ck and ck not in kept_keys and ck not in dropped_keys:
                dropped.append(c)
                dropped_keys.add(ck)
        by_section: dict[str, int] = {}
        for k in kept:
            s = str(k.get("section") or "misc")
            by_section[s] = by_section.get(s, 0) + 1

        # 先校验后写：契约错误一律不落盘（唯一例外 = confirm_empty 的 §11 主动停刊）
        doc = self._build_doc(kept, dropped)
        errs = gs.lint_selected(doc) + gs.pydantic_validate(doc)
        confirmed_empty = not kept and data.get("confirm_empty") is True
        if errs and not confirmed_empty:
            self._send_json(422, {"ok": False, "lint": errs,
                                  "error": "doc failed validation; not written"})
            return
        try:
            with meta.run_lock(self.server.run_dir):
                meta.atomic_write(self.server.run_dir / gs.SEL_NAME, doc)
                meta.stage_done(self.server.run_dir, "gate_select", gs.SEL_NAME,
                                status="done", extra={"decided_by": "human",
                                                      "n_kept": len(kept)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"write failed: {e}"})
            return

        self.server.decided = {"decided_at": doc["decided_at"],
                               "n_kept": len(kept)}
        self.server.already = doc
        resp = {"ok": True, "kept": len(kept), "dropped": len(dropped),
                "by_section": by_section, "unknown_keys": unknown,
                "truncated_keys": truncated_keys,
                "truncated_titles": truncated_titles,
                "no_items": not kept, "warnings": errs}
        print(f"[review_server] decide {self.client_address[0]}: "
              f"kept={len(kept)} dropped={len(dropped)}"
              + (f" unknown={unknown}" if unknown else "")
              + (f" truncated={len(truncated_keys)}" if truncated_keys else "")
              + (f" warnings={errs}" if errs else ""), file=sys.stderr)
        try:
            self._send_json(200, resp)
        finally:
            # BrokenPipe 也要关服——写盘已成功，服务器不能变僵尸
            threading.Timer(0.8, self.server.shutdown).start()

    def _build_doc(self, kept, dropped) -> dict:
        """episode 与 gate_select.write_selected 一致取 run_dir.name；
        非日期名的 dev 拷贝目录回退 env.episode（否则 lint 必炸，没法测）。"""
        rd_name = self.server.run_dir.name
        episode = (rd_name if gs.DATE_RE.match(rd_name)
                   else (self.server.env or {}).get("episode") or rd_name)
        return {
            "schema": "selected/1",
            "episode": episode,
            "decided_at": gs.now_iso(),
            "decided_by": "human",
            "kept": [{"item_key": c["item_key"], "id": c["id"],
                      "section": c["section"], "note": c.get("note")}
                     for c in kept],
            "dropped": [{"item_key": c["item_key"],
                         "reason": c.get("_drop_reason") or "unchecked"}
                        for c in dropped],
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人工闸 1 勾选 UI（PLAN §7.3）")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--port", type=int,
                    default=int(environ.get("REVIEW_PORT", "8923")))
    args = ap.parse_args(argv)
    run_dir = gs.resolve_run_dir(args.run_dir)

    cand_path = run_dir / gs.CAND_NAME
    env, cand_mtime = None, None
    if cand_path.exists():
        try:
            env = json.loads(cand_path.read_text(encoding="utf-8"))
            cand_mtime = cand_path.stat().st_mtime
        except Exception as e:
            print(f"[review_server] {gs.CAND_NAME} 解析失败: {e}", file=sys.stderr)
        if env is not None and not valid_env(env):
            print(f"[review_server] {gs.CAND_NAME} 结构不合格"
                  f"（每行须含 {'/'.join(REQUIRED_CAND_KEYS)}）", file=sys.stderr)
            env = None
    else:
        print(f"[review_server] 缺 {gs.CAND_NAME} — 先跑 `just pick`/--prepare",
              file=sys.stderr)

    already = None
    sel = run_dir / gs.SEL_NAME
    if sel.exists():
        try:
            already = json.loads(sel.read_text(encoding="utf-8"))
        except Exception:
            pass

    token = secrets.token_urlsafe(8)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    try:
        srv = socketserver.ThreadingTCPServer(("0.0.0.0", args.port), Handler)
    except OSError as e:
        print(f"[review_server] 端口 {args.port} 绑定失败 ({e})"
              f" — 端口被占？换 --port 或 REVIEW_PORT", file=sys.stderr)
        return 2
    with srv:
        srv.env = env
        srv.run_dir = run_dir
        srv.already = already
        srv.token = token
        srv.decided = None
        srv.cand_mtime = cand_mtime
        print(f"[review_server] http://127.0.0.1:{args.port}/?t={token} "
              f"(局域网 http://<本机IP>:{args.port}/?t={token}) — {run_dir}",
              file=sys.stderr)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    print("[review_server] bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
