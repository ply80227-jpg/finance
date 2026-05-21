"""CLI entry point. Preserves the historical ``python hermes_market_data.py`` UX.

Concurrency / timeout flags
---------------------------

* ``--provider-timeout SECONDS`` — max seconds to wait per provider before
  giving up and moving to the next (default: ``HERMES_PROVIDER_TIMEOUT`` env
  var or :data:`~hermes_market.fetcher.DEFAULT_PROVIDER_TIMEOUT`).
* ``--deadline SECONDS`` — hard global deadline for the whole fallback chain
  (default: ``HERMES_GLOBAL_DEADLINE`` env var or
  :data:`~hermes_market.fetcher.DEFAULT_GLOBAL_DEADLINE`).
* ``--hedge-delay SECONDS`` — when set to a positive value, enables hedged
  concurrent fallback: the next provider is spawned in parallel after the
  previous one has been pending for this many seconds, and whichever
  succeeds first wins. Default: ``HERMES_HEDGE_DELAY`` env var or strict
  sequential.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .fetcher import (
    DEFAULT_GLOBAL_DEADLINE,
    DEFAULT_PROVIDER_TIMEOUT,
    DEFAULT_WITH_FUNDAMENTALS,
    MarketDataFetcher,
)
from .models import SCHEMA_VERSION, FetchResult
from .tool_spec import render as render_tool_spec


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A/HK free market data fetcher with multi-provider fallback")
    parser.add_argument(
        "--provider-timeout",
        type=float,
        default=_env_float("HERMES_PROVIDER_TIMEOUT", DEFAULT_PROVIDER_TIMEOUT),
        help="Max seconds per provider attempt (env: HERMES_PROVIDER_TIMEOUT)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=_env_float("HERMES_GLOBAL_DEADLINE", DEFAULT_GLOBAL_DEADLINE),
        help="Hard global deadline for the whole fallback chain (env: HERMES_GLOBAL_DEADLINE)",
    )
    parser.add_argument(
        "--hedge-delay",
        type=float,
        default=_env_float("HERMES_HEDGE_DELAY", None),
        help="Enable hedged concurrent fallback: spawn the next provider after this many seconds. "
        "Default = sequential. (env: HERMES_HEDGE_DELAY)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_quote = sub.add_parser("quote", help="Realtime snapshot quote")
    group = p_quote.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="Single stock code (returns the legacy FetchResult envelope).")
    group.add_argument(
        "--symbols",
        help="Comma-separated list, e.g. '600519,000001,00700'. Returns a batch envelope.",
    )
    p_quote.add_argument("--market", choices=["cn", "hk"], default=None)
    fund_group = p_quote.add_mutually_exclusive_group()
    fund_group.add_argument(
        "--with-fundamentals",
        dest="with_fundamentals",
        action="store_true",
        help="Attach PE/PB/market_cap to data.fundamentals (default: on).",
    )
    fund_group.add_argument(
        "--no-fundamentals",
        dest="with_fundamentals",
        action="store_false",
        help="Skip the fundamentals side-channel (lowest latency).",
    )
    p_quote.set_defaults(with_fundamentals=DEFAULT_WITH_FUNDAMENTALS)

    p_hist = sub.add_parser("history", help="Daily K-line history")
    p_hist.add_argument("--symbol", required=True)
    p_hist.add_argument("--market", choices=["cn", "hk"], default=None)
    p_hist.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_hist.add_argument("--end", required=True, help="YYYY-MM-DD")

    p_news = sub.add_parser("news", help="Finance news headlines")
    p_news.add_argument("--limit", type=int, default=20)
    p_news.add_argument("--symbol", default=None)
    p_news.add_argument("--market", choices=["cn", "hk"], default=None)

    p_search = sub.add_parser("search", help="Resolve a name/code substring to candidate (code, name, market) tuples")
    p_search.add_argument("--query", required=True, help="Company name (CN/EN) or code substring.")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--market", choices=["cn", "hk"], default=None)

    p_tools = sub.add_parser("tools", help="Emit LLM tool-calling specs for this CLI")
    p_tools.add_argument(
        "--format",
        choices=["openai", "anthropic", "mcp"],
        default="openai",
        help="Which framework's tool schema to emit (default: openai).",
    )

    p_serve = sub.add_parser(
        "serve",
        help="Run as a long-lived MCP server (stdio JSON-RPC). Reuses one fetcher across calls.",
    )
    p_serve.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Wire protocol (only stdio is currently supported).",
    )
    p_serve.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Bound on concurrent tool-call worker threads (default: 8).",
    )
    p_serve.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Server log level (logs to stderr; default: INFO).",
    )

    return parser


def _emit(result: FetchResult) -> int:
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.ok else 2


def _emit_batch(results: list[FetchResult]) -> int:
    items = [asdict(r) for r in results]
    ok = any(r.ok for r in results)
    payload = {
        "ok": ok,
        "count": len(results),
        "items": items,
        "schema_version": SCHEMA_VERSION,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 2


def _emit_search(query: str, rows: list) -> int:
    payload = {
        "ok": len(rows) > 0,
        "query": query,
        "count": len(rows),
        "items": [r.to_dict() for r in rows],
        "schema_version": SCHEMA_VERSION,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if rows else 2


def _emit_error(provider: str, exc: BaseException) -> int:
    payload = {
        "ok": False,
        "provider": provider,
        "symbol": "",
        "market": "",
        "data": {},
        "error": f"{type(exc).__name__}: {exc}",
        "errors": [{"provider": provider, "message": f"{type(exc).__name__}: {exc}"}],
        "schema_version": SCHEMA_VERSION,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse already wrote a human-readable error to stderr; preserve its code.
        raise e

    # ``tools`` is a pure-Python introspection command; do not pay the cost
    # of bootstrapping the data fetcher (akshare/baostock imports, xueqiu
    # cookie load, etc.) just to render JSON.
    if args.cmd == "tools":
        print(json.dumps(render_tool_spec(args.format), ensure_ascii=False))
        return 0

    # ``serve`` hands control to the long-lived MCP server. The server
    # builds its own MarketDataFetcher on first ``tools/call`` so the
    # ``initialize`` handshake stays fast.
    if args.cmd == "serve":
        from .server import main as serve_main

        return serve_main(
            [
                "--transport",
                args.transport,
                "--max-workers",
                str(args.max_workers),
                "--log-level",
                args.log_level,
            ]
        )

    try:
        fetcher = MarketDataFetcher(
            provider_timeout=args.provider_timeout,
            global_deadline=args.deadline,
            hedge_delay=args.hedge_delay,
        )
        if args.cmd == "quote":
            if args.symbols is not None:
                syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
                results = fetcher.batch_quote(
                    syms,
                    args.market,
                    with_fundamentals=args.with_fundamentals,
                )
                return _emit_batch(results)
            result = fetcher.quote(
                args.symbol,
                args.market,
                with_fundamentals=args.with_fundamentals,
            )
        elif args.cmd == "history":
            result = fetcher.history(args.symbol, args.start, args.end, args.market)
        elif args.cmd == "search":
            rows = fetcher.search(args.query, limit=args.limit, market=args.market)
            return _emit_search(args.query, rows)
        else:
            result = fetcher.news(args.limit, args.symbol, args.market)
    except Exception as e:  # noqa: BLE001
        return _emit_error("cli", e)

    return _emit(result)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
