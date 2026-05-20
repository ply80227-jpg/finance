"""Smoke benchmark for the multi-provider fallback chain.

Usage::

    .venv/bin/python scripts/bench.py
    .venv/bin/python scripts/bench.py --symbols 600519,000001,00700,09988 --iters 5

What it does
------------
For each ``(symbol, mode)`` pair, calls :class:`MarketDataFetcher.quote` ``iters``
times and records per-call elapsed-ms, winning provider, and error list. Then
emits a Markdown summary table with p50/p95/p99 latency per mode and a provider
distribution per symbol.

The "modes" are:

* ``serial`` — strict sequential fallback (``hedge_delay=None``, the default
  behaviour shipped before P3).
* ``hedge-2.0`` — hedge the next provider in after the previous has been
  pending for 2.0s.
* ``hedge-0.5`` — aggressive hedging (next provider after 0.5s). Useful for
  P99 reduction when the primary's failure mode is "slow, then errors".

Output is appended to ``scripts/bench_results.json`` (raw) and printed as a
Markdown table on stdout.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from hermes_market.fetcher import MarketDataFetcher

DEFAULT_SYMBOLS = ["600519", "000001", "300750", "00700", "09988"]
DEFAULT_ITERS = 5
DEFAULT_MODES: list[tuple[str, float | None]] = [
    ("serial", None),
    ("hedge-2.0", 2.0),
    ("hedge-0.5", 0.5),
]
DEFAULT_PROVIDER_TIMEOUT = 6.0
DEFAULT_GLOBAL_DEADLINE = 20.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    k = (len(sorted_v) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_v) - 1)
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


def _run_one(fetcher: MarketDataFetcher, sym: str, mode_name: str) -> dict:
    """Run a single quote() call against a (reused) fetcher and capture outcome."""

    t0 = time.monotonic()
    try:
        result = fetcher.quote(sym)
        ok = result.ok
        provider = result.provider
        errors = [{"provider": e.get("provider"), "message": e.get("message")} for e in result.errors]
    except Exception as e:  # noqa: BLE001
        ok = False
        provider = "exception"
        errors = [{"provider": "exception", "message": f"{type(e).__name__}: {e}"}]
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    return {
        "symbol": sym,
        "mode": mode_name,
        "hedge_delay": fetcher.hedge_delay,
        "ok": ok,
        "provider": provider,
        "elapsed_ms": round(elapsed_ms, 1),
        "errors": errors,
    }


def _summarize(rows: list[dict]) -> str:
    by_mode: dict[str, list[dict]] = {}
    for r in rows:
        by_mode.setdefault(r["mode"], []).append(r)

    lines: list[str] = []
    lines.append("\n## Per-mode latency summary\n")
    lines.append("| mode | n | success | p50 ms | p95 ms | p99 ms | mean ms | max ms |")
    lines.append("| ---- | -: | -: | -: | -: | -: | -: | -: |")
    for mode, mode_rows in by_mode.items():
        elapsed = [r["elapsed_ms"] for r in mode_rows]
        success = sum(1 for r in mode_rows if r["ok"])
        lines.append(
            f"| {mode} | {len(mode_rows)} | {success}/{len(mode_rows)} "
            f"| {_percentile(elapsed, 0.50):.0f} | {_percentile(elapsed, 0.95):.0f} "
            f"| {_percentile(elapsed, 0.99):.0f} | {statistics.mean(elapsed):.0f} | {max(elapsed):.0f} |"
        )

    lines.append("\n## Provider distribution per (symbol, mode)\n")
    lines.append("| symbol | mode | success | providers used |")
    lines.append("| ------ | ---- | -: | -------------- |")
    by_sm: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_sm.setdefault((r["symbol"], r["mode"]), []).append(r)
    for (sym, mode), mode_rows in sorted(by_sm.items()):
        success = sum(1 for r in mode_rows if r["ok"])
        providers = Counter(r["provider"] for r in mode_rows)
        prov_str = ", ".join(f"{p}×{c}" for p, c in providers.most_common())
        lines.append(f"| {sym} | {mode} | {success}/{len(mode_rows)} | {prov_str} |")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols")
    parser.add_argument("--iters", type=int, default=DEFAULT_ITERS, help="Iterations per (symbol, mode)")
    parser.add_argument("--provider-timeout", type=float, default=DEFAULT_PROVIDER_TIMEOUT)
    parser.add_argument("--deadline", type=float, default=DEFAULT_GLOBAL_DEADLINE)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "bench_results.json"),
        help="Where to dump the raw per-call JSON rows",
    )
    parser.add_argument("--warmup", type=int, default=1, help="How many warmup iterations to discard")
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(
        f"# Benchmark: symbols={symbols} iters={args.iters} warmup={args.warmup} "
        f"per-provider-timeout={args.provider_timeout}s global-deadline={args.deadline}s",
        file=sys.stderr,
    )

    # Reuse one fetcher per mode so we don't double-count XueqiuClient bootstrap,
    # baostock login, or akshare module import on every iteration.
    fetchers: dict[str, MarketDataFetcher] = {
        mode_name: MarketDataFetcher(
            provider_timeout=args.provider_timeout,
            global_deadline=args.deadline,
            hedge_delay=hedge_delay,
        )
        for mode_name, hedge_delay in DEFAULT_MODES
    }

    rows: list[dict] = []
    for sym in symbols:
        for mode_name, _hedge_delay in DEFAULT_MODES:
            fetcher = fetchers[mode_name]
            for _ in range(args.warmup):
                _run_one(fetcher, sym, mode_name)
            for i in range(args.iters):
                row = _run_one(fetcher, sym, mode_name)
                rows.append(row)
                print(
                    f"  [{sym} / {mode_name} / {i + 1}/{args.iters}] "
                    f"ok={row['ok']} provider={row['provider']} elapsed={row['elapsed_ms']}ms",
                    file=sys.stderr,
                )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(_summarize(rows))
    print(f"# Raw rows written to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
