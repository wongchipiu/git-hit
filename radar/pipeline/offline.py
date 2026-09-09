"""离线夹具客户端：开发/自测用，**不发出任何真实网络请求**。

用于"点到即止"的验证：跑通整条流水线但不产生任何对外流量。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

SAMPLE_REPOS = [
    {
        "id": 900001,
        "full_name": "acme/alpha-quant-lab",
        "owner": {"login": "acme"},
        "name": "alpha-quant-lab",
        "created_at": str(date.today() - timedelta(days=26)) + "T02:00:00Z",
        "pushed_at": (datetime.now(timezone.utc) - timedelta(hours=6)).replace(microsecond=0).isoformat(),
        "language": "Python",
        "topics": ["quantitative-finance", "backtesting", "alpha"],
        "description": "轻量级 A 股量化研究框架：因子挖掘 + 回测 + 订单流复盘，内置 tushare/akshare 数据源。",
        "homepage": "https://example.com",
        "fork": False, "archived": False, "default_branch": "main",
        "license": {"spdx_id": "MIT"},
        "open_issues_count": 12,
        "stargazers_count": 780, "forks_count": 64, "watchers_count": 780,
    },
    {
        "id": 900002,
        "full_name": "beta-ai/agent-harness",
        "owner": {"login": "beta-ai"},
        "name": "agent-harness",
        "created_at": str(date.today() - timedelta(days=12)) + "T09:00:00Z",
        "pushed_at": (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat(),
        "language": "TypeScript",
        "topics": ["agent", "llm", "mcp"],
        "description": "Minimal agent runtime with MCP tool routing and streaming traces.",
        "homepage": "",
        "fork": False, "archived": False, "default_branch": "main",
        "license": {"spdx_id": "Apache-2.0"},
        "open_issues_count": 5,
        "stargazers_count": 320, "forks_count": 21, "watchers_count": 320,
    },
    {
        "id": 900003,
        "full_name": "sec-lab/cve-playground",
        "owner": {"login": "sec-lab"},
        "name": "cve-playground",
        "created_at": str(date.today() - timedelta(days=40)) + "T03:00:00Z",
        "pushed_at": (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
        "language": "C",
        "topics": ["security", "cve", "ctf"],
        "description": "CVE 复现环境与利用链合集，含中文分析报告。",
        "homepage": "",
        "fork": False, "archived": False, "default_branch": "master",
        "license": {"spdx_id": "NOASSERTION"},
        "open_issues_count": 3,
        "stargazers_count": 150, "forks_count": 18, "watchers_count": 150,
    },
]


def _history(total_stars: int, peak: int) -> list[dict[str, Any]]:
    """生成 4 周按周聚合样例数据（最近一周含较高日增）。"""
    today = datetime.now(timezone.utc).date()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    rows = []
    for w in range(3, -1, -1):
        week_start = sunday - timedelta(weeks=w)
        epoch = int(datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        if w == 0:
            days = [peak, max(peak - 5, 1), max(peak - 10, 1), 0, 0, 0, 0]
        else:
            base = max(int(peak * 0.35), 1)
            days = [base, base + 1, base, base + 2, base, base + 1, base]
        rows.append({"week": epoch, "total": sum(days), "days": days})
    return rows


class FixtureClient:
    """按 URL 路由返回夹具数据，接口与 HttpClient 保持一致。"""

    def __init__(self, repos: list[dict[str, Any]] | None = None) -> None:
        self.repos = repos or SAMPLE_REPOS
        self.stats = {"requests": 0, "from_cache": 0, "rate_limited": 0, "errors": 0}
        self.used = 0

    # ------------------------------------------------------------------ #
    def _repo_of(self, url: str) -> dict[str, Any]:
        for r in self.repos:
            if r["full_name"] in url:
                return r
        return self.repos[0]

    def get_json(self, url: str, **_kw: Any) -> Any:
        self.used += 1
        self.stats["requests"] += 1
        if "/search/repositories" in url:
            return {"total_count": len(self.repos), "items": self.repos}
        if "/stargazers/history" in url:
            repo = self._repo_of(url)
            return _history(repo["stargazers_count"], max(int(repo["stargazers_count"] * 0.08), 12))
        if url.endswith("/commits") or "/commits?" in url:
            return [{"sha": f"c{i}"} for i in range(12)]
        if url.endswith("/contributors") or "/contributors?" in url:
            return [{"login": f"u{i}"} for i in range(5)]
        if "/users/" in url:
            return {"login": "acme", "followers": 240, "public_repos": 38,
                    "created_at": "2016-04-01T00:00:00Z"}
        if "/repos/" in url:
            return self._repo_of(url)
        return {}

    def request(self, url: str, **_kw: Any) -> Any:
        """Trending 页等 HTML 抓取在离线模式下直接返回空结果。"""
        self.used += 1
        self.stats["requests"] += 1
        from ..http_client import Response
        return Response(status=200, headers={}, body=b"", url=url)

    def post_json(self, url: str, payload: Any, **_kw: Any) -> Any:
        return {"data": {}}


def load_fixture_file(path: Any) -> list[dict[str, Any]] | None:
    """可选的外部夹具（JSON 数组），用于扩展离线样例。"""
    p = __import__("pathlib").Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except (ValueError, OSError):
        return None
