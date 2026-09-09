"""T+30 回测与权重校准（P2）。

回测口径：取 N 天前上榜（分数 ≥ 观察档）的项目，看它今天的真实星数。
* 命中 = 30 天后星数 ≥ 500（设计方案 P2 验收标准）
* 同时统计相对增幅与中位星数，避免绝对阈值对小众垂直领域失真
校准：用历史样本回归权重，最大化预测分与真实结果的 Spearman 相关。
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta
from typing import Any

from ..config import local_today
from ..core.scoring import Scorer
from ..logging_setup import get_logger
from .context import build_runtime

log = get_logger("radar.backtest")

HIT_STARS = 500


def _stars_at(store: Any, repo_id: int, day: str, mode: str = "le") -> int | None:
    if mode == "le":
        row = store.query(
            "SELECT stars FROM repo_snapshots WHERE repo_id=? AND snap_date<=? "
            "ORDER BY snap_date DESC LIMIT 1", (repo_id, day))
    else:
        row = store.query(
            "SELECT stars FROM repo_snapshots WHERE repo_id=? AND snap_date>=? "
            "ORDER BY snap_date ASC LIMIT 1", (repo_id, day))
    return int(row[0]["stars"]) if row else None


def run_backtest(cfg: Any, days: int = 30, min_score: float = 55.0,
                 offline: bool = False) -> dict[str, Any]:
    rt = build_runtime(cfg, offline=offline)
    try:
        target_day = str(local_today(cfg) - timedelta(days=days))
        rows = rt.store.scores_for_date(target_day, min_score)
        if not rows:
            return {"date": target_day, "n": 0, "hit_rate": 0.0,
                    "note": f"{target_day} 无上榜记录（可用 --days 指定其它窗口）"}

        items: list[dict[str, Any]] = []
        for r in rows:
            repo_id = int(r["repo_id"])
            stars_then = _stars_at(rt.store, repo_id, target_day)
            if stars_then is None:
                comps = json.loads(r["raw_components"] or "{}")
                stars_then = int(comps.get("stars") or 0)
            stars_now = _stars_at(rt.store, repo_id, str(local_today(cfg)))
            snap_now = rt.store.latest_snapshot(repo_id)
            if stars_now is None and snap_now is not None:
                stars_now = int(snap_now["stars"])
            if stars_now is None:
                continue
            delta = max(stars_now - stars_then, 0)
            items.append({
                "full_name": r["full_name"],
                "score": round(float(r["final_score"]), 2),
                "tier": r["tier"],
                "stars_then": stars_then,
                "stars_now": stars_now,
                "delta": delta,
                "growth": round(delta / max(stars_then, 1), 4),
                "hit": bool(stars_now >= HIT_STARS),
            })

        if not items:
            return {"date": target_day, "n": 0, "hit_rate": 0.0}

        hits = sum(1 for i in items if i["hit"])
        return {
            "date": target_day,
            "window_days": days,
            "n": len(items),
            "hits": hits,
            "hit_rate": round(hits / len(items), 4),
            "hit_threshold": HIT_STARS,
            "median_stars_now": int(statistics.median([i["stars_now"] for i in items])),
            "median_growth": round(statistics.median([i["growth"] for i in items]), 4),
            "top": sorted(items, key=lambda i: -i["stars_now"])[:10],
            "all": items,
        }
    finally:
        rt.close()


def calibrate_weights(cfg: Any, lookback_days: int = 90, horizon: int = 30,
                      iterations: int = 300) -> dict[str, float]:
    """用历史样本回归校准权重，结果写入 data/calibration.json。"""
    rt = build_runtime(cfg)
    try:
        start = str(local_today(cfg) - timedelta(days=lookback_days))
        rows = rt.store.query(
            "SELECT s.*, r.full_name FROM scores s JOIN repos r ON r.repo_id=s.repo_id "
            "WHERE s.score_date>=? ORDER BY s.score_date", (start,))
        train: list[dict[str, Any]] = []
        for r in rows:
            comps = json.loads(r["raw_components"] or "{}")
            score_day = datetime.strptime(r["score_date"], "%Y-%m-%d").date()
            future = str(score_day + timedelta(days=horizon))
            stars_future = _stars_at(rt.store, int(r["repo_id"]), future, mode="ge")
            if stars_future is None:
                continue
            stars_now = int(comps.get("stars") or 0)
            label = math.log1p(max(stars_future, 0))
            train.append({
                "full_name": r["full_name"],
                "components": {
                    "v7": comps.get("v7", 0),
                    "accel_ratio": comps.get("accel_ratio", 0),
                    "burst_ratio": comps.get("burst_ratio", 0),
                    "quality": comps.get("quality_raw", 0),
                    "external": comps.get("external_raw", 0),
                    "age_factor": comps.get("age_factor", 0),
                    "stars_now": stars_now,
                },
                "label": label,
            })

        if len(train) < 5:
            log.warning("样本不足（%d 条 < 5），跳过校准；先积累几天的打分数据", len(train))
            return {"sample_size": len(train), "note": "样本不足"}

        result = Scorer.tune(train, iterations=iterations)
        result["sample_size"] = len(train)
        out = cfg.data_dir / "calibration.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("权重校准完成：样本 %d 条，Spearman=%.4f，已写入 %s",
                 len(train), result.get("spearman", 0), out)
        return result
    finally:
        rt.close()
