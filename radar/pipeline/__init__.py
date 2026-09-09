"""流水线：采集 → 打分 → 报告 → 交付；以及回测与权重校准。"""

from .context import Runtime, build_runtime
from .collect import Collector
from .daily import run_daily
from .backtest import run_backtest, calibrate_weights

__all__ = ["Runtime", "build_runtime", "Collector", "run_daily", "run_backtest", "calibrate_weights"]
