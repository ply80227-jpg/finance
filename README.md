# Hermes A股/港股免费数据源脚本

该仓库提供一个单文件脚本：`hermes_market_data.py`，用于 Hermes agent 接入 A 股与港股行情，并包含自动 fallback。

## 数据源策略

1. **主源：`akshare`**（免费、A/H 股覆盖较好）
2. **一级回退：`yfinance`**（免费、稳定度较高）
3. **二级回退：`xueqiu`**（雪球公开接口，A/H 覆盖较好）
4. **三级回退：`baostock`**（A 股稳定，但为 T+1 延迟，适合历史/兜底）
5. **四级回退：`stooq`（通过 `pandas_datareader`）**（免费公开数据，最终兜底）

> 说明：以上数据源都可免费使用，但都依赖第三方公开接口，稳定性会受网络和上游策略影响。

## 安装

```bash
pip install akshare yfinance baostock pandas pandas_datareader
```

## 用法

### 1) 最新行情

```bash
python hermes_market_data.py quote --symbol 600519 --market cn
python hermes_market_data.py quote --symbol 00700 --market hk
```

### 2) 历史日线

```bash
python hermes_market_data.py history --symbol 600519 --start 2025-01-01 --end 2025-01-31
python hermes_market_data.py history --symbol 00700 --market hk --start 2025-01-01 --end 2025-01-31
```

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
  "error": null
}
```

## Hermes Agent 接入建议

- 直接把脚本当作 tool command 调用。
- 以退出码判断结果：`0=成功`，`2=失败`。
- 以 `provider` 字段上报命中源，便于监控 fallback 比例。
- 建议在 agent 侧增加：
  - 超时（例如 8~15 秒）
  - 重试（主源失败后重试 1 次）
  - 指标（成功率、平均延迟、fallback 率）

## 常见 symbol 规则

- A 股：`600519`、`000001`（或 `sh600519`/`sz000001`）
- 港股：`00700`（或 `700` / `0700.HK`）



## T+1 说明（baostock）

- `baostock` 仅用于 A 股（`market=cn`），不用于港股。
- 作为回退源时，返回数据可能是交易日收盘后的 T+1 可见数据，不适合严格实时场景。
- 输出 `data.note` 字段会标记 `T+1 delayed via baostock`。


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
