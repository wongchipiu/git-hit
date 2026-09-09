# GitHub 宝藏雷达（Treasure Radar）—— 产品与技术设计方案 v0.3

> 一句话定位：一个 **7×24 自动运行的「GitHub 淘金机」**，从每天新增/变更的数十万仓库里，用**增速信号 + 领域分类 + LLM 语义研判**挖出还没被大众发现的宝藏项目，按「财经交易 / 科技资讯 / 新星榜」等垂直频道整理成每日报告呈现给你。
>
> 文档性质：方案 + 实施计划。v0.2 已根据评审决策修订（见 §14.C 决策记录）；v0.2.1 为文档 review 修订；**v0.3 并入 LLM 采购与成本模型（官方直充不用中间商、V4-Flash/Pro 两档、2026-08 调价后成本修正）**。主要事实已联网核实（见 §14.A）。

---

## 目录

1. [决策总览](#1-决策总览)
2. [背景与时机](#2-背景与时机)
3. [核心定义：什么才算"宝藏"](#3-核心定义)
4. [垂直频道设计（全部 P0）](#4-垂直频道设计)
5. [数据源矩阵与合规红线](#5-数据源矩阵与合规红线)
6. [配额预算与四级漏斗](#6-配额预算与四级漏斗)
7. [宝藏评分算法（核心 IP）](#7-宝藏评分算法)
8. [系统架构（Windows 本机版）](#8-系统架构)
9. [技术栈选型（方案 A · Windows 适配）](#9-技术栈选型)
10. [数据模型](#10-数据模型)
11. [产品形态与交付](#11-产品形态与交付)
12. [实施计划 / 路线图](#12-实施计划--路线图)
13. [风险与对策 + 护城河](#13-风险与对策--护城河)
14. [附录](#14-附录)

---

## 1. 决策总览

| # | 决策点 | 结论 | 对方案的影响 |
|---|---|---|---|
| 1 | 产品形态 | **方案 A**（极简自托管） | 单人零运维，不上 K8s/ClickHouse |
| 2 | 推送渠道 | **提交 Git，不用推送** | Delivery 层 = 每日报告 commit 到本地 Git 仓库存档，**不做 IM/webhook 推送** |
| 3 | 频道取舍 | **全部做，不能妥协**；财经频道尤其做深（前沿投资 / 重大订单与交易） | 6 个频道全部 P0；财经频道细分为 5 个子赛道 + 中文召回增强 |
| 4 | LLM 供应商 | **国产**（DeepSeek 主选） | 点评走 DeepSeek API（V4-Flash/Pro 两档）；**官方直充，不依赖任何中间商/router**（见 §9） |
| 5 | 商业模式 | **自己先用** | 无商业化规划；P3 个性化简化为本地配置文件；P4 移除 |
| 6 | 部署环境 | **个人 Windows PC** | 本机运行：SQLite + Python + Windows 任务计划；无需服务器 |

---

## 2. 背景与时机

| 时间点 | 事件 | 对本产品的影响 |
|---|---|---|
| 2025-01-30 | Events API 数据留存从 90 天缩短到 **30 天**（已生效） | 历史信号必须自建时序存储 |
| 2026-06-30 | GitHub 公告将收紧 stargazers/watchers 公开列表接口 | 旧的"逐个看谁 star 了"路线被堵死 |
| 2026-07 | stargazer 列表端点正式仅限 admin/collaborator 访问 | 依赖 stargazer 身份的旧工具失效 |
| **2026-09-04** | 官方上线 **privacy-safe star history** 端点 `GET /repos/{owner}/{repo}/stargazers/history` | **「增速探测」第一次可以低成本、合规、批量地做 —— 本方案的技术地基** |

**为什么现在做**：
- 现有工具要么"已经火了才上榜"（Trending 滞后 3~14 天），要么是宏观统计（OSS Insight），**都不解决"早 7 天发现"这个核心痛点**。
- 新 star history 端点（匿名可用、按周聚合 + 每日增量）让"新仓库增速异常检测"从爬虫灰区变成官方支持的合规能力。
- AI/Agent、量化交易正处于"每天涌现新仓库"的爆发期，垂直挖掘价值高。

---

## 3. 核心定义

「宝藏」用三个正交维度定义：

| 维度 | 含义 | 反面 |
|---|---|---|
| **Early（早）** | 绝对星数还不高（< 5k），但**增速异常** | Trending 只给你已经爆的 |
| **Real（真）** | 不是刷星、不是营销号、不是 fork 换皮；有真实 commit / issue / 外部讨论 | 星数造假、僵尸仓库 |
| **Relevant（相关）** | 命中你关心的垂直领域（财经交易、AI Agent、科技资讯…） | 泛泛而谈的全品类 |

> **宝藏分 = f(加速度, 真实性, 领域匹配, 作者信誉, 外部热度)**

---

## 4. 垂直频道设计

> **决策 3：6 个频道全部 P0，不做取舍。** 财经交易频道按"前沿投资 / 重大订单与交易"深挖（下面标 ⭐ 的子赛道为必做重点）。

| 频道 | 关注什么 | 种子信号 | 备注 |
|---|---|---|---|
| 🚀 **新星榜 Rising Stars** | 30 天内创建、7 日星增速 top | `created:>D-30` + star history 加速度 | 全产品基础，其余频道的公共候选池 |
| 🤖 **AI / Agent 前线** | LLM、Agent、MCP、推理引擎、Skill 生态 | `topic:llm/agent/mcp` + 热词（RAG、workflow、function calling） | 爆发期，趋势跟踪价值最大 |
| 💰 **财经交易 Quant & Trading** | 见下方 5 个子赛道 | 见下方 | **重点频道，必须做深** |
| 📰 **科技资讯 Tech Radar** | 被 HN/Reddit 热议的 repo、重大 release | HN Algolia 为最强早期信号 | 与 Trending 互为补充 |
| 🛡️ 安全 & 逆向 | CVE PoC、渗透、逆向工程 | `topic:security/ctf` + `CVE` | — |
| 🧰 效率工具 | CLI、DevTool、self-hosted | `topic:cli/productivity` | — |

### 4.1 💰 财经交易频道 —— 5 个子赛道（P0 必做）

| 子赛道 | 关注内容 | 种子信号（topic + 关键词） |
|---|---|---|
| **量化框架与回测** | 回测引擎、因子库、策略框架 | `topic:quantitative-finance / backtesting / algorithmic-trading` |
| **行情与数据源** | 免费行情 API、数据采集、tick/bar 数据 | `topic:stock / finance` + 中文关键词：A股、同花顺、tushare、akshare |
| **前沿投资研究** ⭐ | 另类数据（alternative data）、alpha 因子研究、机器学习投资、资产配置研究 | `topic:investment / alpha / alternative-data` + 关键词：投研、因子、alpha、研究 |
| **重大订单与交易信号** ⭐ | 大单/订单流监控、龙虎榜、大宗交易、公告/事件监控、资金流向 | 中文关键词：龙虎榜、大宗交易、订单流、大单、北向资金、主力资金、公告监控 |
| **投研 Agent** ⭐ | LLM 驱动的投研/交易智能体 | 关键词：research agent、trading agent、investor（对标 `TauricResearch/TradingAgents`、`simonlin1212/Vibe-Research`） |

> **中文召回策略（关键）**：实测 GitHub Search API 对中文关键词召回有效（`tick-stock-panel`（A股量化工作台）、`HiThink-Tech/Financial-API`（同花顺数据服务）等中文项目均被召回）。A 股生态的宝藏大量藏在中国作者仓库里，**每个子赛道必须配一组中英双语 query**，单靠英文 topic 会漏掉一多半。

---

## 5. 数据源矩阵与合规红线

### 5.1 一级源（GitHub 官方 API）—— 已实测 / 已核实

| 用途 | 端点 | 配额 | 实测结论 |
|---|---|---|---|
| **星星时序（核心）** | `GET /repos/{o}/{r}/stargazers/history` | core 池 | ✅ 官方文档核实：返回 `[{week, total, days[7]}]`，周日开始计；`per_page≤30, page≤100` → 单仓库最多拉 3000 周（≈57 年）；**匿名可用** |
| 候选粗筛 | `GET /search/repositories?q=created:>DATE stars:>N&sort=stars` | search 30 req/min（PAT） | ✅ `created:>2026-08-01 stars:>200` → 995 条 |
| 垂直粗筛 | `q=topic:trading stars:>100` 等 | 同上 | ✅ 精准，中文 A 股项目也能召回 |
| 仓库元数据 | GraphQL `repository{...}` | 5000 pts/hr | 批量 100 repo/次 |
| 活跃度 | `GET /repos/{o}/{r}`、commits、releases | core 池 | ✅ |
| 事件流 | `GET /repos/{o}/{r}/events`、`/events` | core 池 | ⚠️ **仅留存 30 天，必须自采落库** |

### 5.2 二级源（外部热度，免配额）

| 源 | 接口 | 说明 |
|---|---|---|
| Hacker News | `hn.algolia.com/api/v1/search_by_date?query=github.com&tags=story&numericFilters=points>50` | ✅ 免费无鉴权；**「科技资讯」频道最强早期信号** |
| GitHub Trending 页 | `https://github.com/trending[/{lang}]?since=daily\|weekly\|monthly` | 可抓 HTML（robots 允许） |
| Reddit | r/programming、r/quant、r/algotrading JSON API | 需 UA + 限速，二期 |
| GH Archive | gharchive.org + BigQuery | 全量事件回溯，冷启动回填（二期） |

### 5.3 合规红线（实测 robots.txt 结论）

| 路径 | 是否允许抓取 |
|---|---|
| `/trending`（含子页） | ✅ **允许** |
| `/*/*/stargazers`（HTML 页）、`/search`（HTML）、`/*/*/pulse`、`/*/*/graphs`、`/*/*/contributors` | ❌ **禁止** |

> **铁律**：仓库级数据一律走 REST/GraphQL API，绝不抓 HTML 详情页；Trending 页仅做「候选发现」，抓到 repo 名后立即转 API（`/stargazers/history`）。

---

## 6. 配额预算与四级漏斗

> 关键约束：**Search 30 req/min/token**，**Core 5000 req/hr/token**。个人单 token 预算如下（已含 6 频道全开的余量）：

| 层级 | 动作 | 数量级 | 成本 |
|---|---|---|---|
| **L0 粗筛** | Search / Trending / Events 分领域扫描（中英双语 query × 6 频道） | ~10 万 repo/天 | 0 次 core |
| **L1 增速核验** | 队列消费 → `/stargazers/history` | ~2,000 repo/天 | 2,000 次 core（5000/hr 充裕） |
| **L2 深度画像** | GraphQL 批量元数据（100 repo/次） | ~300 repo/天 | 3 次 GraphQL |
| **L3 LLM 研判** | DeepSeek 点评（见 §9） | ~60 repo/天 | 日成本 ¥1–2 级（两档模型，见 §9） |

**省钱关键点**：
- star history 单次返回 **30 周** → ≤30 周龄的新仓库全量 1 次调用即可；更老的仓库按 30 周/页向前翻页；日常增量只取 `page=1`。
- 单 PAT：core 5000/hr 足够；search 30/min → 43,200/天，6 频道全开仍绰绰有余。
- **必做**：全局令牌桶 + 403/429 退避（读 `Retry-After`/`X-RateLimit-Reset`）+ **ETag 条件请求**（304 不计费）。

---

## 7. 宝藏评分算法

### 7.1 增速特征（把 `days[7]` 展开成日序列后计算）

| 特征 | 定义 | 含义 |
|---|---|---|
| `v1 / v7 / v30` | 近 1 / 7 / 30 天日均新增星 | 增速短/中/长尺度 |
| `accel` | `v7 / max(v30, ε)` | **>3 表示正在起飞** |
| `zscore` | `(v7 − μ_同领域同星段) / σ` | 领域内标准化，避免大项目天然增速高 |
| `age_days` | 仓库年龄 | 越新越有价值 |
| `burst` | 单日峰值 / 生涯日均 | 识别 HN 引爆等事件驱动 |
| `decay` | 近 7 天 vs 前 7 天 | 识别昙花一现 |

### 7.2 真实性 / 质量特征（防刷星、防换皮）

| 特征 | 说明 |
|---|---|
| `commit_velocity` | 近 30 天 commit 数、活跃贡献者数 |
| `contributor_hhi` | 贡献者集中度（单人项目风险） |
| `issue_health` | open/closed 比、平均首响时间、真实用户 issue 量 |
| `star_commit_ratio` | 星数/commit 数，异常高 → 疑似刷星 |
| `stargazer_quality` | 拿不到 stargazer 身份 → 代理指标：日序列平滑度（真人增长有昼夜/周末周期，刷星是恒定方波）+ 周日占比异常 |
| `fork_originality` | 是否 fork；README MinHash 相似度（换皮检测） |
| `author_trust` | 作者历史仓库中位星数、账号年龄、followers |

### 7.3 打分与分档

```text
raw      = w1·log1p(v7) + w2·zscore + w3·log1p(accel) + w4·burst
         + w5·quality + w6·external + w7·author_trust
penalty  = 刷星嫌疑 + 换皮嫌疑 + 停更 + 纯文档/清单类
score    = raw − penalty
early    = f(1/age_days) · [star_total < EARLY_CEILING]      # EARLY_CEILING 初值 5000（见 §3）；越新越小越加分
final    = score · (1 + early) · vertical_match
```

分档：**🔥 爆款前夜**（>85）、**⭐ 高潜**（70–85）、**👀 观察**（55–70）。

**权重校准**：初值人工设定 → T+30 回测验证，用 rank correlation / NDCG 迭代校准。

### 7.4 反刷星

- 日序列 **STL 分解**，检测"无昼夜周期的方波"；星数突增但 commit/issue 零增长 → 标记 `suspect_star_farm`；同作者多仓库联动检测。

---

## 8. 系统架构

**Windows 本机单进程架构（决策 6），全部组件同机运行：**

```text
┌─ Collector（APScheduler 定时任务 / Windows 任务计划触发 run_daily.py）
│   ├─ search_funnel      每 10min：6 频道 × 中英双语 query 扫描
│   ├─ trending_scraper   每 1h：/trending × {daily,weekly,monthly} × {all,py,ts,go,rs}
│   ├─ star_history       队列消费候选 → /stargazers/history（增量只取 page=1）
│   ├─ hn_signal          每 15min：HN Algolia 抓 github 相关热帖
│   └─ event_stream       每 5min：/events 抽样采 → SQLite（30 天留存；全量回填用 GH Archive 二期）
│
├─ Storage
│   ├─ SQLite（radar.db）  repos / repo_snapshots / star_daily / signals / digests
│   └─ output/ 目录        Markdown/JSON 日报 + HTML 可视化报告
│
├─ Intelligence（每日 22:00 批处理）
│   ├─ feature_engine → scorer（§7） → classifier（领域归属） → dedup
│   └─ llm_analyst（DeepSeek）：一句话亮点 / 为什么现在值得关注 / 风险提示 / 同类对比
│
└─ Delivery（决策 2：Git 提交，不推送）
    ├─ 生成 output/daily/YYYY-MM-DD.md + .json + output/report.html
    └─ auto: git add output/ → git commit -m "digest 2026-09-09"
        （本地 Git 仓库存档；不 push 远端，历史用 git log 回溯）
```

**Windows 本机运行要点**：
- 长驻进程：`APScheduler`（Windows 下无需 cron/systemd）；重启自启用「任务计划程序」注册开机任务即可
- 断网/配额用尽：本轮采集跳过并记日志，下轮自愈，不中断主进程
- 出口 IP 稳定、无服务器成本；GitHub 直连若慢可在配置里加代理

---

## 9. 技术栈选型

**方案 A · Windows 单机版（决策 1 + 6 定稿）**

| 组件 | 选型 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 生态最全 |
| HTTP | `httpx`（异步） | 并发限流友好 |
| 调度 | `APScheduler` + Windows 任务计划 | 无 cron/systemd 也可稳定定时 |
| 存储 | **SQLite**（持久主库） | 个人单用户够用，零运维；时序分析可用 `duckdb` 直查 SQLite（可选） |
| 特征计算 | `polars` / `numpy` | 日增量特征，秒级 |
| 报告 | `jinja2` + `pyecharts`/`ECharts`（HTML 内嵌） | 本地双击即看的可视化报告 |
| LLM | **DeepSeek API**（国产，决策 4） | 见下 |
| 存档 | 本地 **Git 仓库**（决策 2） | 每日 auto commit，不 push |

**LLM 供应商（决策 4 · 国产）—— 官方直充，不用中间商**：

本工具每天仅 ~60 次点评 + 1 次汇总（输出约 2 万 token），用量极小：**直接官方充值最划算**。不推荐任何 token 中转/聚合站（加价、跑路、数据过手第三方风险全由自己担）；多模型需求用「官方云平台聚合」或代码内降级切换即可，**不需要第三方 router 中间商**（详见下文"供应商可切换设计"）。

| 优先级 | 平台 | 模型 | 购买方式 |
|---|---|---|---|
| 主选 | **DeepSeek 开放平台** `platform.deepseek.com` | `deepseek-v4-flash` / `deepseek-v4-pro` | 官方直充（微信/支付宝），OpenAI 兼容、余额不过期 |
| 备选 1 | 阿里云百炼 / 火山方舟 | qwen-turbo / 豆包 Pro（方舟亦上架 DeepSeek-V3.1） | 需要"一个账号多家模型"时的正规选择 |
| 备选 2 | 智谱 `open.bigmodel.cn` / 月之暗面 `platform.moonshot.cn` | GLM / Kimi | 主选断服时切换 |

**2026 价格现状与两档策略**（2026-07-24 旧名 `deepseek-chat/reasoner` 停用，改 `v4-*` 系列含 Thinking/Non-Thinking 双模式；2026-08-17 全面调价）：

| 档位 | 模型（mode） | 用途 | 日成本估算 | 定价参考（¥/百万 token，高峰） |
|---|---|---|---|---|
| 日常档 | `deepseek-v4-flash`（non-thinking） | 全部候选点评 | **≈ ¥0.5–1/天** | 显著低于 Pro |
| 深度档 | `deepseek-v4-pro`（thinking） | 已入榜 top 项目研判（选配） | ≈ ¥1–2/天 | 输入 ¥9（缓存命中 ¥0.3）/ 输出 ¥27 |

> 原"几毛/天"估算基于 2026-08 调价前价格，已修正为上述区间；批处理定在 22:00 空闲时段可再降约一半。最终以官方计价页为准，成本模型建议每季度复核。

**省钱要点（落地到代码）**：
1. **上下文缓存命中**：prompt 前缀放不变文本（repo 描述 / README 摘要 / 特征快照）→ 缓存命中价约为未命中的 1/30（¥0.3 vs ¥9）
2. **结果落库去重**：点评写入 SQLite，同一 repo 7 天内不重复调用
3. **错峰**：批处理定 22:00 空闲时段
4. 不囤 token、不买套餐，按量付，余额不过期

**供应商可切换设计（≈10 行代码，替代 router 中间商）**：所有 LLM 调用集中在 `llm_client.py`，配置三字段 `base_url / api_key / model`；主选失败 `try/except` 降级到备选 2（智谱/Kimi 均为 OpenAI 兼容协议），比任何第三方中转更稳、零加价。

**全离线备选**：`ollama` + Qwen 本地小模型（质量略降），供断网应急。

---

## 10. 数据模型

```text
repos(repo_id PK, full_name, owner, name, created_at, first_seen_at,
      language, topics JSON, description, is_fork, parent_repo, archived,
      default_branch, vertical JSON, author_trust, suspect_flags JSON)

repo_snapshots(repo_id, snap_date, stars, forks, watchers, open_issues,
               pushed_at, commit_count_30d, contributors, PK(repo_id, snap_date))

star_daily(repo_id, day DATE, stars_delta INT, PK(repo_id, day))   -- 当日新增星数，由 history 的 days[7] 展开
star_weekly(repo_id, week_epoch, total, days INT[7])

signals(repo_id, ts, source ENUM(hn,trending,reddit), metric, value, url, raw JSON)

scores(repo_id, score_date, final_score, tier, raw_components JSON, vertical_scores JSON)

events(event_id, repo_id, type, actor, created_at)           -- 30 天留存，滚动清理

digests(id, date, vertical, payload JSON)
```

SQLite 单文件即可；`star_daily` 量级 <1 千万行，无需分区，膨胀后再考虑按月分表。

---

## 11. 产品形态与交付

**个人单用户（决策 5），形态收敛为「每日报告 + 本地 Git 存档 + 本地 HTML 可视化」，无 Web 服务、无 IM 推送。**

| 交付物 | 内容 | 查看方式 |
|---|---|---|
| `output/daily/YYYY-MM-DD.md` | 按频道分节的当日简报（卡片 = 名字 + LLM 一句话点评 + tier 徽章 + 信号来源 + 星增速） | 任意 Markdown 阅读器 |
| `output/daily/YYYY-MM-DD.json` | 结构化数据（供二次分析/脚本消费） | 程序 |
| `output/report.html` | 可视化报告：频道 tab + 新星榜 + 爆款前夜 + 星增速图（ECharts） | 浏览器双击打开 |
| Git 历史 | `git log` 每日一条 digest commit | 回溯任意一天 |

**报告频道顺序与侧重**（体现决策 3）：💰 财经交易（5 子赛道，置顶）→ 🤖 AI/Agent → 🚀 新星榜 → 📰 科技资讯 → 🛡️ 安全 → 🧰 工具。

**质量红线：每日每频道 ≤ 10 条，宁缺毋滥。**

---

## 12. 实施计划 / 路线图

> 决策 5 已移除商业化（P4）；决策 6 决定全部跑在个人 Windows PC。**P0 即覆盖全 6 频道（决策 3），不缩水。**

| 阶段 | 周期 | 交付 | 验收标准 |
|---|---|---|---|
| **P0 原型** | 5 天 | 单文件脚本 `radar.py`：6 频道中英双语 search + trending + star history → 增速打分 → Markdown 简报 + DeepSeek 点评 | 当天跑出 ≥20 条与 Trending 不重合的新发现；财经频道 ≥5 条 A 股/投研相关 |
| **P1 本机 MVP** | 2–3 周 | SQLite 落库、APScheduler 定时、Windows 任务计划自启、每日 Git auto commit、HTML 报告 | 连续 14 天自动运行，配额不触顶，每日报告落盘且能 `git log` 回溯 |
| **P2 智能化** | 2–3 周 | 反刷星、换皮去重、DeepSeek 点评全量接入、权重回归校准、T+30 回测统计 | 回测页/统计显示 T+30 命中率 ≥60%（上榜项目 30 天后星数 ≥500） |
| **P3 个人化调优** | 1–2 周 | `config.toml` 本地配置（频道开关/关键词/排除清单/watchlist）、财经子赛道专项调优 | 每天阅读报告 ≤10 分钟，命中自身关注点的比例 ≥50% |

**P0 启动清单（评审通过后第一周执行）**：

- [ ] 申请 GitHub PAT（public repo 只读权限），配置到 `config.toml`
- [ ] 申请 DeepSeek API key，配置两档模型（v4-flash 日常 / v4-pro 深度）+ 备选平台 key（见 §9）
- [ ] 写 L0 search 漏斗（6 频道 × 中英双语 query 模板，财经 5 子赛道单独成组）
- [ ] 接 `/stargazers/history`，日序列展开（**注意**：最新一周未满 7 天需截断到今天，否则低估 v7）
- [ ] §7.1 增速特征 + 固定权重打分
- [ ] DeepSeek 点评 prompt v1（一句话亮点 / 风险 / 同类对比）
- [ ] 每日输出 Markdown + HTML，本地 Git 仓库 auto commit
- [ ] 首周人工核对 20 条，校准 query 与权重初值

---

## 13. 风险与对策 + 护城河

| 风险 | 影响 | 对策 |
|---|---|---|
| Search 30/min 瓶颈 | 采集慢 | 缓存查询；query 错峰执行；事件流补充 |
| API 再次收紧（star history 也可能变） | 核心信号断供 | 多源冗余（Trending "stars today"、GH Archive、自采 event 流）；抽象 `StarSeriesProvider` 接口 |
| 刷星污染榜单 | 推荐质量下降 | §7.4 反作弊 + 人工抽检 + 置信度标注 |
| DeepSeek 断服/限流 | 点评缺失 | 缓存结果；当日降级为"无点评"仍出报告；备选国产 API 切换 |
| 抓取合规 | 封号 | 严守 robots；API 优先；Trending 低频 + UA 声明 |
| 冷启动无历史 | 前 30 天打分不准 | 先用简单规则（v7/age）起步，T+30 后逐步切换回归权重 |
| 信息过载 | 失去阅读兴趣 | 每日每频道 ≤10 条；财经置顶；可配置排除词 |

**护城河**：**早**（比 Trending 早 3–14 天）· **真**（反刷星+换皮去重）· **准**（T+30 回测校准）· **垂直**（财经频道"量化框架 + 数据源 + 投研 Agent + 重大订单/交易信号 + 中文生态"全覆盖，这是通用工具做不到的）。

---

## 14. 附录

### A. 关键事实来源与核实状态（2026-09-09）

| 事实 | 状态 |
|---|---|
| `GET /repos/{o}/{r}/stargazers/history` 端点、参数、返回结构、匿名可用 | ✅ 官方 Docs（apiVersion 2026-03-10）+ Changelog（2026-09-04）联网复核 |
| stargazers 列表 2026-07 起仅限 admin/collaborator | ✅ 官方 Changelog + 第三方报道复核 |
| Events API 30 天留存（2025-01-30 起） | ✅ 官方公告 |
| REST core 5000/hr、Search 30/min、GraphQL 5000 pts/hr | ✅ 官方文档，构建前建议复核 |
| robots.txt 结论（允许 /trending，禁止 stargazers/search HTML 等） | ⚠️ 源文件实测记录，构建前本机 re-run 确认 |
| Trending 页可抓、HN Algolia 无鉴权 | ⚠️ 同上，建议 re-run |
| DeepSeek 2026 模型与定价（`v4-flash`/`v4-pro`、2026-07-24 旧名停用、2026-08-17 调价、缓存命中价） | ✅ 联网搜索核实（2026-09-09）；价格以官方计价页为准 |

### B. 实测样例仓库（素材）

**新星 / AI**：`deepseek-ai/deepseek-harness`（216k+，2026-08-13 建）、`openai/skills`（26k+）、`ayghri/i-have-adhd`（30.9k，单日 656）、`FareedKhan-dev/kimi-k3-in-c`（7.2k）、`deeplethe/utopia`（6k）

**财经 / 量化**：`TauricResearch/TradingAgents`（Multi-Agent LLM 交易）、`shy3130/tick-stock-panel`（4.5k，A股量化工作台）、`HiThink-Tech/Financial-API`（2.9k，同花顺数据服务）、`simonlin1212/Vibe-Research`（2.4k，投研 Agent）、`LuxAlgo/Vela`（金融图表）

### C. 决策记录（2026-09-09）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 产品形态 | 方案 A（极简自托管） |
| 2 | 推送渠道 | 提交 Git，不用推送（本地 Git 存档） |
| 3 | 频道取舍 | 全部做，不妥协；财经做深（前沿投资 / 重大订单与交易） |
| 4 | LLM 供应商 | 国产（DeepSeek 主选） |
| 5 | 商业模式 | 自己先用 |
| 6 | 部署环境 | 个人 Windows PC |

---

*关键 API 事实已联网复核（2026-09-09）。v0.3 修订：按评审决策并入「LLM 采购与成本模型」——官方直充不用中间商/router、V4-Flash/Pro 两档、2026-08 调价后成本修正（§9）、供应商可切换设计、P0 清单与附录 A 同步。下一步：开始 P0 原型（radar.py）开发。*
