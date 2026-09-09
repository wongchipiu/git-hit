"""Markdown 日报渲染。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TIER_LABEL = {
    "explosive": "🔥 爆款前夜",
    "high": "⭐ 高潜",
    "watch": "👀 观察",
}

TIER_MARK = {"explosive": "🔥", "high": "⭐", "watch": "👀"}


@dataclass
class DigestItem:
    full_name: str
    score: float = 0.0
    tier: str = "watch"
    vertical: str = ""
    subtrack: str = ""
    stars: int = 0
    stars_7d: int = 0
    stars_30d: int = 0
    accel: float = 0.0
    language: str = ""
    created_at: str = ""
    age_days: int = 0
    description: str = ""
    topics: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    external_desc: str = ""
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    spark: list[int] = field(default_factory=list)
    llm: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def tier_label(self) -> str:
        return TIER_LABEL.get(self.tier, "👀 观察")


@dataclass
class Digest:
    day: str
    stats: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, list[DigestItem]] = field(default_factory=dict)
    channel_titles: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.sections.values())


def render_markdown(digest: Digest, cfg: Any) -> str:
    lines: list[str] = [f"# GitHub 宝藏雷达 · {digest.day}", ""]
    s = digest.stats
    lines.append(
        f"> 扫描候选 **{s.get('candidates', 0)}** 个 · 增速核验 **{s.get('star_history', 0)}** 个 · "
        f"深度画像 **{s.get('deep_profile', 0)}** 个 · 入选 **{digest.total}** 个"
    )
    lines.append("")

    if digest.total == 0:
        lines.append("_今日无满足质量红线的项目（宁缺毋滥）。_")
        lines.append("")
        return "\n".join(lines)

    for ch, items in digest.sections.items():
        if not items:
            continue
        title = digest.channel_titles.get(ch, ch)
        lines.append(f"## {title}")
        lines.append("")
        for idx, it in enumerate(items, 1):
            lines.append(
                f"### {idx}. {it.full_name} {TIER_MARK.get(it.tier,'👀')} {it.score} · {it.tier_label}"
            )
            lines.append("")
            if it.llm:
                if it.llm.get("highlight"):
                    lines.append(f"- **亮点**：{it.llm['highlight']}")
                if it.llm.get("why_now"):
                    lines.append(f"- **为什么现在**：{it.llm['why_now']}")
                if it.llm.get("risk"):
                    lines.append(f"- **风险**：{it.llm['risk']}")
                if it.llm.get("similar") and it.llm["similar"] not in ("无", ""):
                    lines.append(f"- **同类对比**：{it.llm['similar']}")
            elif it.description:
                lines.append(f"- **简介**：{it.description[:160]}")
            lines.append(
                f"- **数据**：⭐ {it.stars:,} · 7 日 +{it.stars_7d} · 30 日 +{it.stars_30d} · "
                f"加速度 {it.accel:.2f}x · 建库 {it.age_days} 天 · {it.language or '语言未知'}"
            )
            if it.subtrack:
                lines.append(f"- **赛道**：{it.subtrack}")
            src = " · ".join(it.sources[:4]) or "—"
            extra = f" · {it.external_desc}" if it.external_desc else ""
            lines.append(f"- **信号**：{src}{extra}")
            if it.flags:
                lines.append(f"- **提示**：{'、'.join(it.flags)}")
            lines.append(f"- **链接**：{it.url}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_由 Treasure Radar 自动生成（{cfg.report.get('per_channel_limit', 10)} 条/频道上限）_")
    lines.append("")
    return "\n".join(lines)


def digest_to_json(digest: Digest) -> dict[str, Any]:
    return {
        "date": digest.day,
        "stats": digest.stats,
        "sections": {
            ch: [
                {
                    "full_name": it.full_name, "url": it.url, "score": it.score,
                    "tier": it.tier, "vertical": it.vertical, "subtrack": it.subtrack,
                    "stars": it.stars, "stars_7d": it.stars_7d, "stars_30d": it.stars_30d,
                    "accel": it.accel, "language": it.language, "created_at": it.created_at,
                    "age_days": it.age_days, "description": it.description,
                    "topics": it.topics, "sources": it.sources,
                    "external": it.external_desc, "flags": it.flags,
                    "reasons": it.reasons, "spark": it.spark, "llm": it.llm,
                }
                for it in items
            ]
            for ch, items in digest.sections.items()
        },
    }
