"""Hacker News（Algolia）外部热度信号。

免费、无需鉴权，是「科技资讯」频道最强的早期信号。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..logging_setup import get_logger

log = get_logger("radar.hn")

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
# 尾部允许 .git 后缀 / 路径 / 查询参数，但仓库名本身不吃进 .git
GH_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/?#].*)?$")


class HackerNewsSource:
    def __init__(self, cfg: Any, client: Any) -> None:
        self.cfg = cfg
        self.client = client

    def fetch(self, min_points: int | None = None, lookback_days: int | None = None,
              pages: int = 1) -> list[dict[str, Any]]:
        src = self.cfg.sources
        min_points = min_points if min_points is not None else int(src.get("hn_min_points", 50))
        lookback = lookback_days if lookback_days is not None else int(src.get("hn_lookback_days", 3))
        if not src.get("hn_enabled", True):
            return []
        since = int((datetime.now(timezone.utc) - timedelta(days=lookback)).timestamp())
        out: list[dict[str, Any]] = []
        for page in range(pages):
            params = {
                "query": "github.com",
                "tags": "story",
                "numericFilters": f"points>{min_points},created_at_i>{since}",
                "hitsPerPage": 100,
                "page": page,
            }
            try:
                data = self.client.get_json(ALGOLIA, params=params, bucket="core")
            except Exception as e:  # noqa: BLE001
                log.debug("HN 抓取失败：%s", e)
                break
            hits = (data or {}).get("hits") or []
            if not hits:
                break
            out.extend(self._parse(hits))
            if len(hits) < 100:
                break
        return out

    @staticmethod
    def _parse(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for h in hits:
            url = h.get("url") or ""
            m = GH_RE.search(url)
            if not m:
                continue
            out.append({
                "full_name": f"{m.group(1)}/{m.group(2)}",
                "object_id": h.get("objectID"),
                "title": h.get("title") or h.get("story_title") or "",
                "url": url,
                "hn_url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                "created_at": h.get("created_at"),
            })
        return out

    @staticmethod
    def extract_repo(text: str) -> str | None:
        m = GH_RE.search(text or "")
        return f"{m.group(1)}/{m.group(2)}" if m else None
