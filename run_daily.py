#!/usr/bin/env python3
"""每日一键入口：采集 → 打分 → 日报 → Git 存档。

Windows 任务计划程序调用的就是这个脚本（见 scripts/install_task.ps1）。

    python run_daily.py            # 正常每日流程
    python run_daily.py --offline  # 离线跑通（零网络流量）
    python run_daily.py --quick    # 小流量模式
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.config import load_config  # noqa: E402
from radar.logging_setup import get_logger, setup_logging  # noqa: E402
from radar.pipeline.daily import run_daily  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不发出任何网络请求")
    ap.add_argument("--quick", action="store_true", help="小流量：每频道 1 query / 1 页")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--day", default=None)
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.log_dir)
    log = get_logger("radar.run_daily")

    if args.quick:
        cfg.sources["deep_profile_per_run"] = min(int(cfg.sources.get("deep_profile_per_run", 30)), 3)

    result = run_daily(
        cfg,
        offline=args.offline,
        day=args.day,
        with_llm=(False if args.no_llm else None),
        star_limit=20 if args.quick else None,
        search_pages=1 if args.quick else None,
        max_queries=1 if args.quick else None,
        commit=(False if args.no_commit else None),
    )
    log.info("完成：入选 %s 个，报告 %s", result["selected"], result["paths"]["markdown"])
    print(json.dumps({k: v for k, v in result.items() if k != "digest"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
