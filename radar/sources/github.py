"""GitHub 官方 API 封装（REST + 轻量 GraphQL）。

合规约束：仓库级数据一律走 API，不抓任何 HTML 详情页。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..http_client import BudgetExceeded, RateLimited
from ..logging_setup import get_logger

log = get_logger("radar.github")


class GitHubError(RuntimeError):
    pass


class GitHubSource:
    def __init__(self, cfg: Any, client: Any) -> None:
        self.cfg = cfg
        self.client = client
        self.base = cfg.github.get("base_url", "https://api.github.com").rstrip("/")
        self.api_version = cfg.github.get("api_version", "2026-03-10")
        token = cfg.github_token
        self.auth_header = {"Authorization": f"Bearer {token}"} if token else {}
        self.graphql_enabled = bool(token)

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        h = {"X-GitHub-Api-Version": self.api_version}
        h.update(self.auth_header)
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None, bucket: str = "core") -> Any:
        try:
            return self.client.get_json(f"{self.base}{path}", params=params,
                                        headers=self._headers(), bucket=bucket)
        except (RateLimited, BudgetExceeded):
            raise
        except Exception as e:  # noqa: BLE001 - 采集层统一降级
            log.debug("请求失败 %s: %s", path, e)
            return None

    # ------------------------------------------------------------------ #
    # Search（30 次/分钟，独立令牌桶）
    # ------------------------------------------------------------------ #
    def search_repositories(self, query: str, page: int = 1, per_page: int = 100,
                            sort: str = "stars") -> list[dict[str, Any]]:
        data = self._get(
            "/search/repositories",
            {"q": query, "sort": sort, "order": "desc", "per_page": per_page, "page": page},
            bucket="search",
        )
        if not data or not isinstance(data, dict):
            return []
        return data.get("items", []) or []

    def search_repo_page_capped(self, query: str, pages: int, per_page: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in range(1, max(pages, 1) + 1):
            items = self.search_repositories(query, page=p, per_page=per_page)
            if not items:
                break
            out.extend(items)
            if len(items) < per_page:
                break
        return out

    # ------------------------------------------------------------------ #
    # 仓库元数据
    # ------------------------------------------------------------------ #
    def repo(self, full_name: str) -> dict[str, Any] | None:
        return self._get(f"/repos/{full_name}")

    def commits_since_count(self, full_name: str, days: int = 30) -> int | None:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        data = self._get(f"/repos/{full_name}/commits", {"since": since, "per_page": 100})
        return len(data) if isinstance(data, list) else None

    def contributors_count(self, full_name: str) -> int | None:
        data = self._get(f"/repos/{full_name}/contributors", {"per_page": 100, "anon": "false"})
        return len(data) if isinstance(data, list) else None

    def user(self, login: str) -> dict[str, Any] | None:
        return self._get(f"/users/{login}")

    def events(self, full_name: str) -> list[dict[str, Any]]:
        data = self._get(f"/repos/{full_name}/events", {"per_page": 30})
        return data if isinstance(data, list) else []

    def public_events(self, pages: int = 1) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in range(1, max(pages, 1) + 1):
            data = self._get("/events", {"per_page": 100, "page": p})
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
        return out

    # ------------------------------------------------------------------ #
    # GraphQL（需令牌；失败静默降级）
    # ------------------------------------------------------------------ #
    def graphql_batch_meta(self, full_names: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批量取元数据，100 个仓库一次调用（5000 points/hr）。"""
        if not self.graphql_enabled:
            return {}
        names = list(full_names)
        if not names:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(names), 50):
            chunk = names[i:i + 50]
            frags, aliases = [], []
            for idx, full in enumerate(chunk):
                owner, name = full.split("/", 1)
                alias = f"r{idx}"
                aliases.append((alias, full))
                frags.append(
                    f'{alias}: repository(owner:"{owner}", name:"{name}") {{ '
                    "nameWithOwner createdAt pushedAt stargazerCount forkCount "
                    "isFork isArchived description primaryLanguage{name} "
                    "licenseInfo{spdxId} repositoryTopics(first:10){nodes{topic{name}}} "
                    "}}"
                )
            query = "query{" + " ".join(frags) + "}"
            try:
                resp = self.client.post_json(f"{self.base}/graphql", {"query": query},
                                             headers=self._headers(), bucket="core")
            except Exception as e:  # noqa: BLE001
                log.debug("GraphQL 批量元数据失败：%s", e)
                return out
            if not isinstance(resp, dict):
                continue
            node_root = (resp.get("data") or {})
            for alias, full in aliases:
                node = node_root.get(alias)
                if not node:
                    continue
                out[full] = {
                    "full_name": node.get("nameWithOwner") or full,
                    "created_at": (node.get("createdAt") or "")[:10],
                    "pushed_at": node.get("pushedAt"),
                    "stars": node.get("stargazerCount") or 0,
                    "forks": node.get("forkCount") or 0,
                    "is_fork": bool(node.get("isFork")),
                    "archived": bool(node.get("isArchived")),
                    "description": node.get("description") or "",
                    "language": ((node.get("primaryLanguage") or {}) or {}).get("name"),
                    "license_name": ((node.get("licenseInfo") or {}) or {}).get("spdxId"),
                    "topics": [t["topic"]["name"] for t in
                               ((node.get("repositoryTopics") or {}).get("nodes") or [])
                               if t.get("topic")],
                }
        return out


def normalize_repo(item: dict[str, Any]) -> dict[str, Any]:
    """把 Search API 的仓库条目归一化成内部 meta。"""
    return {
        "repo_id": item.get("id"),
        "full_name": item.get("full_name"),
        "owner": (item.get("owner") or {}).get("login"),
        "name": item.get("name"),
        "created_at": (item.get("created_at") or "")[:10] or None,
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "description": item.get("description") or "",
        "homepage": item.get("homepage") or "",
        "is_fork": bool(item.get("fork")),
        "archived": bool(item.get("archived")),
        "default_branch": item.get("default_branch"),
        "license_name": ((item.get("license") or {}) or {}).get("spdx_id"),
        "open_issues": item.get("open_issues_count") or 0,
        "stars": item.get("stargazers_count") or 0,
        "forks": item.get("forks_count") or 0,
        # 注意：watchers_count 因历史遗留恒等于 stars，真实订阅数是 subscribers_count
        "watchers": item.get("subscribers_count") or item.get("watchers_count") or 0,
        "pushed_at": item.get("pushed_at"),
    }
