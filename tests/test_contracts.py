import pytest

from conftest import make_info
from market_data.contracts import detect_leveraged_or_inverse, validate_contract


def check(cfg, symbol="AAPL", **kw):
    info = make_info(symbol=symbol, **kw)
    return validate_contract(symbol, [info], cfg)


class TestAllowed:
    @pytest.mark.parametrize("symbol,stock_type,name", [
        ("AAPL", "COMMON", "APPLE INC"),
        ("SPY", "ETF", "SPDR S&P 500 ETF TRUST"),
        ("TSM", "ADR", "TAIWAN SEMICONDUCTOR-SP ADR"),
        ("O", "REIT", "REALTY INCOME CORP"),
        # names that must NOT trip the leveraged detector
        ("JPST", "ETF", "JPMORGAN ULTRA-SHORT INCOME ETF"),
        ("SPLV", "ETF", "INVESCO S&P 500 LOW VOLATILITY ETF"),
        ("BSCU", "ETF", "INVESCO BULLETSHARES 2030 CORPORATE BOND ETF"),
        ("SGOV", "ETF", "ISHARES 0-3 MONTH TREASURY BOND ETF"),
        ("SHV", "ETF", "ISHARES SHORT TREASURY BOND ETF"),
    ])
    def test_allowed(self, cfg, symbol, stock_type, name):
        res = check(cfg, symbol, stock_type=stock_type, long_name=name, primary="ARCA")
        assert res.ok, res.reasons
        assert res.contract.symbol == symbol


class TestLeveragedInverse:
    @pytest.mark.parametrize("symbol,name", [
        ("TQQQ", "PROSHARES ULTRAPRO QQQ"),                  # deny list + name
        ("XYZ3", "DIREXION DAILY SEMICONDUCTOR BULL 3X SHARES"),
        ("ABCD", "PROSHARES ULTRASHORT S&P500"),
        ("ABCE", "PROSHARES ULTRA S&P500"),
        ("ABCF", "PROSHARES SHORT QQQ"),
        ("ABCG", "TRADR 2X LONG NVDA DAILY ETF"),
        ("ABCH", "SOME FUND -1X INVERSE ETF"),
        ("ABCI", "MICROSECTORS FANG+ INDEX BEAR 3X ETN"),
        ("ABCJ", "PROSHARES VIX SHORT-TERM FUTURES ETF"),
        ("ABCK", "GRANITESHARES 1.5X LONG TSLA DAILY ETF"),
    ])
    def test_name_and_deny_list_detection(self, cfg, symbol, name):
        res = check(cfg, symbol, stock_type="ETF", long_name=name, primary="ARCA")
        assert not res.ok
        assert any("leveraged" in r for r in res.reasons)

    def test_deny_list_alone(self, cfg):
        res = check(cfg, "SQQQ", stock_type="ETF", long_name="SOME NEUTRAL NAME", primary="NASDAQ")
        assert not res.ok and "deny list" in res.reasons[0]

    def test_ibkr_metadata_detection(self, cfg):
        res = check(cfg, "ZZZZ", stock_type="ETF", long_name="NEUTRAL FUND",
                    category="Leveraged Equity", primary="ARCA")
        assert not res.ok and "category" in res.reasons[0]

    def test_etn_stock_type_rejected(self, cfg):
        res = check(cfg, "XYZN", stock_type="ETN", long_name="SOME NOTE", primary="ARCA")
        assert not res.ok and "stockType ETN is prohibited" in res.reasons

    def test_detector_returns_empty_for_clean(self):
        import config
        assert detect_leveraged_or_inverse(make_info(), config) == []


class TestRejected:
    @pytest.mark.parametrize("stock_type", ["PREFERRED", "WARRANT", "CLOSED-END FUND",
                                            "MLP", "RIGHT", "UNIT"])
    def test_prohibited_stock_types(self, cfg, stock_type):
        assert not check(cfg, stock_type=stock_type).ok

    def test_unknown_stock_type(self, cfg):
        res = check(cfg, stock_type="SOMETHING")
        assert "not in the allowed list" in res.reasons[0]

    def test_missing_stock_type(self, cfg):
        assert "stockType missing" in check(cfg, stock_type="").reasons[0]

    @pytest.mark.parametrize("kw", [
        dict(sec_type="OPT"), dict(sec_type="FUT"), dict(sec_type="CASH"),
        dict(currency="CAD"), dict(primary="LSE"), dict(primary="TSE"),
    ])
    def test_prohibited_instruments(self, cfg, kw):
        assert not check(cfg, **kw).ok

    def test_no_candidates(self, cfg):
        res = validate_contract("NOPE", [], cfg)
        assert not res.ok and "no contract found" in res.reasons[0]

    def test_symbol_mismatch(self, cfg):
        res = validate_contract("AAPL", [make_info(symbol="AAPLX")], cfg)
        assert not res.ok

    def test_ambiguous(self, cfg):
        res = validate_contract("AAPL", [make_info(con_id=1), make_info(con_id=2, primary="NYSE")], cfg)
        assert not res.ok and "ambiguous" in res.reasons[0]

    def test_duplicate_same_conid_is_not_ambiguous(self, cfg):
        assert validate_contract("AAPL", [make_info(), make_info()], cfg).ok

    def test_missing_trading_hours_or_tz(self, cfg):
        assert not check(cfg, liquid="").ok
        assert not check(cfg, tz="").ok

    def test_empty_symbol(self, cfg):
        assert not validate_contract("  ", [make_info()], cfg).ok
