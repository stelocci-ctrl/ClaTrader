from decimal import Decimal

from clatrader.backtest import run
from clatrader.indicators import atr, ema, rsi
from clatrader.risk import InstrumentRules, position_size, round_to_tick
from clatrader.strategy import Candle, StrategyParams, evaluate

RULES = InstrumentRules(
    qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), max_qty=Decimal("100"), tick_size=Decimal("0.1")
)


def test_ema_constant_series():
    assert ema([5.0] * 10, 3)[-1] == 5.0
    assert ema([1.0, 2.0], 3) == [None, None]


def test_rsi_extremes():
    assert rsi([float(i) for i in range(30)], 14)[-1] == 100.0
    assert rsi([float(30 - i) for i in range(30)], 14)[-1] == 0.0


def test_atr_constant_range():
    n = 30
    assert abs(atr([11.0] * n, [9.0] * n, [10.0] * n, 14)[-1] - 2.0) < 1e-9


def test_position_size_risk_based():
    # 1% di 10000 = 100 USDT di rischio, stop a 500 -> 0.2 unità
    assert position_size(10_000, 1, 50_000, 500, 1e9, RULES) == Decimal("0.2")


def test_position_size_capped_and_min():
    assert position_size(10_000, 1, 50_000, 500, 5_000, RULES) == Decimal("0.1")
    assert position_size(10, 1, 50_000, 500, 1e9, RULES) == Decimal("0")


def test_round_to_tick():
    assert round_to_tick(123.456, Decimal("0.1")) == Decimal("123.5")


def _series(prices):
    return [Candle(i * 60_000, p, p * 1.002, p * 0.998, p, 1.0) for i, p in enumerate(prices)]


def test_long_signal_on_crossover_in_uptrend():
    p = StrategyParams(ema_fast=3, ema_slow=6, ema_trend=20, rsi_period=5, atr_period=5, rsi_long_max=101)
    prices = [100 + i for i in range(40)] + [138, 136, 134, 133]
    candles = _series(prices)
    # cerca una candela successiva in cui la EMA veloce rincrocia sopra
    sig = None
    for extra in range(1, 10):
        sig = evaluate("BTCUSDT", _series(prices + [133 + 3 * k for k in range(1, extra + 1)]), p)
        if sig:
            break
    assert evaluate("BTCUSDT", candles, p) is None
    assert sig is not None and sig.side == "Buy"
    assert sig.stop_loss < sig.entry < sig.take_profit
    assert abs((sig.take_profit - sig.entry) - 2 * (sig.entry - sig.stop_loss)) < 1e-9


def test_not_enough_candles():
    assert evaluate("X", _series([1.0] * 10), StrategyParams()) is None


def test_backtest_runs():
    import math

    prices = [100 + 10 * math.sin(i / 15) + i * 0.05 for i in range(800)]
    res = run(_series(prices), StrategyParams(ema_trend=50))
    assert res.trades >= 0
