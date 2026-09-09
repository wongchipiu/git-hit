"""运行时上下文：一次性组装配置 / 存储 / 数据源 / 打分器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..core import Classifier, Scorer
from ..http_client import HttpClient
from ..llm import Analyst
from .offline import FixtureClient
from ..logging_setup import get_logger
from ..sources import (
    ApiStarHistoryProvider,
    CompositeStarProvider,
    GitHubSource,
    HackerNewsSource,
    SnapshotDeltaProvider,
    TrendingSource,
)
from ..storage import Store

log = get_logger("radar.runtime")


@dataclass
class Runtime:
    cfg: Config
    store: Store
    http: Any
    gh: GitHubSource
    stars: CompositeStarProvider
    trending: TrendingSource
    hn: HackerNewsSource
    classifier: Classifier
    scorer: Scorer
    analyst: Analyst
    offline: bool = False
    stats: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        self.store.close()


def build_runtime(cfg: Config, offline: bool = False, fixtures: dict[str, Any] | None = None) -> Runtime:
    store = Store(cfg.db_path)

    if offline:
        http = FixtureClient(fixtures.get("repos") if fixtures else None)
    else:
        http = HttpClient(
            user_agent=cfg.user_agent,
            timeout=float(cfg.github.get("timeout", 20)),
            core_per_hour=int(cfg.limits.core_per_hour),
            search_per_minute=int(cfg.limits.search_per_minute),
            min_interval=float(cfg.limits.min_interval),
            max_sleep_on_limit=int(cfg.limits.max_sleep_on_limit),
            max_requests_run=int(cfg.limits.max_requests_run),
            enable_etag=bool(cfg.limits.enable_etag),
            cache=store.cache,
        )

    gh = GitHubSource(cfg, http)
    primary = ApiStarHistoryProvider(gh, store)
    fallback = SnapshotDeltaProvider(store)
    stars = CompositeStarProvider(primary, [fallback])

    enabled = [c.key for c in cfg.channels if c.enabled]
    extra_keywords = {c.key: c.keywords for c in cfg.channels if c.keywords}
    classifier = Classifier(extra_keywords=extra_keywords, enabled_channels=enabled)

    return Runtime(
        cfg=cfg,
        store=store,
        http=http,
        gh=gh,
        stars=stars,
        trending=TrendingSource(cfg, http),
        hn=HackerNewsSource(cfg, http),
        classifier=classifier,
        scorer=Scorer(cfg),
        analyst=Analyst(cfg, store),
        offline=offline,
    )
