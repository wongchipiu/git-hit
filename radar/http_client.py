"""通用 HTTP 传输层（仅标准库 urllib）。

合规/节流是这一层的核心职责：
* 全局令牌桶：core / search 两套独立配额，任意请求前取令牌；
* 单轮请求预算 ``max_requests_run``：超预算直接抛错，杜绝"跑飞"；
* 403/429：读 ``Retry-After`` / ``X-RateLimit-Reset`` 退避；等待超过上限则抛
  :class:`RateLimited`，由上层"本轮跳过、下轮自愈"；
* ETag 条件请求：命中 304 不计费。
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .logging_setup import get_logger

log = get_logger("radar.http")


class RateLimited(RuntimeError):
    """命中限流且等待时间过长，本轮应跳过。"""


class BudgetExceeded(RuntimeError):
    """单轮请求预算耗尽。"""


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float = field(init=False)
    updated: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def take(self, n: float = 1.0) -> float:
        """取令牌，返回需要等待的秒数（0 表示立即可用）。"""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        need = (n - self.tokens) / self.refill_per_sec if self.refill_per_sec > 0 else 1.0
        return max(need, 0.0)


class NullCache:
    def get(self, key: str) -> Any | None:  # pragma: no cover - 接口占位
        return None

    def set(self, key: str, value: Any) -> None:  # pragma: no cover - 接口占位
        return None


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


class HttpClient:
    """带节流、退避、ETag 的只读 HTTP 客户端。"""

    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        core_per_hour: int = 3000,
        search_per_minute: int = 20,
        min_interval: float = 0.4,
        max_sleep_on_limit: int = 120,
        max_requests_run: int = 300,
        enable_etag: bool = True,
        cache: Any | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_sleep_on_limit = max_sleep_on_limit
        self.max_requests_run = max_requests_run
        self.enable_etag = enable_etag
        self.cache = cache or NullCache()
        self.buckets = {
            "core": TokenBucket(capacity=max(core_per_hour // 6, 20),
                                refill_per_sec=core_per_hour / 3600.0),
            "search": TokenBucket(capacity=max(search_per_minute, 1),
                                  refill_per_sec=search_per_minute / 60.0),
        }
        self._last_request = 0.0
        self.used = 0
        self.stats = {"requests": 0, "from_cache": 0, "rate_limited": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    def _wait(self, bucket: str) -> None:
        wait = self.buckets[bucket].take()
        since = time.monotonic() - self._last_request
        wait = max(wait, self.min_interval - since)
        if wait > 0:
            time.sleep(wait)

    def _handle_limit(self, resp_headers: dict[str, str], status: int) -> float:
        """返回建议等待秒数；0 表示无需等待。"""
        retry_after = resp_headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        remaining = resp_headers.get("x-ratelimit-remaining")
        reset = resp_headers.get("x-ratelimit-reset")
        if remaining == "0" and reset:
            try:
                return max(float(reset) - time.time(), 0.0) + 1.0
            except ValueError:
                return 0.0
        if status == 429:
            return 30.0
        return 0.0

    def request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        bucket: str = "core",
    ) -> Response:
        if self.used >= self.max_requests_run:
            raise BudgetExceeded(f"单轮请求预算耗尽（{self.max_requests_run}）")

        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"

        hdrs = {
            "User-Agent": self.user_agent,
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "gzip",
        }
        if headers:
            hdrs.update(headers)

        etag = self.cache.get(f"etag:{url}") if self.enable_etag else None
        if etag:
            hdrs["If-None-Match"] = etag

        self._wait(bucket)
        attempts = 0
        while True:
            attempts += 1
            self._last_request = time.monotonic()
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    enc = (r.headers.get("Content-Encoding") or "").lower()
                    if enc == "gzip":
                        raw = gzip.decompress(raw)
                    elif enc == "deflate":
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    rh = {k.lower(): v for k, v in r.headers.items()}
                    self.used += 1
                    self.stats["requests"] += 1
                    if self.enable_etag and r.status == 200 and rh.get("etag"):
                        self.cache.set(f"etag:{url}", rh["etag"])
                    return Response(r.status, rh, raw, url)

            except urllib.error.HTTPError as e:
                rh = {k.lower(): v for k, v in (e.headers or {}).items()}
                self.used += 1
                self.stats["requests"] += 1

                if e.code == 304 and self.enable_etag:
                    body = self.cache.get(f"body:{url}") or b""
                    self.stats["from_cache"] += 1
                    return Response(304, rh, body, url, from_cache=True)

                if e.code in (403, 429):
                    wait = self._handle_limit(rh, e.code)
                    is_limit = (rh.get("x-ratelimit-remaining") == "0") or e.code == 429
                    if is_limit and wait > 0:
                        self.stats["rate_limited"] += 1
                        if wait > self.max_sleep_on_limit:
                            raise RateLimited(
                                f"限流需等待 {wait:.0f}s（上限 {self.max_sleep_on_limit}s），本轮跳过"
                            )
                        log.warning("命中限流，退避 %.0fs 后重试", wait)
                        time.sleep(wait + random.uniform(0.1, 0.6))
                        if attempts <= 2:
                            continue
                    self.stats["errors"] += 1
                    raise

                if e.code in (500, 502, 503, 504) and attempts <= 3:
                    time.sleep(min(2 ** attempts, 8) + random.uniform(0, 0.5))
                    continue

                self.stats["errors"] += 1
                raise

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.stats["errors"] += 1
                if attempts <= 3:
                    time.sleep(min(2 ** attempts, 8))
                    continue
                raise RuntimeError(f"网络错误：{url} -> {e}") from e

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.request(url, **kw)
        data = resp.json()
        if self.enable_etag and resp.status == 200:
            self.cache.set(f"body:{url}", resp.body)
        return data

    def post_json(self, url: str, payload: Any, headers: dict[str, str] | None = None,
                  bucket: str = "core") -> Any:
        """POST JSON（仅用于 GraphQL）。同样受令牌桶与预算约束。"""
        if self.used >= self.max_requests_run:
            raise BudgetExceeded(f"单轮请求预算耗尽（{self.max_requests_run}）")
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json",
                "Content-Type": "application/json", "Accept-Encoding": "gzip"}
        if headers:
            hdrs.update(headers)
        body = json.dumps(payload).encode("utf-8")
        self._wait(bucket)
        self._last_request = time.monotonic()
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
        self.used += 1
        self.stats["requests"] += 1
        return json.loads(raw.decode("utf-8")) if raw else None


class OfflineClient:
    """离线模式客户端：不发出任何网络请求，仅从夹具数据取样例。

    用于开发/自测，保证"点到即止、不产生真实流量"。
    """

    def __init__(self, payloads: dict[str, Any] | None = None) -> None:
        self.payloads = payloads or {}
        self.stats = {"requests": 0, "from_cache": 0, "rate_limited": 0, "errors": 0}
        self.used = 0

    def get_json(self, url: str, **_kw: Any) -> Any:
        self.used += 1
        self.stats["requests"] += 1
        for key, value in self.payloads.items():
            if key in url:
                return value
        return self.payloads.get("__default__")
