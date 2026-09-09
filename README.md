# 🔭 GitHub 宝藏雷达（Treasure Radar）

7×24 自动运行的「GitHub 淘金机」：从每天新增/变更的仓库里，用**增速信号 + 领域分类 + LLM 语义研判**
挖出还没被大众发现的宝藏项目，按「财经交易 / AI Agent / 新星榜 / 科技资讯 / 安全 / 工具」六个垂直频道
生成每日报告，本地存档、本地看。

> 对应设计方案：`docs/GitHub宝藏雷达-设计方案与实施计划.md`（v0.2.1）。
> 本项目已完整落地 **P0 原型 → P1 本机 MVP → P2 智能化 → P3 个人化调优**。

---

## 1. 30 秒上手

```powershell
cd treasure-radar
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

python radar.py init           # 生成 config.toml / .env / output 目录 / 本地 Git 仓库
python radar.py daily --offline # 离线跑通全流程（零网络流量，先验证环境）
```

配置密钥（可选但强烈建议）：编辑 `.env`

```
GITHUB_TOKEN=ghp_xxx        # public_repo 只读即可；不填则匿名 60 次/小时（按出口 IP 共享，极易耗尽）
DEEPSEEK_API_KEY=sk-xxx     # 不填则自动降级为「无 LLM 点评」，报告照常产出
```

真实运行（小流量）：

```powershell
python radar.py daily --quick       # 每频道 1 条 query / 1 页，核验 20 个仓库
python radar.py daily               # 完整每日流程
start output\report.html            # 浏览器打开可视化报告
```

每日自动运行（Windows 任务计划）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 22:00
```

---

## 2. 命令一览

| 命令 | 说明 |
|---|---|
| `python radar.py init` | 生成配置、目录、本地 Git 仓库（不 push） |
| `python radar.py daily` | 完整流程：采集 → 打分 → LLM → 日报 → Git 存档 |
| `python radar.py daily --offline` | 离线夹具跑通，**不发任何网络请求** |
| `python radar.py daily --quick` | 小流量模式（1 query / 1 页 / 20 个核验） |
| `python radar.py daily --skip-collect` | 用库里已有数据重算报告（零额外流量） |
| `python radar.py collect` | 只采集入库，不出报告 |
| `python radar.py status` | 配置 / 配额 / 库统计 |
| `python radar.py backtest --days 30` | T+30 回测：上榜项目今天多少星 |
| `python radar.py calibrate` | 用历史样本回归校准权重（写入 `data/calibration.json`，**自动生效**） |
| `python radar.py scheduler` | 长驻调度（APScheduler，可不用） |

通用开关：`--no-llm`、`--no-commit`、`--no-trending`、`--no-hn`、`--no-stars`、`--no-profile`、
`--star-limit N`、`--pages N`、`--queries N`。

---

## 3. 产出物

| 文件 | 内容 |
|---|---|
| `output/daily/YYYY-MM-DD.md` | 按频道分节的当日简报（卡片 = 名字 + LLM 点评 + 分档 + 信号 + 增速） |
| `output/daily/YYYY-MM-DD.json` | 结构化数据，供二次分析 |
| `output/report.html` | 自包含可视化报告（频道 tab + 星增速迷你图），双击即看，**无 CDN 依赖** |
| `output/daily/YYYY-MM-DD.html` | 当天报告存档副本 |
| `data/radar.db` | SQLite 主库（repos / star_daily / scores / signals …） |
| `data/calibration.json` | 权重校准结果 |
| `logs/radar.log` | 运行日志（按天滚动，保留 14 天） |
| Git 历史 | 每日一条 `digest YYYY-MM-DD` 提交，`git log` 回溯任意一天 |

报告频道顺序（财经置顶）：💰 财经交易 → 🤖 AI/Agent → 🚀 新星榜 → 📰 科技资讯 → 🛡️ 安全 → 🧰 工具。
**质量红线：每频道每日 ≤10 条，宁缺毋滥。**

---

## 4. 四级漏斗与配额

| 层级 | 动作 | 默认上限 | 说明 |
|---|---|---|---|
| L0 粗筛 | 6 频道 × 中英双语 query 的 Search + Trending + HN | ~30 次 search/轮 | search 独立令牌桶 |
| L1 增速核验 | `/repos/{o}/{r}/stargazers/history` | 150 个/轮 | 单次返回 30 周日粒度序列 |
| L2 深度画像 | commit / 贡献者 / 作者信誉（+GraphQL 批量元数据） | 30 个/轮 | 只在 L1 之后做 |
| L3 LLM 研判 | DeepSeek 点评 | 60 条/天 | 带 SQLite 缓存，不重复烧钱 |

节流设计（**不会打满配额**）：

* 全局双令牌桶：core 默认 3000/h（官方 5000）、search 默认 20/min（官方 30），各留 1/3 余量；
* 任意请求最小间隔 0.4s；单轮硬预算 `limits.max_requests_run = 300`，超出即停；
* 403/429 读 `Retry-After` / `X-RateLimit-Reset` 退避，等待超过 `max_sleep_on_limit`
  则**本轮跳过、下轮自愈**，绝不长时间阻塞；
* ETag 条件请求落 SQLite，304 不计费。

---

## 5. 合规红线

* 仓库级数据**一律走 REST/GraphQL API**，`/stargazers` HTML、`/search` HTML、`/pulse`、
  `/graphs`、`/contributors` 页面**一律不抓**；
* 仅 `/trending` 页做候选发现（robots 允许），且低频（默认 6 小时一次）、带 UA 声明，
  抓到仓库名后立即转 API；可用 `sources.trending_enabled=false` 完全关闭；
* stargazer 身份列表自 2026-07 起已不可访问，反刷星改用**行为代理指标**（日序列变异系数、
  周末节律、星/commit 比、贡献者集中度），不尝试任何规避手段；
* 交付只做**本地 Git commit，绝不 push**（`delivery.git_push` 默认 false，代码层面做了拦截）；
* 密钥只走 `.env` / 环境变量，`config.toml` 与 `.env` 均已加入 `.gitignore`。

---

## 6. 宝藏评分

```text
raw   = w1·log1p(v7) + w2·zscore + w3·log1p(accel) + w4·log1p(burst)
      + w5·quality + w6·external + w7·author_trust
final = squash(raw − penalty) · (1 + early) · vertical_match
```

* **增速**：`v1/v7/v30`、`accel = v7/v30`、`burst = 单日峰值/生涯日均`、`decay = v7/前7天`；
  最新一周未满 7 天时会截断到今天，避免系统性低估 v7；
* **真实性**：commit 速度、贡献者数、issue、License、主页、topic 完整度、新鲜度；
* **反刷星**：星突增但零 commit、恒定方波（低变异系数）、星/commit 比失衡、高星 fork 换皮、
  停更、纯清单/教程类、昙花一现；
* **去重**：名称 + 描述 + 语言的 64 位 SimHash，汉明距离 ≤8 视为换皮/镜像，只保留分高者；
* **z-score**：按「频道 × 星数量级」分组做领域内标准化，避免大项目天然增速高；
* **early 加成**：越新 + 星数越低加成越大，超过 `early_ceiling=5000` 不再享受；
* **频道去重**：同一项目只出现在一个频道（新星榜不再与垂直频道重复展示）。

分档：🔥 爆款前夜 ≥85 · ⭐ 高潜 70–85 · 👀 观察 55–70（低于 55 不进报告）。

权重可在 `config.toml [scoring]` 手工调整；跑 `python radar.py calibrate`
用 T+30 真实结果做 Spearman 回归校准后，`data/calibration.json` 中的权重会
**自动生效并优先于手工值**（删除该文件即回到手工配置）。

---

## 7. 目录结构

```text
treasure-radar/
├─ radar.py               CLI 入口
├─ run_daily.py           每日一键入口（任务计划调用它）
├─ config.example.toml    配置模板（首次运行自动复制为 config.toml）
├─ radar/
│  ├─ config.py           配置 + .env 加载
│  ├─ http_client.py      令牌桶 / 退避 / ETag / 预算（仅标准库 urllib）
│  ├─ storage.py          SQLite 建表 + DAO + ETag 缓存
│  ├─ sources/            github（REST+GraphQL）· star_history · trending · hn
│  ├─ core/               features · scoring · classifier · quality（反刷星+去重）
│  ├─ llm/analyst.py      DeepSeek 点评（可降级 + 可缓存 + 有上限）
│  ├─ pipeline/           context · collect · daily · backtest · offline（夹具）
│  ├─ report/             Markdown + 自包含 HTML（jinja2，缺失时降级）
│  ├─ delivery/           本地 Git 存档
│  └─ scheduler.py        APScheduler 长驻（缺失时降级为内置循环调度）
├─ scripts/               install_task.ps1 / uninstall_task.ps1
└─ tests/                 离线单元测试（零网络流量）
```

---

## 8. 阶段交付对照

| 阶段 | 交付 | 验收 | 状态 |
|---|---|---|---|
| **P0 原型** | 6 频道中英双语 query + trending + star history → 增速打分 → Markdown 简报 + DeepSeek 点评 | 当日跑出与 Trending 不重合的新发现 | ✅ |
| **P1 本机 MVP** | SQLite 落库、APScheduler/任务计划、每日 Git auto commit、HTML 报告 | 连续自动运行、配额不触顶、`git log` 可回溯 | ✅ |
| **P2 智能化** | 反刷星、换皮去重、LLM 全量接入、T+30 回测、权重回归校准 | `backtest` 输出命中率；`calibrate` 输出 Spearman | ✅ |
| **P3 个人化** | `config.toml`：频道开关 / 关键词 / 排除清单 / watchlist / 子赛道 / 权重 | 报告 10 分钟读完、命中关注点 | ✅ |

---

## 9. 自测

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -t .
```

覆盖：star history 周→日展开与"未满一周截断"、增速特征、中英双语领域分类与财经子赛道、
反刷星规则、SimHash 去重、打分分档、权重校准、Markdown/HTML 渲染，
以及一条**端到端离线流水线**（`--offline`，零网络流量）。

> 遵守"点到即止"原则：所有自动化测试均为离线夹具，不产生任何对外请求；
> 真实 API 只在人工执行 `--quick` 时产生个位数请求。

---

## 10. 已知限制

* 无 `GITHUB_TOKEN` 时匿名配额仅 60 次/小时且按出口 IP 共享，L1 可能整轮跳过——配置令牌即可；
* 冷启动前 30 天没有历史快照，`z-score`/回测样本不足，评分以简单规则为主，
  积累 30 天后可用 `calibrate` 切换到回归权重；
* Events API 仅留存 30 天，本项目按 30 天滚动清理 `events` 表；历史回填（GH Archive）留待后续；
* Reddit / Product Hunt 等外部源按方案留到二期。
