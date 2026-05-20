"""Tests for market detection and symbol normalization."""

from __future__ import annotations

import pytest
from hermes_market.normalize import (
    detect_market,
    normalize_symbol,
    to_baostock_code,
    to_xq_symbol,
    to_yf_symbol,
)


class TestDetectMarket:
    @pytest.mark.parametrize("symbol", ["600519", "000001", "300750", "688981", "sh600519", "sz000001"])
    def test_detects_cn(self, symbol: str) -> None:
        assert detect_market(symbol, None) == "cn"

    @pytest.mark.parametrize("symbol", ["00700", "0700.HK", "700", "9988", "00388"])
    def test_detects_hk(self, symbol: str) -> None:
        assert detect_market(symbol, None) == "hk"

    def test_explicit_overrides_auto(self) -> None:
        assert detect_market("00700", "cn") == "cn"
        assert detect_market("600519", "hk") == "hk"

    def test_unrecognized_raises(self) -> None:
        with pytest.raises(ValueError):
            detect_market("AAPL", None)


class TestNormalizeSymbol:
    @pytest.mark.parametrize(
        ("symbol", "market", "expected"),
        [
            ("600519", "cn", "600519"),
            ("sh600519", "cn", "600519"),
            ("SH600519", "cn", "600519"),
            ("sz000001", "cn", "000001"),
            ("BJ430047", "cn", "430047"),
            ("00700", "hk", "00700"),
            ("700", "hk", "00700"),
            ("0700.HK", "hk", "00700"),
        ],
    )
    def test_normalize(self, symbol: str, market: str, expected: str) -> None:
        assert normalize_symbol(symbol, market) == expected

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_symbol("AAPL", "cn")


class TestExchangeMapping:
    """The regression-critical mapping that the original script got wrong."""

    @pytest.mark.parametrize(
        ("sym", "expected_xq", "expected_yf", "expected_bs"),
        [
            ("600519", "SH600519", "600519.SS", "sh.600519"),  # Shanghai main board
            ("688981", "SH688981", "688981.SS", "sh.688981"),  # STAR market
            ("000001", "SZ000001", "000001.SZ", "sz.000001"),  # Shenzhen main board
            ("300750", "SZ300750", "300750.SZ", "sz.300750"),  # ChiNext
            ("430047", "BJ430047", "430047.BJ", "bj.430047"),  # Beijing 4xx
            ("832000", "BJ832000", "832000.BJ", "bj.832000"),  # Beijing 8xx
        ],
    )
    def test_a_share_exchange_routing(self, sym: str, expected_xq: str, expected_yf: str, expected_bs: str) -> None:
        assert to_xq_symbol(sym, "cn") == expected_xq
        assert to_yf_symbol(sym, "cn") == expected_yf
        assert to_baostock_code(sym) == expected_bs

    def test_hk_routing(self) -> None:
        assert to_xq_symbol("00700", "hk") == "HK00700"
        assert to_yf_symbol("00700", "hk") == "00700.HK"
