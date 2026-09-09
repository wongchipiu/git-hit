"""自包含 HTML 报告（无外部 CDN 依赖，双击即看）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .markdown import Digest

TEMPLATE_DIR = Path(__file__).parent / "templates"

TIER_CLASS = {"explosive": "tier-explosive", "high": "tier-high", "watch": "tier-watch"}
TIER_LABEL = {"explosive": "🔥 爆款前夜", "high": "⭐ 高潜", "watch": "👀 观察"}


def sparkline_svg(values: list[int], width: int = 140, height: int = 34,
                  color: str = "#4f9cf9") -> str:
    vals = list(values or [])
    if len(vals) < 2:
        vals = vals + [0] * (2 - len(vals))
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(vals)
    step = width / (n - 1)
    pts = []
    for i, v in enumerate(vals):
        x = i * step
        y = height - 3 - (v - lo) / span * (height - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)
    area = f"0,{height} " + polyline + f" {width},{height}"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" role="img" aria-label="近 30 日星增速">'
        f'<polyline points="{area}" fill="{color}" fill-opacity="0.12" stroke="none"/>'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _fallback_html(digest: Digest) -> str:
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>GitHub 宝藏雷达 · {digest.day}</title>",
        "<style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "margin:32px;color:#1f2328;background:#fff}h1{font-size:24px}"
        ".card{border:1px solid #d0d7de;border-radius:8px;padding:14px 16px;margin:12px 0}"
        ".score{float:right;font-weight:700;font-size:18px}"
        "a{color:#0969da;text-decoration:none}</style></head><body>",
        f"<h1>GitHub 宝藏雷达 · {digest.day}</h1>",
    ]
    for ch, items in digest.sections.items():
        parts.append(f"<h2>{digest.channel_titles.get(ch, ch)}</h2>")
        for it in items:
            parts.append(
                f"<div class='card'><span class='score'>{it.score}</span>"
                f"<div><a href='{it.url}' target='_blank'><b>{it.full_name}</b></a> "
                f"{TIER_LABEL.get(it.tier,'')}</div>"
                f"<div>{it.description[:200]}</div>"
                f"<div>⭐ {it.stars} · 7日 +{it.stars_7d} · 加速度 {it.accel:.2f}x</div></div>"
            )
    parts.append("</body></html>")
    return "".join(parts)


def render_html(digest: Digest, cfg: Any | None = None) -> str:
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        return _fallback_html(digest)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html.j2")
    return template.render(
        digest=digest,
        day=digest.day,
        stats=digest.stats,
        sections=digest.sections,
        titles=digest.channel_titles,
        tier_label=TIER_LABEL,
        tier_class=TIER_CLASS,
        spark=sparkline_svg,
        total=digest.total,
    )
