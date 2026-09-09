"""领域分类：把仓库归入 6 个垂直频道 + 财经 5 个子赛道。

规则优先（topic / 名称 / 描述 的中英双语关键词），成本为零、可解释；
P2 阶段可外接轻量 embedding 分类器兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "ai": [
        "llm", "agent", "agents", "mcp", "rag", "gpt", "chatgpt", "prompt",
        "transformer", "diffusion", "inference", "vllm", "langchain", "openai",
        "大模型", "智能体", "多模态", "推理", "提示词", "向量", "knowledge-base",
    ],
    "finance": [
        "quant", "quantitative", "trading", "trader", "backtest", "backtesting",
        "stock", "stocks", "finance", "financial", "investment", "investor",
        "alpha", "portfolio", "orderflow", "order-flow", "market-data", "ticker",
        "量化", "交易", "股票", "A股", "港股", "美股", "期货", "期权", "基金",
        "投研", "因子", "回测", "行情", "龙虎榜", "大宗交易", "订单流", "大单",
        "北向资金", "主力资金", "资金流", "公告", "涨停", "选股", "财务",
    ],
    "tech": [
        "news", "rss", "reader", "aggregator", "digest", "headline",
        "资讯", "新闻", "阅读", "聚合", "订阅",
    ],
    "security": [
        "security", "cve", "exploit", "pentest", "penetration", "ctf", "malware",
        "reverse", "reversing", "vulnerability", "fuzzing", "redteam", "红队",
        "逆向", "渗透", "漏洞", "木马", "免杀", "取证",
    ],
    "tools": [
        "cli", "tool", "tools", "toolkit", "productivity", "self-hosted", "utility",
        "launcher", "terminal", "automation", "效率", "工具", "命令行", "自动化",
    ],
    "rising": [],
}

TOPIC_HINTS: dict[str, list[str]] = {
    "ai": ["llm", "agent", "agents", "mcp", "rag", "gpt", "chatgpt", "openai",
           "transformer", "diffusion", "prompt-engineering", "langchain"],
    "finance": ["quant", "quantitative-finance", "trading", "algorithmic-trading",
                "backtesting", "stock", "finance", "financial", "investment",
                "alternative-data", "crypto", "trading-bot"],
    "tech": ["news", "rss", "aggregator", "hacker-news"],
    "security": ["security", "ctf", "pentest", "malware", "reverse-engineering", "cve"],
    "tools": ["cli", "productivity", "self-hosted", "dotfiles", "terminal"],
    "rising": [],
}

FINANCE_SUBTRACKS: dict[str, list[str]] = {
    "量化框架与回测": ["quant", "backtest", "backtesting", "algorithmic-trading", "量化", "回测", "策略"],
    "行情与数据源": ["stock", "market-data", "ticker", "tushare", "akshare", "行情", "A股", "同花顺", "数据源"],
    "前沿投资研究": ["investment", "alpha", "alternative-data", "portfolio", "投研", "因子", "研究"],
    "重大订单与交易信号": ["龙虎榜", "大宗交易", "订单流", "orderflow", "order-flow", "大单",
                          "北向资金", "主力资金", "资金流", "公告", "涨停"],
    "投研 Agent": ["research agent", "trading agent", "investor", "投研 agent", "research-agent"],
}

_WORD_SPLIT = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return {t for t in _WORD_SPLIT.split(text.lower()) if t}


def _has_non_ascii(word: str) -> bool:
    return any(ord(ch) > 127 for ch in word)


_WORD_BOUNDARY: dict[str, "re.Pattern[str]"] = {}


def _word_bounded(word: str, text: str) -> bool:
    """纯 ASCII 词按词边界匹配，避免 management→agent、tooltip→tool 类子串误报。"""
    pat = _WORD_BOUNDARY.get(word)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(word)}\b")
        _WORD_BOUNDARY[word] = pat
    return pat.search(text) is not None


@dataclass
class Classification:
    primary: str = "rising"
    scores: dict[str, float] = field(default_factory=dict)
    subtrack: str = ""
    matched: list[str] = field(default_factory=list)

    @property
    def vertical_match(self) -> float:
        """用于打分放大：0.85 ~ 1.15。"""
        return 0.85 + 0.30 * min(max(self.scores.get(self.primary, 0.0), 0.0), 1.0)


class Classifier:
    def __init__(self, extra_keywords: dict[str, list[str]] | None = None,
                 enabled_channels: Iterable[str] | None = None) -> None:
        self.keywords = {k: [w.lower() for w in v] for k, v in DEFAULT_KEYWORDS.items()}
        for k, v in (extra_keywords or {}).items():
            self.keywords.setdefault(k, [])
            self.keywords[k].extend(w.lower() for w in v)
        self.enabled = set(enabled_channels) if enabled_channels else set(self.keywords)

    # ------------------------------------------------------------------ #
    def classify(self, meta: dict[str, Any]) -> Classification:
        topics = {str(t).lower() for t in (meta.get("topics") or [])}
        name = str(meta.get("name") or "")
        full_name = str(meta.get("full_name") or "")
        desc = str(meta.get("description") or "")
        name_tokens = tokenize(name) | tokenize(full_name.replace("/", " "))
        desc_tokens = tokenize(desc)
        haystack_text = f"{full_name} {name} {desc}".lower()

        scores: dict[str, float] = {}
        matched: list[str] = []
        subtrack = ""

        for channel, words in self.keywords.items():
            if channel not in self.enabled or not words:
                continue
            hit = 0.0
            for w in words:
                wl = w.lower()
                if wl in topics:
                    hit += 2.0
                    matched.append(f"topic:{wl}")
                elif " " in wl:
                    if wl in haystack_text:
                        hit += 1.5
                        matched.append(wl)
                elif wl in name_tokens:
                    hit += 1.5
                    matched.append(wl)
                elif wl in desc_tokens:
                    hit += 1.0
                    matched.append(wl)
                elif _has_non_ascii(wl):
                    # 中文等 CJK 词只能子串匹配
                    if wl in haystack_text:
                        hit += 1.0
                        matched.append(wl)
                elif _word_bounded(wl, haystack_text):
                    hit += 1.0
                    matched.append(wl)
            if hit > 0:
                scores[channel] = min(hit / 4.0, 1.0)

        for channel, hints in TOPIC_HINTS.items():
            if channel not in self.enabled:
                continue
            if topics & set(hints):
                scores[channel] = min(scores.get(channel, 0.0) + 0.6, 1.0)

        primary = max(scores, key=scores.get) if scores else "rising"
        if primary == "finance":
            subtrack = self._subtrack(haystack_text, topics)
        return Classification(primary=primary, scores=scores, subtrack=subtrack,
                              matched=sorted(set(matched))[:12])

    @staticmethod
    def _subtrack(text: str, topics: set[str]) -> str:
        for name, words in FINANCE_SUBTRACKS.items():
            for w in words:
                if " " in w:
                    if w in text:
                        return name
                elif w in topics or w in text:
                    return name
        return "其他财经"
