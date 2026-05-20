from __future__ import annotations

from hermes_market.symbol_index import (
    SymbolRow,
    _normalize_cn_row,
    _normalize_hk_row,
    _score,
    search,
)


class TestNormalize:
    def test_cn_six_digit_routing(self) -> None:
        assert _normalize_cn_row("600519", "贵州茅台").exchange == "sh"
        assert _normalize_cn_row("000001", "平安银行").exchange == "sz"
        assert _normalize_cn_row("300750", "宁德时代").exchange == "sz"
        assert _normalize_cn_row("430047", "诺思兰德").exchange == "bj"

    def test_cn_short_code_is_padded(self) -> None:
        # Some akshare tables return un-padded numeric codes; we must zfill.
        row = _normalize_cn_row("1", "平安银行")
        assert row.symbol == "000001"

    def test_hk_five_digit(self) -> None:
        row = _normalize_hk_row("700", "腾讯控股")
        assert row.symbol == "00700"
        assert row.market == "hk"
        assert row.exchange == "hk"
        # 5-digit input (akshare normalized form) must round-trip unchanged.
        assert _normalize_hk_row("00700", "腾讯").symbol == "00700"
        assert _normalize_hk_row("09988", "阿里巴巴-W").symbol == "09988"


def _row(sym: str, name: str, market: str = "cn", exch: str = "sh") -> SymbolRow:
    return SymbolRow(symbol=sym, name=name, market=market, exchange=exch)


class TestScore:
    def test_exact_code_beats_substring(self) -> None:
        rows = [_row("600519", "贵州茅台"), _row("600518", "康美药业")]
        # exact code "600519" must rank first
        out = search("600519", rows, limit=5)
        assert out[0].symbol == "600519"

    def test_exact_name_match(self) -> None:
        rows = [_row("600519", "贵州茅台"), _row("600518", "茅台兄弟")]
        out = search("贵州茅台", rows, limit=5)
        assert out[0].name == "贵州茅台"

    def test_name_prefix_beats_substring(self) -> None:
        # "茅台" prefix in 茅台兄弟 vs "茅台" only inside 贵州茅台
        rows = [_row("600519", "贵州茅台"), _row("600518", "茅台兄弟")]
        out = search("茅台", rows, limit=2)
        assert [r.symbol for r in out] == ["600518", "600519"]

    def test_case_insensitive(self) -> None:
        rows = [_row("00700", "Tencent Holdings", market="hk", exch="hk")]
        assert search("TENCENT", rows) == rows
        assert search("tencent", rows) == rows

    def test_market_filter(self) -> None:
        rows = [
            _row("600519", "贵州茅台", market="cn", exch="sh"),
            _row("00700", "腾讯", market="hk", exch="hk"),
        ]
        # query matches both via "0" substring, but market filter restricts
        out_hk = search("0", rows, market="hk", limit=10)
        assert all(r.market == "hk" for r in out_hk)

    def test_no_match_returns_empty(self) -> None:
        rows = [_row("600519", "贵州茅台")]
        assert search("totallyabsent", rows) == []

    def test_empty_query_returns_zero(self) -> None:
        rows = [_row("600519", "贵州茅台")]
        assert _score("", rows[0]) == 0
        assert search("", rows) == []
