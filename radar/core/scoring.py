"""宝藏评分（设计方案 §7.3）。

raw = w1·log1p(v7) + w2·zscore + w3·log1p(accel) + w4·log1p(burst)
      + w5·quality + w6·external + w7·author_trust
final = squash(raw − penalty) · (1 + early) · vertical_match
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .classifier import Classification
from .features import GrowthFeatures
from .quality import FraudVerdict

log = get_logger("radar.scoring")

CALIBRATION_FILE = "calibration.json"
# calibrate 输出的权重名 -> 本模块打分权重名
CALIBRATION_MAP = {
    "w1": "w1_velocity",   # log1p(v7)
    "w2": "w3_accel",      # log1p(accel)
    "w3": "w4_burst",      # log1p(burst)
    "w4": "w5_quality",    # quality
    "w5": "w6_external",   # external
}

DEFAULT_WEIGHTS = {
    "w1_velocity": 1.6,
    "w2_zscore": 0.9,
    "w3_accel": 1.1,
    "w4_burst": 0.8,
    "w5_quality": 1.2,
    "w6_external": 1.0,
    "w7_author": 0.6,
    "early_ceiling": 5000,
    "early_bonus_max": 0.6,
    "tier_explosive": 85,
    "tier_high": 70,
    "tier_watch": 55,
}


@dataclass
class ScoreResult:
    repo_id: int = 0
    full_name: str = ""
    raw: float = 0.0
    penalty: float = 0.0
    final: float = 0.0
    tier: str = ""
    vertical: str = ""
    subtrack: str = ""
    components: dict[str, float] = field(default_factory=dict)
    vertical_scores: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    growth: GrowthFeatures | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple[Any, ...]:
        return (self.repo_id, self.final, self.tier, self.components, self.vertical_scores)


class Scorer:
    def __init__(self, cfg: Any) -> None:
        w = dict(DEFAULT_WEIGHTS)
        w.update({k: v for k, v in (cfg.scoring or {}).items() if k in DEFAULT_WEIGHTS})
        calib = self._load_calibration(cfg)
        if calib:
            applied = {CALIBRATION_MAP[k]: float(v) for k, v in calib.items()
                       if k in CALIBRATION_MAP}
            if applied:
                w.update(applied)
                log.info("已应用校准权重（%d 项，Spearman=%s）；删除 data/%s 可回到手工配置",
                         len(applied), calib.get("spearman", "?"), CALIBRATION_FILE)
        self.w = w

    @staticmethod
    def _load_calibration(cfg: Any) -> dict[str, Any] | None:
        """读取 data/calibration.json（`radar.py calibrate` 的产出）；存在则优先于手工权重。"""
        try:
            path = cfg.data_dir / CALIBRATION_FILE
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as e:
            log.debug("读取校准文件失败：%s", e)
            return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _squash(raw: float) -> float:
        return 100.0 / (1.0 + math.exp(-(raw - 6.0) / 2.2))

    def tier_of(self, final: float) -> str:
        if final >= float(self.w["tier_explosive"]):
            return "explosive"
        if final >= float(self.w["tier_high"]):
            return "high"
        if final >= float(self.w["tier_watch"]):
            return "watch"
        return ""

    # ------------------------------------------------------------------ #
    def score(
        self,
        meta: dict[str, Any],
        growth: GrowthFeatures,
        classification: Classification,
        quality: float,
        external: float,
        author_trust: float,
        z: float = 0.0,
        fraud: FraudVerdict | None = None,
        watch: bool = False,
    ) -> ScoreResult:
        w = self.w
        fraud = fraud or FraudVerdict()

        c_velocity = float(w["w1_velocity"]) * math.log1p(max(growth.v7, 0))
        c_z = float(w["w2_zscore"]) * z
        c_accel = float(w["w3_accel"]) * math.log1p(max(growth.accel, 0))
        c_burst = float(w["w4_burst"]) * math.log1p(max(growth.burst, 0))
        c_quality = float(w["w5_quality"]) * quality * 3.0
        c_external = float(w["w6_external"]) * max(min(external, 1.0), 0.0) * 3.0
        c_author = float(w["w7_author"]) * max(min(author_trust, 1.0), 0.0) * 3.0

        raw = c_velocity + c_z + c_accel + c_burst + c_quality + c_external + c_author
        penalty = float(fraud.penalty or 0.0)
        base = self._squash(raw - penalty)

        # 「早」加成：越新 + 星数越低 → 加成越大；超过 early_ceiling 不再享受
        ceiling = max(float(w["early_ceiling"]), 1.0)
        size_factor = max(0.0, 1.0 - (growth.total_stars or 0) / ceiling)
        age_factor = max(0.0, 1.0 - (growth.age_days or 0) / 180.0)
        early = float(w["early_bonus_max"]) * size_factor * age_factor

        vm = classification.vertical_match
        final = base * (1.0 + early) * vm
        if watch:
            final += 8.0
        final = round(max(min(final, 100.0), 0.0), 2)

        return ScoreResult(
            repo_id=int(meta.get("repo_id") or 0),
            full_name=str(meta.get("full_name") or ""),
            raw=round(raw, 4),
            penalty=round(penalty, 4),
            final=final,
            tier=self.tier_of(final),
            vertical=classification.primary,
            subtrack=classification.subtrack,
            components={
                "velocity": round(c_velocity, 4), "zscore": round(c_z, 4),
                "accel": round(c_accel, 4), "burst": round(c_burst, 4),
                "quality": round(c_quality, 4), "external": round(c_external, 4),
                "author": round(c_author, 4), "early": round(early, 4),
                "vertical_match": round(vm, 4), "v7": round(growth.v7, 3),
                "quality_raw": round(quality, 4), "external_raw": round(external, 4),
                "author_raw": round(author_trust, 4),
                "age_factor": round(age_factor, 4), "size_factor": round(size_factor, 4),
                "v30": round(growth.v30, 3), "accel_ratio": round(growth.accel, 3),
                "burst_ratio": round(growth.burst, 3), "decay": round(growth.decay, 3),
                "stars": growth.total_stars, "age_days": growth.age_days,
            },
            vertical_scores=classification.scores,
            flags=list(fraud.flags),
            reasons=list(fraud.reasons),
            growth=growth,
            meta=meta,
        )

    # ------------------------------------------------------------------ #
    # P2：权重校准（随机搜索，最大化 T+30 排序相关性）
    # ------------------------------------------------------------------ #
    @staticmethod
    def tune(train: list[dict[str, Any]], iterations: int = 300,
             seed: int = 20260909) -> dict[str, float]:
        """用历史样本回归校准权重。

        ``train`` 元素需含 ``components``(各分量原始特征) 与 ``label``(T+30 真实星级)。
        目标：最大化 Spearman 相关；用随机搜索避免引入 sklearn 依赖。
        """
        import random

        if len(train) < 5:
            return {}

        def spearman(a: list[float], b: list[float]) -> float:
            ra, rb = _rank(a), _rank(b)
            n = len(a)
            ma, mb = sum(ra) / n, sum(rb) / n
            num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
            den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
            return num / den if den else 0.0

        def predict(weights: dict[str, float], row: dict[str, Any]) -> float:
            c = row["components"]
            return (
                weights["w1"] * math.log1p(max(c.get("v7", 0), 0))
                + weights["w2"] * math.log1p(max(c.get("accel_ratio", 0), 0))
                + weights["w3"] * math.log1p(max(c.get("burst_ratio", 0), 0))
                + weights["w4"] * c.get("quality", 0)
                + weights["w5"] * c.get("external", 0)
                + weights["w6"] * c.get("age_factor", 0)
            )

        rng = random.Random(seed)
        best = {"w1": 1.6, "w2": 1.1, "w3": 0.8, "w4": 1.2, "w5": 1.0, "w6": 0.6}
        labels = [float(r["label"]) for r in train]
        best_score = spearman([predict(best, r) for r in train], labels)

        for _ in range(iterations):
            cand = {k: max(0.05, v * (1 + rng.uniform(-0.35, 0.35))) for k, v in best.items()}
            s = spearman([predict(cand, r) for r in train], labels)
            if s > best_score:
                best, best_score = cand, s
        return {**best, "spearman": round(best_score, 4)}


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks
