"""LLM tool/function-calling specs for hermes-market.

Provides canonical schemas in three formats so an agent author can paste them
straight into their framework:

* :func:`get_openai_tools` — OpenAI function-calling format
  (``{"type": "function", "function": {"name", "description", "parameters"}}``).
* :func:`get_anthropic_tools` — Anthropic tool-use format
  (``{"name", "description", "input_schema"}``).
* :func:`get_mcp_tools` — Model Context Protocol tool format (same shape as
  Anthropic's, but kept as a distinct accessor in case MCP diverges).

Also exposes :func:`get_output_schema` which returns a JSON Schema document
describing the ``FetchResult`` envelope returned by every command, plus the
``batch_quote`` and ``search`` envelopes added in this milestone.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Tool definitions (shared shape; rendered into each framework's wrapper below)
# ---------------------------------------------------------------------------

_QUOTE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "Stock code. CN A-shares: 6 digits (e.g. '600519' Maotai, "
                "'000001' Ping An, '300750' CATL, '430047' BJ). HK: 4-5 digits "
                "(e.g. '00700' Tencent, '09988' Alibaba). Market is auto-detected "
                "from the code prefix unless you pass `market` explicitly."
            ),
        },
        "market": {
            "type": "string",
            "enum": ["cn", "hk"],
            "description": "Override the auto-detected market. Optional.",
        },
        "with_fundamentals": {
            "type": "boolean",
            "default": True,
            "description": (
                "If true (default), attach PE/PB/market_cap via a hedged xueqiu+akshare race. "
                "Set to false for absolute minimum latency."
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}

_BATCH_QUOTE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbols": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": (
                "List of stock codes to fetch in parallel. Each symbol follows "
                "the same rules as `quote.symbol`. Market is auto-detected per "
                "symbol; mixed CN/HK lists are fine."
            ),
        },
        "market": {
            "type": "string",
            "enum": ["cn", "hk"],
            "description": "Force-apply this market to every symbol. Usually omit.",
        },
        "with_fundamentals": {
            "type": "boolean",
            "default": True,
            "description": (
                "Apply the same fundamentals enrichment as `quote` to every item. "
                "Set to false when you only need prices (faster on large batches)."
            ),
        },
    },
    "required": ["symbols"],
    "additionalProperties": False,
}

_HISTORY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "description": "Stock code (same format as `quote.symbol`)."},
        "start": {
            "type": "string",
            "description": "Inclusive start date, ISO format 'YYYY-MM-DD'.",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
        },
        "end": {
            "type": "string",
            "description": "Inclusive end date, ISO format 'YYYY-MM-DD'.",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
        },
        "market": {"type": "string", "enum": ["cn", "hk"]},
    },
    "required": ["symbol", "start", "end"],
    "additionalProperties": False,
}

_NEWS_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
            "description": "Max number of headlines to return.",
        },
        "symbol": {
            "type": "string",
            "description": "Optional: filter news to a specific stock. Omit for market-wide news.",
        },
        "market": {"type": "string", "enum": ["cn", "hk"]},
    },
    "required": [],
    "additionalProperties": False,
}

_SEARCH_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Natural-language query: company name (Chinese or English), "
                "ticker substring, or full code. E.g. '腾讯', 'tencent', '00700', "
                "'maotai', '茅台'."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
            "description": "Max matches to return.",
        },
        "market": {
            "type": "string",
            "enum": ["cn", "hk"],
            "description": "Restrict search to one market. Omit to search both.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "quote",
        "description": (
            "Fetch a real-time price snapshot for a single A-share or HK stock. "
            "Returns last price, open/high/low/prev_close, volume, turnover, "
            "as_of, and the provider that served the request. When "
            "`with_fundamentals` is true (default), `data.fundamentals` also "
            "includes PE TTM, PB, market cap, and dividend yield."
        ),
        "parameters": _QUOTE_PARAMS,
    },
    {
        "name": "batch_quote",
        "description": (
            "Fetch real-time price snapshots for many symbols concurrently. "
            "Returns a list of per-symbol results; individual failures do not "
            "fail the batch. Use this when comparing positions or building a "
            "portfolio view in one shot. Same `with_fundamentals` flag as quote."
        ),
        "parameters": _BATCH_QUOTE_PARAMS,
    },
    {
        "name": "history",
        "description": ("Fetch daily K-line history (OHLCV) for a single stock between `start` and `end` (inclusive)."),
        "parameters": _HISTORY_PARAMS,
    },
    {
        "name": "news",
        "description": (
            "Fetch finance news headlines. With `symbol`, returns news filtered "
            "to that stock (HK-with-symbol routes to xueqiu); without, returns "
            "market-wide news from akshare/xueqiu/sina."
        ),
        "parameters": _NEWS_PARAMS,
    },
    {
        "name": "search",
        "description": (
            "Resolve a natural-language company name or ticker substring to a "
            "list of candidate (code, name, market) tuples. Use this BEFORE "
            "calling quote/history/news when the user mentions a stock by name "
            "instead of code."
        ),
        "parameters": _SEARCH_PARAMS,
    },
]


def _ensure_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Some hosts reject schemas without explicit ``additionalProperties``."""

    schema = deepcopy(schema)
    schema.setdefault("additionalProperties", False)
    return schema


def get_openai_tools() -> list[dict[str, Any]]:
    """Return tools in OpenAI function-calling format."""

    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": _ensure_additional_properties(t["parameters"]),
            },
        }
        for t in _TOOLS
    ]


def get_anthropic_tools() -> list[dict[str, Any]]:
    """Return tools in Anthropic tool-use format."""

    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": _ensure_additional_properties(t["parameters"]),
        }
        for t in _TOOLS
    ]


def get_mcp_tools() -> list[dict[str, Any]]:
    """Return tools in MCP (Model Context Protocol) tool format.

    MCP currently mirrors Anthropic's shape (``{name, description,
    inputSchema}`` with camelCase). Kept as a distinct accessor so we can
    evolve the two independently if MCP diverges.
    """

    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": _ensure_additional_properties(t["parameters"]),
        }
        for t in _TOOLS
    ]


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

_FETCH_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FetchResult",
    "description": "Standard envelope returned by quote/history/news.",
    "type": "object",
    "required": ["ok", "provider", "symbol", "market", "data", "errors", "schema_version"],
    "properties": {
        "ok": {"type": "boolean"},
        "provider": {
            "type": "string",
            "description": "Which provider served this result, or 'none' on full failure.",
        },
        "symbol": {"type": "string"},
        "market": {"type": "string", "enum": ["cn", "hk", ""]},
        "data": {
            "type": "object",
            "description": "Payload — shape depends on command (see examples).",
            "additionalProperties": True,
        },
        "error": {
            "type": ["string", "null"],
            "description": "Legacy semicolon-joined error string. Prefer `errors`.",
        },
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["provider", "message"],
                "properties": {
                    "provider": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
    },
}

_FUNDAMENTALS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Fundamentals",
    "description": (
        "Valuation snapshot attached to `data.fundamentals` when "
        "`with_fundamentals=true`. All numeric fields may be null when the "
        "upstream source omits them."
    ),
    "type": "object",
    "properties": {
        "pe_ttm": {"type": ["number", "null"], "description": "Price-to-Earnings (TTM)."},
        "pe_lyr": {"type": ["number", "null"], "description": "Price-to-Earnings (latest reported year)."},
        "pb": {"type": ["number", "null"], "description": "Price-to-Book."},
        "ps_ttm": {"type": ["number", "null"], "description": "Price-to-Sales (TTM); xueqiu only."},
        "market_cap": {
            "type": ["number", "null"],
            "description": "Total market cap in the listing currency (CNY for A-share, HKD for HK).",
        },
        "float_market_cap": {
            "type": ["number", "null"],
            "description": "Free-float market cap; xueqiu only.",
        },
        "dividend_yield": {
            "type": ["number", "null"],
            "description": "Trailing 12-month dividend yield (percent); xueqiu only.",
        },
        "currency": {"type": ["string", "null"], "enum": ["CNY", "HKD", None]},
        "as_of": {"type": ["string", "null"], "description": "ISO timestamp when this snapshot was taken."},
        "source": {
            "type": "string",
            "description": "Which provider supplied the bundle: 'xueqiu' or 'akshare_baidu'.",
        },
    },
    "additionalProperties": False,
}

_BATCH_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "BatchQuoteResult",
    "description": "Envelope returned by batch quote. Individual items follow FetchResult.",
    "type": "object",
    "required": ["ok", "count", "items", "schema_version"],
    "properties": {
        "ok": {
            "type": "boolean",
            "description": "True iff at least one item succeeded. Use per-item `ok` for fine-grained checks.",
        },
        "count": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": {"$ref": "#/$defs/FetchResult"}},
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
    },
    "$defs": {"FetchResult": _FETCH_RESULT_SCHEMA},
}

_SEARCH_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "SearchResult",
    "description": "Envelope returned by search.",
    "type": "object",
    "required": ["ok", "query", "count", "items", "schema_version"],
    "properties": {
        "ok": {"type": "boolean"},
        "query": {"type": "string"},
        "count": {"type": "integer", "minimum": 0},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["symbol", "name", "market"],
                "properties": {
                    "symbol": {"type": "string", "description": "Canonical code (zero-padded for HK)."},
                    "name": {"type": "string", "description": "Chinese (or English) company name."},
                    "market": {"type": "string", "enum": ["cn", "hk"]},
                    "exchange": {
                        "type": "string",
                        "description": "Sub-market: 'sh', 'sz', 'bj' for CN; 'hk' for HK.",
                    },
                },
            },
        },
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
    },
}


def get_output_schemas() -> dict[str, dict[str, Any]]:
    """Return a mapping of command name → JSON Schema for its output."""

    return {
        "quote": deepcopy(_FETCH_RESULT_SCHEMA),
        "batch_quote": deepcopy(_BATCH_RESULT_SCHEMA),
        "history": deepcopy(_FETCH_RESULT_SCHEMA),
        "news": deepcopy(_FETCH_RESULT_SCHEMA),
        "search": deepcopy(_SEARCH_RESULT_SCHEMA),
        "fundamentals": deepcopy(_FUNDAMENTALS_SCHEMA),
    }


def render(fmt: str) -> dict[str, Any]:
    """Render the full tool spec for ``fmt`` in {'openai', 'anthropic', 'mcp'}.

    Returns a dict with ``tools`` and ``output_schemas`` keys, suitable for
    dumping to JSON or piping directly into an agent runtime.
    """

    fmt = fmt.lower()
    if fmt == "openai":
        tools = get_openai_tools()
    elif fmt == "anthropic":
        tools = get_anthropic_tools()
    elif fmt == "mcp":
        tools = get_mcp_tools()
    else:
        raise ValueError(f"unknown format: {fmt!r} (expected one of openai/anthropic/mcp)")
    return {
        "format": fmt,
        "tools": tools,
        "output_schemas": get_output_schemas(),
    }
