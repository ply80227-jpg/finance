# Benchmark results

Captured by `scripts/bench.py` from a Devin sandbox VM on **2026-05-20**.
Reflects the latency profile of the multi-provider fallback chain when the
primary (akshare via Eastmoney) is unreliable from the box and a downstream
provider (yfinance via Yahoo) is healthy.

```
python scripts/bench.py \
    --symbols 600519,000001,00700,09988 \
    --iters 3 --warmup 1 \
    --provider-timeout 4 --deadline 10
```

## Per-mode latency summary

| mode      | n  | success | p50 ms | p95 ms | p99 ms | mean ms | max ms |
| --------- | -: | -:      | -:     | -:     | -:     | -:      | -:     |
| serial    | 12 | 12/12   | 4244   | 4268   | 4275   | 4240    | 4277   |
| hedge-2.0 | 12 | 12/12   | 2237   | 2271   | 2283   | 2222    | 2286   |
| hedge-0.5 | 12 | 12/12   |  750   |  782   |  797   |  736    |  800   |

**Take-away:** with `--hedge-delay 0.5`, the p50 latency drops from 4244ms to
750ms — roughly a **5.6×** speedup on the happy path — because the chain
stops waiting for the primary's per-provider timeout before trying the next
provider. The success rate is unchanged (12/12 in all modes); hedging trades
a small amount of duplicated work for tail-latency.

The p99 follows the same pattern (4275ms → 797ms, **5.4×**), so the win is
not just an average-case artifact.

## Provider distribution per (symbol, mode)

| symbol  | mode      | success | providers used                |
| ------- | --------- | -:      | ----------------------------- |
| 000001  | serial    | 3/3     | yfinance×3                    |
| 000001  | hedge-2.0 | 3/3     | yfinance×2, akshare×1         |
| 000001  | hedge-0.5 | 3/3     | yfinance×3                    |
| 00700   | serial    | 3/3     | yfinance×3                    |
| 00700   | hedge-2.0 | 3/3     | yfinance×3                    |
| 00700   | hedge-0.5 | 3/3     | yfinance×3                    |
| 09988   | serial    | 3/3     | yfinance×3                    |
| 09988   | hedge-2.0 | 3/3     | yfinance×3                    |
| 09988   | hedge-0.5 | 3/3     | yfinance×3                    |
| 600519  | serial    | 3/3     | yfinance×3                    |
| 600519  | hedge-2.0 | 3/3     | yfinance×3                    |
| 600519  | hedge-0.5 | 3/3     | yfinance×3                    |

In all four symbols yfinance ended up serving the data because akshare's
Eastmoney quote endpoints (both `stock_bid_ask_em` and the
`stock_zh_a_spot_em` full-table) were intermittently returning a 1xx HTML
page or hanging long enough to be killed by the per-provider timeout from
this VM's egress. On a box with healthy access to Eastmoney, akshare would
win the serial race in ~1.5–2s and the hedge would only kick in for the
worst-case tail (when akshare is having a bad day).

## Environment notes (provider availability on this sandbox)

| provider  | reachable | quote works | notes                                                                                                              |
| --------- | --------- | ----------- | ------------------------------------------------------------------------------------------------------------------ |
| akshare   | yes       | flaky       | `stock_bid_ask_em` → `JSONDecodeError` (~1s). Spot fallback returns `RemoteDisconnected` after ~37s. Eastmoney throttling. |
| yfinance  | rate-limited | yes      | Returns within ~3-4s including cold-start. Tencent worked once HK symbol was down-padded to 4 digits (`0700.HK`).                                                                                            |
| xueqiu    | yes       | needs-cookie | Cookie bootstrap returns 403/400 without proper session; cookie-cache will re-bootstrap once it primes.            |
| baostock  | yes       | yes (T+1)   | Health-check passes; live-quote path uses 7-day lookback and yields the most recent trading day's close.           |
| stooq     | yes       | yes (HTTP CSV) | Replaced `pandas-datareader` with a direct urllib + `csv` implementation (P6). Quote endpoint is free; historical CSV requires `STOOQ_APIKEY`. |

## How to reproduce

```bash
pip install -e ".[dev]"   # or: pip install -r requirements.txt
pip install akshare yfinance baostock setuptools

# Fast 4-symbol sweep (~2-3 min on this box)
python scripts/bench.py \
    --symbols 600519,000001,00700,09988 \
    --iters 3 --warmup 1 \
    --provider-timeout 4 --deadline 10
```

Raw per-call rows are written to `scripts/bench_results.json` for later
diffing across runs.
