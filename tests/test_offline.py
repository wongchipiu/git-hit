"""离线单元测试：全程不发出任何网络请求。

运行：python -m unittest discover -s tests -v
（点到即止：只覆盖核心算法与一条端到端离线流水线，不产生外部流量）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.config import load_config  # noqa: E402
from radar.core import (  # noqa: E402
    Classifier, Scorer, anti_fraud, compute_growth, dedup_flag, hamming,
    quality_score, simhash,
)
from radar.report import render_html, render_markdown  # noqa: E402
from radar.sources import expand_weekly  # noqa: E402


def _series(values: dict[int, int], today: date | None = None) -> list[tuple[str, int]]:
    today = today or date.today()
    return sorted(((str(today - timedelta(days=i)), v) for i, v in values.items()))


class TestStarHistory(unittest.TestCase):
    def test_expand_weekly_truncates_future_days(self):
        today = date(2026, 9, 9)          # 周三
        sunday = date(2026, 9, 6)
        import datetime as dt
        epoch = int(dt.datetime.combine(sunday, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        rows = [{"week": epoch, "total": 10, "days": [1, 2, 3, 40, 50, 60, 70]}]
        got = expand_weekly(rows, today=today)
        self.assertEqual([d for d, _ in got], [str(sunday + timedelta(days=i)) for i in range(4)])
        # 未来日期（周四~周六）的 50/60/70 必须被丢弃，只保留到今天
        self.assertEqual(sum(v for _, v in got), 46)

    def test_compute_growth_basic(self):
        series = _series({0: 50, 1: 40, 2: 30, 3: 20, 4: 10, 5: 10, 6: 10})
        g = compute_growth(series, stars_total=1000, created_at=str(date.today() - timedelta(days=20)))
        self.assertAlmostEqual(g.v7, sum(v for _, v in series) / 7, places=5)
        self.assertGreater(g.accel, 1.0)
        self.assertEqual(g.age_days, 20)
        self.assertGreaterEqual(g.accel, 0.0)

    def test_expand_weekly_multi_week_overlap(self):
        # 分页边界导致同一周重复返回时，日序列应累加而非覆盖
        today = date(2026, 9, 9)
        import datetime as dt
        w1 = dt.date(2026, 8, 30)  # 周日
        epoch = int(dt.datetime.combine(w1, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        rows = [
            {"week": epoch, "total": 10, "days": [1, 2, 0, 0, 0, 0, 0]},
            {"week": epoch, "total": 5, "days": [3, 0, 0, 0, 0, 0, 0]},
        ]
        got = dict(expand_weekly(rows, today=today))
        self.assertEqual(got.get("2026-08-30"), 4)  # 1 + 3
        self.assertEqual(got.get("2026-08-31"), 2)


class TestClassifier(unittest.TestCase):
    def test_finance_chinese(self):
        c = Classifier()
        r = c.classify({"full_name": "acme/alpha-quant-lab", "name": "alpha-quant-lab",
                        "description": "A 股量化研究框架：因子挖掘 + 回测 + 龙虎榜复盘",
                        "topics": ["quantitative-finance"]})
        self.assertEqual(r.primary, "finance")
        self.assertEqual(r.subtrack, "量化框架与回测")

    def test_ai_topic(self):
        c = Classifier()
        r = c.classify({"full_name": "x/agent", "name": "agent",
                        "description": "minimal agent runtime", "topics": ["agent", "llm"]})
        self.assertEqual(r.primary, "ai")

    def test_english_word_boundary(self):
        # "management" 不应因子串包含 "agent" 而误入 AI 频道
        c = Classifier()
        r = c.classify({"full_name": "x/management", "name": "management",
                        "description": "management tool for containers", "topics": []})
        self.assertNotIn("ai", r.scores)
        self.assertEqual(r.primary, "tools")

    def test_chinese_substring_still_matches(self):
        c = Classifier()
        r = c.classify({"full_name": "x/tick-panel", "name": "tick-panel",
                        "description": "A股龙虎榜数据面板", "topics": []})
        self.assertEqual(r.primary, "finance")


class TestQuality(unittest.TestCase):
    def test_constant_pulse_flagged(self):
        g = compute_growth(_series({i: 30 for i in range(30)}), stars_total=2000,
                           created_at=str(date.today() - timedelta(days=40)))
        v = anti_fraud(g, {"full_name": "a/b", "description": "x", "topics": []},
                       {"commit_count_30d": 0, "contributors": 1})
        self.assertIn("star_farm_suspect", v.flags)
        self.assertIn("constant_pulse", v.flags)
        self.assertGreater(v.penalty, 0)

    def test_simhash_dedup(self):
        a = simhash("A股量化研究框架 因子挖掘 回测")
        b = simhash("A股量化研究框架 因子挖掘 回测 增强版")
        dist = hamming(a, b)
        self.assertLessEqual(dist, 20)
        sup = dedup_flag([{"full_name": "x/1", "simhash": a}, {"full_name": "x/2", "simhash": b}],
                         threshold=dist)
        self.assertEqual(sup, {"x/2"}, f"相似项目应被抑制（汉明距离 {dist}）")

    def test_quality_score_range(self):
        q = quality_score(compute_growth(_series({0: 5})), {"description": "x" * 100,
                                                            "license_name": "MIT"})
        self.assertGreaterEqual(q, 0.0)
        self.assertLessEqual(q, 1.0)

    def test_fading_and_stale_flags(self):
        # 近 7 天趋零、前一周高增 → fading；120 天未推送 → stale
        vals = {i: 2 for i in range(7)}
        vals.update({i: 60 for i in range(7, 14)})
        vals.update({i: 20 for i in range(14, 30)})
        g = compute_growth(_series(vals), stars_total=800,
                           created_at=str(date.today() - timedelta(days=60)))
        pushed = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        v = anti_fraud(g, {"full_name": "a/b", "description": "x", "topics": []},
                       {"pushed_at": pushed})
        self.assertIn("fading", v.flags)
        self.assertIn("stale", v.flags)
        self.assertGreater(v.penalty, 0)


class TestScoring(unittest.TestCase):
    def _cfg(self):
        return load_config()

    def test_tier_and_range(self):
        cfg = self._cfg()
        scorer = Scorer(cfg)
        g = compute_growth(_series({0: 200, 1: 150, 2: 100, 3: 80}), stars_total=3000,
                           created_at=str(date.today() - timedelta(days=20)))
        cls = Classifier().classify({"full_name": "a/b", "description": "agent llm",
                                     "topics": ["llm"]})
        r = scorer.score({"repo_id": 1, "full_name": "a/b"}, g, cls,
                         quality=0.8, external=0.5, author_trust=0.5, z=2.0)
        self.assertGreaterEqual(r.final, 0.0)
        self.assertLessEqual(r.final, 100.0)
        self.assertIn(r.tier, {"explosive", "high", "watch", ""})

    def test_tune_returns_weights(self):
        train = [{"components": {"v7": i, "accel_ratio": i / 2, "burst_ratio": i / 3,
                                 "quality": i / 10, "external": i / 20, "age_factor": 1 - i / 10},
                  "label": float(i)} for i in range(1, 12)]
        out = Scorer.tune(train, iterations=30)
        self.assertIn("spearman", out)
        self.assertGreater(out["spearman"], 0.8)


class TestOfflinePipeline(unittest.TestCase):
    """端到端离线跑通：零网络流量，验证采集→打分→报告链路。"""

    def test_daily_offline(self):
        from radar.pipeline.daily import run_daily

        cfg = load_config()
        cfg.llm["enabled"] = False
        cfg.delivery["git_commit"] = False
        with tempfile.TemporaryDirectory() as td:
            cfg.general["data_dir"] = str(Path(td) / "data")
            cfg.general["output_dir"] = str(Path(td) / "output")
            cfg.general["log_dir"] = str(Path(td) / "logs")
            result = run_daily(cfg, offline=True)

            self.assertGreater(result["scored"], 0, "离线夹具应至少打出一个仓库")
            md = Path(result["paths"]["markdown"])
            html = Path(result["paths"]["html"])
            self.assertTrue(md.exists() and md.stat().st_size > 0)
            self.assertTrue(html.exists() and "<html" in html.read_text(encoding="utf-8").lower())
            self.assertIn("财经交易", md.read_text(encoding="utf-8"))

    def test_markdown_and_html_render(self):
        from radar.report import Digest, DigestItem

        d = Digest(day="2026-09-09", stats={"candidates": 3},
                   sections={"finance": [DigestItem(full_name="a/b", score=88.0,
                                                    tier="explosive", stars=100,
                                                    stars_7d=50, description="x")]},
                   channel_titles={"finance": "💰 财经交易"})
        cfg = load_config()
        md = render_markdown(d, cfg)
        self.assertIn("a/b", md)
        self.assertIn("爆款前夜", md)
        self.assertIn("<html", render_html(d, cfg).lower())


class TestDigest(unittest.TestCase):
    def test_rising_excludes_other_channels(self):
        # 已进入垂直频道的年轻项目不应在新星榜重复出现
        from radar.core.features import GrowthFeatures
        from radar.core.scoring import ScoreResult
        from radar.pipeline.daily import build_digest

        cfg = load_config()
        r1 = ScoreResult(repo_id=1, full_name="a/quant", final=80.0, tier="high",
                         vertical="finance",
                         growth=GrowthFeatures(total_stars=100, age_days=20))
        r2 = ScoreResult(repo_id=2, full_name="a/fresh", final=75.0, tier="high",
                         vertical="rising",
                         growth=GrowthFeatures(total_stars=50, age_days=10))
        d = build_digest(cfg, [r1, r2], "2026-09-09", {})
        finance = [i.full_name for i in d.sections.get("finance", [])]
        rising = [i.full_name for i in d.sections.get("rising", [])]
        self.assertIn("a/quant", finance)
        self.assertNotIn("a/quant", rising, "跨频道重复：a/quant 已在财经频道展示")
        self.assertIn("a/fresh", rising)


class TestStorage(unittest.TestCase):
    def test_upsert_preserves_author_trust(self):
        from radar.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                rid = store.upsert_repo({"full_name": "a/b", "description": "x"})
                store.update_repo_fields(rid, author_trust=0.7)
                # 模拟 _enrich_missing 路径：meta 不含 author_trust 的全量 upsert
                store.upsert_repo({"full_name": "a/b", "description": "y",
                                   "language": "Python", "stars": 10})
                row = store.get_repo("a/b")
                self.assertEqual(float(row["author_trust"]), 0.7,
                                 "upsert 不应把 author_trust 清零")
                self.assertEqual(row["description"], "y")  # 元数据正常更新
                self.assertEqual(row["language"], "Python")
            finally:
                store.close()

    def test_prune_cleans_http_cache(self):
        from radar.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.cache.set("etag:x", '"abc"')
                store.execute(
                    "INSERT INTO http_cache(key, value, updated_at) "
                    "VALUES('old', x'', '2020-01-01 00:00:00')")
                out = store.prune()
                self.assertGreaterEqual(out.get("http_cache", 0), 1)
                self.assertEqual(store.cache.get("etag:x"), '"abc"')  # 新鲜缓存保留
            finally:
                store.close()


class TestScheduler(unittest.TestCase):
    def test_daily_due_time_compare(self):
        from radar.scheduler import _daily_due

        # 22:30 的任务，23:10 已过点必须触发（旧实现按 hour/minute 分量比较会漏）
        self.assertTrue(_daily_due(datetime(2026, 9, 9, 23, 10), "", "22:30"))
        self.assertTrue(_daily_due(datetime(2026, 9, 9, 22, 30), "", "22:30"))
        self.assertFalse(_daily_due(datetime(2026, 9, 9, 22, 29), "", "22:30"))
        # 当天已触发过则不再触发
        self.assertFalse(_daily_due(datetime(2026, 9, 9, 23, 10), "2026-09-09", "22:30"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
