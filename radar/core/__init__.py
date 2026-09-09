"""核心计算：特征工程、打分、领域分类、反作弊与去重。"""

from .features import GrowthFeatures, compute_growth, daily_series_from_dict
from .scoring import ScoreResult, Scorer
from .classifier import Classifier
from .quality import anti_fraud, dedup_flag, quality_score, simhash, hamming

__all__ = [
    "GrowthFeatures", "compute_growth", "daily_series_from_dict",
    "ScoreResult", "Scorer", "Classifier",
    "anti_fraud", "dedup_flag", "quality_score", "simhash", "hamming",
]
