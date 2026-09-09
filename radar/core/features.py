"""增速特征工程（设计方案 §7.1）。

把 star history 的日序列展开成：v1 / v7 / v30 / accel / burst / decay 等特征。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

EPS = 0.5


def _d(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class GrowthFeatures:
    v1: float = 0.0            # 最近 1 天新增星
    v7: float = 0.0            # 近 7 天日均新增星
    v30: float = 0.0           # 近 30 天日均新增星
    v_prev7: float = 0.0       # 前 7 天（第 8~14 天）日均，用于 decay
    accel: float = 0.0         # v7 / v30，>3 表示正在起飞
    burst: float = 0.0         # 单日峰值 / 生涯日均
    decay: float = 1.0         # v7 / v_prev7，<1 表示热度衰减
    lifetime_mean: float = 0.0  # 生涯日均新增星
    total_stars: int = 0
    age_days: int = 0
    max_day: int = 0
    weekend_ratio: float = 0.0  # 近 28 天周末星占比
    zero_ratio: float = 0.0     # 近 30 天零增长日占比
    cv14: float = 0.0           # 近 14 天日增变异系数（刷星常为恒定方波）
    series_days: int = 0
    stars_7d: int = 0
    stars_30d: int = 0

    def as_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


def daily_series_from_dict(mapping: dict[str, int]) -> list[tuple[str, int]]:
    return sorted((k, int(v)) for k, v in mapping.items())


def compute_growth(
    series: Sequence[tuple[str, int]] | dict[str, int],
    stars_total: int = 0,
    created_at: str | date | None = None,
    today: date | None = None,
) -> GrowthFeatures:
    """由日序列计算全部增速特征。

    ``stars_total`` 用于计算生涯日均；``created_at`` 用于计算年龄与窗口有效性。
    仓库不足 30 天时，按实际存在天数取均值，避免系统性低估。
    """
    if isinstance(series, dict):
        series = daily_series_from_dict(series)
    today = today or datetime.now(timezone.utc).date()
    created = _d(created_at)
    dmap: dict[date, int] = {}
    for day, cnt in series or []:
        dd = _d(day)
        if dd is not None:
            dmap[dd] = dmap.get(dd, 0) + int(cnt)

    f = GrowthFeatures(total_stars=int(stars_total or 0), series_days=len(dmap))
    f.age_days = max((today - created).days, 0) if created else 0

    def window(offset_from: int, offset_to: int) -> tuple[list[int], int]:
        """返回 [offset_from, offset_to) 天前窗口内的有效日增列表与有效天数。"""
        vals: list[int] = []
        for i in range(offset_from, offset_to):
            day = today - timedelta(days=i)
            if created and day < created:
                continue
            vals.append(dmap.get(day, 0))
        return vals, max(len(vals), 1)

    last7, n7 = window(0, 7)
    last30, n30 = window(0, 30)
    prev7, nprev = window(7, 14)
    last14, _ = window(0, 14)
    last28, _ = window(0, 28)

    f.v1 = float(last7[0]) if last7 else 0.0
    f.v7 = sum(last7) / n7
    f.v30 = sum(last30) / n30
    f.v_prev7 = sum(prev7) / nprev
    f.stars_7d = sum(last7)
    f.stars_30d = sum(last30)
    f.accel = f.v7 / max(f.v30, EPS)
    f.decay = f.v7 / max(f.v_prev7, EPS)
    f.max_day = max(last7) if last7 else 0

    f.lifetime_mean = (f.total_stars / f.age_days) if f.age_days > 0 else float(f.total_stars)
    f.burst = f.max_day / max(f.lifetime_mean, EPS)

    weekend = sum(v for i, v in enumerate(last28) if (today - timedelta(days=i)).weekday() >= 5)
    total28 = sum(last28)
    f.weekend_ratio = (weekend / total28) if total28 > 0 else 0.0
    f.zero_ratio = (sum(1 for v in last30 if v == 0) / len(last30)) if last30 else 0.0

    if len(last14) >= 3:
        mean = statistics.fmean(last14)
        sd = statistics.pstdev(last14)
        f.cv14 = (sd / mean) if mean > 0 else 0.0

    # 数值兜底，避免 NaN 污染打分
    for key in ("accel", "burst", "decay"):
        value = getattr(f, key)
        if value is None or math.isnan(value) or math.isinf(value):
            setattr(f, key, 0.0 if key != "decay" else 1.0)
    return f


def cohort_stats(values: Sequence[float]) -> tuple[float, float]:
    if len(values) < 2:
        return 0.0, 1.0
    return statistics.fmean(values), max(statistics.pstdev(values), 1e-6)


def zscore(value: float, mean: float, sd: float) -> float:
    return max(min((value - mean) / max(sd, 1e-6), 5.0), -5.0)
