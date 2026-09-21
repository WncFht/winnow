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
绑定 0.0.0.0 —— 验收要求局域网手机可开。
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import socketserver
import sys
import threading
from os import environ
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> contracts/adapters
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))     # stages/ -> gate_select

from lib import meta
import gate_select as gs  # slugify_id/unique_slug/lint_selected/write_selected/now_iso


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def page(env: dict, already: dict | None) -> bytes:
    cands = env.get("candidates", [])
    cfg = env.get("config") or {}
    ep = env.get("episode", "?")
    max_items = cfg.get("max_items", 20)
    deadline = cfg.get("gate1_deadline", "08:30")

    rows, last_sec = [], None
    for n, c in enumerate(cands):
        if c["section"] != last_sec:
            rows.append(f"<h2>{esc(c['section'])}</h2>")
            last_sec = c["section"]
        badges = []
        if isinstance(c.get("news_value"), (int, float)):
            badges.append(f"<span class=b>nv {c['news_value']:g}</span>")
        if isinstance(c.get("ai_relevance"), (int, float)):
            badges.append(f"<span class=b>ai {c['ai_relevance']:.2f}</span>")
        if c.get("gray") or c.get("filter_verdict") == "review":
            badges.append("<span class='b gray'>灰区</span>")
        d = c.get("dedup") or {}
        if d.get("cluster_id") is not None:
            mc = f" ρ{d['match_cos']:.2f}" if isinstance(d.get("match_cos"), (int, float)) else ""
            badges.append(f"<span class=b>dup:{esc(d.get('verdict'))}#{d['cluster_id']}{mc}</span>")
        if d.get("verdict") == "reissue":
            badges.append("<span class='b re'>更新</span>")
        reasons = " · ".join(esc(r) for r in (c.get("reasons") or [])[:4])
        rows.append(f"""
<div class=row data-key="{esc(c['item_key'])}" id=r{n}>
 <input type=checkbox class=cb id=c{n} {'checked' if c.get('recommend') else ''}>
 <div class=bd>
  <div class=t>{esc(c['title_zh'])}</div>
  <div class=s>{esc(c['summary'])}</div>
  <div class=meta>{' '.join(badges)} <span class=src>{esc(c['source'])}</span>
   <a href="{esc(c['url'])}" target=_blank rel=noopener>源</a>
   <span class=rs>{reasons}</span></div>
  <div class=ed>id <input class=id value="{esc(c['id'])}" maxlength=24>
   分区 <input class=sec value="{esc(c['section'])}" maxlength=12>
   注 <input class=nt placeholder="可选" maxlength=60></div>
 </div>
</div>""")

    banner = ""
    if already:
        banner = (f"<div id=banner>已提交过（decided_by={esc(already.get('decided_by'))} "
                  f"{esc(already.get('decided_at'))}，kept={len(already.get('kept') or [])}）"
                  f"— 再次提交将覆盖</div>")

    items_js = json.dumps(
        [{"key": c["item_key"], "recommend": bool(c.get("recommend"))} for c in cands],
        ensure_ascii=False)

    return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>AI早报 {esc(ep)} · 人工筛选</title>
<style>
*{{box-sizing:border-box}}
body{{font:15px/1.55 system-ui,sans-serif;max-width:920px;margin:10px auto;padding:0 10px 90px;background:#fafaf7;color:#222}}
h2{{font-size:15px;margin:16px 0 4px;color:#8a5a00;border-bottom:1px solid #e5ddc8;padding-bottom:2px}}
.row{{display:flex;gap:9px;padding:9px 6px;border-radius:8px;align-items:flex-start;border-bottom:1px solid #f0ead8}}
.row.cur{{background:#fff3d6;outline:1px solid #e8c96a}}
.cb{{transform:scale(1.7);margin:8px 4px 0 2px;flex:none}}
.bd{{min-width:0;flex:1}}
.t{{font-weight:650;font-size:15px}}
.s{{color:#555;font-size:13px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.meta{{font-size:12px;color:#888;margin-top:2px}}
.b{{display:inline-block;background:#eee7d2;border-radius:4px;padding:0 5px;margin-right:4px}}
.b.gray{{background:#f6d8d8;color:#8a2a2a}}
.b.re{{background:#d8e8f6;color:#1a4a8a}}
a{{color:#1a5fd0}}
.rs{{color:#999}}
.ed{{margin-top:4px;font-size:12px;color:#888}}
.ed input{{font-size:12px;padding:2px 4px;border:1px solid #ddd;border-radius:4px;background:#fff}}
.ed .id{{width:9em}} .ed .sec{{width:7em}} .ed .nt{{width:9em}}
#bar{{position:sticky;top:0;z-index:5;background:#fafaf7;padding:9px 0;border-bottom:2px solid #333;
 display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
#count{{font-weight:700}}
button{{font-size:15px;padding:7px 16px;border-radius:8px;border:1px solid #999;background:#fff}}
#go{{background:#2c6bed;color:#fff;border-color:#2c6bed}}
#banner{{background:#fff0f0;border:1px solid #e0a0a0;border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px}}
.kbd{{color:#999;font-size:12px}}
</style>
<div id=bar><b>人工筛选 · {esc(ep)}</b><span id=count></span>
<button id=go onclick="submit()">提交 (Enter)</button>
<button onclick="setAll(true)">全选</button><button onclick="setAll(false)">全不选</button>
<span class=kbd>≤{max_items} 条 · 死线 {esc(deadline)} · j/k 移动 x/空格 勾 a 全切</span></div>
{banner}
<div id=list>{''.join(rows)}</div>
<script>
const ITEMS = {items_js};
const N = ITEMS.length;
let cur = 0;
function rows(){{return [...document.querySelectorAll('.row')]}}
function upd(){{let k=rows().filter(r=>r.querySelector('.cb').checked).length;
 document.getElementById('count').textContent = `${{k}} / ${{N}} 保留`;}}
function paint(){{rows().forEach((r,i)=>r.classList.toggle('cur',i===cur));
 const r=rows()[cur]; if(r) r.scrollIntoView({{block:'nearest'}});}}
function setAll(v){{rows().forEach(r=>r.querySelector('.cb').checked=v);upd();}}
function submit(){{
 const kept=rows().filter(r=>r.querySelector('.cb').checked).map(r=>({{
  item_key:r.dataset.key,
  id:r.querySelector('.id').value,
  section:r.querySelector('.sec').value,
  note:r.querySelector('.nt').value||null}}));
 fetch('/decide',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{kept}})}})
 .then(r=>r.json()).then(d=>{{
  document.body.innerHTML='<h1 style="margin:60px;text-align:center">saved '+d.kept
   +' kept / '+d.dropped+' dropped'+(d.warnings&&d.warnings.length?'<br><small>'+d.warnings.join('; ')+'</small>':'')+'</h1>';}})
 .catch(e=>alert('提交失败: '+e));}}
document.querySelectorAll('.cb').forEach(c=>c.addEventListener('change',upd));
document.addEventListener('keydown', e=>{{
 if(/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) {{
  if(e.key==='Enter') e.target.blur(); return; }}
 if(e.key==='j'||e.key==='ArrowDown'){{cur=Math.min(cur+1,N-1);paint()}}
 else if(e.key==='k'||e.key==='ArrowUp'){{cur=Math.max(cur-1,0);paint()}}
 else if(e.key==='x'||e.key===' '){{const c=document.getElementById('c'+cur);
  if(c){{c.checked=!c.checked;upd()}} e.preventDefault()}}
 else if(e.key==='a'){{setAll(!rows().every(r=>r.querySelector('.cb').checked))}}
 else if(e.key==='Enter'){{submit()}}
}});
upd();
</script>""".encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        env = self.server.env
        if env is None:
            self._send(500, ("<h1>40_candidates.json 不存在</h1><p>先跑 "
                             "<code>just pick</code> / <code>gate_select.py --prepare</code></p>"
                             ).encode())
            return
        if self.path == "/api/candidates":
            self._send_json(200, env)
            return
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._send(200, page(env, self.server.already))

    def do_POST(self):
        if self.path.split("?")[0] != "/decide":
            self._send_json(404, {"ok": False, "error": "unknown route"})
            return
        env = self.server.env
        if env is None:
            self._send_json(409, {"ok": False, "error": "no candidates"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad json: {e}"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "body must be an object"})
            return
        kept_in = data.get("kept", [])
        if not isinstance(kept_in, list):
            self._send_json(400, {"ok": False, "error": "kept must be a list"})
            return

        bykey = {c["item_key"]: c for c in env.get("candidates", [])}
        max_items = int((env.get("config") or {}).get("max_items") or 20)
        kept, dropped, unknown = [], [], []
        taken: set[str] = set()
        seen: set[str] = set()
        for e in kept_in:
            if isinstance(e, str):
                e = {"item_key": e}
            if not isinstance(e, dict):
                continue
            key = str(e.get("item_key") or "")
            if key in seen:
                continue
            seen.add(key)
            cand = bykey.get(key)
            if cand is None:
                unknown.append(key)
                continue
            k = dict(cand)
            slug = gs.slugify_id(str(e.get("id") or cand["id"]), key)
            if not gs.SLUG_RE.match(slug):
                slug = cand["id"]
            k["id"] = gs.unique_slug(slug, key, taken)
            sec = str(e.get("section") or cand["section"] or "其他").strip()
            k["section"] = sec or "其他"
            note = e.get("note")
            k["note"] = str(note).strip() or None if note else None
            kept.append(k)
        overflow = kept[max_items:]
        if overflow:
            kept = kept[:max_items]
            for c in overflow:
                c["_drop_reason"] = "over_max_items"
            dropped.extend(overflow)
        kept_keys = {k["item_key"] for k in kept}
        for c in env.get("candidates", []):
            if c["item_key"] not in kept_keys:
                dropped.append(c)

        try:
            with meta.run_lock(self.server.run_dir):
                doc = self._write(kept, dropped)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"write failed: {e}"})
            return

        warnings = gs.lint_selected(doc) + gs.pydantic_validate(doc)
        resp = {"ok": True, "kept": len(kept), "dropped": len(dropped),
                "unknown_keys": unknown, "warnings": warnings}
        print(f"[review_server] decide: kept={len(kept)} dropped={len(dropped)}"
              + (f" unknown={unknown}" if unknown else "")
              + (f" warnings={warnings}" if warnings else ""), file=sys.stderr)
        self._send_json(200, resp)
        threading.Timer(0.8, self.server.shutdown).start()

    def _write(self, kept, dropped) -> dict:
        doc = {
            "schema": "selected/1",
            "episode": self.server.env.get("episode") or self.server.run_dir.name,
            "decided_at": gs.now_iso(),
            "decided_by": "human",
            "kept": [{"item_key": c["item_key"], "id": c["id"],
                      "section": c["section"], "note": c.get("note")} for c in kept],
            "dropped": [{"item_key": c["item_key"],
                         "reason": c.get("_drop_reason") or "unchecked"}
                        for c in dropped],
        }
        meta.atomic_write(self.server.run_dir / gs.SEL_NAME, doc)
        meta.stage_done(self.server.run_dir, "gate_select", gs.SEL_NAME,
                        status="done", extra={"decided_by": "human",
                                              "n_kept": len(kept)})
        return doc

    def log_message(self, *a):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人工闸 1 勾选 UI（PLAN §7.3）")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--port", type=int,
                    default=int(environ.get("REVIEW_PORT", "8923")))
    args = ap.parse_args(argv)
    run_dir = gs.resolve_run_dir(args.run_dir)

    cand_path = run_dir / gs.CAND_NAME
    env = None
    if cand_path.exists():
        try:
            env = json.loads(cand_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[review_server] {gs.CAND_NAME} 解析失败: {e}", file=sys.stderr)
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

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", args.port), Handler) as srv:
        srv.env = env
        srv.run_dir = run_dir
        srv.already = already
        print(f"[review_server] http://127.0.0.1:{args.port} "
              f"(局域网 http://<本机IP>:{args.port}) — {run_dir}", file=sys.stderr)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    print("[review_server] bye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
