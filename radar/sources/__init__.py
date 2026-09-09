"""数据采集源：GitHub REST/GraphQL、Trending 页、Hacker News。"""

from .github import GitHubSource
from .star_history import (
    ApiStarHistoryProvider,
    CompositeStarProvider,
    SnapshotDeltaProvider,
    StarSeriesProvider,
    expand_weekly,
)
from .trending import TrendingSource
from .hn import HackerNewsSource

__all__ = [
    "GitHubSource",
    "ApiStarHistoryProvider",
    "CompositeStarProvider",
    "SnapshotDeltaProvider",
    "StarSeriesProvider",
    "expand_weekly",
    "TrendingSource",
    "HackerNewsSource",
]
