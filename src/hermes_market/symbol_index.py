"""Local symbol → name index, used by the ``search`` subcommand.

The first call builds an index by calling akshare's full-market roster tables
(``stock_info_a_code_name`` for CN A-shares including BJ, and
``stock_hk_spot_em`` for HK). The result is persisted to
``~/.cache/hermes_market/symbol_index.json`` and reused for 24 hours so that
later ``search`` calls are fully offline.

If akshare is unavailable, we fall back to xueqiu's ``/stock/suggest`` HTTP
endpoint so that ``search`` still works on a fresh box.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .cache import cache_dir
from .normalize import _cn_exchange_prefix

_INDEX_FILE = "symbol_index.json"
_INDEX_TTL_S = 24 * 3600


@dataclass(frozen=True)
class SymbolRow:
    """One indexed stock. ``exchange`` is the CN sub-market or ``'hk'``."""

    symbol: str
    name: str
    market: str  # "cn" | "hk"
    exchange: str  # "sh" | "sz" | "bj" | "hk"

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
        }


def _normalize_cn_row(code: str, name: str) -> SymbolRow:
    code = str(code).strip().zfill(6)
    exch = _cn_exchange_prefix(code)
    return SymbolRow(symbol=code, name=str(name).strip(), market="cn", exchange=exch)


def _normalize_hk_row(code: str, name: str) -> SymbolRow:
    digits = str(code).strip().lstrip("0") or "0"
    return SymbolRow(symbol=digits.zfill(5), name=str(name).strip(), market="hk", exchange="hk")


def _load_cached() -> list[SymbolRow] | None:
    path = cache_dir() / _INDEX_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "items" not in raw or "built_at_ts" not in raw:
        return None
    if time.time() - float(raw["built_at_ts"]) > _INDEX_TTL_S:
        return None
    return [
        SymbolRow(
            symbol=str(it.get("symbol", "")),
            name=str(it.get("name", "")),
            market=str(it.get("market", "")),
            exchange=str(it.get("exchange", "")),
        )
        for it in raw["items"]
        if it.get("symbol") and it.get("name")
    ]


def _store_cached(rows: list[SymbolRow]) -> None:
    path = cache_dir() / _INDEX_FILE
    payload = {
        "built_at_ts": time.time(),
        "items": [r.to_dict() for r in rows],
    }
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _build_from_akshare(ak: Any) -> list[SymbolRow]:  # type: ignore[no-untyped-def]
    """Build an index via akshare. Raises if both CN and HK tables fail."""

    rows: list[SymbolRow] = []
    cn_err: Exception | None = None
    hk_err: Exception | None = None

    fn_cn = getattr(ak, "stock_info_a_code_name", None)
    if fn_cn is not None:
        try:
            df = fn_cn()
            for _, r in df.iterrows():
                code = r.get("code") if "code" in df.columns else r.get("代码")
                name = r.get("name") if "name" in df.columns else r.get("名称")
                if code is None or name is None:
                    continue
                rows.append(_normalize_cn_row(str(code), str(name)))
        except Exception as exc:  # noqa: BLE001
            cn_err = exc

    fn_hk = getattr(ak, "stock_hk_spot_em", None)
    if fn_hk is not None:
        try:
            df = fn_hk()
            for _, r in df.iterrows():
                code = r.get("代码")
                name = r.get("名称")
                if code is None or name is None:
                    continue
                rows.append(_normalize_hk_row(str(code), str(name)))
        except Exception as exc:  # noqa: BLE001
            hk_err = exc

    if not rows:
        raise RuntimeError(f"akshare index build failed (cn={cn_err}, hk={hk_err})")
    return rows


def _search_xueqiu(query: str, xq_get: Any) -> list[SymbolRow]:  # type: ignore[no-untyped-def]
    """Online fallback: hit xueqiu /v5/stock/search/.

    ``xq_get`` is the bound ``XueqiuClient._get_json`` callable, which the
    fetcher passes in so we keep cookie bootstrapping out of this module.
    """

    url = f"https://stock.xueqiu.com/v5/stock/search.json?code={query}&size=20"
    data = xq_get(url)
    out: list[SymbolRow] = []
    items = []
    if isinstance(data, dict):
        items = data.get("data", {}).get("list", []) or []
    for it in items:
        sym = str(it.get("code") or "").strip()
        name = str(it.get("name") or "").strip()
        if not sym or not name:
            continue
        if sym.startswith("HK"):
            out.append(_normalize_hk_row(sym[2:], name))
        elif sym.startswith(("SH", "SZ", "BJ")):
            out.append(_normalize_cn_row(sym[2:], name))
        # Skip US tickers etc.
    return out


def get_index(ak: Any = None, *, force_rebuild: bool = False) -> list[SymbolRow]:  # type: ignore[no-untyped-def]
    """Return the cached index, rebuilding via akshare if missing/expired."""

    if not force_rebuild:
        cached = _load_cached()
        if cached:
            return cached
    if ak is None:
        return []
    rows = _build_from_akshare(ak)
    _store_cached(rows)
    return rows


def _score(query: str, row: SymbolRow) -> int:
    """Higher = better. 0 = no match."""

    q = query.strip().lower()
    if not q:
        return 0
    sym = row.symbol.lower()
    name = row.name.lower()
    if sym == q:
        return 1000
    if name == q:
        return 900
    if sym.startswith(q):
        return 600 - (len(sym) - len(q))
    if q in sym:
        return 400
    if name.startswith(q):
        return 500
    if q in name:
        return 300
    return 0


def search(
    query: str,
    rows: list[SymbolRow],
    *,
    limit: int = 10,
    market: str | None = None,
) -> list[SymbolRow]:
    """Filter ``rows`` by ``query``, return top ``limit`` matches."""

    scored: list[tuple[int, SymbolRow]] = []
    for r in rows:
        if market is not None and r.market != market:
            continue
        s = _score(query, r)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda t: (-t[0], t[1].symbol))
    return [r for _, r in scored[:limit]]
