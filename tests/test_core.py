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
        risk_per_trade_pct=1.0, testnet=True, dry_run=True, region="global",
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


def test_analysis_explains_missing_conditions():
    from clatrader.strategy import analyze

    a = analyze("X", _series([100.0 + i for i in range(60)]), PARAMS)  # rialzo costante
    assert a.ready and a.signal is None
    assert any("nessun incrocio" in m for m in a.missing_long)
    assert analyze("X", _series([1.0] * 5), PARAMS).ready is False


class _FakeClient:
    def __init__(self, candles):
        self.candles = candles
        self.balances = {"BTC": 1.0, "USDC": 10_000.0}  # il demo parte già con BTC
        self.orders = []

    def base_currency(self, symbol):
        return "BTC"

    def get_coin_balance(self, coin):
        return self.balances.get(coin, 0.0)

    def ensure_leverage(self, *a):
        return None

    def place_market_order(self, signal, qty):
        self.orders.append((signal.symbol, qty, signal.take_profit))
        # la commissione (0,1%) viene trattenuta in BTC, come su OKX
        self.balances["BTC"] += float(qty) * 0.999
        return {"orderId": "X1"}

    def cancel_protective_orders(self, symbol):
        self.cancelled = getattr(self, "cancelled", 0) + 1

    def sell_market(self, symbol, qty):
        self.sold = getattr(self, "sold", []) + [qty]
        self.balances["BTC"] -= float(qty)
        return {"orderId": "S1"}

    def get_closed_candles(self, *a):
        return self.candles

    def get_last_price(self, symbol):
        return self.candles[-1].close

    def has_open_position(self, symbol):
        return False

    def get_equity(self, symbol):
        return 10_000.0

    def quote_currency(self, symbol):
        return "USDC"

    def get_rules(self, symbol):
        return RULES


class _Msg:
    def __init__(self, sent):
        self.sent = sent

    async def reply_html(self, text):
        self.sent.append(text)

    async def reply_text(self, text):
        self.sent.append(text)


def _test_bot(testnet=True, category="spot"):
    from types import SimpleNamespace

    from clatrader.bot import ClaTrader

    bot = object.__new__(ClaTrader)
    bot.cfg = SimpleNamespace(
        telegram_chat_id=1, symbols=("BTCUSDC",), interval="1", category=category, region="eu",
        risk_per_trade_pct=1.0, testnet=testnet, dry_run=True, max_position_usdt=1000.0,
        leverage=1, approval_timeout_sec=300,
    )
    bot.params = PARAMS
    bot.client = _FakeClient(_series([100.0 + i for i in range(60)]))
    bot.pending, bot.last_signal_candle, bot.paused = {}, {}, False
    import tempfile
    from pathlib import Path

    from clatrader.positions import SpotPositionBook

    bot.book = SpotPositionBook(Path(tempfile.mkdtemp()) / "positions.json")
    bot.cfg.max_price_deviation_pct = 0.5
    bot.telegram = []

    async def send(app, text, **kw):
        bot.telegram.append(text)
        return SimpleNamespace(message_id=1)

    bot._send = send
    return bot


def _ctx(args=()):
    from types import SimpleNamespace

    return SimpleNamespace(args=list(args), application=None)


def test_scan_command_reports_state_as_valid_html():
    import asyncio
    import re
    from types import SimpleNamespace

    bot = _test_bot()
    sent = []
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=_Msg(sent))
    asyncio.run(bot.cmd_scan(update, _ctx()))
    assert "BTCUSDC" in sent[0] and "LONG: manca" in sent[0] and "SHORT" not in sent[0]
    assert "<" not in re.sub(r"</?(b|i|code)>", "", sent[0])


def test_test_command_only_on_testnet():
    import asyncio
    from types import SimpleNamespace

    sent = []
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=_Msg(sent))
    bot = _test_bot(testnet=False)
    asyncio.run(bot.cmd_test(update, _ctx()))
    assert "solo" in sent[0] and not bot.pending


def test_test_command_requests_approval():
    import asyncio
    from types import SimpleNamespace

    sent = []
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=_Msg(sent))
    bot = _test_bot()
    asyncio.run(bot.cmd_test(update, _ctx()))
    assert len(bot.pending) == 1
    trade = next(iter(bot.pending.values()))
    assert trade.signal.side == "Buy" and trade.signal.stop_loss < trade.signal.entry < trade.signal.take_profit
    assert "PROVA" in bot.telegram[-1] and "USDC" in bot.telegram[-1]


def test_diagnose_reports_which_order_is_refused(monkeypatch, capsys):
    import clatrader.diagnose as dg
    from pybit.exceptions import InvalidRequestError

    class Session(_FakeSession):
        def __init__(self):
            self.cancelled = []

        def get_tickers(self, **kw):
            return {"result": {"list": [{"lastPrice": "73000"}]}}

        def place_order(self, **kw):
            if "takeProfit" in kw:
                raise InvalidRequestError("POST /v5/order/create", "regulatory restrictions", 10024, "now", None)
            return {"result": {"orderId": "abc"}}

        def cancel_order(self, **kw):
            self.cancelled.append(kw["orderId"])

    session = Session()
    _bot_env(monkeypatch, BYBIT_REGION="eu", BYBIT_CATEGORY="spot", SYMBOLS="BTCUSDC", BYBIT_TESTNET="true")
    monkeypatch.setattr(dg.BybitClient, "__init__", lambda self, *a: (
        setattr(self, "category", "spot"), setattr(self, "session", session),
        setattr(self, "_rules", {}), setattr(self, "_base_coin", {}), setattr(self, "_quote_coin", {})
    ) and None)
    monkeypatch.setattr("sys.argv", ["diagnose"])
    dg.main()
    out = capsys.readouterr().out
    assert "1. Ordine limite semplice: ACCETTATO" in out
    assert "2. Ordine limite con TP/SL agganciati: RIFIUTATO (codice 10024)" in out
    assert session.cancelled  # gli ordini accettati vengono cancellati


def test_diagnose_refuses_mainnet(monkeypatch):
    import pytest

    import clatrader.diagnose as dg

    _bot_env(monkeypatch, BYBIT_REGION="eu", BYBIT_CATEGORY="spot", SYMBOLS="BTCUSDC", BYBIT_TESTNET="false")
    with pytest.raises(SystemExit, match="solo con TESTNET=true"):
        dg.main()


# ------------------------------------------------------------------ OKX
OKX_BTC_USDC = {
    "instType": "SPOT", "instId": "BTC-USDC", "baseCcy": "BTC", "quoteCcy": "USDC",
    "lotSz": "0.00000001", "minSz": "0.00001", "tickSz": "0.1", "state": "live",
    "maxMktSz": "1000000", "maxLmtSz": "9999999999", "ctVal": "", "ctMult": "", "settleCcy": "",
    "ctValCcy": "", "uly": "", "instFamily": "", "listTime": "1600000000000", "expTime": "",
    "lever": "", "optType": "", "stk": "", "alias": "", "ctType": "",
}


def _okx_client(testnet=True):
    from clatrader.okx import OkxClient

    client = OkxClient("k", "s", "p", testnet, "eu")
    client.ex.set_markets([client.ex.parse_market(OKX_BTC_USDC)])
    client._markets_loaded = True
    return client


def test_timeframe_conversion():
    from clatrader.okx import to_timeframe

    assert [to_timeframe(x) for x in ("1", "15", "60", "240", "D", "W")] == ["1m", "15m", "1h", "4h", "1d", "1w"]


def test_okx_eu_demo_endpoint_and_rules():
    client = _okx_client()
    assert client.ex.hostname == "eea.okx.com"
    assert client.ex.headers.get("x-simulated-trading") == "1"  # Demo Trading
    rules = client.get_rules("BTCUSDC")
    assert rules.qty_step == Decimal("0.00000001") and rules.min_qty == Decimal("0.00001")
    assert rules.tick_size == Decimal("0.1")
    assert client.quote_currency("BTCUSDC") == "USDC"


def test_okx_unknown_symbol():
    import pytest

    with pytest.raises(RuntimeError, match="BTCUSDT non disponibile su OKX"):
        _okx_client().get_rules("BTCUSDT")


def test_okx_drops_open_candle():
    client = _okx_client()
    now = 10 * 60_000 + 30_000  # a metà della candela delle 10:00 (TF 1m)
    client.ex.milliseconds = lambda: now
    client.ex.fetch_ohlcv = lambda *a, **k: [[i * 60_000, 1, 2, 0.5, 1.5, 3] for i in range(11)]
    candles = client.get_closed_candles("BTCUSDC", "1", 50)
    assert len(candles) == 10 and candles[-1].start_ms == 9 * 60_000


def test_okx_market_order_attaches_tp_sl():
    from clatrader.strategy import Signal

    client = _okx_client()
    sent = {}

    def fake_order(request, params=None):
        sent.update(request[0] if isinstance(request, list) else request)
        return {"code": "0", "data": [{"ordId": "123", "sCode": "0", "sMsg": ""}]}

    # ccxt usa batch-orders di default; intercettiamo entrambi gli endpoint
    client.ex.privatePostTradeBatchOrders = fake_order
    client.ex.privatePostTradeOrder = fake_order
    sig = Signal("BTCUSDC", "Buy", 73000.0, 72000.04, 75000.06, 0, "test")
    res = client.place_market_order(sig, Decimal("0.0137"))

    assert res["orderId"] == "123"
    assert sent["instId"] == "BTC-USDC" and sent["side"] == "buy" and sent["ordType"] == "market"
    assert sent["tdMode"] == "cash" and sent["tgtCcy"] == "base_ccy" and sent["sz"] == "0.0137"
    algo = sent["attachAlgoOrds"][0]
    assert algo["tpTriggerPx"] == "75000.1" and algo["slTriggerPx"] == "72000"
    assert algo["tpOrdPx"] == "-1" and algo["slOrdPx"] == "-1"  # chiusura a mercato


def test_okx_balance_and_position():
    client = _okx_client()
    client.ex.fetch_balance = lambda *a, **k: {"total": {"USDC": 5000.0, "BTC": 0.002}}
    assert client.get_equity("BTCUSDC") == 5000.0
    assert client.has_open_position("BTCUSDC")


def test_config_okx(monkeypatch):
    import pytest

    from clatrader.config import Config
    from clatrader.exchange import make_client
    from clatrader.okx import OkxClient

    for k, v in {"EXCHANGE": "okx", "REGION": "eu", "TESTNET": "true", "OKX_API_KEY": "k",
                 "OKX_API_SECRET": "s", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                 "SYMBOLS": "BTCUSDC"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("OKX_API_PASSPHRASE", raising=False)
    with pytest.raises(RuntimeError, match="OKX_API_PASSPHRASE"):
        Config.from_env()
    monkeypatch.setenv("OKX_API_PASSPHRASE", "p")
    cfg = Config.from_env()
    assert cfg.exchange == "okx" and cfg.category == "spot" and cfg.testnet
    assert isinstance(make_client(cfg), OkxClient)


def test_backtest_fetch_okx_keeps_closed_sorted_unique(monkeypatch):
    import clatrader.backtest as bt
    import clatrader.okx as okx

    real = okx.OkxClient

    def fake_client(*a, **k):
        c = real("k", "s", "p", False, "eu")
        c.ex.set_markets([c.ex.parse_market(OKX_BTC_USDC)])
        c._markets_loaded = True
        c.ex.milliseconds = lambda: 5 * 60_000 + 10_000
        rows = [[i * 60_000, 1, 2, 0.5, 1.5, 3] for i in (3, 1, 2, 2, 4, 5)]  # disordinate, duplicate, 5 aperta
        c.ex.fetch_ohlcv = lambda *a, **k: rows
        return c

    monkeypatch.setattr(okx, "OkxClient", fake_client)
    candles = bt.fetch_okx("BTCUSDC", "1", 3)
    assert [c.start_ms // 60_000 for c in candles] == [2, 3, 4]



def test_spot_existing_coins_are_not_a_bot_position():
    """Le monete già nel conto (es. BTC regalati dal demo) non bloccano i segnali."""
    import asyncio
    from types import SimpleNamespace

    sent = []
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=_Msg(sent))
    bot = _test_bot()
    assert bot.client.balances["BTC"] == 1.0
    asyncio.run(bot.cmd_test(update, _ctx()))
    assert len(bot.pending) == 1
    assert not any("posizione già aperta" in t for t in bot.telegram)


def test_spot_position_lifecycle_open_and_close():
    import asyncio

    from clatrader.positions import SpotPositionBook

    bot = _test_bot()
    bot.cfg.dry_run = False
    asyncio.run(bot.cmd_test(_update_with([]), _ctx()))
    trade = next(iter(bot.pending.values()))

    result = asyncio.run(bot._execute(trade))
    assert "Ordine inviato" in result and bot.client.orders
    rec = bot.book.get("BTCUSDC")
    assert rec["base_before"] == 1.0 and rec["qty"] == float(trade.qty)
    assert asyncio.run(bot._has_position("BTCUSDC"))
    assert SpotPositionBook(bot.book.path).get("BTCUSDC") is not None  # salvato su file

    # un secondo ordine sullo stesso simbolo viene bloccato
    assert "già una posizione" in asyncio.run(bot._execute(trade))

    # lo stop/target vende i BTC comprati: il bot se ne accorge e avvisa
    bot.client.balances["BTC"] = 1.0
    asyncio.run(bot._check_closed_positions(None))
    assert bot.book.get("BTCUSDC") is None
    assert "Posizione <b>BTCUSDC</b> chiusa" in bot.telegram[-1]


def test_dry_run_does_not_record_position():
    import asyncio

    bot = _test_bot()
    asyncio.run(bot.cmd_test(_update_with([]), _ctx()))
    trade = next(iter(bot.pending.values()))
    assert "DRY-RUN" in asyncio.run(bot._execute(trade))
    assert bot.book.get("BTCUSDC") is None and not bot.client.orders


def _update_with(sent):
    from types import SimpleNamespace

    return SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=_Msg(sent))


# ------------------------------------------------------------------ breakout
def _daily(prices, spread=0.01):
    day = 86_400_000
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        out.append(Candle(i * day, prev, max(prev, p) * (1 + spread), min(prev, p) * (1 - spread), p, 1.0))
    return out


def test_breakout_rides_trend_and_exits_on_channel():
    from clatrader.breakout import BreakoutParams, simulate

    p = BreakoutParams(entry_period=10, exit_period=5, atr_period=5, stop_atr=3.0)
    prices = [100.0] * 15 + [100 + 2 * i for i in range(1, 41)] + [180 - 4 * i for i in range(1, 15)]
    r = simulate(_daily(prices), p, fee_pct=0.1, slippage_pct=0.05)
    assert r.n == 1
    t = r.trades[0]
    assert t.reason == "uscita" and t.r_net > 2  # trend lungo cavalcato: guadagno di più R
    assert t.r_net < t.r_gross  # commissioni e slippage sottratti
    assert r.buy_hold_pct != 0


def test_breakout_stop_and_gap_below_stop():
    from clatrader.breakout import BreakoutParams, simulate

    p = BreakoutParams(entry_period=10, exit_period=5, atr_period=5, stop_atr=1.0)
    prices = [100.0] * 15 + [103.0, 104.0, 105.0, 60.0, 60.0]
    candles = _daily(prices, spread=0.002)
    # il giorno del crollo APRE già a 70, molto sotto lo stop (gap)
    c = candles[18]
    candles[18] = Candle(c.start_ms, 70.0, 70.5, 59.0, 60.0, 1.0)
    r = simulate(candles, p, fee_pct=0, slippage_pct=0)
    t = r.trades[0]
    assert t.reason == "stop" and t.exit == 70.0 and t.exit < t.stop  # esce all'apertura, non allo stop
    assert t.r_gross < -1

    # senza gap si esce esattamente allo stop: -1 R prima dei costi
    r2 = simulate(_daily([100.0] * 15 + [103.0, 104.0, 60.0, 60.0], spread=0.002), p, fee_pct=0, slippage_pct=0)
    assert r2.trades[0].reason == "stop" and abs(r2.trades[0].r_gross + 1) < 1e-9


def test_breakout_has_no_lookahead():
    """Le operazioni chiuse con i dati fino al giorno k non cambiano aggiungendo giorni dopo."""
    import random

    from clatrader.breakout import BreakoutParams, simulate

    rng = random.Random(7)
    prices = [100.0]
    for _ in range(400):
        prices.append(prices[-1] * (1 + rng.gauss(0.001, 0.03)))
    candles = _daily(prices)
    full = simulate(candles, BreakoutParams())
    for k in (150, 250, 350):
        part = simulate(candles[:k], BreakoutParams())
        closed_before = [t for t in full.closed if t.exit_ms < candles[k - 1].start_ms]
        assert [(t.entry_ms, t.exit_ms, round(t.r_net, 9)) for t in part.closed if t.exit_ms < candles[k - 1].start_ms] == \
               [(t.entry_ms, t.exit_ms, round(t.r_net, 9)) for t in closed_before]


def test_backtest_breakout_cli(monkeypatch, capsys):
    import random
    import sys

    import clatrader.backtest as bt

    rng = random.Random(1)
    prices = [100.0]
    for _ in range(600):
        prices.append(prices[-1] * (1 + rng.gauss(0.002, 0.03)))
    seen = {}

    def fake_fetch_okx(symbol, interval, n, region):
        seen.update(interval=interval, n=n)
        return _daily(prices)

    monkeypatch.setattr(bt, "fetch_okx", fake_fetch_okx)
    monkeypatch.setenv("EXCHANGE", "okx")
    monkeypatch.setenv("SYMBOLS", "BTCUSDC")
    monkeypatch.setattr(sys, "argv", ["backtest", "-s", "breakout"])
    bt.main()
    out = capsys.readouterr().out
    assert seen == {"interval": "D", "n": 2000}
    assert "buy & hold" in out and "Robustezza" in out and "su 12 combinazioni" in out


def test_breakout_compounding_and_leverage_cap():
    from clatrader.breakout import BreakoutResult, Trade

    # stop all'8%: rischiando il 5% la posizione vale 62,5% del capitale, la leva non serve
    win = Trade(0, 1, 100.0, 116.0, 92.0, 2.0, 2.0, "uscita")   # +2 R = +16%
    loss = Trade(2, 3, 100.0, 92.0, 92.0, -1.0, -1.0, "stop")    # -1 R = -8%
    r = BreakoutResult(trades=[win, loss])
    ret, dd = r.compounded(5.0, 1.0)
    assert abs(ret - ((1 + 0.625 * 0.16) * (1 - 0.625 * 0.08) - 1) * 100) < 1e-9
    assert abs(dd - 5.0) < 1e-9
    assert r.compounded(5.0, 5.0) == r.compounded(5.0, 1.0)  # leva inutile se lo stop è largo

    # stop all'2%: senza leva la posizione è limitata al 100% del capitale
    tight = Trade(0, 1, 100.0, 104.0, 98.0, 2.0, 2.0, "uscita")
    r2 = BreakoutResult(trades=[tight])
    assert abs(r2.compounded(5.0, 1.0)[0] - 4.0) < 1e-9      # 100% x +4%
    assert abs(r2.compounded(5.0, 5.0)[0] - 10.0) < 1e-9     # 250% x +4% con la leva



def _breakout_bot(prices):
    from clatrader.breakout import BreakoutParams

    bot = _test_bot()
    bot.strategy_name = "breakout"
    bot.breakout = BreakoutParams(entry_period=10, exit_period=5, atr_period=5, stop_atr=2.0)
    bot.cfg.interval = "D"
    bot.cfg.risk_per_trade_pct = 5.0
    bot.cfg.max_position_usdt = 1_000_000.0
    bot.client = _FakeClient(_daily(prices))
    return bot


def test_breakout_live_analysis_signal_without_target():
    from clatrader.breakout import BreakoutParams, analyze as analyze_bo

    prices = [100.0] * 30 + [101.0, 102.0, 110.0]
    a = analyze_bo("BTCUSDC", _daily(prices), BreakoutParams(entry_period=10, exit_period=5, atr_period=5))
    assert a.signal is not None and a.signal.take_profit is None
    assert a.signal.stop_loss < a.signal.entry == 110.0
    assert not a.exit


def test_breakout_bot_entry_then_automatic_exit():
    import asyncio

    bot = _breakout_bot([100.0] * 30 + [101.0, 102.0, 110.0])
    bot.cfg.dry_run = False
    asyncio.run(bot._scan_symbol(None, "BTCUSDC"))
    assert len(bot.pending) == 1
    msg = bot.telegram[-1]
    assert "uscita automatica" in msg and "Target" not in msg
    trade = next(iter(bot.pending.values()))
    # rischio 5%: posizione = 5% del saldo / distanza dello stop, sotto il 100% del saldo
    risk = float(trade.qty) * trade.signal.stop_distance
    assert abs(risk / 10_000 - 0.05) < 0.001

    bot.client.get_last_price = lambda s: 110.0
    assert "Ordine inviato" in asyncio.run(bot._execute(trade))
    assert bot.client.orders[-1][2] is None  # nessun take profit inviato
    assert bot.book.get("BTCUSDC")["take_profit"] is None

    # nessuna uscita finché il prezzo tiene
    asyncio.run(bot._check_exits(None))
    assert bot.book.get("BTCUSDC") is not None and not getattr(bot.client, "sold", [])

    # la chiusura scende sotto il minimo a 5 giorni -> uscita automatica
    bot.client.candles = _daily([100.0] * 30 + [101.0, 102.0, 110.0, 111.0, 112.0, 95.0])
    bot.client.get_last_price = lambda s: 95.0
    asyncio.run(bot._check_exits(None))
    assert bot.client.cancelled == 1  # stop loss cancellato prima di vendere
    sold = bot.client.sold[0]
    # vende solo ciò che ha comprato il bot, al netto della commissione in BTC
    assert sold <= trade.qty * Decimal("0.999") and sold > 0
    assert abs(bot.client.balances["BTC"] - 1.0) < 0.002  # i BTC che avevi già restano
    assert bot.book.get("BTCUSDC") is None
    assert "Uscita automatica BTCUSDC" in bot.telegram[-1] and "R" in bot.telegram[-1]


def test_breakout_exit_failure_is_reported_once_per_candle():
    import asyncio

    bot = _breakout_bot([100.0] * 30 + [101.0, 102.0, 110.0, 111.0, 112.0, 95.0])
    bot.cfg.dry_run = False
    bot.book.open("BTCUSDC", 0.5, 1.0, 110.0, 100.0, None)
    bot.client.balances["BTC"] = 1.5

    def boom(symbol, qty):
        raise RuntimeError("exchange non raggiungibile")

    bot.client.sell_market = boom
    asyncio.run(bot._check_exits(None))
    assert "NON riuscita" in bot.telegram[-1]
    n = len(bot.telegram)
    asyncio.run(bot._check_exits(None))  # stessa candela: nessun nuovo tentativo né messaggio
    assert len(bot.telegram) == n


def test_okx_breakout_order_has_only_stop_loss():
    from clatrader.strategy import Signal

    client = _okx_client()
    sent = {}

    def fake_order(request, params=None):
        sent.update(request[0] if isinstance(request, list) else request)
        return {"code": "0", "data": [{"ordId": "9", "sCode": "0", "sMsg": ""}]}

    client.ex.privatePostTradeBatchOrders = fake_order
    client.ex.privatePostTradeOrder = fake_order
    client.place_market_order(Signal("BTCUSDC", "Buy", 73000.0, 69000.0, None, 0, "bo"), Decimal("0.01"))
    algo = sent["attachAlgoOrds"][0]
    assert algo["slTriggerPx"] == "69000" and "tpTriggerPx" not in algo


def test_okx_cancel_protective_orders():
    client = _okx_client()
    calls = []
    client.ex.fetch_open_orders = lambda sym, params=None: (
        calls.append(("fetch", sym, params["ordType"])) or ([{"id": "a1"}] if params["ordType"] == "conditional" else [])
    )
    client.ex.cancel_order = lambda oid, sym, params=None: calls.append(("cancel", oid, params))
    assert client.cancel_protective_orders("BTCUSDC") == 1
    assert ("cancel", "a1", {"trigger": True}) in calls
    assert ("fetch", "BTC/USDC", "oco") in calls


def test_config_breakout(monkeypatch):
    import pytest

    from clatrader.config import Config

    for k, v in {"EXCHANGE": "okx", "REGION": "eu", "OKX_API_KEY": "k", "OKX_API_SECRET": "s",
                 "OKX_API_PASSPHRASE": "p", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                 "STRATEGY": "breakout", "INTERVAL": "15", "BREAKOUT_ENTRY_DAYS": "55"}.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert cfg.strategy_name == "breakout" and cfg.interval == "D" and cfg.breakout.entry_period == 55

    monkeypatch.setenv("EXCHANGE", "bybit")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    monkeypatch.setenv("CATEGORY", "linear")
    with pytest.raises(RuntimeError, match="spot"):
        Config.from_env()


def test_execute_resizes_on_current_price():
    """Se il prezzo sale prima dell'autorizzazione, la quantità scende: il rischio resta il 5%."""
    import asyncio

    bot = _breakout_bot([100.0] * 30 + [101.0, 102.0, 110.0])
    bot.cfg.dry_run = False
    bot.cfg.max_price_deviation_pct = 3.0
    asyncio.run(bot._scan_symbol(None, "BTCUSDC"))
    trade = next(iter(bot.pending.values()))
    qty_signal = trade.qty
    bot.client.get_last_price = lambda s: 112.0  # +1,8% prima del clic
    assert "Ordine inviato" in asyncio.run(bot._execute(trade))
    assert trade.qty < qty_signal
    risk = float(trade.qty) * (112.0 - trade.signal.stop_loss)
    assert abs(risk / 10_000 - 0.05) < 0.001


def test_config_breakout_longer_approval(monkeypatch):
    from clatrader.config import Config

    for k, v in {"EXCHANGE": "okx", "OKX_API_KEY": "k", "OKX_API_SECRET": "s", "OKX_API_PASSPHRASE": "p",
                 "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "STRATEGY": "breakout"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APPROVAL_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("MAX_PRICE_DEVIATION_PCT", raising=False)
    cfg = Config.from_env()
    assert cfg.approval_timeout_sec == 43200 and cfg.max_price_deviation_pct == 3.0
