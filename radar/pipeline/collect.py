"""L0~L2 采集漏斗。

L0 粗筛：Search（6 频道 × 中英双语 query）+ Trending + HN
L1 增速核验：/stargazers/history（受 star_history_per_run 限额）
L2 深度画像：commit / 贡献者 / 作者信誉（受 deep_profile_per_run 限额）
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..core.quality import simhash
from ..http_client import BudgetExceeded, RateLimited
from ..logging_setup import get_logger
from ..sources.github import normalize_repo
from .context import Runtime

log = get_logger("radar.collect")


def render_query(query: str, cfg: Any) -> str:
    today = datetime.now(timezone.utc).date()
    return (query
            .replace("{date}", str(today - timedelta(days=int(cfg.sources.get("created_within_days", 45)))))
            .replace("{push_date}", str(today - timedelta(days=int(cfg.sources.get("pushed_within_days", 30))))))


def is_excluded(cfg: Any, meta: dict[str, Any]) -> bool:
    ex = cfg.exclude or {}
    text = f"{meta.get('full_name','')} {meta.get('description','')}".lower()
    for kw in ex.get("keywords", []):
        if kw and kw in text:
            return True
    topics = {str(t).lower() for t in (meta.get("topics") or [])}
    if topics & set(ex.get("topics", [])):
        return True
    owners = {str(o).lower() for o in ex.get("owners", [])}
    if owners and str(meta.get("owner", "")).lower() in owners:
        return True
    return False


class Collector:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.cfg = rt.cfg
        self.store = rt.store
        self.candidates: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, Any] = {
            "search_queries": 0, "search_hits": 0, "trending_hits": 0,
            "hn_hits": 0, "star_history": 0, "deep_profile": 0, "skipped": 0,
        }

    # ------------------------------------------------------------------ #
    # L0
    # ------------------------------------------------------------------ #
    def run_search(self, channels: Iterable[str] | None = None, pages: int | None = None,
                   max_queries: int | None = None) -> None:
        if not self.cfg.sources.get("search_enabled", True):
            return
        pages = pages or int(self.cfg.sources.get("search_pages", 2))
        wanted = set(channels) if channels else None
        for ch in self.cfg.channels:
            if not ch.enabled or (wanted and ch.key not in wanted):
                continue
            queries = ch.all_queries()
            if max_queries:
                queries = queries[:max_queries]
            for q in queries:
                rendered = render_query(q, self.cfg)
                self.stats["search_queries"] += 1
                try:
                    items = self.rt.gh.search_repo_page_capped(rendered, pages=pages)
                except (RateLimited, BudgetExceeded) as e:
                    log.warning("采集中断（%s），本轮已采 %d 个候选", e, len(self.candidates))
                    return
                for item in items:
                    meta = normalize_repo(item)
                    if not meta.get("full_name"):
                        continue
                    if (meta.get("stars") or 0) < ch.min_stars:
                        continue
                    if is_excluded(self.cfg, meta):
                        self.stats["skipped"] += 1
                        continue
                    self._add(meta, channel=ch.key, source=f"search:{q}")
                    self.stats["search_hits"] += 1

    def run_trending(self) -> None:
        try:
            rows = self.rt.trending.fetch_all()
        except (RateLimited, BudgetExceeded) as e:
            log.warning("Trending 跳过：%s", e)
            return
        for r in rows:
            meta = {
                "full_name": r["full_name"], "owner": r["owner"], "name": r["name"],
                "description": r.get("description") or "", "language": r.get("language"),
                "topics": [], "stars": 0,
            }
            if is_excluded(self.cfg, meta):
                continue
            cand = self._add(meta, channel="", source=f"trending:{r['since']}")
            cand.setdefault("external_signals", []).append(
                {"source": "trending", "metric": r["metric"], "value": r["value"],
                 "rank": r.get("rank")})
            self.stats["trending_hits"] += 1

    def run_hn(self) -> None:
        try:
            rows = self.rt.hn.fetch()
        except (RateLimited, BudgetExceeded) as e:
            log.warning("HN 跳过：%s", e)
            return
        for r in rows:
            meta = {
                "full_name": r["full_name"],
                "owner": r["full_name"].split("/")[0],
                "name": r["full_name"].split("/")[-1],
                "description": r.get("title") or "", "topics": [], "stars": 0,
            }
            if is_excluded(self.cfg, meta):
                continue
            cand = self._add(meta, channel="", source="hn")
            cand.setdefault("external_signals", []).append(
                {"source": "hn", "metric": "points", "value": r["points"],
                 "url": r.get("hn_url"), "title": r.get("title")})
            self.stats["hn_hits"] += 1

    def _add(self, meta: dict[str, Any], channel: str, source: str) -> dict[str, Any]:
        full = meta["full_name"]
        cand = self.candidates.get(full)
        if cand is None:
            meta["simhash"] = simhash(f"{full} {meta.get('description','')} {meta.get('language','')}")
            repo_id = self.store.upsert_repo(meta)
            meta["repo_id"] = repo_id
            self.store.save_snapshot(repo_id, str(date.today()), meta)
            cand = {"meta": meta, "channels": [], "sources": [], "external_signals": []}
            self.candidates[full] = cand
        else:
            for k, v in meta.items():
                if v not in (None, "", [], 0) and not cand["meta"].get(k):
                    cand["meta"][k] = v
        if channel and channel not in cand["channels"]:
            cand["channels"].append(channel)
        if source not in cand["sources"]:
            cand["sources"].append(source)
        return cand

    # ------------------------------------------------------------------ #
    # L1 增速核验
    # ------------------------------------------------------------------ #
    def load_from_db(self, days: int = 2) -> int:
        """把近 N 天首次发现的仓库加载为候选（供 scheduler 分离作业消费）。

        interval 模式下 search/trending/hn 与 stars 是独立作业、不共享内存，
        必须从库里恢复候选，否则增速核验会空转。
        """
        cutoff = str(date.today() - timedelta(days=max(int(days), 0)))
        try:
            rows = self.store.query(
                "SELECT r.*, COALESCE((SELECT s.stars FROM repo_snapshots s "
                "WHERE s.repo_id = r.repo_id ORDER BY s.snap_date DESC LIMIT 1), 0) AS last_stars "
                "FROM repos r WHERE r.first_seen_at >= ?", (cutoff,))
        except Exception as e:  # noqa: BLE001
            log.warning("从库加载候选失败：%s", e)
            return 0
        added = 0
        for row in rows:
            full = row["full_name"]
            if not full or full in self.candidates:
                continue
            self.candidates[full] = {
                "meta": {
                    "repo_id": int(row["repo_id"]),
                    "full_name": full,
                    "owner": row["owner"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "language": row["language"],
                    "topics": json.loads(row["topics"] or "[]"),
                    "description": row["description"] or "",
                    "stars": int(row["last_stars"] or 0),
                    "simhash": row["simhash"],
                },
                "channels": [], "sources": ["db"], "external_signals": [],
            }
            added += 1
        if added:
            log.info("从库加载 %d 个候选（近 %d 天）", added, days)
        return added

    def select_for_star_history(self, limit: int) -> list[dict[str, Any]]:
        by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in self.candidates.values():
            key = c["channels"][0] if c["channels"] else "rising"
            by_channel[key].append(c)
        for v in by_channel.values():
            v.sort(key=lambda c: -(int(c["meta"].get("stars") or 0)))
        order = list(by_channel)
        picked: list[dict[str, Any]] = []
        i = 0
        while len(picked) < limit and order:
            added = False
            for ch in order:
                if i < len(by_channel[ch]):
                    picked.append(by_channel[ch][i])
                    added = True
                    if len(picked) >= limit:
                        break
            if not added:
                break
            i += 1
        return picked

    def run_star_history(self, limit: int | None = None) -> None:
        limit = limit or int(self.cfg.sources.get("star_history_per_run", 150))
        picked = self.select_for_star_history(limit)
        for cand in picked:
            meta = cand["meta"]
            try:
                series = self.rt.stars.daily_series(meta["full_name"], int(meta["repo_id"]))
            except (RateLimited, BudgetExceeded) as e:
                log.warning("增速核验中断（%s），已完成 %d 个", e, self.stats["star_history"])
                break
            if not series:
                continue
            cand["series"] = series
            self.stats["star_history"] += 1

        if self.stats["star_history"] == 0:
            log.warning(
                "本轮未能核验任何仓库的 star history。匿名访问配额仅 60 次/小时且按出口 IP 共享，"
                "极易被占满；请在项目根目录 .env 中配置 GITHUB_TOKEN（public_repo 只读即可）后重试。"
            )

    # ------------------------------------------------------------------ #
    # L2 深度画像
    # ------------------------------------------------------------------ #
    def run_deep_profile(self, limit: int | None = None) -> None:
        limit = limit or int(self.cfg.sources.get("deep_profile_per_run", 30))
        ranked = sorted(
            [c for c in self.candidates.values() if c.get("series")],
            key=lambda c: -sum(v for _, v in c["series"][-7:]),
        )[:limit]
        today = str(date.today())
        for cand in ranked:
            meta = cand["meta"]
            full = meta["full_name"]
            try:
                commits = self.rt.gh.commits_since_count(full, days=30)
                contributors = self.rt.gh.contributors_count(full)
                user = self.rt.gh.user(meta.get("owner") or full.split("/")[0])
            except (RateLimited, BudgetExceeded) as e:
                log.warning("深度画像中断（%s）", e)
                break
            snap = {
                "stars": meta.get("stars", 0), "forks": meta.get("forks", 0),
                "watchers": meta.get("watchers", 0), "open_issues": meta.get("open_issues", 0),
                "pushed_at": meta.get("pushed_at"),
                "commit_count_30d": commits or 0,
                "contributors": contributors or 0,
            }
            self.store.save_snapshot(int(meta["repo_id"]), today, snap)
            cand["snapshot"] = snap
            if isinstance(user, dict):
                cand["author_trust"] = author_trust_from_user(user)
                self.store.update_repo_fields(int(meta["repo_id"]),
                                              author_trust=cand["author_trust"])
            self.stats["deep_profile"] += 1

    # ------------------------------------------------------------------ #
    def run(self, stages: Iterable[str] = ("search", "trending", "hn", "stars", "profile"),
            star_limit: int | None = None, search_pages: int | None = None,
            max_queries: int | None = None) -> dict[str, Any]:
        stages = set(stages)
        if "search" in stages:
            self.run_search(pages=search_pages, max_queries=max_queries)
        if "trending" in stages:
            self.run_trending()
        if "hn" in stages:
            self.run_hn()
        if "stars" in stages:
            self.run_star_history(star_limit)
        if "profile" in stages:
            self.run_deep_profile()
        self.stats["candidates"] = len(self.candidates)
        return self.stats


def author_trust_from_user(user: dict[str, Any]) -> float:
    """作者信誉代理指标：followers / 仓库数 / 账号年龄。"""
    followers = int(user.get("followers") or 0)
    repos = int(user.get("public_repos") or 0)
    created = str(user.get("created_at") or "")[:10]
    years = 0.0
    if created:
        try:
            years = (datetime.now(timezone.utc).date()
                     - datetime.strptime(created, "%Y-%m-%d").date()).days / 365.0
        except ValueError:
            years = 0.0
    score = (0.4 * min(followers / 200.0, 1.0)
             + 0.3 * min(repos / 30.0, 1.0)
             + 0.3 * min(max(years, 0) / 5.0, 1.0))
    return round(min(max(score, 0.0), 1.0), 4)


def external_score(cand: dict[str, Any]) -> tuple[float, str]:
    """把 HN / Trending 信号折算成 0~1 外部热度分 + 人类可读描述。"""
    score = 0.0
    desc: list[str] = []
    for s in cand.get("external_signals", []):
        if s.get("source") == "hn":
            pts = int(s.get("value") or 0)
            score = max(score, min(pts / 200.0, 1.0))
            desc.append(f"HN {pts} points")
        elif s.get("source") == "trending":
            rank = int(s.get("rank") or 99)
            score = max(score, max(0.0, 1.0 - rank / 25.0) * 0.8)
            desc.append(f"Trending 第 {rank} 位（+{s.get('value')}）")
    return round(min(score, 1.0), 4), " · ".join(desc)


def star_bucket(stars: int) -> int:
    return min(int(math.log10(max(int(stars), 1))), 5)
