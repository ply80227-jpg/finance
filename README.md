# Hermes A股/港股免费数据源脚本

该仓库提供一个单文件脚本：`hermes_market_data.py`，用于 Hermes agent 接入 A 股与港股行情，并包含自动 fallback。

## 数据源策略

1. **主源：`akshare`**（免费、A/H 股覆盖较好）
2. **一级回退：`yfinance`**（免费、稳定度较高）
3. **二级回退：`xueqiu`**（雪球公开接口，A/H 覆盖较好）
4. **三级回退：`baostock`**（A 股稳定，但为 T+1 延迟，适合历史/兜底）
5. **四级回退：`stooq`（直接 HTTP CSV）**（免费公开数据，最终兜底；历史 K 线需要 `STOOQ_APIKEY` 环境变量，实时行情免费）

> 说明：以上数据源都可免费使用，但都依赖第三方公开接口，稳定性会受网络和上游策略影响。

## 安装

仓库现已支持作为 Python 包安装（推荐）；老的 `python hermes_market_data.py` 入口仍然可用（通过同名 shim 转发到 `hermes_market.cli`）。

```bash
# 选项 A：包安装（推荐，自动注册 hermes-market 控制台命令）
pip install -e .

# 选项 B：仅装运行时依赖（用于直接调用 hermes_market_data.py 时）
pip install -r requirements.txt

# 选项 C：开发环境（含 ruff / pytest / pre-commit）
pip install -r requirements-dev.txt
pre-commit install
```

## 代码结构

```
finance/
├── hermes_market_data.py     # 向后兼容 shim,转发到 hermes_market.cli
├── pyproject.toml            # 包定义 + ruff/pytest 配置
├── requirements.txt          # 运行时依赖
├── requirements-dev.txt      # 开发依赖(ruff/pytest/pre-commit)
├── .pre-commit-config.yaml   # 提交前钩子(ruff lint/format + 通用检查)
├── .github/workflows/ci.yml  # CI: ruff + pytest
├── src/hermes_market/
│   ├── cli.py                # argparse + 顶层异常兜底,产出统一 JSON
│   ├── fetcher.py            # 多源 fallback 编排器
│   ├── models.py             # FetchResult + fail_result(含 schema_version)
│   ├── normalize.py          # 市场识别 + 上交/深交/北交所路由
│   ├── cache.py              # TTL 内存缓存 + Xueqiu cookie 磁盘缓存
│   ├── utils.py              # to_float / utc_now_iso / retry
│   └── providers/
│       ├── akshare_provider.py
│       ├── yfinance_provider.py
│       ├── xueqiu_provider.py   # 含 XueqiuClient
│       ├── baostock_provider.py
│       ├── stooq_provider.py
│       └── sina_rss.py
└── tests/                    # 单测(无网络,全部 mock 第三方)
```

## 用法

### 1) 最新行情

```bash
# 通过 shim 脚本(向后兼容)
python hermes_market_data.py quote --symbol 600519 --market cn
python hermes_market_data.py quote --symbol 00700 --market hk

# 或通过控制台命令(pip install -e . 后可用)
hermes-market quote --symbol 600519 --market cn
hermes-market quote --symbol 00700 --market hk
```

### 2) 历史日线

```bash
python hermes_market_data.py history --symbol 600519 --start 2025-01-01 --end 2025-01-31
python hermes_market_data.py history --symbol 00700 --market hk --start 2025-01-01 --end 2025-01-31
```

### 3) 并发 fallback / 超时控制

所有子命令支持三个顶层 flag（也可用同名环境变量），用于约束 fallback 链的尾延迟：

| Flag | 环境变量 | 默认 | 含义 |
| ---- | -------- | ---- | ---- |
| `--provider-timeout` | `HERMES_PROVIDER_TIMEOUT` | `6.0` | 每家 provider 最多等待秒数 |
| `--deadline` | `HERMES_GLOBAL_DEADLINE` | `20.0` | 整条 fallback 链的全局截止时间 |
| `--hedge-delay` | `HERMES_HEDGE_DELAY` | _未设置_ → 串行 | 设为正数开启 hedged 并发：上一家等待该秒数后还没返回就并发拉起下一家 |

例：

```bash
# 严格串行(默认行为)
python hermes_market_data.py quote --symbol 600519

# 上一家 1.5s 没回就并发拉下一家,缩短 P99 尾延迟
python hermes_market_data.py --hedge-delay 1.5 quote --symbol 00700 --market hk

# 通过环境变量
HERMES_HEDGE_DELAY=1.5 HERMES_PROVIDER_TIMEOUT=4 python hermes_market_data.py quote --symbol 600519
```

### 4) 批量行情、搜索、Agent 工具描述

```bash
# 一次拉多只股票(并发,单 JSON 信封 items:[...])
hermes-market quote --symbols 600519,000001,00700,09988

# 自然语言/代码片段搜索 → 候选 (code, name, market)
# 第一次会从 akshare 下载全市场代码↔名称索引(~440KB,缓存到 ~/.cache/hermes_market/,
# 24 小时 TTL),之后离线匹配
hermes-market search --query 茅台
hermes-market search --query 平安 --limit 5
hermes-market search --query tencent --market hk

# 为 LLM agent 生成 tool-calling 描述(OpenAI/Anthropic/MCP 三选一)
hermes-market tools --format openai     > tools/hermes_openai.json
hermes-market tools --format anthropic  > tools/hermes_anthropic.json
hermes-market tools --format mcp        > tools/hermes_mcp.json
```

## Agent Integration

为方便 agent 框架开箱即用,本仓库预生成了三套工具描述并 check-in 在
[`tools/`](tools/) 目录下:

| 文件 | 用途 |
| --- | --- |
| [`tools/hermes_tools.openai.json`](tools/hermes_tools.openai.json) | OpenAI function-calling: `tools=[{type:"function", function:{name,description,parameters}}]` |
| [`tools/hermes_tools.anthropic.json`](tools/hermes_tools.anthropic.json) | Anthropic tool-use: `tools=[{name,description,input_schema}]` |
| [`tools/hermes_tools.mcp.json`](tools/hermes_tools.mcp.json) | MCP server: `{name, description, inputSchema}`(camelCase) |
| [`tools/output_schemas.json`](tools/output_schemas.json) | 输出 JSON Schema(`FetchResult` / `BatchQuoteResult` / `SearchResult`) |

每份描述包含 5 个工具: `quote` / `batch_quote` / `history` / `news` / `search`。
推荐的 agent 调用流程:

1. 用户说"看下贵州茅台的最新价" → agent 先调 `search(query="茅台")` 拿到 `600519`
2. 再调 `quote(symbol="600519")` 拿价格
3. 比较组合时调 `batch_quote(symbols=["600519","000001","00700"])` 一次拉齐

OpenAI Python SDK 接入示例:

```python
import json, openai
tools = json.load(open("tools/hermes_tools.openai.json"))["tools"]
resp = openai.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "贵州茅台现在多少钱?"}],
    tools=tools,
)
# resp.choices[0].message.tool_calls[0] → {name: "search", arguments: {"query": "贵州茅台"}}
# 你的代码: shell out 到 `hermes-market search --query 贵州茅台` 拿到 600519,
# 再 `hermes-market quote --symbol 600519`,把结果回给 LLM
```

工具描述每次发版会同步更新; CI 会校验 `tools/*.json` 文件与 `tool_spec.py`
保持一致(避免改了 spec 但忘记同步)。

## Benchmarks

从一台 Devin sandbox VM 实测（2026-05-20，akshare 的 Eastmoney quote 接口在
该 VM 出口下不稳定，yfinance 健康）。完整结果与原始数据见
[`scripts/bench_results.md`](scripts/bench_results.md) /
[`scripts/bench_results.json`](scripts/bench_results.json)。

| mode               | n  | success | p50    | p95    | p99    | mean   |
| ------------------ | -: | -:      | -:     | -:     | -:     | -:     |
| serial (默认)      | 12 | 12/12   | 4244ms | 4268ms | 4275ms | 4240ms |
| `--hedge-delay 2`  | 12 | 12/12   | 2237ms | 2271ms | 2283ms | 2222ms |
| `--hedge-delay 0.5`| 12 | 12/12   |  750ms |  782ms |  797ms |  736ms |

在主 provider 退化但下游健康的场景下，`--hedge-delay 0.5` 把 p50 / p99
从 ~4.3s 降到 ~0.8s，约 **5.5×** 提速；成功率不受影响。当主 provider
健康时 hedge 几乎不会触发，行为退化为串行，开销可忽略。

复现：

```bash
pip install -e ".[dev]"
pip install akshare yfinance baostock setuptools
python scripts/bench.py --symbols 600519,000001,00700,09988 \
    --iters 3 --warmup 1 --provider-timeout 4 --deadline 10
```

### 真实接口冒烟测试

`tests/test_smoke_live.py` 提供 5 个针对真实 provider 的冒烟测试，**默认
跳过**（不进 CI 门禁）。本地启用：

```bash
HERMES_RUN_LIVE=1 pytest tests/test_smoke_live.py -v
```

契约是"至少一家 provider 成功"——单家 provider 失败属正常，5 家全失败才
是 bug。

## 输出格式（JSON）

```json
{
  "ok": true,
  "provider": "akshare",
  "symbol": "600519",
  "market": "cn",
  "data": {
    "name": "贵州茅台",
    "last": 1688.0,
    "change_pct": 0.75,
    "turnover": 123456789.0,
    "timestamp": "2026-05-07T10:00:00Z"
  },
  "error": null,
  "errors": [],
  "schema_version": 1
}
```

新增字段说明：

- `schema_version`：输出 schema 版本号，未来字段变更时方便上游兼容。
- `errors`：结构化失败列表（每条 `{provider, message}`），失败时记录每一跳 fallback 的报错，便于排障。`error` 字段保留为 `"; "` 拼接的字符串以维持兼容。
- yfinance 路径的 `turnover` 改为 `last * volume` 的近似值，并额外提供 `volume` 字段；其它 provider 仍直接返回成交额（CNY/HKD）。
- baostock 路径新增 `data.as_of` 字段，标明 T+1 数据对应的真实交易日。

## Hermes Agent 接入建议

- 直接把脚本当作 tool command 调用。
- 以退出码判断结果：`0=成功`，`2=失败`。
- 以 `provider` 字段上报命中源，便于监控 fallback 比例。
- 建议在 agent 侧增加：
  - 超时（例如 8~15 秒）
  - 重试（主源失败后重试 1 次）
  - 指标（成功率、平均延迟、fallback 率）

## 常见 symbol 规则

- A 股：
  - 上交所：`600xxx`、`688xxx`（科创板）、`9xxxxx` → `sh.xxx` / `.SS` / `SHxxx`
  - 深交所：`000xxx`、`300xxx`（创业板） → `sz.xxx` / `.SZ` / `SZxxx`
  - 北交所：`4xxxxx`、`8xxxxx` → `bj.xxx` / `.BJ` / `BJxxx`
  - 也可以带前缀：`sh600519`、`sz000001`、`bj430047`
- 港股：`00700` / `700` / `0700.HK` 都会被自动识别。



## T+1 说明（baostock）

- `baostock` 仅用于 A 股（`market=cn`），不用于港股。
- 作为回退源时，返回数据是 T+1 可见数据，不适合严格实时场景。
- 实现会回溯最近 7 天找到最后一个有数据的交易日，返回其收盘行情。
- 输出 `data.note` 字段标记 `T+1 delayed via baostock`，`data.as_of` 标记实际数据对应的交易日。


## 雪球数据源（GitHub 生态）

- 已集成内置 `XueqiuClient`（无需额外 SDK），通过雪球公开接口拉取 `quote` 与 `kline`。
- 可参考社区项目：`1dot75cm/xueqiu`、`liqiongyu/xueqiu_mcp`。
- Hermes 接入建议：将雪球作为实时/准实时 fallback，并保留 `baostock` 作为 A 股 T+1 稳定兜底。


## 财经新闻数据源

- 新增 `news` 命令，优先使用 `akshare`（东方财富新闻相关接口），失败后 fallback 到 `xueqiu`，再兜底到 `新浪财经 RSS`。
- 支持 `--limit`，可选 `--symbol/--market` 做聚焦新闻抓取。

```bash
python hermes_market_data.py news --limit 20
python hermes_market_data.py news --symbol 600519 --market cn --limit 10
```

## 参考的 GitHub 项目

- `akfamily/akshare`（财经数据接口总库，包含新闻接口）
- `1dot75cm/xueqiu`（雪球 API Python 封装）
- `liqiongyu/xueqiu_mcp`（雪球数据 MCP 接入思路）


## 雪球稳定性增强

- 使用双入口 cookie bootstrap（`/` 与 `/hq`）。
- 针对 `401/403` 自动刷新 cookie 并重试一次。
- 网络瞬时错误（`URLError`）增加一次重试。

- 额外增加 `sina_rss` 新闻兜底源，适合在雪球受限时保证基础新闻可用性。

- 可参考 GitHub 项目：`DIYgod/RSSHub`（财经 RSS 聚合思路）。
