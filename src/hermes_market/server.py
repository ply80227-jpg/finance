"""Stdio MCP server for ``hermes-market``.

Why a long-lived server matters
-------------------------------

The CLI pays a 1-2 second cold start on every invocation: ``import akshare``
alone is ~1.2s and the xueqiu cookie warm-up is another ~150-300ms. When the
host LLM agent fires 5-10 tool calls in a single conversation turn, that
adds up to **a third of the user-visible latency**. This server hosts a
single :class:`~hermes_market.fetcher.MarketDataFetcher` instance for the
lifetime of the process so the import cost is paid once, and the in-process
TTL cache (`cache.py`) actually becomes useful across calls.

Wire protocol: **stdio JSON-RPC 2.0** as specified by the Model Context
Protocol (https://spec.modelcontextprotocol.io). We deliberately implement
the subset by hand on top of stdlib only — no new dependency on the
``mcp`` package — so the package keeps installing cleanly on the same
constrained boxes the CLI runs on.

Supported methods
-----------------

* ``initialize`` — handshake; replies with ``protocolVersion / capabilities
  / serverInfo``.
* ``notifications/initialized`` — handshake completion (notification, no
  reply).
* ``ping`` — empty result.
* ``tools/list`` — returns :func:`~hermes_market.tool_spec.get_mcp_tools`.
* ``tools/call`` — invokes one of the 5 tools (``quote``, ``batch_quote``,
  ``history``, ``news``, ``search``) on the shared fetcher and returns the
  JSON envelope inside an MCP ``content`` block.
* ``shutdown`` (optional) — graceful exit; the server also exits cleanly on
  EOF on stdin.

Concurrency
-----------

A bounded thread pool runs tool calls so a slow ``akshare`` request does
not block the next ``tools/call`` from being parsed off stdin. Stdout
writes are serialized through a single lock to keep the JSON-RPC frames
intact. A small **single-flight** layer (:class:`_SingleFlight`)
deduplicates concurrent identical tool calls (same name + same arguments)
so two LLM tool calls fired in parallel for the same symbol only do one
upstream HTTP round-trip.

Logging is written to stderr only — stdout is reserved for JSON-RPC
frames per the MCP spec.
"""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import IO, Any

from .fetcher import DEFAULT_GLOBAL_DEADLINE, DEFAULT_PROVIDER_TIMEOUT, MarketDataFetcher
from .models import SCHEMA_VERSION, FetchResult
from .tool_spec import get_mcp_tools, get_output_schemas

logger = logging.getLogger("hermes_market.server")

# Pin a widely-supported MCP protocol version. Older clients (Claude
# Desktop pre-2025-Q1) speak this; newer servers can negotiate up via
# ``initialize`` if needed.
MCP_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "hermes-market"
SERVER_VERSION = "0.2.0"

# JSON-RPC 2.0 error codes (https://www.jsonrpc.org/specification#error_object).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Single-flight de-duplication
# ---------------------------------------------------------------------------


class _SingleFlight:
    """Coalesce concurrent identical calls onto a single in-flight Future.

    Two ``tools/call`` requests for the same (name, args) that arrive while
    a first one is still running will share the first call's result. The
    keyed Future is removed once it resolves so a follow-up call after
    completion will run again (and pick up fresh data).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, cf.Future[Any]] = {}

    def do(self, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                fut: cf.Future[Any] = existing
                first = False
            else:
                fut = cf.Future()
                self._inflight[key] = fut
                first = True

        if first:
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - we re-raise below
                fut.set_exception(exc)
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
        return fut.result()


def _singleflight_key(name: str, args: dict[str, Any]) -> str:
    """Stable string key for ``(tool_name, args)``."""

    return name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _result_to_dict(result: FetchResult) -> dict[str, Any]:
    return dataclasses.asdict(result)


def _dispatch_tool(fetcher: MarketDataFetcher, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a single MCP ``tools/call`` into a fetcher method invocation."""

    if name == "quote":
        symbol = args.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("quote: 'symbol' is required and must be a non-empty string")
        market = args.get("market")
        return _result_to_dict(fetcher.quote(symbol, market))

    if name == "batch_quote":
        symbols = args.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ValueError("batch_quote: 'symbols' is required and must be a non-empty array")
        market = args.get("market")
        results = fetcher.batch_quote(symbols, market)
        items = [_result_to_dict(r) for r in results]
        return {
            "ok": any(r.ok for r in results),
            "count": len(results),
            "items": items,
            "schema_version": SCHEMA_VERSION,
        }

    if name == "history":
        symbol = args.get("symbol")
        start = args.get("start")
        end = args.get("end")
        if not all(isinstance(v, str) and v for v in (symbol, start, end)):
            raise ValueError("history: 'symbol', 'start', 'end' are required strings")
        market = args.get("market")
        # mypy/type-narrowing: the `isinstance` guard above proved these are str
        return _result_to_dict(fetcher.history(symbol, start, end, market))  # type: ignore[arg-type]

    if name == "news":
        limit = int(args.get("limit", 20))
        symbol = args.get("symbol")
        market = args.get("market")
        return _result_to_dict(fetcher.news(limit, symbol, market))

    if name == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search: 'query' is required and must be a non-empty string")
        limit = int(args.get("limit", 10))
        market = args.get("market")
        rows = fetcher.search(query, limit=limit, market=market)
        return {
            "ok": len(rows) > 0,
            "query": query,
            "count": len(rows),
            "items": [r.to_dict() for r in rows],
            "schema_version": SCHEMA_VERSION,
        }

    raise ValueError(f"unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class StdioMCPServer:
    """Reads JSON-RPC frames from ``stdin``, writes responses to ``stdout``.

    Each line of stdin is treated as one JSON-RPC message. We also support
    the LSP-style ``Content-Length: N\\r\\n\\r\\n<body>`` framing because
    some MCP clients use it; ``_read_message`` auto-detects.
    """

    def __init__(
        self,
        fetcher: MarketDataFetcher | None = None,
        *,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
        max_workers: int = 8,
    ) -> None:
        self._fetcher = fetcher
        # Lazy construction so import errors / cookie load only happen
        # after the client successfully completes ``initialize``.
        self._fetcher_lock = threading.Lock()
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._stdout_lock = threading.Lock()
        self._executor = cf.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="hermes-mcp",
        )
        self._singleflight = _SingleFlight()
        self._initialized = False
        self._shutdown = threading.Event()
        # Last-resort stats for ``ping`` / debugging via stderr logging.
        self._stats = {"requests": 0, "tools_call": 0, "singleflight_hits": 0}

    # ------------------------------------------------------------- fetcher

    def _get_fetcher(self) -> MarketDataFetcher:
        if self._fetcher is None:
            with self._fetcher_lock:
                if self._fetcher is None:
                    t0 = time.monotonic()
                    self._fetcher = MarketDataFetcher(
                        provider_timeout=DEFAULT_PROVIDER_TIMEOUT,
                        global_deadline=DEFAULT_GLOBAL_DEADLINE,
                        hedge_delay=None,
                    )
                    logger.info("fetcher warm-up complete in %.2fs", time.monotonic() - t0)
        return self._fetcher

    # ------------------------------------------------------------- framing

    def _read_message(self) -> str | None:
        """Read one JSON-RPC message from stdin. Returns None on EOF."""

        line = self._stdin.readline()
        if line == "":
            return None
        # Content-Length framing (LSP-style): some MCP clients send headers.
        if line.lower().startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
            # Consume header block terminator.
            while True:
                blank = self._stdin.readline()
                if blank in ("\r\n", "\n", ""):
                    break
            body = self._stdin.read(length)
            return body
        return line.strip()

    def _write(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        with self._stdout_lock:
            self._stdout.write(data + "\n")
            self._stdout.flush()

    # ------------------------------------------------------------- replies

    def _ok(self, msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(self, msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": msg_id, "error": err}

    # ------------------------------------------------------------- handlers

    def _handle_initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        # ``capabilities.tools`` advertises we expose tools; ``listChanged``
        # is False because our tool list is static for a given build.
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _handle_tools_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": get_mcp_tools()}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise ValueError("tools/call: 'name' is required")
        if not isinstance(args, dict):
            raise ValueError("tools/call: 'arguments' must be an object")

        key = _singleflight_key(name, args)
        fetcher = self._get_fetcher()

        # Track whether this is a singleflight reuse for stats only; the
        # actual coalescing happens inside ``_SingleFlight.do``.
        with self._singleflight._lock:  # noqa: SLF001 - stats peek
            if key in self._singleflight._inflight:  # noqa: SLF001
                self._stats["singleflight_hits"] += 1

        envelope = self._singleflight.do(key, lambda: _dispatch_tool(fetcher, name, args))
        # MCP tools/call result shape: ``{content: [{type, text|json}], isError?: bool}``.
        # We emit the envelope as a JSON-stringified text block so any MCP
        # host can parse it without negotiating a richer content type.
        is_error = isinstance(envelope, dict) and envelope.get("ok") is False
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(envelope, ensure_ascii=False),
                }
            ],
            "isError": bool(is_error),
        }

    def _handle_resources_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        # We expose the output schemas as a single static resource so an
        # MCP host can pull them without an out-of-band ``hermes-market
        # tools`` shell-out.
        return {
            "resources": [
                {
                    "uri": "hermes://output-schemas",
                    "name": "Hermes Market output JSON Schemas",
                    "description": "JSON Schemas for quote/batch_quote/history/news/search envelopes.",
                    "mimeType": "application/json",
                }
            ]
        }

    def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if uri != "hermes://output-schemas":
            raise ValueError(f"unknown resource uri: {uri!r}")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(get_output_schemas(), ensure_ascii=False),
                }
            ]
        }

    # ------------------------------------------------------------- dispatch

    def _process(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            self._write(self._err(msg_id, _INVALID_PARAMS, "params must be an object"))
            return

        # Notifications (no id) get no reply.
        is_notification = "id" not in msg
        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "notifications/initialized":
                self._initialized = True
                return
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._handle_tools_list(params)
            elif method == "tools/call":
                self._stats["tools_call"] += 1
                result = self._handle_tools_call(params)
            elif method == "resources/list":
                result = self._handle_resources_list(params)
            elif method == "resources/read":
                result = self._handle_resources_read(params)
            elif method == "shutdown":
                self._shutdown.set()
                result = {}
            else:
                if is_notification:
                    return
                self._write(self._err(msg_id, _METHOD_NOT_FOUND, f"unknown method: {method!r}"))
                return
        except ValueError as exc:
            if is_notification:
                logger.warning("notification %s raised: %s", method, exc)
                return
            self._write(self._err(msg_id, _INVALID_PARAMS, str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("internal error handling %s", method)
            if is_notification:
                return
            self._write(self._err(msg_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"))
            return

        if not is_notification:
            self._write(self._ok(msg_id, result))

    # ------------------------------------------------------------- run

    def serve_forever(self) -> None:
        """Block reading stdin until EOF or shutdown notification."""

        logger.info("hermes-market MCP server starting (stdio)")
        try:
            while not self._shutdown.is_set():
                raw = self._read_message()
                if raw is None:
                    logger.info("EOF on stdin; exiting")
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": _PARSE_ERROR, "message": f"parse error: {exc}"},
                        }
                    )
                    continue
                if not isinstance(msg, dict):
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": _INVALID_REQUEST, "message": "request must be an object"},
                        }
                    )
                    continue
                self._stats["requests"] += 1
                # Dispatch in a worker so a slow upstream call (e.g. xueqiu
                # cookie warm-up on first hit) does not block parsing the
                # next request.
                self._executor.submit(self._process, msg)
        finally:
            self._executor.shutdown(wait=True)
            logger.info("hermes-market MCP server stopped (stats=%s)", self._stats)


def main(argv: list[str] | None = None) -> int:
    """``hermes-market serve`` entry point; kept thin so the CLI module can call it."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="hermes-market serve",
        description="Run hermes-market as a long-lived MCP server over stdio.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Wire protocol. Only 'stdio' is currently supported.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Bound on concurrent tool-call worker threads (default: 8).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level for the stderr logger (default: INFO).",
    )
    args = parser.parse_args(argv)

    # Logs to stderr — stdout is reserved for JSON-RPC frames.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    server = StdioMCPServer(max_workers=args.max_workers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
