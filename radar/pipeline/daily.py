"""每日批处理：采集 → 特征 → 打分 → 去重 → LLM 点评 → 报告 → Git 存档。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import local_today
from ..core import Classifier  # noqa: F401 - 便于外部复用
from ..core.features import cohort_stats, compute_growth, zscore
from ..core.quality import anti_fraud, dedup_flag, quality_score
from ..core.scoring import ScoreResult
from ..delivery import commit_digest
from ..http_client import BudgetExceeded, RateLimited
from ..logging_setup import get_logger
from ..report import Digest, DigestItem, digest_to_json, render_html, render_markdown
from ..sources.github import normalize_repo
from .collect import Collector, external_score, star_bucket
from .context import Runtime, build_runtime

log = get_logger("radar.daily")


# --------------------------------------------------------------------------- #
# 准备：把候选仓库整理成可打分样本
# --------------------------------------------------------------------------- #
def _enrich_missing(rt: Runtime, candidates: dict[str, dict[str, Any]], limit: int) -> int:
    """Trending / HN 发现的候选缺元数据，按需补一次 /repos/{o}/{r}（受限额约束）。"""
    done = 0
    for cand in candidates.values():
        if done >= limit:
            break
        meta = cand["meta"]
        if (meta.get("stars") or 0) > 0 and meta.get("created_at"):
            continue
        try:
            data = rt.gh.repo(meta["full_name"])
        except (RateLimited, BudgetExceeded) as e:
            log.warning("元数据补全中断（%s），本轮已补 %d 个，下轮自愈", e, done)
            break
        if not isinstance(data, dict):
            continue
        meta.update(normalize_repo(data))
        meta["repo_id"] = rt.store.upsert_repo(meta)
        rt.store.save_snapshot(int(meta["repo_id"]), str(date.today()), meta)
        done += 1
    return done


def _series_of(rt: Runtime, cand: dict[str, Any]) -> list[tuple[str, int]]:
    if cand.get("series"):
        return cand["series"]
    repo_id = int(cand["meta"].get("repo_id") or 0)
    if repo_id:
        return rt.store.star_series(repo_id)
    return []


def prepare(rt: Runtime, candidates: dict[str, dict[str, Any]],
            metadata_limit: int = 40) -> list[dict[str, Any]]:
    cfg = rt.cfg
    _enrich_missing(rt, candidates, metadata_limit)
    penalties = {k: v for k, v in cfg.scoring.items() if k.startswith("penalty_")}
    watch = {w.lower() for w in cfg.watchlist}

    prepared: list[dict[str, Any]] = []
    for cand in candidates.values():
        meta = cand["meta"]
        series = _series_of(rt, cand)
        if not series:
            continue
        snap = cand.get("snapshot") or {}
        snap_row = rt.store.latest_snapshot(int(meta.get("repo_id") or 0)) if meta.get("repo_id") else None
        stars = int(snap.get("stars") or (snap_row["stars"] if snap_row else 0) or meta.get("stars") or 0)
        if not stars:
            stars = sum(v for _, v in series)
        merged_snapshot = dict(snap)
        if snap_row and not merged_snapshot.get("commit_count_30d"):
            merged_snapshot["commit_count_30d"] = snap_row["commit_count_30d"]
            merged_snapshot["contributors"] = snap_row["contributors"]
            merged_snapshot["pushed_at"] = merged_snapshot.get("pushed_at") or snap_row["pushed_at"]

        growth = compute_growth(series, stars_total=stars, created_at=meta.get("created_at"))
        cls = rt.classifier.classify(meta)
        if cls.primary == "rising" and cand.get("channels"):
            cls.primary = cand["channels"][0]
            cls.scores.setdefault(cls.primary, 0.6)
        quality = quality_score(growth, meta, merged_snapshot)
        ext, ext_desc = external_score(cand)
        row = rt.store.get_repo(meta["full_name"])
        author = float(cand.get("author_trust")
                       or (row["author_trust"] if row is not None else 0) or 0)
        fraud = anti_fraud(growth, meta, merged_snapshot, penalties)

        prepared.append({
            "meta": meta,
            "growth": growth,
            "cls": cls,
            "quality": quality,
            "external": ext,
            "external_desc": ext_desc,
            "author": author,
            "fraud": fraud,
            "series": series,
            "sources": cand.get("sources", []),
            "watch": meta["full_name"].lower() in watch,
        })
    return prepared


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def score_all(rt: Runtime, prepared: Sequence[dict[str, Any]], day: str) -> list[ScoreResult]:
    cohorts: dict[tuple[str, int], list[float]] = defaultdict(list)
    for p in prepared:
        key = (p["cls"].primary, star_bucket(p["growth"].total_stars))
        cohorts[key].append(p["growth"].v7)
    stats = {k: cohort_stats(v) for k, v in cohorts.items()}

    results: list[ScoreResult] = []
    for p in prepared:
        mean, sd = stats.get((p["cls"].primary, star_bucket(p["growth"].total_stars)), (0.0, 1.0))
        z = zscore(p["growth"].v7, mean, sd)
        r = rt.scorer.score(
            meta=p["meta"], growth=p["growth"], classification=p["cls"],
            quality=p["quality"], external=p["external"], author_trust=p["author"],
            z=z, fraud=p["fraud"], watch=p["watch"],
        )
        r.meta["sources"] = p["sources"]
        r.meta["external_desc"] = p["external_desc"]
        r.meta["series"] = p["series"]
        r.meta["stars"] = p["growth"].total_stars
        rt.store.save_score(r.repo_id, day, r.final, r.tier, r.components, r.vertical_scores)
        results.append(r)
    return results


# --------------------------------------------------------------------------- #
# 去重 + 组装日报
# --------------------------------------------------------------------------- #
def select(results: Sequence[ScoreResult], cfg: Any) -> list[ScoreResult]:
    ranked = sorted(results, key=lambda r: -r.final)
    suppressed = dedup_flag([
        {"full_name": r.full_name, "simhash": r.meta.get("simhash") or 0}
        for r in ranked if r.meta.get("simhash")
    ])
    return [r for r in ranked if r.full_name not in suppressed and r.tier]


def build_digest(cfg: Any, chosen: Sequence[ScoreResult], day: str,
                 stats: dict[str, Any]) -> Digest:
    limit = int(cfg.report.get("per_channel_limit", 10))
    sections: dict[str, list[DigestItem]] = {}
    titles: dict[str, str] = {}
    placed: set[str] = set()  # 已进入前面频道的仓库，避免跨频道重复展示
    for ch in cfg.ordered_channels():
        titles[ch.key] = f"{ch.emoji} {ch.name}".strip()
        pool = [r for r in chosen
                if r.full_name not in placed
                and (r.vertical == ch.key
                     or (ch.key == "rising" and (r.growth.age_days or 999) <= 45))]
        picked = pool[:limit]
        sections[ch.key] = [
            DigestItem(
                full_name=r.full_name, score=r.final, tier=r.tier, vertical=r.vertical,
                subtrack=r.subtrack, stars=r.growth.total_stars,
                stars_7d=r.growth.stars_7d, stars_30d=r.growth.stars_30d,
                accel=r.growth.accel, language=r.meta.get("language") or "",
                created_at=str(r.meta.get("created_at") or ""), age_days=r.growth.age_days,
                description=str(r.meta.get("description") or ""),
                topics=list(r.meta.get("topics") or []),
                sources=list(r.meta.get("sources") or []),
                external_desc=str(r.meta.get("external_desc") or ""),
                flags=r.flags, reasons=r.reasons,
                spark=[v for _, v in (r.meta.get("series") or [])][-30:],
            )
            for r in picked
        ]
        placed.update(r.full_name for r in picked)
    return Digest(day=day, stats=stats, sections=sections, channel_titles=titles)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_daily(
    cfg: Any,
    offline: bool = False,
    fixtures: dict[str, Any] | None = None,
    day: str | None = None,
    with_llm: bool | None = None,
    stages: Iterable[str] = ("search", "trending", "hn", "stars", "profile"),
    star_limit: int | None = None,
    search_pages: int | None = None,
    max_queries: int | None = None,
    skip_collect: bool = False,
    commit: bool | None = None,
) -> dict[str, Any]:
    day = day or local_today(cfg).isoformat()
    if offline:
        # 离线自测：报告写到 <output_dir>/offline/（正式环境即 output/offline/，被
        # .gitignore 忽略），且不做 Git 提交；数据库已在 build_runtime 中隔离为
        # offline_radar.db——夹具产物与正式日报完全隔离
        cfg.general["output_dir"] = str(Path(cfg.general.get("output_dir", "output")) / "offline")
        commit = False
        log.info("离线模式：产物写入离线目录与 offline_radar.db，不提交 Git")
    rt = build_runtime(cfg, offline=offline, fixtures=fixtures)
    try:
        candidates: dict[str, dict[str, Any]] = {}
        stats: dict[str, Any] = {}
        if skip_collect:
            candidates = _candidates_from_db(rt)
            stats = {"candidates": len(candidates), "star_history": len(candidates)}
        else:
            collector = Collector(rt)
            stats = collector.run(stages=stages, star_limit=star_limit,
                                  search_pages=search_pages, max_queries=max_queries)
            candidates = collector.candidates

        prepared = prepare(rt, candidates,
                           metadata_limit=int(cfg.sources.get("metadata_per_run", 40)))
        results = score_all(rt, prepared, day)
        chosen = select(results, cfg)
        log.info("候选 %d → 打分 %d → 入选 %d", len(candidates), len(results), len(chosen))

        # L3 LLM 点评
        if with_llm is None:
            with_llm = bool(cfg.llm.get("enabled", True))
        llm_map: dict[str, dict[str, Any]] = {}
        if with_llm and chosen:
            top = chosen[:int(cfg.llm.get("max_items_per_day", 60))]
            llm_map = rt.analyst.analyze([
                {
                    "meta": {**r.meta, "stars": r.growth.total_stars,
                             "stars_7d": r.growth.stars_7d, "stars_30d": r.growth.stars_30d,
                             "age_days": r.growth.age_days},
                    "external_desc": r.meta.get("external_desc", ""),
                }
                for r in top
            ])
            stats["llm_calls"] = rt.analyst.calls

        digest = build_digest(cfg, chosen, day, stats)
        for item_list in digest.sections.values():
            for it in item_list:
                it.llm = llm_map.get(it.full_name, {})

        paths = _write_outputs(cfg, digest)
        for ch, items in digest.sections.items():
            if items:
                rt.store.save_digest(day, ch, [i.full_name for i in items])

        if commit is None:
            commit = bool(cfg.delivery.get("git_commit", True))
        committed = False
        if commit:
            prefix = cfg.delivery.get("commit_prefix", "digest")
            committed = commit_digest(cfg.root, list(paths.values()),
                                      f"{prefix} {day}", push=bool(cfg.delivery.get("git_push", False)))

        rt.store.prune(keep_days=int(cfg.report.get("keep_days", 90)))
        return {
            "day": day, "stats": stats, "candidates": len(candidates),
            "scored": len(results), "selected": len(chosen),
            "paths": {k: str(v) for k, v in paths.items()},
            "committed": committed,
            "digest": digest,
        }
    finally:
        rt.close()


def _candidates_from_db(rt: Runtime) -> dict[str, dict[str, Any]]:
    """跳过采集时，从库里近 N 天的仓库重建候选（用于离线重算/复现）。"""
    window = int(rt.cfg.report.get("db_candidate_days", 60))
    since = str(date.today() - timedelta(days=window))
    out: dict[str, dict[str, Any]] = {}
    for row in rt.store.iter_repos(since=since):
        repo_id = int(row["repo_id"])
        series = rt.store.star_series(repo_id)
        if not series:
            continue
        snap = rt.store.latest_snapshot(repo_id)
        out[row["full_name"]] = {
            "meta": {
                "repo_id": repo_id, "full_name": row["full_name"], "owner": row["owner"],
                "name": row["name"], "created_at": row["created_at"],
                "language": row["language"], "topics": json.loads(row["topics"] or "[]"),
                "description": row["description"], "simhash": row["simhash"],
            },
            "series": series,
            "snapshot": dict(snap) if snap else {},
            "channels": [], "sources": ["db"],
        }
    return out


def _write_outputs(cfg: Any, digest: Digest) -> dict[str, Path]:
    out = cfg.output_dir
    daily_dir = out / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    md_path = daily_dir / f"{digest.day}.md"
    json_path = daily_dir / f"{digest.day}.json"
    html_latest = out / "report.html"
    html_dated = daily_dir / f"{digest.day}.html"

    md_path.write_text(render_markdown(digest, cfg), encoding="utf-8")
    json_path.write_text(json.dumps(digest_to_json(digest), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    html = render_html(digest, cfg)
    html_latest.write_text(html, encoding="utf-8")
    html_dated.write_text(html, encoding="utf-8")
    return {"markdown": md_path, "json": json_path, "html": html_latest, "html_dated": html_dated}
