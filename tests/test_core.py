from decimal import Decimal

from clatrader.backtest import run
from clatrader.indicators import atr, ema, rsi, sma
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


def _series(prices, last_volume=1.0):
    n = len(prices)
    return [
        Candle(i * 60_000, p, p * 1.002, p * 0.998, p, last_volume if i == n - 1 else 1.0)
        for i, p in enumerate(prices)
    ]


def test_sma():
    assert sma([1.0, 2.0, 3.0, 4.0], 2) == [None, 1.5, 2.5, 3.5]


PARAMS = StrategyParams(ema_fast=3, ema_slow=6, ema_trend=20, rsi_period=5, atr_period=5, rsi_long_max=101)


def _first_cross(last_volume, params=PARAMS):
    prices = [100 + i for i in range(40)] + [138, 136, 134, 133]
    for extra in range(1, 10):
        series = prices + [133 + 3 * k for k in range(1, extra + 1)]
        sig = evaluate("BTCUSDT", _series(series, last_volume), params)
        if sig or evaluate("BTCUSDT", _series(series, 100.0), params):
            return sig
    return None


def test_volume_filter_rejects_low_volume():
    assert _first_cross(last_volume=1.0) is None  # volume pari alla media: scartato


def test_volume_filter_accepts_high_volume():
    sig = _first_cross(last_volume=2.0)
    assert sig is not None and sig.side == "Buy"
    assert abs(sig.indicators["rel_volume"] - 2.0) < 1e-9


def test_volume_filter_disabled():
    from dataclasses import replace

    assert _first_cross(last_volume=1.0, params=replace(PARAMS, volume_mult=0)) is not None


def test_long_signal_on_crossover_in_uptrend():
    p = PARAMS
    prices = [100 + i for i in range(40)] + [138, 136, 134, 133]
    candles = _series(prices)
    # cerca una candela successiva in cui la EMA veloce rincrocia sopra
    sig = None
    for extra in range(1, 10):
        sig = evaluate("BTCUSDT", _series(prices + [133 + 3 * k for k in range(1, extra + 1)], 5.0), p)
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


def test_strategy_params_from_env(monkeypatch):
    from clatrader.config import strategy_params_from_env

    monkeypatch.setenv("VOLUME_MULT", "0")
    monkeypatch.setenv("EMA_FAST", "5")
    monkeypatch.delenv("EMA_SLOW", raising=False)
    p = strategy_params_from_env()
    assert p.volume_mult == 0 and p.ema_fast == 5 and p.ema_slow == StrategyParams().ema_slow


def test_config_uses_same_strategy_params(monkeypatch):
    from clatrader.config import Config

    for k, v in {
        "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "VOLUME_MULT": "2.5",
    }.items():
        monkeypatch.setenv(k, v)
    assert Config.from_env().strategy.volume_mult == 2.5


def test_backtest_main_reads_env(monkeypatch, capsys):
    import math
    import sys

    import clatrader.backtest as bt

    prices = [100 + 10 * math.sin(i / 15) + i * 0.05 for i in range(800)]
    monkeypatch.setattr(bt, "fetch", lambda *a, **k: _series(prices))
    monkeypatch.setenv("SYMBOLS", "AAAUSDT,BBBUSDT")
    monkeypatch.setenv("VOLUME_MULT", "0")
    monkeypatch.setenv("EMA_TREND", "50")
    monkeypatch.setenv("BYBIT_CATEGORY", "spot")
    monkeypatch.setattr(sys, "argv", ["backtest"])
    bt.main()
    out = capsys.readouterr().out
    assert "filtro volume off" in out and "trend EMA 50" in out and "solo long" in out
    assert "AAAUSDT:" in out and "BBBUSDT:" in out


def test_start_message_is_valid_telegram_html():
    """Telegram rifiuta i messaggi HTML con '<' o '>' fuori dai tag (es. 'long < 70')."""
    import asyncio
    import re
    from types import SimpleNamespace

    from clatrader.bot import ClaTrader

    sent = []

    class Msg:
        async def reply_html(self, text):
            sent.append(text)

    bot = object.__new__(ClaTrader)
    bot.cfg = SimpleNamespace(
        telegram_chat_id=1, symbols=("BTCUSDT",), interval="15", category="linear",
        risk_per_trade_pct=1.0, bybit_testnet=True, dry_run=True, region="global",
    )
    bot.params = StrategyParams()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=Msg())
    asyncio.run(bot.cmd_start(update, None))

    assert sent, "/start non ha risposto"
    without_tags = re.sub(r"</?(b|i|code)>", "", sent[0])
    assert "<" not in without_tags and ">" not in without_tags


def _bot_env(monkeypatch, **extra):
    for k, v in {
        "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_bybit_eu_requires_spot(monkeypatch):
    import pytest

    from clatrader.config import Config

    _bot_env(monkeypatch, BYBIT_REGION="eu", BYBIT_CATEGORY="linear")
    with pytest.raises(RuntimeError, match="spot"):
        Config.from_env()


def test_bybit_eu_uses_eu_servers(monkeypatch):
    from clatrader.config import Config
    from clatrader.exchange import BybitClient

    _bot_env(monkeypatch, BYBIT_REGION="eu", BYBIT_CATEGORY="spot", SYMBOLS="BTCUSDC")
    cfg = Config.from_env()
    assert cfg.tld == "eu" and cfg.symbols == ("BTCUSDC",)
    client = BybitClient("k", "s", True, "spot", cfg.tld)
    assert client.session.endpoint == "https://api-testnet.bybit.eu"


def test_default_region_is_global(monkeypatch):
    from clatrader.config import Config
    from clatrader.exchange import BybitClient

    _bot_env(monkeypatch)
    monkeypatch.delenv("BYBIT_REGION", raising=False)
    cfg = Config.from_env()
    assert BybitClient("k", "s", True, "linear", cfg.tld).session.endpoint == "https://api-testnet.bybit.com"


class _FakeSession:
    """Risposte minime dell'API Bybit per una coppia spot in USDC."""

    def get_instruments_info(self, **kw):
        return {"result": {"list": [{
            "baseCoin": "BTC", "quoteCoin": "USDC",
            "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.00001",
                              "maxOrderQty": "100", "minOrderAmt": "5"},
            "priceFilter": {"tickSize": "0.01"},
        }]}}

    def get_wallet_balance(self, **kw):
        return {"result": {"list": [{"coin": [
            {"coin": "USDT", "walletBalance": "999"},
            {"coin": "USDC", "walletBalance": "250.5"},
        ]}]}}


def test_spot_equity_uses_quote_coin():
    from clatrader.exchange import BybitClient

    client = BybitClient("k", "s", True, "spot", "eu")
    client.session = _FakeSession()
    assert client.quote_currency("BTCUSDC") == "USDC"
    assert client.get_equity("BTCUSDC") == 250.5


def test_unknown_symbol_has_clear_error():
    import pytest

    from clatrader.exchange import BybitClient

    class Empty(_FakeSession):
        def get_instruments_info(self, **kw):
            return {"result": {"list": []}}

    client = BybitClient("k", "s", True, "spot", "eu")
    client.session = Empty()
    with pytest.raises(RuntimeError, match="BTCUSDT non disponibile"):
        client.get_rules("BTCUSDT")
