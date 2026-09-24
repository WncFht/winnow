"""stages/lib/sources/api.py — json_api 源适配器 + 通用 JSON walker。

每个 adapter: fn(data, src, ctx) -> (partial_items, extra_meta|None)
partial item 键: title/url/date/summary/image/tags/guid —— mk_item 在
collect._finish_items 统一收口。data = 已解析 JSON（GET 拿到非 JSON →
调用方降级 html diff）。

API_ADAPTERS:         源名 → adapter(data, src, ctx)
SELF_FETCH_ADAPTERS:  自抓型 adapter，签名 (src, ctx)，先返 {"items","meta"}
REQUEST_SPECS:        json_api 但需 POST/特殊头的请求覆写（collect
                      fetch_with_failover 消费）
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import time
from urllib.parse import quote, urljoin, urlsplit

from stages.lib import http as lhttp, normalize
from stages.lib.sources.common import SEEN_URL_CAP


def _first_str(v) -> str | None:
    """取首个可用 str：list 取 [0]，dict 取 rendered/name/title。"""
    if isinstance(v, str):
        return v or None
    if isinstance(v, list) and v:
        return _first_str(v[0])
    if isinstance(v, dict):
        for k in ("rendered", "name", "title", "term", "url"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return None


def api_hn(d, src, ctx):
    out = []
    for h in d.get("hits") or []:
        url = h.get("url") or \
            f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({"title": h.get("title"), "url": url,
                    "date": normalize.parse_date_utc(h.get("created_at")),
                    "summary": normalize.strip_html(h.get("story_text")),
                    "guid": str(h.get("objectID") or ""),
                    "tags": ["points:%s" % h.get("points", 0)]})
    return out, None


def api_lobsters(d, src, ctx):
    out = []
    for it in d if isinstance(d, list) else []:
        out.append({"title": it.get("title"),
                    "url": it.get("url") or it.get("short_id_url")
                           or it.get("comments_url"),
                    "date": normalize.parse_date_utc(it.get("created_at")),
                    "summary": normalize.strip_html(it.get("description")),
                    "guid": it.get("short_id"),
                    "tags": [t for t in it.get("tags") or [] if t][:8]})
    return out, None


def api_github_search(d, src, ctx):
    out = []
    for it in d.get("items") or []:
        out.append({"title": it.get("full_name") or it.get("name"),
                    "url": it.get("html_url"),
                    "date": normalize.parse_date_utc(it.get("created_at")
                                        or it.get("pushed_at")),
                    "summary": it.get("description"),
                    "guid": str(it.get("id") or ""),
                    "image": (it.get("owner") or {}).get("avatar_url"),
                    "tags": [f"stars:{it.get('stargazers_count', 0)}"]})
    return out, None


def api_bilibili(d, src, ctx):
    out = []
    for it in ((d.get("data") or {}).get("archives") or []):
        bvid = it.get("bvid")
        out.append({"title": it.get("title"),
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid
                           else None,
                    "date": normalize.parse_date_utc(it.get("pubdate")),
                    "summary": it.get("desc"),
                    "guid": bvid or str(it.get("aid") or ""),
                    "image": ("https:" + it["pic"]) if str(
                        it.get("pic", "")).startswith("//") else it.get("pic")})
    return [o for o in out if o["url"]], None


def api_huggingface(d, src, ctx):
    out = []
    for it in d if isinstance(d, list) else []:
        p = it.get("paper") or {}
        pid = p.get("id") or it.get("id")
        out.append({"title": it.get("title") or p.get("title"),
                    "url": f"https://huggingface.co/papers/{pid}" if pid else None,
                    "date": normalize.parse_date_utc(it.get("publishedAt")
                                        or p.get("publishedAt")),
                    "summary": it.get("summary") or p.get("summary"),
                    "guid": str(pid or ""),
                    "image": it.get("thumbnail"),
                    "tags": [f"upvotes:{it.get('upvotes', p.get('upvotes', 0))}"]})
    return [o for o in out if o["url"]], None


def api_jiqizhixin(d, src, ctx):
    out = []
    for it in d.get("articles") or []:
        slug = it.get("slug")
        out.append({"title": it.get("title"),
                    "url": f"https://www.jiqizhixin.com/articles/{slug}"
                           if slug else None,
                    "date": normalize.parse_date_utc(it.get("publishedAt")),
                    "summary": it.get("content"),
                    "guid": it.get("id"),
                    "image": it.get("coverImageUrl"),
                    "tags": it.get("tagList") or []})
    return [o for o in out if o["url"]], None


def api_sspai(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        out.append({"title": it.get("morning_paper_title") or it.get("title"),
                    "url": f"https://sspai.com/post/{iid}" if iid else None,
                    "date": normalize.parse_date_utc(it.get("released_time")
                                        or it.get("created_time")),
                    "summary": it.get("summary"),
                    "guid": str(iid or ""),
                    "image": it.get("banner")})
    return [o for o in out if o["url"]], None


def api_tmtpost(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        img = it.get("thumb_image") or {}
        try:
            img = img["original"][0].get("url")
        except (KeyError, IndexError, TypeError):
            img = None
        out.append({"title": it.get("title"),
                    "url": it.get("short_url") or it.get("share_link"),
                    "date": normalize.parse_date_utc(it.get("time_published")),
                    "summary": it.get("summary"),
                    "guid": str(it.get("guid") or it.get("post_guid") or ""),
                    "image": img})
    return [o for o in out if o["url"]], None


def api_zhihu_col(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        tgt = it.get("target")
        if isinstance(tgt, dict):      # topstory hot-list failover 的包装层：
            it = tgt                   # 标题/url/created 都在 target 内
        aid = it.get("id")
        url = it.get("url") or ""
        if "/questions/" in url and aid:
            url = f"https://www.zhihu.com/question/{aid}"
        elif "api.zhihu.com" in url and aid:
            url = f"https://zhuanlan.zhihu.com/p/{aid}"
        elif aid and "zhihu.com" not in url:
            url = f"https://zhuanlan.zhihu.com/p/{aid}"
        out.append({"title": it.get("title"), "url": url or None,
                    "date": normalize.parse_date_utc(it.get("created")),
                    "summary": it.get("excerpt") or normalize.strip_html(it.get("content")),
                    "guid": str(aid or ""),
                    "image": it.get("image_url") or it.get("title_image")})
    return [o for o in out if o["url"]], None


def api_qwen(d, src, ctx):
    out = []
    arts = ((d.get("data") or {}).get("articles")
            or (d.get("data") or {}).get("list") or [])
    for it in arts:
        if not isinstance(it, dict):
            continue
        path = it.get("path") or it.get("slug")
        url = f"https://qwen.ai/blog/{path}" if path else it.get("url")
        extra = it.get("extra") or {}
        out.append({"title": it.get("title"), "url": url,
                    "date": normalize.parse_date_utc(extra.get("date") or it.get("date")),
                    "summary": extra.get("introduction")
                               or extra.get("description")
                               or normalize.strip_html(it.get("content"), 800),
                    "guid": str(it.get("id") or "")})
    return [o for o in out if o["url"]], None


def api_infoq(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        out.append({"title": it.get("article_title") or it.get("article_sharetitle"),
                    "url": f"https://www.infoq.cn/article/{it.get('uuid')}"
                           if it.get("uuid") else None,
                    "date": normalize.parse_date_utc(it.get("publish_time") or it.get("ctime")),
                    "summary": it.get("article_summary"),
                    "guid": str(it.get("uuid") or it.get("aid") or ""),
                    "image": it.get("article_cover")})
    return [o for o in out if o["url"]], None


def api_juejin(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        ai = (it or {}).get("article_info") or {}
        aid = ai.get("article_id") or it.get("article_id")
        out.append({"title": ai.get("title"),
                    "url": f"https://juejin.cn/post/{aid}" if aid else None,
                    "date": normalize.parse_date_utc(ai.get("ctime")),
                    "summary": ai.get("brief_content"),
                    "guid": str(aid or ""),
                    "image": ai.get("cover_image"),
                    "tags": [f"view:{ai.get('view_count', 0)}",
                             f"digg:{ai.get('digg_count', 0)}"]})
    return [o for o in out if o["url"]], None


def api_oschina(d, src, ctx):
    out = []
    res = d.get("result") or []
    if isinstance(res, dict):
        res = res.get("items") or res.get("list") or []
    for it in res:
        if not isinstance(it, dict):
            continue
        oid = it.get("obj_id") or it.get("id")
        url = it.get("url") or it.get("obj_url") or \
            (f"https://www.oschina.net/news/{oid}" if oid else None)
        out.append({"title": it.get("title") or it.get("obj_title"),
                    "url": url,
                    "date": normalize.parse_date_utc(it.get("time") or it.get("pub_time")
                                        or it.get("create_time")),
                    "summary": it.get("summary") or it.get("obj_summary"),
                    "guid": str(oid or "")})
    return [o for o in out if o["url"]], None


def api_alphaxiv(d, src, ctx):
    out = []
    papers = d.get("papers") or (d.get("data") or {}).get("papers") or []
    for it in papers:
        if not isinstance(it, dict):
            continue
        pid = it.get("canonical_id") or it.get("id")
        url = it.get("external_link") or \
            (f"https://www.alphaxiv.org/abs/{pid}" if pid else None)
        out.append({"title": it.get("title"), "url": url,
                    "date": normalize.parse_date_utc(it.get("publication_date")
                                        or it.get("first_publication_date")),
                    "summary": it.get("feed_description")
                               or it.get("paper_summary") or it.get("abstract"),
                    "guid": str(pid or ""),
                    "image": it.get("image_url"),
                    "tags": it.get("topics") or []})
    return [o for o in out if o["url"]], None


def api_zenodo(d, src, ctx):
    out = []
    for it in (d.get("data") or d.get("hits", {}).get("hits") or []):
        a = it.get("attributes") or it.get("metadata") or it
        doi = a.get("doi") or it.get("id")
        url = a.get("url") or (f"https://doi.org/{doi}" if doi else None)
        out.append({"title": _first_str(a.get("titles")) or a.get("title"),
                    "url": url,
                    "date": normalize.parse_date_utc(a.get("published") or a.get("created")
                                        or a.get("publication_date")),
                    "summary": normalize.strip_html(_first_str(a.get("descriptions"))
                                           or a.get("description")),
                    "guid": str(doi or it.get("id") or "")})
    return [o for o in out if o["url"]], None


def api_openrouter(d, src, ctx):
    """模型目录 diff —— 标 signal 只发新增（见 §5.2 signal 语义）。"""
    out = []
    for it in d.get("data") or []:
        mid = it.get("id")
        out.append({"title": it.get("name") or mid,
                    "url": f"https://openrouter.ai/{mid}" if mid else None,
                    "date": normalize.parse_date_utc(it.get("created")),
                    "summary": normalize.strip_html(it.get("description"), 800),
                    "guid": mid,
                    "signal": True})           # 每日 diff：只发新模型
    return [o for o in out if o["url"]], None


def api_cohere(d, src, ctx):
    out = []
    res = d.get("result") or d.get("data") or []
    for it in res:
        if not isinstance(it, dict):
            continue
        slug = it.get("slug")
        out.append({"title": it.get("title"),
                    "url": f"https://cohere.com/blog/{slug}" if slug else None,
                    "date": normalize.parse_date_utc(it.get("date")),
                    "summary": it.get("subtitle"),
                    "guid": str(slug or it.get("_id") or "")})
    return [o for o in out if o["url"]], None


def api_rsshub_routes(d, src, ctx):
    """routes.json —— 生态雷达，signal 只发新增路由。
    现版结构 {site:{routes:{"/path/:param":{…}}}}（旧版曾把 "/path" 平铺在
    顶层，两种都兼容）。round_urls 记全量路径快照，bootstrap 只发 cap 条
    但全量进 seen，后续只对真正新增的路由发信号。"""
    out, all_canon = [], []
    if isinstance(d, dict):
        def emit(path, route):
            cats = (route or {}).get("categories") or []
            cat = cats[0] if isinstance(cats, list) and cats else "other"
            url = ("https://docs.rsshub.app/routes/" + cat +
                   "?route=" + quote(path, safe=""))
            all_canon.append(normalize.url_canon(url))
            out.append({"title": f"RSSHub route {path}", "url": url,
                        "guid": path, "signal": True})
        for k in list(d.keys())[:8000]:
            v = d[k]
            if not isinstance(v, dict):
                continue
            if k.startswith("/"):
                emit(k, v)
            else:
                for p, route in list((v.get("routes") or {}).items())[:8000]:
                    if p.startswith("/"):
                        emit(p, route)
    if all_canon:
        ctx.seen.setdefault(src["name"], {})["round_urls"] = \
            list(dict.fromkeys(all_canon))[:SEEN_URL_CAP]
    return out, None


def api_docs_trae(d, src, ctx):
    """docs.trae.ai __loader=layout JSON：busStructure 是全站文档树。
    en 叶子文档按 updated_at 倒序发 signal；updated_at 拼进 url 的 docv
    参数让「文档更新」产生新 canon 再次触发。round_urls 记全量快照。"""
    def walk(nodes):
        for n in nodes or []:
            if isinstance(n, dict):
                yield n
                yield from walk(n.get("subs"))
    leaves = [n for n in walk((d or {}).get("busStructure"))
              if n.get("lang") == "en" and not n.get("is_dir")
              and n.get("path") and n.get("status") == 1]
    leaves.sort(key=lambda n: str(n.get("updated_at") or ""),
                reverse=True)
    out, all_canon = [], []
    for n in leaves[:500]:
        ver = str(n.get("updated_at") or "")[:10] or "na"
        url = f"https://docs.trae.ai/ide/{n['path']}?docv={ver}"
        all_canon.append(normalize.url_canon(url))
        out.append({"title": n.get("title") or n["path"], "url": url,
                    "guid": f"{n.get('_id') or n['path']}@{ver}",
                    "signal": True})
    if all_canon:
        ctx.seen.setdefault(src["name"], {})["round_urls"] = \
            list(dict.fromkeys(all_canon))[:SEEN_URL_CAP]
    return out, None


_TRUST_GQL = "https://trust.anthropic.com/graphql"
# query 文本 2026-09-21 从部署 bundle 捕获（签名绑死文本，改一个字就 401）；
# 重捕获流程见 experiments/trust-anthropic-monitor/capture_gql.py。
_TRUST_UPDATES_QUERY = (
    "query fetchTrustReportUpdates($slugId: String!, $first: Int!, "
    "$after: String, $searchString: String) {\n  trust {\n    "
    "trustReportBySlugId(slugId: $slugId) {\n      id\n      "
    "publicUpdates(first: $first, after: $after, searchString: "
    "$searchString) {\n        totalCount\n        pageInfo {\n          "
    "startCursor\n          endCursor\n          hasNextPage\n          "
    "hasPreviousPage\n          __typename\n        }\n        edges {\n"
    "          cursor\n          node {\n            id\n            "
    "title\n            description\n            createdAt\n            "
    "updatedAt\n            category\n            visibilityType\n"
    "            __typename\n          }\n          __typename\n        }"
    "\n        __typename\n      }\n      __typename\n    }\n    "
    "__typename\n  }\n}")


def api_trust_anthropic(src, ctx):
    """Vanta Trust Center（纯 SPA）：着陆页 → signature-manifest → 签名
    GraphQL publicUpdates。签名每次从 manifest 现取，不过期；Vanta 改版
    换 query 文本时 401 → 按 experiments/trust-anthropic-monitor 重捕获。"""
    base = "https://trust.anthropic.com"
    res = lhttp.FetchResult(status=0)
    home = ctx.get(base + "/", src, timeout=15)
    if not home.ok:
        res.error = home.error or f"http_{home.status}"
        res.detail = home.detail
        res.status = home.status
        return [], res
    m_url = re.search(r'data-signature-manifest-url="([^"]+)"', home.text)
    m_slug = re.search(r'data-slugid="([^"]+)"', home.text)
    if not (m_url and m_slug):
        res.error, res.detail = "parse_error", "manifest/slugid missing"
        return [], res
    man_r = ctx.get(m_url.group(1), src, timeout=15)
    if not man_r.ok:
        return [], man_r
    try:
        man = json.loads(man_r.text)
        sig = man["operations"]["fetchTrustReportUpdates"]
        signed_at = man["signedAt"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        res.error, res.detail = "parse_error", f"manifest: {e}"
        return [], res
    payload = {
        "operationName": "fetchTrustReportUpdates",
        "variables": {"slugId": m_slug.group(1), "first": 50,
                      "searchString": ""},
        "extensions": {"signedQuery": {"signedAt": signed_at,
                                     "signature": sig}},
        "query": _TRUST_UPDATES_QUERY,
    }
    r = lhttp.post_json(_TRUST_GQL + "?operation=fetchTrustReportUpdates",
                   payload, {"Origin": base, "Referer": base + "/"},
                   ctx.proxy_arg(src))
    if not r.ok:
        return [], r
    try:
        edges = json.loads(r.text)["data"]["trust"][
            "trustReportBySlugId"]["publicUpdates"]["edges"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        r.error, r.detail = "parse_error", f"gql resp: {e}"
        return [], r
    out = []
    for e in edges:
        n = e.get("node") or {}
        nid = n.get("id")
        if not nid:
            continue
        out.append({"title": n.get("title"),
                    "url": f"{base}/updates?update={nid}",
                    "date": normalize.parse_date_utc(n.get("createdAt")),
                    "summary": normalize.strip_html(n.get("description")),
                    "guid": str(nid)})
    return out, r


def api_bloomberglaw(d, src, ctx):
    arts = ((d.get("data") or {}).get("articles") or {})
    out = []
    for it in arts.get("items") or []:
        out.append({"title": it.get("headline"), "url": it.get("url"),
                    "date": normalize.parse_date_utc(it.get("postedDate")),
                    "summary": normalize.strip_html(it.get("summary")),
                    "guid": it.get("id"),
                    "tags": ["free" if it.get("free") else "paywalled"]})
    return out, None


def api_xiaoyuzhou(src, ctx):
    """小宇宙播客：__NEXT_DATA__ 取 buildId → _next/data/podcast/<pid>.json。"""
    pid = urlsplit(src["feed_url"]).path.rstrip("/").split("/")[-1]
    home = ctx.get("https://www.xiaoyuzhoufm.com/", src, timeout=15)
    res = lhttp.FetchResult(status=home.status)
    if not home.ok:
        res.error = home.error
        return [], res
    m = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>',
                  home.text, re.S)
    if not m:
        res.error = "parse_error"
        return [], res
    try:
        bid = json.loads(htmlmod.unescape(m.group(1)))["buildId"]
    except (json.JSONDecodeError, KeyError):
        res.error = "parse_error"
        return [], res
    api = f"https://www.xiaoyuzhoufm.com/_next/data/{bid}/podcast/{pid}.json"
    r = ctx.get(api, src, timeout=15)
    if not r.ok:
        return [], r
    try:
        pod = json.loads(r.text)["pageProps"]["podcast"]
    except (json.JSONDecodeError, KeyError):
        r.error = "parse_error"
        return [], r
    out = []
    for ep in pod.get("episodes") or []:
        eid = ep.get("eid") or ep.get("id")
        img = ep.get("image") or pod.get("image") or {}
        out.append({"title": ep.get("title"),
                    "url": f"https://www.xiaoyuzhoufm.com/episode/{eid}"
                           if eid else None,
                    "date": normalize.parse_date_utc(ep.get("pubDate")),
                    "summary": normalize.strip_html(ep.get("shownotes")
                                           or ep.get("description")),
                    "guid": eid,
                    "image": img.get("picUrl") if isinstance(img, dict) else None})
    return [o for o in out if o["url"]], r


# 具名 adapter 注册表；值 None → 走通用 walker
API_ADAPTERS = {
    "hn_algolia": api_hn,
    "lobsters": api_lobsters,
    "github_search": api_github_search,
    "bilibili_newlist": api_bilibili,
    "huggingface_daily": api_huggingface,
    "jiqizhixin": api_jiqizhixin,
    "sspai": api_sspai,
    "tmtpost": api_tmtpost,
    "zhihu_qbitai": api_zhihu_col,
    "qwen_blog": api_qwen,
    "infoq_ai": api_infoq,
    "juejin_ai": api_juejin,
    "oschina_ai": api_oschina,
    "alphaxiv_feed": api_alphaxiv,
    "zenodo_datacite": api_zenodo,
    "openrouter_models": api_openrouter,
    "cohere_blog": api_cohere,
    "rsshub_routes": api_rsshub_routes,
    "bloomberglaw_ai": api_bloomberglaw,
    "docs_trae": api_docs_trae,
    "xiaoyuzhoufm_ai": api_xiaoyuzhou,   # 签名特殊：src/ctx 自取
    "trust_anthropic": api_trust_anthropic,  # 同上：自抓 manifest+签名 POST
}

# 自抓型 adapter（签名 (src, ctx)，不吃 feed body）
SELF_FETCH_ADAPTERS = {api_xiaoyuzhou, api_trust_anthropic}

# json_api 但需 POST/特殊头的请求覆写
REQUEST_SPECS = {
    "juejin_ai": {
        "method": "POST",
        "json": {"id_type": 2, "sort_type": 300,
                 "cate_id": "6809637773935378440", "cursor": "0", "limit": 30},
    },
    "infoq_ai": {
        "method": "POST",
        "json": {"id": 31, "size": 30},
        "headers": {"Referer": "https://www.infoq.cn/",
                    "Origin": "https://www.infoq.cn"},
    },
    "bloomberglaw_ai": {
        "method": "POST",
        "json": {"query": "query($c:[String],$since:String){articles("
                          "channelIds:$c,startDate:$since,order:PostedDate,"
                          "direction:Descending,limit:50){count items{id "
                          "headline postedDate url free summary}}}",
                 "variables": {"c": ["00000188-05d6-db7f-a7e8-f7d6f0170000"],
                               "since": None}},   # 运行时填 window 起点日期
    },
    "tmtpost": {"headers": {"app-version": "web1.0"}},
}

_TITLE_KEYS = {"title", "name", "headline", "article_title", "morning_paper_title",
               "post_title"}
_URL_KEYS = {"url", "link", "share_url", "item_url", "html_url", "article_url",
             "short_url", "permalink", "web_url", "page_url", "post_url"}
_ID_KEYS = {"id", "guid", "article_id", "objectid", "aid", "uuid", "eid",
            "bvid", "slug", "path", "short_id"}
_DATE_KEYS = {"published", "published_at", "publishedat", "pubdate", "date",
              "created", "created_at", "createdat", "created_time", "ctime",
              "utime", "updated", "updated_at", "posteddate", "posted_at",
              "released_time", "release_time", "first_publication_date",
              "publication_date", "publish_time", "time_published", "post_time"}
_SUM_KEYS = {"summary", "description", "desc", "brief_content", "excerpt",
             "article_summary", "subtitle", "feed_description", "introduction",
             "content", "abstract", "post_excerpt"}
_IMG_KEYS = {"image", "cover", "coverimageurl", "cover_url", "thumbnail",
             "thumb", "pic", "image_url", "share_pic", "article_cover", "banner",
             "post_cover_image"}


def _walk_lists(node, depth=0):
    if depth > 6:
        return
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if len(dicts) >= 3:
            yield node
        for v in node[:200]:
            yield from _walk_lists(v, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_lists(v, depth + 1)


def _score_list(lst) -> int:
    n = 0
    for it in lst[:50]:
        ks = {k.lower() for k in it.keys()}
        if ks & _TITLE_KEYS and (ks & _URL_KEYS or ks & _ID_KEYS):
            n += 1
    return n


def api_generic(d, src, ctx):
    """通用 JSON walker：找「最像条目列表」的 dict list，启发式字段映射。"""
    best, best_score = [], 0
    for lst in _walk_lists(d):
        s = _score_list(lst)
        if s > best_score:
            best, best_score = lst, s
    if best_score < 3:
        return [], None
    out = []
    for it in best:
        ks = {k.lower(): k for k in it.keys()}     # lc -> orig
        def gv(keyset):
            for k in keyset:
                if k in ks:
                    return it[ks[k]]
            return None
        url = _first_str(gv(_URL_KEYS))
        if url and not url.startswith("http"):
            url = urljoin(src["feed_url"], url)
        if not url:
            continue
        title = _first_str(gv(_TITLE_KEYS))
        guid = _first_str(gv(_ID_KEYS))
        out.append({"title": title, "url": url,
                    "date": normalize.parse_date_utc(gv(_DATE_KEYS)),
                    "summary": normalize.strip_html(_first_str(gv(_SUM_KEYS)), 1500),
                    "guid": str(guid or ""),
                    "image": _first_str(gv(_IMG_KEYS))})
    return out, None
