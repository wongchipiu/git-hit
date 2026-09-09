"""DeepSeek 点评（L3）。

设计原则：
* **可降级**：无 Key / 限流 / 断服时返回空点评，报告照常产出；
* **可缓存**：按 (repo_id, prompt_version, 内容哈希) 落 SQLite，同一仓库不重复烧钱；
* **有上限**：每日点评条数由 ``llm.max_items_per_day`` 控制。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..logging_setup import get_logger

log = get_logger("radar.llm")

SYSTEM_PROMPT = (
    "你是一位资深的开源项目猎手，擅长从海量新仓库中识别真正有潜力的宝藏项目。"
    "请用简体中文回答，风格克制、信息密度高，避免空话和营销腔。"
)

USER_TEMPLATE = """请研判下面这个 GitHub 仓库是否值得关注。

仓库：{full_name}
语言：{language}
星数：{stars}（近 7 天 +{stars_7d}，近 30 天 +{stars_30d}）
创建于：{created_at}（{age_days} 天）
简介：{description}
主题标签：{topics}
外部热度：{external}

请严格按以下 JSON 输出（不要输出多余文字、不要 markdown 代码块）：
{{
  "highlight": "一句话亮点，不超过 40 字",
  "why_now": "为什么现在值得关注，不超过 60 字",
  "risk": "主要风险或不确定性，不超过 40 字；若无明显风险填『暂无明显风险』",
  "similar": "最相近的同类知名项目名（1 个），没有则填『无』",
  "verdict": "ignore 或 watch 或 recommend"
}}
"""


class Analyst:
    def __init__(self, cfg: Any, store: Any | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.enabled = bool(cfg.llm.get("enabled", True))
        self.api_key = cfg.llm_api_key
        self.base = str(cfg.llm.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = cfg.llm.get("model", "deepseek-chat")
        self.timeout = float(cfg.llm.get("timeout", 60))
        self.temperature = float(cfg.llm.get("temperature", 0.3))
        self.max_items = int(cfg.llm.get("max_items_per_day", 60))
        self.prompt_version = str(cfg.llm.get("prompt_version", "v1"))
        self.use_cache = bool(cfg.llm.get("cache_enabled", True))
        self.calls = 0

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    @staticmethod
    def content_hash(meta: dict[str, Any]) -> str:
        seed = "|".join([
            str(meta.get("full_name") or ""),
            str(meta.get("description") or "")[:400],
            ",".join(map(str, meta.get("topics") or [])),
            str(meta.get("stars") or 0),
            str(meta.get("stars_7d") or 0),
        ])
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ #
    def analyze(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """对候选仓库生成点评，返回 {full_name: 点评}。"""
        out: dict[str, dict[str, Any]] = {}
        if not self.available or not items:
            if not self.available:
                log.info("LLM 未启用或无 Key，本轮跳过点评（报告照常产出）")
            return out

        todo: list[dict[str, Any]] = []
        for it in items[:self.max_items]:
            meta = it.get("meta", it)
            repo_id = int(meta.get("repo_id") or 0)
            ch = self.content_hash(meta)
            cached = self.store.llm_get(repo_id, self.prompt_version, ch) if (self.store and self.use_cache) else None
            if cached:
                out[meta["full_name"]] = cached
            else:
                todo.append(it)

        for it in todo:
            meta = it.get("meta", it)
            result = self._call(meta, it)
            if not result:
                continue
            out[meta["full_name"]] = result
            if self.store and self.use_cache:
                self.store.llm_put(int(meta.get("repo_id") or 0), self.prompt_version,
                                   self.content_hash(meta), result)
            time.sleep(0.2)
        return out

    # ------------------------------------------------------------------ #
    def _call(self, meta: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
        prompt = USER_TEMPLATE.format(
            full_name=meta.get("full_name", ""),
            language=meta.get("language") or "未知",
            stars=meta.get("stars", 0),
            stars_7d=meta.get("stars_7d", 0),
            stars_30d=meta.get("stars_30d", 0),
            created_at=meta.get("created_at") or "未知",
            age_days=meta.get("age_days", 0),
            description=(meta.get("description") or "无")[:300],
            topics=", ".join(map(str, meta.get("topics") or [])) or "无",
            external=item.get("external_desc", "无"),
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": 500,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.cfg.user_agent,
            },
            method="POST",
        )
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                # 429 / 5xx 视为瞬时故障，退避后重试；其余 4xx（密钥/参数错误）立即放弃
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    wait = 2.0 * (attempt + 1)
                    log.warning("DeepSeek 临时错误 HTTP %s，%.0f 秒后重试（%d/3）",
                                e.code, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                log.warning("DeepSeek 调用失败（降级为无点评）：HTTP %s", e.code)
                return None
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as e:
                # 连接重置 / 分块读取不完整（IncompleteRead）等瞬时网络故障，退避重试
                if attempt < 2:
                    wait = 2.0 * (attempt + 1)
                    log.warning("DeepSeek 网络中断（%s），%.0f 秒后重试（%d/3）",
                                type(e).__name__, wait, attempt + 1)
                    time.sleep(wait)
                    continue
                log.warning("DeepSeek 调用失败（降级为无点评）：%s", e)
                return None
            except ValueError as e:
                # 200 但响应体非 JSON（网关异常页等），不重试，直接降级
                log.warning("DeepSeek 响应解析失败（降级为无点评）：%s", e)
                return None
        if data is None:
            return None

        self.calls += 1
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        try:
            obj = json.loads(text)
        except ValueError:
            return {"highlight": text[:80], "why_now": "", "risk": "", "similar": "无",
                    "verdict": "watch", "raw": True}
        return {
            "highlight": str(obj.get("highlight", ""))[:80],
            "why_now": str(obj.get("why_now", ""))[:120],
            "risk": str(obj.get("risk", ""))[:80],
            "similar": str(obj.get("similar", "无"))[:40],
            "verdict": str(obj.get("verdict", "watch")).lower(),
        }
