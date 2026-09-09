"""真实性/质量评估：反刷星、换皮去重、质量分（设计方案 §7.2 / §7.4）。

说明：2026-07 起 stargazer 身份列表已不可访问，因此改用**行为代理指标**
（日序列平滑度、周末/工作日节律、星-commit 比、贡献者集中度）识别刷星。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .features import GrowthFeatures

DOC_ONLY_PATTERN = re.compile(
    r"awesome|cheatsheet|roadmap|interview|leetcode|booklist|resources|"
    r"教程|笔记|题库|面试|刷题|清单|收藏", re.I)


@dataclass
class FraudVerdict:
    flags: list[str] = field(default_factory=list)
    penalty: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).days
    except ValueError:
        return None


def anti_fraud(
    growth: GrowthFeatures,
    meta: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
    penalties: dict[str, float] | None = None,
) -> FraudVerdict:
    p = {
        "penalty_farm": 1.5,
        "penalty_skin": 1.2,
        "penalty_stale": 0.8,
        "penalty_doconly": 0.6,
    }
    p.update(penalties or {})

    v = FraudVerdict()
    snap = snapshot or {}
    commits_30d = int(snap.get("commit_count_30d") or 0)
    contributors = int(snap.get("contributors") or 0)
    stars = int(growth.total_stars or 0)

    # 1) 星数突增但代码零活动：最典型的买星特征
    if growth.stars_7d >= 100 and commits_30d == 0 and contributors <= 1:
        v.flags.append("star_farm_suspect")
        v.reasons.append(f"7 日新增 {growth.stars_7d} 星，但 30 天 commit=0、贡献者≤1")
        v.penalty += p["penalty_farm"]

    # 2) 恒定方波：真人增长有昼夜/周末节律，刷星往往是恒定量
    if growth.v7 >= 20 and growth.cv14 < 0.15 and growth.zero_ratio < 0.1:
        v.flags.append("constant_pulse")
        v.reasons.append(f"近 14 天日增变异系数仅 {growth.cv14:.2f}，疑似恒定投放")
        v.penalty += 0.6 * p["penalty_farm"]

    # 2b) 周末节律异常：真人增长工作日/周末有差异，周末占比畸高且量大
    if growth.stars_30d >= 100 and growth.weekend_ratio >= 0.48:
        v.flags.append("weekend_anomaly")
        v.reasons.append(f"近 28 天周末星占比 {growth.weekend_ratio:.0%}，节律异常")
        v.penalty += 0.3 * p["penalty_farm"]

    # 3) 星 / commit 比失衡
    if commits_30d > 0:
        ratio = stars / max(commits_30d, 1)
        if ratio > 300 and stars > 300:
            v.flags.append("star_commit_imbalance")
            v.reasons.append(f"星/commit 比 {ratio:.0f}，远超正常区间")
            v.penalty += 0.5 * p["penalty_farm"]

    # 4) fork 换皮
    if meta.get("is_fork") and stars >= 500:
        v.flags.append("fork_copy")
        v.reasons.append("高星 fork 仓库，原创性存疑")
        v.penalty += p["penalty_skin"]

    # 5) 停更
    stale_days = _days_since(snap.get("pushed_at") or meta.get("pushed_at"))
    if stale_days is not None and stale_days > 90:
        v.flags.append("stale")
        v.reasons.append(f"已 {stale_days} 天未推送代码")
        v.penalty += p["penalty_stale"]

    # 6) 纯文档/清单类
    text = f"{meta.get('full_name','')} {meta.get('description','')} {' '.join(meta.get('topics') or [])}"
    if DOC_ONLY_PATTERN.search(text or ""):
        v.flags.append("doc_only")
        v.reasons.append("疑似清单/教程类仓库，非工具型宝藏")
        v.penalty += p["penalty_doconly"]

    # 7) 归档
    if meta.get("archived"):
        v.flags.append("archived")
        v.penalty += p["penalty_stale"]

    # 8) 昙花一现
    if growth.decay < 0.35 and growth.v30 > 5:
        v.flags.append("fading")
        v.reasons.append(f"近 7 天增速仅为前一周的 {growth.decay:.0%}，热度回落")
        v.penalty += 0.4 * p["penalty_stale"]

    return v


def quality_score(
    growth: GrowthFeatures,
    meta: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> float:
    """综合质量分，0~1。无深度画像时只用元数据做保守估计。"""
    snap = snapshot or {}
    parts: list[tuple[float, float]] = []  # (得分, 权重)

    commits = int(snap.get("commit_count_30d") or 0)
    contributors = int(snap.get("contributors") or 0)
    if commits or contributors:
        parts.append((min(commits / 30.0, 1.0), 0.30))
        parts.append((min(contributors / 8.0, 1.0), 0.25))
    else:
        parts.append((0.35, 0.30))
        parts.append((0.30, 0.25))

    desc_len = len(str(meta.get("description") or ""))
    parts.append((min(desc_len / 120.0, 1.0), 0.10))

    has_license = 1.0 if meta.get("license_name") else 0.0
    parts.append((has_license, 0.08))

    has_homepage = 1.0 if meta.get("homepage") else 0.35
    parts.append((has_homepage, 0.07))

    topics = meta.get("topics") or []
    parts.append((min(len(topics) / 5.0, 1.0), 0.08))

    stale = _days_since(snap.get("pushed_at") or meta.get("pushed_at"))
    freshness = 1.0 if stale is None else max(0.0, 1.0 - stale / 60.0)
    parts.append((freshness, 0.12))

    total_w = sum(w for _, w in parts) or 1.0
    score = sum(s * w for s, w in parts) / total_w
    return round(max(min(score, 1.0), 0.0), 4)


# --------------------------------------------------------------------------- #
# 换皮 / 去重：SimHash + 汉明距离（无网络开销）
# --------------------------------------------------------------------------- #
def _hash64(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(text: str) -> int:
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", (text or "").lower())
    if not tokens:
        return 0
    vec = [0] * 64
    for t in tokens:
        h = _hash64(t)
        for i in range(64):
            vec[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i, v in enumerate(vec):
        if v > 0:
            out |= (1 << i)
    # SQLite INTEGER 上限为 2^63-1，这里转成有符号 64 位再落库
    return out - (1 << 64) if out >= (1 << 63) else out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def dedup_flag(candidates: list[dict[str, Any]], threshold: int = 8) -> set[str]:
    """在候选集内标记互为换皮/镜像的仓库，返回被抑制的 full_name 集合。

    只保留每个相似簇中分数最高的一个（调用方需按分数降序传入）。
    """
    suppressed: set[str] = set()
    seen: list[tuple[int, str]] = []
    for c in candidates:
        sig = c.get("simhash")
        if not sig:
            continue
        dup_of = next((name for s, name in seen if hamming(sig, s) <= threshold), None)
        if dup_of:
            suppressed.add(c["full_name"])
            continue
        seen.append((sig, c["full_name"]))
    return suppressed
