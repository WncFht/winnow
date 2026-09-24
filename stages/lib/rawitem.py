"""stages/lib/rawitem.py — raw_item/1 构造收口。

collect.mk_item、x_*/weibo/reddit 采集器、pool 回放行此前各自手拼
契约 dict（extra=forbid 下多一个键就炸、少一个键就缺）——统一走
build()：item_key/id/url_canon 自动推导（可覆盖），_source/_fetch
子对象统一成型，出参前 RawItem.model_validate 构造即校验。
"""
from __future__ import annotations

from typing import Iterable

from contracts.models import RawItem
from stages.lib import normalize


def build(
    url: str,
    *,
    title: str,
    source_name: str,
    kind: str,
    date_fetched: str,
    feed_url: str = "",
    item_guid: str | None = None,
    content_text: str | None = None,
    date_published: str | None = None,
    language: str | None = None,
    tags: Iterable[str] = (),
    image: str | None = None,
    status: int = 200,
    via: str = "direct",
    reachable: bool = True,
    etag: str | None = None,
    content_sha256: str | None = None,
    raw_ref: str | None = None,
    item_key: str | None = None,
    url_canon: str | None = None,
    fetch: dict | None = None,
) -> dict:
    """组装 raw_item/1 dict 并契约校验后返回。

    item_key/url_canon 缺省由 url 推导（pool 回放行等已有键的场景可覆盖）。
    fetch 预成型 dict 时原样走 _fetch（如 reddit fetch_meta、pool cache 语义），
    否则由 status/via/reachable/etag/content_sha256 组装。
    """
    key = item_key or normalize.item_key(url)
    item = {
        "schema": "raw_item/1",
        "item_key": key,
        "id": key,
        "url": url,
        "url_canon": url_canon if url_canon is not None
        else normalize.url_canon(url),
        "title": title,
        "content_text": content_text,
        "date_published": date_published,
        "date_fetched": date_fetched,
        "language": language,
        "tags": list(tags),
        "image": image,
        "_source": {"name": source_name, "feed_url": feed_url,
                    "kind": kind, "item_guid": item_guid},
        "_fetch": dict(fetch) if fetch is not None else {
            "status": status, "via": via, "reachable": reachable,
            "etag": etag, "content_sha256": content_sha256,
        },
        "_raw_ref": raw_ref,
    }
    RawItem.model_validate(item)
    return item
