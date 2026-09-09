"""GitHub Trending 页采集（候选发现用）。

合规说明：``/trending`` 在 robots.txt 中是**允许**抓取的；本项目低频抓取（默认
每小时一次），且只提取仓库名后立即转官方 API 取数据，不解析任何被禁页面
（``/stargazers`` HTML、``/search`` HTML、``/pulse``、``/graphs`` 一律不碰）。
"""

from __future__ import annotations

import re
from typing import Any

from ..logging_setup import get_logger

log = get_logger("radar.trending")

REPO_RE = re.compile(r'href="/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)/stargazers"')
TODAY_RE = re.compile(r"([\d,]+)\s*stars\s+today", re.I)
WEEK_RE = re.compile(r"([\d,]+)\s*stars\s+this\s+week", re.I)
DESC_RE = re.compile(r'<p class="col-9 color-fg-muted my-1 pr-4">\s*(.*?)\s*</p>', re.S)
LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')


class TrendingSource:
    BASE = "https://github.com/trending"

    def __init__(self, cfg: Any, client: Any) -> None:
        self.cfg = cfg
        self.client = client

    def fetch(self, language: str = "", since: str = "daily") -> list[dict[str, Any]]:
        url = self.BASE
        if language:
            url = f"{url}/{language}"
        try:
            resp = self.client.request(f"{url}?since={since}", bucket="core")
        except Exception as e:  # noqa: BLE001
            log.debug("Trending 抓取失败 %s(%s)：%s", language, since, e)
            return []
        if resp.status != 200:
            return []
        html = resp.body.decode("utf-8", "ignore")
        return self.parse(html, language=language, since=since)

    @staticmethod
    def parse(html: str, language: str = "", since: str = "daily") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in html.split("<article")[1:]:
            m = REPO_RE.search(chunk)
            if not m:
                continue
            owner, name = m.group(1), m.group(2)
            full = f"{owner}/{name}"
            if full in seen:
                continue
            seen.add(full)
            key = "stars_today" if since == "daily" else "stars_week"
            mt = TODAY_RE.search(chunk) if since == "daily" else WEEK_RE.search(chunk)
            value = int(mt.group(1).replace(",", "")) if mt else 0
            md = DESC_RE.search(chunk)
            ml = LANG_RE.search(chunk)
            out.append({
                "full_name": full,
                "owner": owner,
                "name": name,
                "metric": key,
                "value": value,
                "language": (ml.group(1).strip() if ml else None) or (language or None),
                "description": _strip_tags(md.group(1)) if md else "",
                "since": since,
                "rank": len(out) + 1,
            })
        return out

    def fetch_all(self) -> list[dict[str, Any]]:
        src = self.cfg.sources
        if not src.get("trending_enabled", True):
            return []
        langs = src.get("trending_languages", [""]) or [""]
        sinces = src.get("trending_since", ["daily"]) or ["daily"]
        out: list[dict[str, Any]] = []
        for since in sinces:
            for lang in langs:
                out.extend(self.fetch(language=lang, since=since))
        return out


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return (text.replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")).strip()
