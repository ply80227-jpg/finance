"""Data models and helpers shared across providers.

Defined here in their natural dependency order so that ``fail_result`` can refer
to :class:`FetchResult` without forward references.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 2
"""Output envelope schema version.

* **v1**: original ``{ok, provider, symbol, market, data, error, errors,
  schema_version}`` shape.
* **v2** (additive, backwards-compatible): ``data`` may include
  ``fundamentals: {pe_ttm, pe_lyr, pb, ps_ttm, market_cap, float_market_cap,
  dividend_yield, currency, as_of, source}`` and ``fundamentals_errors: [{provider,
  message}]``. Consumers pinned to v1 can ignore the new keys without breaking.
"""


@dataclass
class FetchResult:
    """Unified envelope returned by every provider call.

    ``errors`` is a structured list of ``{provider, message}`` dicts so that
    callers can introspect each fallback hop individually. ``error`` is kept
    as the legacy ``"; "``-joined string for backwards compatibility.
    """

    ok: bool
    provider: str
    symbol: str
    market: str
    data: dict[str, Any]
    error: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


def fail_result(
    provider: str,
    symbol: str,
    market: str,
    errors: list[str] | list[dict[str, str]],
) -> FetchResult:
    """Build a failing :class:`FetchResult` from a list of provider errors."""

    structured: list[dict[str, str]] = []
    flat: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            structured.append({"provider": item.get("provider", ""), "message": item.get("message", "")})
            flat.append(f"{item.get('provider', '')}: {item.get('message', '')}".strip(": "))
        else:
            structured.append({"provider": provider, "message": item})
            flat.append(item)
    return FetchResult(
        ok=False,
        provider=provider,
        symbol=symbol,
        market=market,
        data={},
        error="; ".join(flat),
        errors=structured,
    )
