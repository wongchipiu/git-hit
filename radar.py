#!/usr/bin/env python3
"""Treasure Radar —— GitHub 宝藏雷达命令行入口。

常用命令：
    python radar.py init                # 生成 config.toml / 目录 / Git 仓库
    python radar.py daily --offline     # 离线跑通全流程（零网络流量，自测用）
    python radar.py daily --quick       # 小流量真实采集（每频道仅 1 条 query、1 页）
    python radar.py daily               # 完整每日流程
    python radar.py status              # 查看库统计
    python radar.py backtest --days 30  # T+30 回测
    python radar.py calibrate           # 权重校准
    python radar.py scheduler           # 长驻调度（APScheduler）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.config import ROOT, ensure_config, load_config  # noqa: E402
from radar.logging_setup import get_logger, setup_logging  # noqa: E402


def _cfg(args: argparse.Namespace):
    cfg = load_config()
    setup_logging(cfg.log_dir, level=getattr(args, "log_level", "INFO"))
    if getattr(args, "offline", False):
        cfg.limits.max_requests_run = 10 ** 6  # 离线无真实流量，放开预算方便跑通
    return cfg


def cmd_init(args: argparse.Namespace) -> int:
    from radar.delivery import ensure_repo

    path = ensure_config()
    cfg = load_config()
    setup_logging(cfg.log_dir)
    log = get_logger("radar.init")
    for d in (cfg.data_dir, cfg.output_dir, (cfg.output_dir / "daily"), cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)
    (cfg.output_dir / ".gitkeep").touch(exist_ok=True)
    if ensure_repo(cfg.root):
        log.info("Git 仓库就绪：%s（仅本地提交，绝不 push）", cfg.root)
    log.info("配置文件：%s", path)
    log.info("请在 .env 中填写 GITHUB_TOKEN / DEEPSEEK_API_KEY（可选，缺失自动降级）")
    if not (ROOT / ".env").exists():
        (ROOT / ".env").write_text("GITHUB_TOKEN=\nDEEPSEEK_API_KEY=\n", encoding="utf-8")
        log.info("已生成 .env 模板")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from radar.pipeline.collect import Collector
    from radar.pipeline.context import build_runtime

    cfg = _cfg(args)
    log = get_logger("radar.collect")
    rt = build_runtime(cfg, offline=args.offline)
    try:
        c = Collector(rt)
        stages = ["search"]
        if not args.no_trending:
            stages.append("trending")
        if not args.no_hn:
            stages.append("hn")
        if not args.no_stars:
            stages.append("stars")
        if not args.no_profile:
            stages.append("profile")
        stats = c.run(stages=stages, star_limit=args.star_limit,
                      search_pages=args.pages, max_queries=args.queries)
        log.info("采集完成：%s", json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        rt.close()


def cmd_daily(args: argparse.Namespace) -> int:
    from radar.pipeline.daily import run_daily

    cfg = _cfg(args)
    log = get_logger("radar.daily")
    if args.quick:
        args.queries = args.queries or 1
        args.pages = args.pages or 1
        args.star_limit = args.star_limit or 20
        cfg.sources["deep_profile_per_run"] = min(int(cfg.sources.get("deep_profile_per_run", 30)), 3)
    stages = ["search"]
    for name, flag in (("trending", args.no_trending), ("hn", args.no_hn),
                       ("stars", args.no_stars), ("profile", args.no_profile)):
        if not flag:
            stages.append(name)
    result = run_daily(
        cfg,
        stages=stages,
        offline=args.offline,
        day=args.day,
        with_llm=(False if args.no_llm else None),
        star_limit=args.star_limit,
        search_pages=args.pages,
        max_queries=args.queries,
        skip_collect=args.skip_collect,
        commit=(False if args.no_commit else None),
    )
    summary = {k: v for k, v in result.items() if k != "digest"}
    log.info("日报生成完成：%s", json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from radar.pipeline.context import build_runtime

    cfg = _cfg(args)
    rt = build_runtime(cfg, offline=True)
    try:
        stats = rt.store.stats()
        token = "已配置" if cfg.github_token else "未配置（匿名 60 次/小时）"
        llm = "已配置" if cfg.llm_api_key else "未配置（降级为无点评）"
        print(json.dumps({
            "channels": [c.key for c in cfg.ordered_channels()],
            "github_token": token,
            "llm": llm,
            "limits": vars(cfg.limits),
            "db": stats,
            "watchlist": cfg.watchlist,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        rt.close()


def cmd_backtest(args: argparse.Namespace) -> int:
    from radar.pipeline.backtest import run_backtest

    cfg = _cfg(args)
    result = run_backtest(cfg, days=args.days, min_score=args.min_score)
    print(json.dumps({k: v for k, v in result.items() if k != "all"},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from radar.pipeline.backtest import calibrate_weights

    cfg = _cfg(args)
    result = calibrate_weights(cfg, lookback_days=args.lookback,
                               horizon=args.days, iterations=args.iterations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    from radar.scheduler import run_scheduler

    cfg = _cfg(args)
    return run_scheduler(cfg, offline=args.offline)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="radar", description="GitHub 宝藏雷达")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="生成配置与目录").set_defaults(func=cmd_init)

    c = sub.add_parser("collect", help="仅采集（不入日报）")
    c.add_argument("--offline", action="store_true")
    c.add_argument("--quick", action="store_true")
    c.add_argument("--pages", type=int, default=None)
    c.add_argument("--queries", type=int, default=None)
    c.add_argument("--star-limit", type=int, default=None)
    c.add_argument("--no-trending", action="store_true")
    c.add_argument("--no-hn", action="store_true")
    c.add_argument("--no-stars", action="store_true")
    c.add_argument("--no-profile", action="store_true")
    c.set_defaults(func=cmd_collect)

    d = sub.add_parser("daily", help="完整每日流程")
    d.add_argument("--offline", action="store_true")
    d.add_argument("--quick", action="store_true", help="小流量模式：每频道 1 query / 1 页")
    d.add_argument("--day", default=None)
    d.add_argument("--pages", type=int, default=None)
    d.add_argument("--queries", type=int, default=None)
    d.add_argument("--star-limit", type=int, default=None)
    d.add_argument("--no-llm", action="store_true")
    d.add_argument("--no-commit", action="store_true")
    d.add_argument("--no-trending", action="store_true")
    d.add_argument("--no-hn", action="store_true")
    d.add_argument("--no-stars", action="store_true")
    d.add_argument("--no-profile", action="store_true")
    d.add_argument("--skip-collect", action="store_true", help="用库里已有数据重算报告")
    d.set_defaults(func=cmd_daily)

    sub.add_parser("status", help="查看配置与库统计").set_defaults(func=cmd_status)

    b = sub.add_parser("backtest", help="T+30 回测")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--min-score", type=float, default=55.0)
    b.set_defaults(func=cmd_backtest)

    cal = sub.add_parser("calibrate", help="权重校准")
    cal.add_argument("--lookback", type=int, default=90)
    cal.add_argument("--days", type=int, default=30)
    cal.add_argument("--iterations", type=int, default=300)
    cal.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("scheduler", help="长驻调度模式")
    s.add_argument("--offline", action="store_true")
    s.set_defaults(func=cmd_scheduler)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
