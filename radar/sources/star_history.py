"""星星时序采集（四级漏斗的 L1）。

核心端点：``GET /repos/{owner}/{repo}/stargazers/history``
（2026-09-04 上线的 privacy-safe 端点，匿名可用，按周聚合 + 每日增量）。

抽象出 :class:`StarSeriesProvider` 是为了对抗"API 再次收紧"的风险：
主提供者失败时自动切换快照差分等备用源。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from ..logging_setup import get_logger

log = get_logger("radar.stars")

DAY_SECONDS = 86_400


def expand_weekly(rows: Sequence[dict[str, Any]], today: date | None = None) -> list[tuple[str, int]]:
    """把按周聚合的 ``[{week, total, days[7]}]`` 展开成日序列。

    关键细节：最新一周可能只有几天（其余为 0），必须截断到今天，
    否则 v7 会被系统性低估。
    """
    today = today or datetime.now(timezone.utc).date()
    acc: dict[date, int] = {}
    for row in rows or []:
        try:
            week = int(row.get("week", 0))
        except (TypeError, ValueError):
            continue
        days = row.get("days") or []
        for i, cnt in enumerate(days[:7]):
            try:
                cnt = int(cnt)
            except (TypeError, ValueError):
                cnt = 0
            d = datetime.fromtimestamp(week + i * DAY_SECONDS, tz=timezone.utc).date()
            if d > today:
                continue
            acc[d] = acc.get(d, 0) + cnt
    return sorted((d.isoformat(), c) for d, c in acc.items())


class StarSeriesProvider(ABC):
    """日粒度星星序列提供者。"""

    name = "base"

    @abstractmethod
    def daily_series(self, full_name: str, repo_id: int, weeks: int = 30) -> list[tuple[str, int]] | None:
        """返回 ``[(YYYY-MM-DD, 当日新增星数), ...]``，失败返回 None。"""


class ApiStarHistoryProvider(StarSeriesProvider):
    """官方 star history 端点（主源）。"""

    name = "api_star_history"

    def __init__(self, gh: Any, store: Any | None = None) -> None:
        self.gh = gh
        self.store = store

    def weekly(self, full_name: str, per_page: int = 30, page: int = 1) -> list[dict[str, Any]] | None:
        per_page = max(1, min(int(per_page), 30))
        page = max(1, min(int(page), 100))
        data = self.gh._get(  # noqa: SLF001 - 同包内复用底层请求
            f"/repos/{full_name}/stargazers/history",
            {"per_page": per_page, "page": page},
        )
        if isinstance(data, list):
            return data
        return None

    def daily_series(self, full_name: str, repo_id: int, weeks: int = 30) -> list[tuple[str, int]] | None:
        rows = self.weekly(full_name, per_page=min(weeks, 30), page=1)
        if rows is None:
            return None
        series = expand_weekly(rows)
        if self.store is not None:
            for row in rows:
                try:
                    self.store.save_star_weekly(repo_id, int(row["week"]), int(row.get("total") or 0),
                                                list(row.get("days") or []))
                except Exception:  # noqa: BLE001 - 落库失败不影响主流程
                    pass
            try:
                self.store.save_star_daily(repo_id, series)
            except Exception:  # noqa: BLE001
                pass
        return series


class SnapshotDeltaProvider(StarSeriesProvider):
    """备用源：用每日快照差分推算增速（API 收紧时的兜底）。"""

    name = "snapshot_delta"

    def __init__(self, store: Any) -> None:
        self.store = store

    def daily_series(self, full_name: str, repo_id: int, weeks: int = 30) -> list[tuple[str, int]] | None:
        rows = self.store.query(
            "SELECT snap_date, stars FROM repo_snapshots WHERE repo_id=? ORDER BY snap_date",
            (repo_id,))
        if len(rows) < 2:
            return None
        series: list[tuple[str, int]] = []
        prev = None
        for r in rows:
            stars = int(r["stars"] or 0)
            if prev is not None:
                delta = max(stars - prev[1], 0)
                series.append((r["snap_date"], delta))
            prev = (r["snap_date"], stars)
        return series


class CompositeStarProvider(StarSeriesProvider):
    """主源优先，失败自动降级到备用源（多源冗余）。"""

    def __init__(self, primary: StarSeriesProvider, fallbacks: Sequence[StarSeriesProvider] = ()) -> None:
        self.primary = primary
        self.fallbacks = list(fallbacks)
        self.name = primary.name

    def daily_series(self, full_name: str, repo_id: int, weeks: int = 30) -> list[tuple[str, int]] | None:
        try:
            series = self.primary.daily_series(full_name, repo_id, weeks)
        except Exception as e:  # noqa: BLE001
            log.debug("主时序源 %s 失败：%s", self.primary.name, e)
            series = None
        if series:
            return series
        for fb in self.fallbacks:
            try:
                s = fb.daily_series(full_name, repo_id, weeks)
            except Exception as e:  # noqa: BLE001
                log.debug("备用时序源 %s 失败：%s", fb.name, e)
                continue
            if s:
                self.name = fb.name
                return s
        return series if series else None
