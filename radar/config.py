"""配置加载：config.toml（结构） + 环境变量（密钥）。

密钥永不写入配置文件：
* GitHub 令牌 -> 环境变量 ``GITHUB_TOKEN``
* DeepSeek Key -> 环境变量 ``DEEPSEEK_API_KEY``
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅 Python 3.10 及以下
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config.example.toml"
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ChannelGroup:
    name: str
    queries: list[str] = field(default_factory=list)


@dataclass
class Channel:
    key: str
    name: str
    emoji: str = ""
    enabled: bool = True
    min_stars: int = 0
    keywords: list[str] = field(default_factory=list)   # P3：个性化关键词（叠加到内置词表）
    groups: list[ChannelGroup] = field(default_factory=list)

    def all_queries(self) -> list[str]:
        return [q for g in self.groups for q in g.queries]

    def group_names(self) -> list[str]:
        return [g.name for g in self.groups]


@dataclass
class Limits:
    core_per_hour: int = 3000
    search_per_minute: int = 20
    min_interval: float = 0.4
    max_sleep_on_limit: int = 120
    max_requests_run: int = 300
    enable_etag: bool = True


@dataclass
class Config:
    root: Path = ROOT
    general: dict[str, Any] = field(default_factory=dict)
    github: dict[str, Any] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    sources: dict[str, Any] = field(default_factory=dict)
    schedule: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    scoring: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)
    channels: list[Channel] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    watch_keywords: list[str] = field(default_factory=list)
    exclude: dict[str, list[str]] = field(default_factory=dict)

    # ---- 路径 ---- #
    @property
    def data_dir(self) -> Path:
        return self._dir(self.general.get("data_dir", "data"))

    @property
    def output_dir(self) -> Path:
        return self._dir(self.general.get("output_dir", "output"))

    @property
    def log_dir(self) -> Path:
        return self._dir(self.general.get("log_dir", "logs"))

    def _dir(self, value: str) -> Path:
        p = Path(value)
        p = p if p.is_absolute() else (self.root / p)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.db"

    # ---- 密钥 ---- #
    @property
    def github_token(self) -> str:
        return os.environ.get(self.github.get("token_env", "GITHUB_TOKEN"), "").strip()

    @property
    def llm_api_key(self) -> str:
        return os.environ.get(self.llm.get("api_key_env", "DEEPSEEK_API_KEY"), "").strip()

    @property
    def user_agent(self) -> str:
        return self.general.get("user_agent", "TreasureRadar/1.0 (personal research)")

    def channel(self, key: str) -> Channel | None:
        for c in self.channels:
            if c.key == key:
                return c
        return None

    def ordered_channels(self) -> list[Channel]:
        order = self.report.get("order", [])
        enabled = [c for c in self.channels if c.enabled]
        enabled.sort(key=lambda c: order.index(c.key) if c.key in order else 999)
        return enabled


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = ENV_PATH) -> None:
    """极简 .env 加载（不引入第三方依赖），已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def ensure_config(path: Path = CONFIG_PATH) -> Path:
    """首次运行时从模板生成 config.toml。"""
    if not path.exists():
        shutil.copy(EXAMPLE_CONFIG, path)
    return path


def load_config(path: Path | None = None) -> Config:
    load_dotenv()
    path = path or CONFIG_PATH
    if not path.exists():
        path = ensure_config(path)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    cfg = Config()
    cfg.general = raw.get("general", {})
    cfg.github = raw.get("github", {})
    cfg.sources = raw.get("sources", {})
    cfg.schedule = raw.get("schedule", {})
    cfg.llm = raw.get("llm", {})
    cfg.scoring = raw.get("scoring", {})
    cfg.report = raw.get("report", {})
    cfg.delivery = raw.get("delivery", {})
    _limit_fields = {f.name for f in fields(Limits)}
    cfg.limits = Limits(**{k: v for k, v in raw.get("limits", {}).items()
                           if k in _limit_fields})

    for ch in raw.get("channels", []):
        groups = [ChannelGroup(name=g.get("name", ""), queries=list(g.get("queries", [])))
                  for g in ch.get("groups", [])]
        cfg.channels.append(Channel(
            key=ch.get("key", ""),
            name=ch.get("name", ch.get("key", "")),
            emoji=ch.get("emoji", ""),
            enabled=bool(ch.get("enabled", True)),
            min_stars=int(ch.get("min_stars", 0)),
            keywords=[str(k) for k in ch.get("keywords", [])],
            groups=groups,
        ))

    wl = raw.get("watchlist", {})
    cfg.watchlist = [str(x) for x in wl.get("repos", [])]
    cfg.watch_keywords = [str(x) for x in wl.get("keywords", [])]
    ex = raw.get("exclude", {})
    cfg.exclude = {k: [str(i).lower() for i in v] for k, v in ex.items() if isinstance(v, list)}
    return cfg


def local_today(cfg: Any) -> date:
    """按配置时区返回今天（报告 / 存档 / 快照的日期口径）。

    注意：star_daily 的日期来自 GitHub API（UTC 口径），与之配套的
    compute_growth / expand_weekly 仍用 UTC today，勿改。
    """
    tz_name = ""
    try:
        tz_name = str((getattr(cfg, "general", None) or {}).get("timezone", ""))
    except Exception:  # noqa: BLE001 - cfg 形态异常时退回 UTC
        tz_name = ""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name or "Asia/Shanghai")).date()
    except Exception:  # noqa: BLE001 - 无效时区 / Windows 缺 tzdata 时退回 UTC
        return datetime.now(timezone.utc).date()
