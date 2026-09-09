"""报告渲染：Markdown 日报 + 自包含 HTML 可视化报告。"""

from .markdown import Digest, DigestItem, digest_to_json, render_markdown
from .html_report import render_html

__all__ = ["Digest", "DigestItem", "digest_to_json", "render_markdown", "render_html"]
