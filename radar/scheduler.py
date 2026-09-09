"""长驻调度模式（APScheduler 可用时优先，否则用标准库循环调度降级）。

Windows 下没有 cron/systemd，长驻进程是可选方案；生产推荐用
``scripts/install_task.ps1`` 注册「任务计划程序」定时触发 ``run_daily.py``。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from .logging_setup import get_logger

log = get_logger("radar.scheduler")


def _jobs(cfg: Any) -> list[dict[str, Any]]:
    sch = cfg.schedule or {}
    return [
        {"id": "search", "kind": "interval", "minutes": int(sch.get("search_interval_minutes", 60)),
         "stages": ["search"]},
        {"id": "trending", "kind": "interval", "hours": int(sch.get("trending_interval_hours", 6)),
         "stages": ["trending"]},
        {"id": "hn", "kind": "interval", "minutes": int(sch.get("hn_interval_minutes", 180)),
         "stages": ["hn"]},
        {"id": "stars", "kind": "interval", "minutes": int(sch.get("star_interval_minutes", 30)),
         "stages": ["stars", "profile"]},
        {"id": "daily", "kind": "cron", "at": str(sch.get("daily_report_time", "22:00")),
         "stages": ["search", "trending", "hn", "stars", "profile"]},
    ]


def _daily_due(now: datetime, last_daily: str, at: str) -> bool:
    """定点任务是否应触发（HH:MM 按整体时间比较，避免分量各自比较造成盲区）。"""
    hh, mm = (at.split(":") + ["0"])[:2]
    stamp = now.strftime("%Y-%m-%d")
    return stamp != last_daily and (now.hour, now.minute) >= (int(hh), int(mm))


def _make_task(cfg: Any, spec: dict[str, Any], offline: bool) -> Callable[[], None]:
    def task() -> None:
        from .pipeline.collect import Collector
        from .pipeline.context import build_runtime
        from .pipeline.daily import run_daily

        try:
            if spec["id"] == "daily":
                run_daily(cfg, offline=offline)
            else:
                rt = build_runtime(cfg, offline=offline)
                try:
                    c = Collector(rt)
                    if "stars" in spec["stages"]:
                        # interval 分离作业不共享内存候选，先从库恢复，
                        # 否则 search/trending 落库的候选无人核验（空转）
                        c.load_from_db(days=int((cfg.schedule or {}).get("star_candidate_days", 2)))
                    c.run(stages=spec["stages"],
                          star_limit=int((cfg.schedule or {}).get("star_limit_per_round", 40)))
                finally:
                    rt.close()
        except Exception as e:  # noqa: BLE001 - 单轮失败不中断主进程
            log.exception("任务 %s 本轮失败，下轮自愈：%s", spec["id"], e)

    return task


def run_scheduler(cfg: Any, offline: bool = False) -> int:
    log.info("启动长驻调度（offline=%s），Ctrl+C 退出", offline)
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        return _run_simple(cfg, offline)

    sched = BackgroundScheduler(timezone=str(cfg.general.get("timezone", "Asia/Shanghai")))
    for spec in _jobs(cfg):
        if spec["kind"] == "interval":
            trigger = IntervalTrigger(minutes=spec.get("minutes", 0) or 0,
                                      hours=spec.get("hours", 0) or 0)
        else:
            hh, mm = (spec["at"].split(":") + ["0"])[:2]
            trigger = CronTrigger(hour=int(hh), minute=int(mm))
        sched.add_job(_make_task(cfg, spec, offline), trigger=trigger, id=spec["id"],
                      max_instances=1, coalesce=True, misfire_grace_time=600)
    sched.start()
    try:
        while True:
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号，关闭调度器")
        sched.shutdown(wait=False)
    return 0


def _run_simple(cfg: Any, offline: bool) -> int:
    """APScheduler 缺失时的降级实现：间隔轮询 + 每日定点。"""
    specs = [s for s in _jobs(cfg) if s["kind"] == "interval"]
    daily = next((s for s in _jobs(cfg) if s["kind"] == "cron"), None)
    next_run = {s["id"]: 0.0 for s in specs}
    last_daily = ""
    try:
        while True:
            now = time.time()
            for s in specs:
                period = (s.get("minutes", 0) * 60) or (s.get("hours", 0) * 3600) or 3600
                if now >= next_run[s["id"]]:
                    _make_task(cfg, s, offline)()
                    next_run[s["id"]] = now + period
            if daily and _daily_due(datetime.now(), last_daily, daily["at"]):
                _make_task(cfg, daily, offline)()
                last_daily = datetime.now().strftime("%Y-%m-%d")
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        log.info("收到退出信号，停止调度")
    return 0
