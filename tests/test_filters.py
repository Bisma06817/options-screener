from datetime import date, timedelta

from screener.screen.filters import (
    FilterParams,
    expected_move,
    passes_dte,
    passes_delta,
    passes_earnings,
    passes_ivr,
    pop_from_delta,
    screen,
)

P = FilterParams(ivr_min=50, dte_min=30, dte_max=60, delta_min=0.15, delta_max=0.25)
TODAY = date(2026, 4, 28)


def _c(**kw) -> dict:
    base = {
        "scan_date": TODAY,
        "symbol": "MU",
        "ivr": 70.0,
        "dte": 45,
        "delta": -0.20,
        "expiry": TODAY + timedelta(days=45),
        "earnings_date": None,
    }
    base.update(kw)
    return base


def test_ivr_threshold():
    assert passes_ivr(_c(ivr=50.0), P)
    assert passes_ivr(_c(ivr=80.0), P)
    assert not passes_ivr(_c(ivr=49.9), P)
    assert not passes_ivr(_c(ivr=None), P)


def test_dte_band():
    assert passes_dte(_c(dte=30), P)
    assert passes_dte(_c(dte=60), P)
    assert not passes_dte(_c(dte=29), P)
    assert not passes_dte(_c(dte=61), P)


def test_delta_band_uses_absolute_value():
    assert passes_delta(_c(delta=-0.15), P)
    assert passes_delta(_c(delta=-0.25), P)
    assert passes_delta(_c(delta=0.20), P)
    assert not passes_delta(_c(delta=-0.10), P)
    assert not passes_delta(_c(delta=-0.30), P)


def test_earnings_excluded_when_in_window():
    expiry = TODAY + timedelta(days=45)
    assert not passes_earnings(_c(expiry=expiry, earnings_date=TODAY + timedelta(days=10)))
    assert passes_earnings(_c(expiry=expiry, earnings_date=expiry + timedelta(days=1)))
    assert passes_earnings(_c(expiry=expiry, earnings_date=None))


def test_screen_ranks_by_ivr_desc():
    a = _c(symbol="A", ivr=60.0)
    b = _c(symbol="B", ivr=80.0)
    c = _c(symbol="C", ivr=70.0)
    out = screen([a, b, c], P)
    assert [r["symbol"] for r in out] == ["B", "C", "A"]


def test_screen_drops_failing_rows():
    rows = [
        _c(symbol="OK", ivr=70),
        _c(symbol="LowIVR", ivr=10),
        _c(symbol="WideDTE", dte=80),
        _c(symbol="Earn", earnings_date=TODAY + timedelta(days=5)),
    ]
    out = screen(rows, P)
    assert [r["symbol"] for r in out] == ["OK"]


def test_pop_from_delta():
    assert pop_from_delta(-0.20) == 80.0
    assert pop_from_delta(0.20) == 80.0
    assert pop_from_delta(None) is None


def test_expected_move():
    em = expected_move(100.0, 0.40, 45)
    assert em is not None
    assert 13.0 < em < 15.0  # 100 * 0.40 * sqrt(45/365) ≈ 14.04
    assert expected_move(100.0, None, 45) is None
    assert expected_move(0, 0.4, 45) is None
