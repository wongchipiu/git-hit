"""SQLite 持久层：建表 + 数据访问对象（DAO）。

表结构对应设计方案 §10：repos / repo_snapshots / star_daily / star_weekly /
signals / scores / events / digests，另加 llm_cache、http_cache、watchlist、meta。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .logging_setup import get_logger

log = get_logger("radar.storage")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS repos (
    repo_id          INTEGER PRIMARY KEY,
    full_name        TEXT UNIQUE NOT NULL,
    owner            TEXT,
    name             TEXT,
    created_at       TEXT,
    first_seen_at    TEXT,
    language         TEXT,
    topics           TEXT,
    description      TEXT,
    homepage         TEXT,
    is_fork          INTEGER DEFAULT 0,
    parent_repo      TEXT,
    archived         INTEGER DEFAULT 0,
    default_branch   TEXT,
    license_name     TEXT,
    open_issues      INTEGER DEFAULT 0,
    vertical         TEXT,
    author_trust     REAL DEFAULT 0,
    suspect_flags    TEXT,
    simhash          INTEGER,
    last_enriched_at TEXT
);

CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id          INTEGER NOT NULL,
    snap_date        TEXT NOT NULL,
    stars            INTEGER DEFAULT 0,
    forks            INTEGER DEFAULT 0,
    watchers         INTEGER DEFAULT 0,
    open_issues      INTEGER DEFAULT 0,
    pushed_at        TEXT,
    commit_count_30d INTEGER DEFAULT 0,
    contributors     INTEGER DEFAULT 0,
    PRIMARY KEY (repo_id, snap_date)
);

CREATE TABLE IF NOT EXISTS star_daily (
    repo_id     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    stars_delta INTEGER DEFAULT 0,
    PRIMARY KEY (repo_id, day)
);

CREATE TABLE IF NOT EXISTS star_weekly (
    repo_id    INTEGER NOT NULL,
    week_epoch INTEGER NOT NULL,
    total      INTEGER DEFAULT 0,
    days       TEXT,
    PRIMARY KEY (repo_id, week_epoch)
);

CREATE TABLE IF NOT EXISTS signals (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    ts      TEXT,
    source  TEXT,
    metric  TEXT,
    value   REAL,
    url     TEXT,
    raw     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_repo ON signals(repo_id, source);

CREATE TABLE IF NOT EXISTS scores (
    repo_id         INTEGER NOT NULL,
    score_date      TEXT NOT NULL,
    final_score     REAL,
    tier            TEXT,
    raw_components  TEXT,
    vertical_scores TEXT,
    PRIMARY KEY (repo_id, score_date)
);
CREATE INDEX IF NOT EXISTS idx_scores_date ON scores(score_date, final_score DESC);

CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    repo_id    INTEGER,
    type       TEXT,
    actor      TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS digests (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT,
    vertical TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    repo_id        INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    payload        TEXT,
    created_at     TEXT,
    PRIMARY KEY (repo_id, prompt_version, content_hash)
);

CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    value      BLOB,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    full_name TEXT PRIMARY KEY,
    note      TEXT,
    added_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class SqliteCache:
    """把 ETag / 响应体落到 SQLite，供 HttpClient 做条件请求。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> Any | None:
        row = self.conn.execute("SELECT value FROM http_cache WHERE key=?", (key,)).fetchone()
        if row is None or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return raw if key.startswith("body:") else raw.decode("utf-8", "ignore")

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.conn.execute(
            "INSERT INTO http_cache(key, value, updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value),
        )
        self.conn.commit()


class Store:
    """SQLite 数据访问对象。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.cache = SqliteCache(self.conn)

    # ---------------------------------------------------------------- #
    # 基础
    # ---------------------------------------------------------------- #
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query("SELECT v FROM meta WHERE k=?", (key,))
        return row[0]["v"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )

    # ---------------------------------------------------------------- #
    # repos
    # ---------------------------------------------------------------- #
    def upsert_repo(self, meta: dict[str, Any]) -> int:
        """写入/更新仓库元数据，返回 repo_id。"""
        full_name = meta["full_name"]
        row = self.query("SELECT repo_id, first_seen_at FROM repos WHERE full_name=?", (full_name,))
        first_seen = row[0]["first_seen_at"] if row else meta.get("first_seen_at") or str(date.today())
        if "/" in full_name:
            owner, name = full_name.split("/", 1)
        else:  # pragma: no cover - 兜底
            owner, name = "", full_name

        topics = meta.get("topics") or []
        fields = {
            # 无 API id 时用稳定哈希（不能用内置 hash：PYTHONHASHSEED 随机化会导致跨进程漂移）
            "repo_id": (meta.get("repo_id")
                        or (row[0]["repo_id"] if row else None)
                        or (zlib.crc32(full_name.encode("utf-8")) & 0xFFFFFFFF))
            & ((1 << 63) - 1),
            "full_name": full_name,
            "owner": meta.get("owner") or owner,
            "name": meta.get("name") or name,
            "created_at": meta.get("created_at"),
            "first_seen_at": first_seen,
            "language": meta.get("language"),
            "topics": json.dumps(topics, ensure_ascii=False),
            "description": (meta.get("description") or "")[:2000],
            "homepage": meta.get("homepage"),
            "is_fork": int(bool(meta.get("is_fork"))),
            "parent_repo": meta.get("parent_repo"),
            "archived": int(bool(meta.get("archived"))),
            "default_branch": meta.get("default_branch"),
            "license_name": (meta.get("license") or {}).get("spdx_id")
            if isinstance(meta.get("license"), dict) else meta.get("license_name"),
            "open_issues": int(meta.get("open_issues") or 0),
            # 画像类字段：无值时置 None，UPDATE 时跳过，避免覆盖 deep_profile 等已写入的结果
            "vertical": json.dumps(meta["vertical"], ensure_ascii=False)
            if meta.get("vertical") else None,
            "author_trust": float(meta["author_trust"])
            if meta.get("author_trust") is not None else None,
            "suspect_flags": json.dumps(meta["suspect_flags"], ensure_ascii=False)
            if meta.get("suspect_flags") else None,
            "simhash": meta.get("simhash"),
            "last_enriched_at": meta.get("last_enriched_at"),
        }
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        # None 字段不参与 UPDATE（保留库中原值），防止元数据补全把画像字段清零
        update_cols = [c for c in fields
                      if c not in ("repo_id", "first_seen_at") and fields[c] is not None]
        if update_cols:
            updates = ",".join(f"{c}=excluded.{c}" for c in update_cols)
            self.execute(
                f"INSERT INTO repos({cols}) VALUES({marks}) "
                f"ON CONFLICT(full_name) DO UPDATE SET {updates}",
                tuple(fields.values()),
            )
        else:
            self.execute(f"INSERT OR IGNORE INTO repos({cols}) VALUES({marks})",
                         tuple(fields.values()))
        got = self.query("SELECT repo_id FROM repos WHERE full_name=?", (full_name,))[0]
        return int(got["repo_id"])

    def get_repo(self, full_name: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM repos WHERE full_name=?", (full_name,))
        return rows[0] if rows else None

    def get_repo_by_id(self, repo_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM repos WHERE repo_id=?", (repo_id,))
        return rows[0] if rows else None

    def iter_repos(self, since: str | None = None) -> list[sqlite3.Row]:
        if since:
            return self.query("SELECT * FROM repos WHERE first_seen_at>=?", (since,))
        return self.query("SELECT * FROM repos")

    def repo_count(self) -> int:
        return int(self.query("SELECT COUNT(*) c FROM repos")[0]["c"])

    def update_repo_fields(self, repo_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE repos SET {sets} WHERE repo_id=?", (*fields.values(), repo_id))

    # ---------------------------------------------------------------- #
    # snapshots / star series
    # ---------------------------------------------------------------- #
    def save_snapshot(self, repo_id: int, snap_date: str, snap: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO repo_snapshots(repo_id,snap_date,stars,forks,watchers,open_issues,"
            "pushed_at,commit_count_30d,contributors) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(repo_id,snap_date) DO UPDATE SET stars=excluded.stars,forks=excluded.forks,"
            "watchers=excluded.watchers,open_issues=excluded.open_issues,pushed_at=excluded.pushed_at,"
            "commit_count_30d=excluded.commit_count_30d,contributors=excluded.contributors",
            (
                repo_id, snap_date,
                int(snap.get("stars") or 0), int(snap.get("forks") or 0),
                int(snap.get("watchers") or 0), int(snap.get("open_issues") or 0),
                snap.get("pushed_at"), int(snap.get("commit_count_30d") or 0),
                int(snap.get("contributors") or 0),
            ),
        )

    def latest_snapshot(self, repo_id: int) -> sqlite3.Row | None:
        rows = self.query(
            "SELECT * FROM repo_snapshots WHERE repo_id=? ORDER BY snap_date DESC LIMIT 1", (repo_id,))
        return rows[0] if rows else None

    def save_star_weekly(self, repo_id: int, week_epoch: int, total: int, days: list[int]) -> None:
        self.execute(
            "INSERT INTO star_weekly(repo_id,week_epoch,total,days) VALUES(?,?,?,?) "
            "ON CONFLICT(repo_id,week_epoch) DO UPDATE SET total=excluded.total, days=excluded.days",
            (repo_id, week_epoch, total, json.dumps(days)),
        )

    def save_star_daily(self, repo_id: int, series: Sequence[tuple[str, int]]) -> None:
        rows = [(repo_id, d, int(c)) for d, c in series]
        if rows:
            self.executemany(
                "INSERT INTO star_daily(repo_id,day,stars_delta) VALUES(?,?,?) "
                "ON CONFLICT(repo_id,day) DO UPDATE SET stars_delta=excluded.stars_delta",
                rows,
            )

    def star_series(self, repo_id: int, since: str | None = None) -> list[tuple[str, int]]:
        if since:
            rows = self.query(
                "SELECT day, stars_delta FROM star_daily WHERE repo_id=? AND day>=? ORDER BY day",
                (repo_id, since))
        else:
            rows = self.query(
                "SELECT day, stars_delta FROM star_daily WHERE repo_id=? ORDER BY day", (repo_id,))
        return [(r["day"], int(r["stars_delta"])) for r in rows]

    # ---------------------------------------------------------------- #
    # signals / scores / events
    # ---------------------------------------------------------------- #
    def add_signal(self, repo_id: int, ts: str, source: str, metric: str,
                   value: float, url: str = "", raw: Any = None) -> None:
        self.execute(
            "INSERT INTO signals(repo_id,ts,source,metric,value,url,raw) VALUES(?,?,?,?,?,?,?)",
            (repo_id, ts, source, metric, float(value), url,
             json.dumps(raw, ensure_ascii=False) if raw is not None else None),
        )

    def signals_for(self, source: str, since_ts: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM signals WHERE source=? AND ts>=? ORDER BY value DESC", (source, since_ts))

    def repo_signals(self, repo_id: int, since_ts: str | None = None) -> list[sqlite3.Row]:
        if since_ts:
            return self.query("SELECT * FROM signals WHERE repo_id=? AND ts>=?", (repo_id, since_ts))
        return self.query("SELECT * FROM signals WHERE repo_id=?", (repo_id,))

    def save_score(self, repo_id: int, score_date: str, final_score: float, tier: str,
                   raw_components: dict[str, Any], vertical_scores: dict[str, float]) -> None:
        self.execute(
            "INSERT INTO scores(repo_id,score_date,final_score,tier,raw_components,vertical_scores) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(repo_id,score_date) DO UPDATE SET "
            "final_score=excluded.final_score,tier=excluded.tier,"
            "raw_components=excluded.raw_components,vertical_scores=excluded.vertical_scores",
            (repo_id, score_date, float(final_score), tier,
             json.dumps(raw_components, ensure_ascii=False),
             json.dumps(vertical_scores, ensure_ascii=False)),
        )

    def scores_for_date(self, score_date: str, min_score: float = 0) -> list[sqlite3.Row]:
        return self.query(
            "SELECT s.*, r.full_name, r.description, r.language, r.topics, r.created_at, r.vertical "
            "FROM scores s JOIN repos r ON r.repo_id=s.repo_id "
            "WHERE s.score_date=? AND s.final_score>=? ORDER BY s.final_score DESC",
            (score_date, min_score))

    def score_history(self, repo_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM scores WHERE repo_id=? ORDER BY score_date", (repo_id,))

    def add_events(self, rows: Sequence[tuple[str, int | None, str, str, str]]) -> int:
        if not rows:
            return 0
        self.executemany(
            "INSERT OR IGNORE INTO events(event_id,repo_id,type,actor,created_at) VALUES(?,?,?,?,?)",
            rows)
        return len(rows)

    def save_digest(self, day: str, vertical: str, payload: Any) -> None:
        self.execute("INSERT INTO digests(date,vertical,payload) VALUES(?,?,?)",
                     (day, vertical, json.dumps(payload, ensure_ascii=False)))

    # ---------------------------------------------------------------- #
    # LLM 缓存 / watchlist
    # ---------------------------------------------------------------- #
    def llm_get(self, repo_id: int, prompt_version: str, content_hash: str) -> dict[str, Any] | None:
        rows = self.query(
            "SELECT payload FROM llm_cache WHERE repo_id=? AND prompt_version=? AND content_hash=?",
            (repo_id, prompt_version, content_hash))
        return json.loads(rows[0]["payload"]) if rows else None

    def llm_put(self, repo_id: int, prompt_version: str, content_hash: str, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO llm_cache(repo_id,prompt_version,content_hash,payload,created_at) "
            "VALUES(?,?,?,?,datetime('now')) ON CONFLICT(repo_id,prompt_version,content_hash) "
            "DO UPDATE SET payload=excluded.payload",
            (repo_id, prompt_version, content_hash, json.dumps(payload, ensure_ascii=False)))

    def watchlist(self) -> list[str]:
        return [r["full_name"] for r in self.query("SELECT full_name FROM watchlist ORDER BY added_at")]

    def add_watch(self, full_name: str, note: str = "") -> None:
        self.execute(
            "INSERT INTO watchlist(full_name,note,added_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(full_name) DO UPDATE SET note=excluded.note", (full_name, note))

    # ---------------------------------------------------------------- #
    # 清理
    # ---------------------------------------------------------------- #
    def prune(self, keep_days: int = 90, event_keep_days: int = 30,
              http_keep_days: int = 30) -> dict[str, int]:
        cutoff = str(date.today() - timedelta(days=keep_days))
        event_cutoff = str(date.today() - timedelta(days=event_keep_days))
        with self._lock:
            c1 = self.conn.execute("DELETE FROM star_daily WHERE day<?", (cutoff,)).rowcount
            c2 = self.conn.execute("DELETE FROM events WHERE created_at<?", (event_cutoff,)).rowcount
            c3 = self.conn.execute("DELETE FROM signals WHERE ts<?", (cutoff,)).rowcount
            c4 = self.conn.execute("DELETE FROM digests WHERE date<?", (cutoff,)).rowcount
            c5 = self.conn.execute(
                "DELETE FROM http_cache WHERE updated_at < datetime('now', ?)",
                (f"-{http_keep_days} days",)).rowcount
            self.conn.commit()
        return {"star_daily": c1, "events": c2, "signals": c3, "digests": c4,
                "http_cache": c5}

    def stats(self) -> dict[str, int]:
        out = {}
        for t in ("repos", "repo_snapshots", "star_daily", "star_weekly", "signals",
                  "scores", "events", "digests", "llm_cache"):
            out[t] = int(self.query(f"SELECT COUNT(*) c FROM {t}")[0]["c"])
        return out
